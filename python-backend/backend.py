"""
SPHEREx time-lapse backend - NASA Space Apps 2026 "Planet X and SPHEREx".

Called by the middleware:

    python backend.py <DEC> <RA> <PIXELS> <UUID>

    DEC, RA  target position, either
               plain numbers = decimal degrees   "-7.24515"       "133.79513"
               sexagesimal                       "-7d14m42.5s"    "08h55m10.8s"
                                                 "-07:14:42.5"    "08:55:10.8"
             With colons RA is in HOURS and DEC in DEGREES (astronomy convention).
             Don't put spaces inside a coordinate (the shell would split it).
    PIXELS   width = height of the frames in SPHEREx pixels (6.15" each), 3..2040
    UUID     the user's id

What happens:
    1. Cache/usr/.pending_<UUID>/ is created
    2. the frames are built into it:
         frame_000.jpg, frame_001.jpg, ...   oldest first, PIXELS x PIXELS, colour
                                             (blue = D1-D3 0.75-2.4 um, red = D4-D6 2.4-5 um)
         diff/diff_000.jpg, ...              each frame minus the average of all frames
                                             (grey = unchanged, white = brighter now,
                                             black = was brighter) - a subfolder, so the
                                             middleware's *.jpg list doesn't pick it up
         meta.json                           status, timestamps, frame info, catalogue links
    3. the folder is renamed to Cache/usr/<UUID>/ and the program exits
    The backend never deletes anything in Cache/usr/ - the middleware does that.
    On an error the folder is renamed as well, with meta.json["status"] == "error".

Exit codes (the description goes to stderr and into meta.json["error"]):
    0  success
    1  invalid arguments
    2  no SPHEREx images cover this position
    3  images exist but none could be downloaded (archive/network trouble)
    4  anything else unexpected (e.g. out of memory for a huge PIXELS)
    5  Cache/usr/<UUID>/ or Cache/usr/.pending_<UUID>/ already exists

Pipeline:
    find_exposures       IRSA query: every SPHEREx image that covers (ra, dec)
    group_into_passes    group the images by SPHEREx pass over the spot
    coverage_of          which images cover the whole frame (headers only)
    plan_frames          split passes into >= MIN_FRAMES frames, same detectors in each
    read_cutout          read only a small square of each image (public AWS copy)
    subtract_background  local sky model per image (removes gradients/bands) + noise
    reproject_cutout     line every square up on one common grid
    detectors.combine    kappa-sigma stack per detector, noise-weighted per colour
    render_frames        same brightness scale for every frame -> colour JPGs
    render_differences   frame minus average -> movers stand out
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sys
import threading
import time
import urllib.parse
import uuid as uuidlib
import warnings
from collections import deque
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

import astropy.units as u
import numpy as np
import requests
from astropy.coordinates import Angle, SkyCoord
from astropy.io import fits
from astropy.stats import sigma_clipped_stats
from astropy.time import Time
from astropy.visualization import AsinhStretch, ImageNormalize
from astropy.wcs import WCS, FITSFixedWarning
from astroquery.ipac.irsa import Irsa
from PIL import Image
from scipy.ndimage import (binary_dilation, binary_fill_holes, find_objects, gaussian_filter,
                           label, map_coordinates, median_filter)

import detectors

warnings.simplefilter("ignore", FITSFixedWarning)
log = logging.getLogger("spherex_backend")

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
BACKEND_DIR = Path(__file__).resolve().parent

# CACHE LAYOUT
#   Cache/
#     queries/collections.json                    newest SPHEREx release (24h TTL)
#     queries/<collection>/<coord>.json           image list for a position (24h TTL)
#     headers/<collection>/<obs_id>/D<n>.json      IMAGE header of one image (any position)
#     cutouts/<collection>/<obs_id>/D<n>_<coord>_h<half>.npz
#                                                 downloaded pixel square of one image
#     usr/.pending_<uuid>/                        job still running
#     usr/<uuid>/                                 finished job, picked up + deleted by the middleware
CACHE_DIR = BACKEND_DIR / "Cache"
USR_DIR = CACHE_DIR / "usr"
QUERY_TTL_S = 24 * 3600

# Used if IRSA can't be asked for the newest "spherex_drN" collection.
DEFAULT_COLLECTION = "spherex_dr1"
COLLECTION_RE = re.compile(r"^spherex_dr(\d+)$")

PIXEL_SCALE_ARCSEC = 6.15
MIN_PIXELS, MAX_PIXELS = 3, 2040  # 2040 = width of one SPHEREx detector

# Images are grouped by SPHEREx pass over the spot (like Backyard Worlds' epochs):
EPOCH_GAP_DAYS = 30.0   # a gap longer than this between images starts a new pass
EPOCH_MAX_DAYS = 60.0   # a pass never spans longer (spots near the ecliptic
                        # poles are seen almost continuously)
# ... and passes are split into several frames until the video has at least
# this many (as far as the data allows, see plan_frames).
MIN_FRAMES = 6

# Sky level is estimated per tile of this many pixels (~3.3') and smoothed, so
# large-scale gradients and stray-light bands are removed but stars are kept.
BACKGROUND_TILE = 32

# Frames in colour (blue = D1-D3, red = D4-D6, see detectors.CHANNELS) or grey.
COLOR_FRAMES = True
# Two kinds of workers (see build_frames):
#  - DOWNLOAD_THREADS threads fetch images into the cache. Waiting for the
#    network needs almost no CPU, so many at once is cheap.
#  - WORKER_PROCESSES processes line the images up. That's number crunching,
#    which threads can't spread over cores (Python's GIL) but processes can.
#    Each process uses ~130 MB of RAM.
DOWNLOAD_THREADS = 32
WORKER_PROCESSES = min(16, getattr(os, "process_cpu_count", os.cpu_count)() or 4)
# Lined-up images waiting to be combined are kept in RAM; bins are queued ahead
# until roughly this much would be waiting (at least 2 x DOWNLOAD_THREADS images).
QUEUE_AHEAD_BYTES = 1024**3

# Reading the FLAGS extension doubles download size and time per image, so it is
# off. The few glitch pixels it would mask (~0.3% per image, at random places)
# are removed by the median when a detector has >= 3 images in a frame (see
# detectors.MAX_IMAGES_PER_FRAME).
USE_FLAGS = False

# FLAGS extension bits treated as bad pixels (only used if USE_FLAGS). Bits 0-2 are confirmed
# (TRANSIENT, OVERFLOW, SUR_ERROR). The full list is in Table 10 of the SPHEREx
# Explanatory Supplement - add hot/cold/persistence/outlier bits once verified.
# Bit 21 marks pixels on a *source*; never mask that one.
BAD_FLAG_BITS = (0, 1, 2)
BAD_FLAG_MASK = sum(1 << b for b in BAD_FLAG_BITS)

EXIT_OK = 0
EXIT_BAD_ARGS = 1
EXIT_NO_DATA = 2
EXIT_DOWNLOAD_FAILED = 3
EXIT_UNEXPECTED = 4
EXIT_FOLDER_EXISTS = 5


class BackendError(Exception):
    """An error with a known exit code."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- #
# Small cache helpers (atomic writes so a crash never leaves half a file)
# --------------------------------------------------------------------------- #
def _coord_key(ra: float, dec: float) -> str:
    return f"ra{ra:09.5f}_dec{dec:+09.5f}"


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _write_json(path: Path, obj) -> None:
    _atomic_write_bytes(path, json.dumps(obj, indent=1).encode())


def _read_json(path: Path, max_age_s: float | None = None):
    if not path.exists():
        return None
    if max_age_s is not None and time.time() - path.stat().st_mtime > max_age_s:
        return None
    return json.loads(path.read_text())


# --------------------------------------------------------------------------- #
# Input
# --------------------------------------------------------------------------- #
USAGE = "usage: python backend.py <DEC> <RA> <PIXELS> <UUID>"


def parse_angle(text: str, kind: str) -> float:
    """One coordinate -> degrees.

    "133.79"                     plain number: decimal degrees, for RA *and* DEC
    "08h55m10.8s" / "-7d14m42s"  units written out
    "08:55:10.8"  / "-07:14:42"  colons: hours for RA, degrees for DEC
    The text is parsed as a whole, so the sign of e.g. "-00:30:00" is kept
    (splitting into numbers first would turn -0 into 0 and lose it).
    """
    try:
        value = float(text)  # plain number = degrees
    except ValueError:
        unit = u.hourangle if kind == "RA" else u.deg
        try:
            value = float(Angle(text, unit=unit).deg)
        except Exception:
            raise BackendError(EXIT_BAD_ARGS,
                               f"{kind} is not a valid coordinate: {text!r} "
                               f"(use e.g. {'133.795, 08h55m10.8s or 08:55:10.8' if kind == 'RA' else '-7.245, -7d14m42.5s or -07:14:42.5'})")
    if not math.isfinite(value):
        raise BackendError(EXIT_BAD_ARGS, f"{kind} must be a finite number, got {text!r}")
    return value


def parse_args(argv: list[str]) -> tuple[float, float, int, str]:
    """argv without the program name -> (dec, ra, pixels, uuid). Raises BackendError(1)."""
    if len(argv) != 4:
        hint = ""
        if len(argv) > 4:  # most likely "08 55 10.8" without quotes
            hint = (" - if a coordinate contains spaces, write it as 08:55:10.8 or 08h55m10.8s"
                    " (or put it in quotes)")
        raise BackendError(EXIT_BAD_ARGS, f"expected 4 arguments, got {len(argv)}{hint}. {USAGE}")
    dec_s, ra_s, pixels_s, uuid_s = argv

    dec = parse_angle(dec_s, "DEC")
    ra = parse_angle(ra_s, "RA")
    if not -90.0 <= dec <= 90.0:
        raise BackendError(EXIT_BAD_ARGS, f"DEC must be in [-90, 90] degrees, got {dec:g} ({dec_s!r})")
    if not 0.0 <= ra <= 360.0:  # no silent wrapping: "25h" or "400" is a typo, not 1h / 40 deg
        raise BackendError(EXIT_BAD_ARGS,
                           f"RA must be in [0, 360] degrees = [0, 24] hours, got {ra:g} deg ({ra_s!r})")

    try:
        pixels = int(pixels_s)
    except ValueError:
        raise BackendError(EXIT_BAD_ARGS, f"PIXELS must be a whole number, got {pixels_s!r}")
    if not MIN_PIXELS <= pixels <= MAX_PIXELS:
        raise BackendError(EXIT_BAD_ARGS,
                           f"PIXELS must be in [{MIN_PIXELS}, {MAX_PIXELS}], got {pixels}")

    # Parsing as a UUID also guarantees it can't contain "/" or ".." (path tricks).
    try:
        user_id = str(uuidlib.UUID(uuid_s))
    except ValueError:
        raise BackendError(EXIT_BAD_ARGS, f"UUID is not a valid UUID: {uuid_s!r}")

    return dec, ra % 360.0, pixels, user_id


# --------------------------------------------------------------------------- #
# 1. Which SPHEREx images cover this position?
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Exposure:
    obs_id: str
    detector: int  # 1..6, see detectors.py
    mjd: float
    url: str       # IRSA download URL
    s3_url: str    # same file on the public AWS bucket (faster, usually up)
    wl_min_um: float
    wl_max_um: float


def latest_collection() -> str:
    """Newest SPHEREx data release at IRSA, so new releases are picked up automatically."""
    path = CACHE_DIR / "queries" / "collections.json"
    cached = _read_json(path, QUERY_TTL_S)
    if cached:
        return cached["latest"]
    try:
        names = Irsa.list_collections(servicetype="SIA")["collection"]
        releases = [(int(m.group(1)), n) for n in names if (m := COLLECTION_RE.match(str(n)))]
        latest = max(releases)[1] if releases else DEFAULT_COLLECTION
    except Exception as exc:  # network trouble -> fall back, don't cache
        log.warning("could not list IRSA collections (%s); using %s", exc, DEFAULT_COLLECTION)
        return DEFAULT_COLLECTION
    _write_json(path, {"latest": latest, "checked": Time.now().isot})
    return latest


def find_exposures(ra: float, dec: float, collection: str) -> list[Exposure]:
    path = CACHE_DIR / "queries" / collection / f"{_coord_key(ra, dec)}.json"
    cached = _read_json(path, QUERY_TTL_S)
    if cached is not None:
        return [Exposure(**e) for e in cached]

    try:
        t = Irsa.query_sia(pos=(SkyCoord(ra * u.deg, dec * u.deg), 1 * u.arcsec),
                           collection=collection, maxrec=20000)
    except Exception as exc:
        raise BackendError(EXIT_DOWNLOAD_FAILED, f"IRSA image search failed: {exc}")
    exposures = []
    for row in t:
        if str(row["dataproduct_subtype"]) != "science":
            continue
        aws = json.loads(str(row["cloud_access"]))["aws"]
        exposures.append(Exposure(
            obs_id=str(row["obs_id"]),
            detector=int(str(row["energy_bandpassname"])[-1]),  # "SPHEREx-D3" -> 3
            mjd=float(row["t_min"]),
            url=str(row["access_url"]),
            s3_url=f"https://{aws['bucket_name']}.s3.amazonaws.com/{aws['key']}",
            wl_min_um=float(row["em_min"]) * 1e6,
            wl_max_um=float(row["em_max"]) * 1e6,
        ))
    exposures.sort(key=lambda e: (e.mjd, e.detector))
    _write_json(path, [asdict(e) for e in exposures])
    return exposures


# --------------------------------------------------------------------------- #
# 2. SPHEREx passes -> video frames
# --------------------------------------------------------------------------- #
def group_into_passes(exposures: list[Exposure]) -> list[list[Exposure]]:
    """Group the images by SPHEREx pass over the spot.

    SPHEREx sees a given spot for a few days to weeks every ~6 months. A new
    pass starts after a gap of more than EPOCH_GAP_DAYS, or once the pass
    would span more than EPOCH_MAX_DAYS -> typically 2-3 passes per year.
    """
    passes: list[list[Exposure]] = []
    for e in sorted(exposures, key=lambda e: e.mjd):
        if (passes and e.mjd - passes[-1][-1].mjd <= EPOCH_GAP_DAYS
                and e.mjd - passes[-1][0].mjd <= EPOCH_MAX_DAYS):
            passes[-1].append(e)
        else:
            passes.append([e])
    return passes


# An image "fully covers" the frame when at least this fraction of the test
# points spread over the frame lies on its detector (see frame_coverage).
FULL_COVERAGE = 0.999


def frame_test_points(ra: float, dec: float, pixels: int) -> tuple[np.ndarray, np.ndarray]:
    """Sky positions of a 7x7 grid of points spread over the whole output frame (corners included)."""
    t = np.linspace(-0.5, pixels - 0.5, 7)
    gx, gy = np.meshgrid(t, t)
    return make_grid_wcs(ra, dec, pixels).pixel_to_world_values(gx, gy)


def frame_coverage(hdr: fits.Header, test_ra: np.ndarray, test_dec: np.ndarray) -> float:
    """Fraction of the frame's test points that land on this image's detector."""
    x, y = WCS(hdr).world_to_pixel_values(test_ra, test_dec)
    nx, ny = hdr["NAXIS1"], hdr["NAXIS2"]
    return float(np.mean((x >= -0.5) & (x <= nx - 0.5) & (y >= -0.5) & (y <= ny - 0.5)))


def coverage_of(exposures: list[Exposure], ra: float, dec: float, pixels: int,
                collection: str) -> dict[tuple[str, int], float | None]:
    """How much of the frame each image covers (None = header not available).

    Only the headers are needed (cached per image, see image_header), fetched
    in parallel - much cheaper than downloading pixels that turn out unusable.
    """
    test_ra, test_dec = frame_test_points(ra, dec, pixels)

    def one(e: Exposure) -> float | None:
        try:
            return frame_coverage(image_header(e, collection)[0], test_ra, test_dec)
        except Exception as exc:
            log.warning("no header for %s D%d: %s", e.obs_id, e.detector, exc)
            return None

    with ThreadPoolExecutor(DOWNLOAD_THREADS) as pool:
        return {(e.obs_id, e.detector): c for e, c in zip(exposures, pool.map(one, exposures))}


def plan_frames(passes: list[list[Exposure]], min_frames: int = MIN_FRAMES,
                coverage: dict[tuple[str, int], float | None] | None = None) -> list[list[Exposure]]:
    """Turn the passes into at least min_frames frames (as far as the data allows).

    A pass is split into k frames *per detector, in time order*: each
    detector's images are cut into k consecutive parts, and frame j gets part
    j of every detector. So every frame of a pass has the same wavelength mix
    (no colour jumps), the frames stay in time order, and each detector keeps
    >= MIN_IMAGES_PER_DETECTOR images per frame (glitch removal keeps working).
    Splits go to the pass with the most images per frame first. A detector
    with too few images for k parts is left out of that pass, as long as both
    colour channels stay represented. Bonus: frames days apart within one pass
    show fast movers such as asteroids moving.

    With `coverage` (see coverage_of), each detector uses only its images that
    cover the whole frame, if there are enough of them for its k parts;
    otherwise all its images (partly-covering images leave edges where a
    detector has only 1-2 images - detectors.weighted_mean copes with that).
    -> list of frames (each a list of images, at most MAX_IMAGES_PER_FRAME via
       detectors.pick), in time order; passes without enough images are dropped.
    """
    m = detectors.MIN_IMAGES_PER_DETECTOR

    def covers_frame(e: Exposure) -> bool:
        return coverage is not None and (coverage.get((e.obs_id, e.detector)) or 0.0) >= FULL_COVERAGE

    def channels(by_det: dict) -> set[str]:
        return {c for c, dets in detectors.CHANNELS.items() if any(d in by_det for d in dets)}

    def images_for(imgs: list[Exposure], k: int) -> list[Exposure] | None:
        """A detector's images for k frames: the fully-covering ones if they suffice, else all."""
        full = [e for e in imgs if covers_frame(e)]
        if len(full) >= m * k:
            return full
        return imgs if len(imgs) >= m * k else None

    def usable(by_det: dict, k: int) -> dict | None:
        """Detectors that can give >= m images to each of k frames, if both colours survive."""
        keep = {d: sel for d, imgs in by_det.items() if (sel := images_for(imgs, k))}
        return keep if keep and channels(keep) == channels(by_det) else None

    per_pass = []
    for p in passes:
        by_det: dict[int, list[Exposure]] = {}
        for e in sorted(p, key=lambda e: e.mjd):
            by_det.setdefault(e.detector, []).append(e)
        per_pass.append({d: imgs for d, imgs in by_det.items() if len(imgs) >= m})

    splits = [1 if by_det else 0 for by_det in per_pass]
    while sum(splits) < min_frames:
        candidates = [i for i, by_det in enumerate(per_pass)
                      if splits[i] and usable(by_det, splits[i] + 1)]
        if not candidates:
            break  # the data doesn't allow more frames
        richest = max(candidates, key=lambda i: sum(map(len, per_pass[i].values())) / splits[i])
        splits[richest] += 1

    frames = []
    for by_det, k in zip(per_pass, splits):
        if not k:
            continue
        keep = usable(by_det, k)
        for j in range(k):
            part = [e for imgs in keep.values()
                    for e in imgs[j * len(imgs) // k:(j + 1) * len(imgs) // k]]
            frames.append(detectors.pick(part))
    return [f for f in frames if f]


# --------------------------------------------------------------------------- #
# 3. Read a small square of one image (only those bytes are downloaded)
# --------------------------------------------------------------------------- #
@dataclass
class Cutout:
    exposure: Exposure
    data: np.ndarray        # MJy/sr, background-subtracted, bad pixels = NaN
    x0: int                 # position of data[0, 0] in the full 2040x2040 image
    y0: int
    header: str             # IMAGE header (for the WCS)
    flags_near_target: int  # OR of FLAGS bits within 2 px of the target

    @property
    def wcs(self) -> WCS:
        return WCS(fits.Header.fromstring(self.header))


class DownloadError(Exception):
    pass


# ---- exact byte-range downloads (used when USE_FLAGS is off) ---------------
# A FITS file is a sequence of text headers (80-character "cards" in blocks of
# 2880 bytes) and binary data. The IMAGE data is stored row after row as
# big-endian float32, so the rows of our square are one contiguous byte range:
# 1 small request for the headers + 1 request for exactly those rows.
HEADER_PROBE_BYTES = 64 * 1024  # first bytes of the file: PRIMARY + IMAGE header (~29 KB)
FITS_BLOCK = 2880

# Stuck connections: when 32 downloads share a line, some lose packets and TCP
# makes them wait longer and longer (measured: 1 image took 24 s while the
# median was 6 s). Instead of waiting, abort and retry on a fresh connection:
# Stuck connections usually still trickle (~1 Mbit/s), so "slow" is judged
# relative to the downloads that just finished - that works on fast and slow lines.
HTTP_CONNECT_TIMEOUT_S = 5.0
HTTP_STALL_TIMEOUT_S = 4.0       # no bytes at all for this long -> retry
HTTP_SLOW_AFTER_S = 3.0          # still running after this long ...
HTTP_SLOW_FRACTION = 0.25        # ... and slower than 1/4 of the typical speed -> retry
HTTP_MIN_BYTES_PER_S = 32_000    # never call anything faster than this "slow"
HTTP_ATTEMPTS = 3                # the last attempt is never aborted for being slow

_http = threading.local()  # one requests.Session (connection pool) per download thread
_speeds_lock = threading.Lock()
_recent_speeds: deque[float] = deque(maxlen=50)  # bytes/s of recently finished big requests


def _session() -> requests.Session:
    if not hasattr(_http, "session"):
        _http.session = requests.Session()
    return _http.session


def _too_slow_below() -> float:
    """Speed (bytes/s) under which a running download counts as stuck."""
    with _speeds_lock:
        speeds = sorted(_recent_speeds)
    if len(speeds) < 5:
        return HTTP_MIN_BYTES_PER_S  # not enough experience yet
    return max(HTTP_MIN_BYTES_PER_S, HTTP_SLOW_FRACTION * speeds[len(speeds) // 2])


def _http_range(url: str, start: int, end: int) -> bytes:
    """Download bytes start..end (inclusive) of url, retrying stuck/failed attempts."""
    want = end - start + 1
    errors = []
    for attempt in range(1, HTTP_ATTEMPTS + 1):
        t0 = time.monotonic()
        try:
            with _session().get(url, headers={"Range": f"bytes={start}-{end}"}, stream=True,
                                timeout=(HTTP_CONNECT_TIMEOUT_S, HTTP_STALL_TIMEOUT_S)) as r:
                if r.status_code != 206:  # 206 = "here is the part you asked for"
                    raise DownloadError(f"HTTP {r.status_code} (expected 206)")
                buf = bytearray()
                for chunk in r.iter_content(64 * 1024):
                    buf += chunk
                    elapsed = time.monotonic() - t0
                    # (the limit is re-read each time: early downloads start before
                    # anything has finished, so "typical speed" isn't known yet)
                    if (attempt < HTTP_ATTEMPTS and elapsed > HTTP_SLOW_AFTER_S
                            and len(buf) / elapsed < _too_slow_below()):
                        raise DownloadError(f"too slow ({len(buf) / elapsed / 1000:.0f} kB/s)")
            if len(buf) != want:
                raise DownloadError(f"got {len(buf)} of {want} bytes")
            if want >= 256 * 1024:  # small requests are mostly waiting time, not speed
                with _speeds_lock:
                    _recent_speeds.append(want / max(time.monotonic() - t0, 1e-3))
            return bytes(buf)
        except (requests.RequestException, DownloadError) as exc:
            errors.append(f"try {attempt}: {type(exc).__name__}: {exc}")
    raise DownloadError("; ".join(errors))


def _parse_header(buf: bytes, offset: int) -> tuple[fits.Header, int] | None:
    """FITS header starting at offset -> (header, offset where its data starts).

    None if the END card isn't inside buf (header longer than what we downloaded).
    """
    for pos in range(offset, len(buf) - 79, 80):
        if buf[pos:pos + 8] == b"END     ":
            header_end = offset + math.ceil((pos + 80 - offset) / FITS_BLOCK) * FITS_BLOCK
            return fits.Header.fromstring(buf[offset:pos + 80].decode("ascii")), header_end
    return None


def _data_bytes(hdr: fits.Header) -> int:
    """Size of an HDU's data on disk (padded to whole 2880-byte blocks)."""
    naxis = hdr.get("NAXIS", 0)
    if naxis == 0:
        return 0
    n = abs(hdr["BITPIX"]) // 8 * math.prod(hdr[f"NAXIS{i}"] for i in range(1, naxis + 1))
    return math.ceil(n / FITS_BLOCK) * FITS_BLOCK


def _fetch_image_header(url: str) -> tuple[fits.Header, int]:
    """IMAGE header + the byte offset where its data starts, from the file's first bytes."""
    buf = _http_range(url, 0, HEADER_PROBE_BYTES - 1)
    primary = _parse_header(buf, 0)
    image = primary and _parse_header(buf, primary[1] + _data_bytes(primary[0]))
    if image is None or image[0].get("EXTNAME") != "IMAGE":
        raise DownloadError("unexpected file layout (IMAGE is not the first extension)")
    hdr, data_offset = image
    if hdr.get("BITPIX") != -32 or hdr.get("NAXIS") != 2:
        raise DownloadError(f"unexpected IMAGE format BITPIX={hdr.get('BITPIX')} NAXIS={hdr.get('NAXIS')}")
    return hdr, data_offset


def image_header(exp: Exposure, collection: str, fetch: bool = True) -> tuple[fits.Header, int] | None:
    """IMAGE header of one exposure + its data offset, cached in Cache/headers/.

    A header doesn't depend on the position looked at, so once fetched it is
    reused for every request (and saves that request when downloading pixels).
    fetch=False: only look in the cache (None if not there).
    """
    path = CACHE_DIR / "headers" / collection / exp.obs_id / f"D{exp.detector}.json"
    cached = _read_json(path)
    if cached is not None:
        return fits.Header.fromstring(cached["header"]), cached["data_offset"]
    if not fetch:
        return None
    errors = []
    for url in (exp.s3_url, exp.url):  # AWS first, IRSA as fallback
        try:
            hdr, data_offset = _fetch_image_header(url)
            break
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
    else:
        raise DownloadError(f"{exp.obs_id} D{exp.detector} header: {' | '.join(errors)}")
    _write_json(path, {"header": hdr.tostring(), "data_offset": data_offset})
    return hdr, data_offset


def _read_square_ranges(url: str, ra: float, dec: float, half: int,
                        header: tuple[fits.Header, int] | None = None):
    """Fast path: IMAGE header (or the cached one) + exactly the rows of the square.

    See _read_square_fits for the result.
    """
    hdr, data_offset = header if header is not None else _fetch_image_header(url)

    xc, yc, x0, x1, y0, y1, nx, ny = _square_bounds(hdr, ra, dec, half)
    if x1 - x0 < 3 or y1 - y0 < 3:
        return hdr, None, None, (xc, yc, x0, y0, nx, ny)  # square doesn't overlap this detector
    row_bytes = nx * 4
    raw = _http_range(url, data_offset + y0 * row_bytes, data_offset + y1 * row_bytes - 1)
    data = np.frombuffer(raw, dtype=">f4").reshape(y1 - y0, nx)[:, x0:x1].astype(np.float32)
    if hdr.get("BSCALE", 1) != 1 or hdr.get("BZERO", 0) != 0:
        data = data * np.float32(hdr.get("BSCALE", 1)) + np.float32(hdr.get("BZERO", 0))
    return hdr, data, None, (xc, yc, x0, y0, nx, ny)


def _read_square_fits(url: str, ra: float, dec: float, half: int):
    """With FLAGS: generic FITS reader over HTTP (fsspec), needed for the compressed FLAGS.

    -> (IMAGE header, data or None if off the detector, flags or None, (xc, yc, x0, y0, nx, ny))
    """
    with fits.open(url, use_fsspec=True,
                   fsspec_kwargs={"block_size": 1024**2, "cache_type": "bytes"}) as h:
        hdr = h["IMAGE"].header
        xc, yc, x0, x1, y0, y1, nx, ny = _square_bounds(hdr, ra, dec, half)
        if x1 - x0 < 3 or y1 - y0 < 3:
            return hdr, None, None, (xc, yc, x0, y0, nx, ny)
        data = np.array(h["IMAGE"].section[y0:y1, x0:x1], dtype=np.float32)
        flags = np.array(h["FLAGS"].section[y0:y1, x0:x1]).astype(np.int64) & 0xFFFFFFFF
    return hdr, data, flags, (xc, yc, x0, y0, nx, ny)


def _square_bounds(hdr: fits.Header, ra: float, dec: float, half: int):
    """Where the target is in this image and which part of the image the square covers."""
    x, y = (float(v) for v in WCS(hdr).world_to_pixel_values(ra, dec))
    nx, ny = hdr["NAXIS1"], hdr["NAXIS2"]
    xc, yc = round(x), round(y)
    x0, x1 = max(xc - half, 0), min(xc + half + 1, nx)
    y0, y1 = max(yc - half, 0), min(yc + half + 1, ny)
    return xc, yc, x0, x1, y0, y1, nx, ny


def _read_cutout_remote(url: str, exp: Exposure, ra: float, dec: float, half: int,
                        header: tuple[fits.Header, int] | None = None) -> Cutout | None:
    if USE_FLAGS:
        hdr, data, flags, (xc, yc, x0, y0, nx, ny) = _read_square_fits(url, ra, dec, half)
    else:
        hdr, data, flags, (xc, yc, x0, y0, nx, ny) = _read_square_ranges(url, ra, dec, half, header)
    if data is None:
        return None  # the square doesn't overlap this detector

    flags_near = 0
    if flags is not None:
        data[(flags & BAD_FLAG_MASK) != 0] = np.nan
        if 0 <= xc < nx and 0 <= yc < ny:
            lx, ly = xc - x0, yc - y0
            win = flags[max(ly - 2, 0):ly + 3, max(lx - 2, 0):lx + 3]
            flags_near = int(np.bitwise_or.reduce(win.ravel()))

    sample = data[::4, ::4] if data.size > 250_000 else data  # sky level: a sample is plenty
    good = sample[np.isfinite(sample)]
    median = sigma_clipped_stats(good)[1] if good.size else float("nan")
    if np.isfinite(median):
        data -= median  # remove zodiacal light / sky level of this image
    return Cutout(exp, data, x0, y0, hdr.tostring(), flags_near)


def read_cutout(exp: Exposure, ra: float, dec: float, half: int, collection: str) -> Cutout | None:
    """Cached. Returns None if the square is off this detector; raises DownloadError if unreachable."""
    path = (CACHE_DIR / "cutouts" / collection / exp.obs_id
            / f"D{exp.detector}_{_coord_key(ra, dec)}_h{half}.npz")
    if path.exists():
        z = np.load(path)
        meta = json.loads(str(z["meta"]))
        if meta.get("off_chip"):
            return None
        return Cutout(exp, z["data"], meta["x0"], meta["y0"], meta["header"],
                      meta["flags_near_target"])

    header = None if USE_FLAGS else image_header(exp, collection, fetch=False)  # saves a request
    errors = []
    for url in (exp.s3_url, exp.url):  # AWS first, IRSA as fallback
        try:
            cut = _read_cutout_remote(url, exp, ra, dec, half, header)
            break
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
    else:
        raise DownloadError(f"{exp.obs_id} D{exp.detector}: {' | '.join(errors)}")  # not cached

    if cut is None:
        meta, data = {"off_chip": True}, np.zeros((0, 0), np.float32)
    else:
        meta = {"x0": cut.x0, "y0": cut.y0, "header": cut.header,
                "flags_near_target": cut.flags_near_target}
        data = cut.data
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.stem}.{os.getpid()}.tmp.npz")
    np.savez_compressed(tmp, data=data, meta=json.dumps(meta))
    os.replace(tmp, path)
    return cut


# --------------------------------------------------------------------------- #
# 4. Line up on a common grid
# --------------------------------------------------------------------------- #
def make_grid_wcs(ra: float, dec: float, npix: int) -> WCS:
    """North up, east left, SPHEREx pixel scale, target in the middle."""
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crval = [ra, dec]
    w.wcs.crpix = [(npix + 1) / 2, (npix + 1) / 2]
    w.wcs.cdelt = [-PIXEL_SCALE_ARCSEC / 3600, PIXEL_SCALE_ARCSEC / 3600]
    return w


# The exact sky->photo-pixel calculation (with lens distortion) is slow, but the
# result changes very smoothly across the image. So do it exactly only for every
# ALIGN_STEP-th output pixel and fill in the pixels between by interpolation.
ALIGN_STEP = 16


@dataclass
class AlignmentGrid:
    coarse_ra: np.ndarray  # sky position of every ALIGN_STEP-th output pixel
    coarse_dec: np.ndarray
    fine_index: np.ndarray  # (2, npix, npix): where each output pixel sits between those


def make_alignment_grid(grid: WCS, npix: int, step: int = ALIGN_STEP) -> AlignmentGrid:
    n_coarse = (npix - 1) // step + 2  # one extra point so the last pixel is inside
    c = np.arange(n_coarse) * step
    cx, cy = np.meshgrid(c, c)
    coarse_ra, coarse_dec = grid.pixel_to_world_values(cx, cy)
    fine = np.arange(npix, dtype=np.float32) / step
    fy, fx = np.meshgrid(fine, fine, indexing="ij")
    return AlignmentGrid(coarse_ra, coarse_dec, np.stack([fy, fx]))


def reproject_cutout(cut: Cutout, align: AlignmentGrid, data: np.ndarray | None = None) -> np.ndarray:
    """Resample one cutout (or `data` in the cutout's pixel layout) onto the common output grid."""
    x, y = cut.wcs.world_to_pixel_values(align.coarse_ra, align.coarse_dec)  # exact, few points
    x_full = map_coordinates(x, align.fine_index, order=1)  # in between: interpolated
    y_full = map_coordinates(y, align.fine_index, order=1)
    return map_coordinates(cut.data if data is None else data, [y_full - cut.y0, x_full - cut.x0],
                           order=1, mode="constant", cval=np.nan)


def subtract_background(data: np.ndarray) -> tuple[np.ndarray, float]:
    """Remove the sky (zodiacal light, stray-light bands, gradients) and measure the noise.

    Like a background mesh: the sky level is a sigma-clipped median per
    BACKGROUND_TILE x BACKGROUND_TILE tile (stars are clipped away), smoothed
    over 3x3 tiles (one star-filled tile can't dent it), interpolated back to
    every pixel and subtracted. Squares too small for tiles get one level.
    -> (data minus background, noise sigma of the result)
    """
    ny, nx = data.shape
    t = BACKGROUND_TILE
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # all-NaN tiles are fine
        if min(ny, nx) >= 2 * t:
            ty, tx = math.ceil(ny / t), math.ceil(nx / t)
            padded = np.full((ty * t, tx * t), np.nan, np.float32)
            padded[:ny, :nx] = data
            tiles = padded.reshape(ty, t, tx, t).swapaxes(1, 2).reshape(ty, tx, t * t)
            level = np.asarray(sigma_clipped_stats(tiles, axis=2, sigma=3, maxiters=3)[1], float)
            known = np.isfinite(level)
            if known.any():
                level[~known] = np.median(level[known])
                level = median_filter(level, size=3, mode="nearest")
                gy, gx = np.meshgrid((np.arange(ny) + 0.5) / t - 0.5, (np.arange(nx) + 0.5) / t - 0.5,
                                     indexing="ij")
                background = map_coordinates(level, [gy, gx], order=1, mode="nearest")
            else:
                background = 0.0
        else:
            good = data[np.isfinite(data)]
            background = sigma_clipped_stats(good)[1] if good.size else 0.0
        out = (data - background).astype(np.float32)
        sample = out[::4, ::4] if out.size > 250_000 else out
        good = sample[np.isfinite(sample)]
        noise = float(sigma_clipped_stats(good)[2]) if good.size > 10 else float("nan")
    return out, noise


# --------------------------------------------------------------------------- #
# 5. Rendering (same brightness scale for every frame, or blinking is useless)
# --------------------------------------------------------------------------- #
# A "no data" gap counts as a saturated star centre (-> white) when the pixels
# around it are at least this bright on the 0..1 display scale.
SATURATED_SURROUNDING = 0.6


def fill_holes(img01: np.ndarray) -> np.ndarray:
    """Fill "no data" (NaN) gaps that are completely surrounded by image (0..1 scale).

    - surrounded by bright pixels: a saturated star centre (SPHEREx sets those
      to NaN) -> white, i.e. "brighter than measurable", not a black hole
    - otherwise: a small gap between image edges in dark sky -> the median of
      the pixels around it, so it doesn't show as a dot
    NaN touching the frame edge (area not covered by any image) is left alone
    and drawn black later.
    """
    valid = np.isfinite(img01)
    holes = binary_fill_holes(valid) & ~valid
    if not holes.any():
        return img01
    out = img01.copy()
    labels, _ = label(holes)
    for k, (ys, xs) in enumerate(find_objects(labels), start=1):
        # one pixel more on each side, so the ring around the gap is included
        ys = slice(max(ys.start - 1, 0), ys.stop + 1)
        xs = slice(max(xs.start - 1, 0), xs.stop + 1)
        gap = labels[ys, xs] == k
        ring = binary_dilation(gap) & valid[ys, xs]
        around = img01[ys, xs][ring]
        level = float(np.median(around)) if around.size else 0.0
        out[ys, xs][gap] = 1.0 if level >= SATURATED_SURROUNDING else level
    return out


def _shared_stretch(images: list[np.ndarray | None]) -> ImageNormalize:
    """One brightness scale for all frames, or blinking between them is useless."""
    finite = [img[np.isfinite(img)] for img in images if img is not None]
    values = np.concatenate(finite) if finite else np.array([])
    if values.size == 0:
        values = np.array([0.0, 1.0])
    vmin, vmax = np.percentile(values, [0.5, 99.8])
    return ImageNormalize(vmin=vmin, vmax=vmax, stretch=AsinhStretch(0.1), clip=True)


def _save_jpg(img01: np.ndarray, path: Path) -> None:
    """(H, W) grey or (H, W, 3) colour values 0..1 -> JPG (FITS row 0 is the bottom)."""
    arr = (np.clip(img01, 0, 1) * 255).astype(np.uint8)
    Image.fromarray(np.ascontiguousarray(np.flipud(arr))).save(path, quality=92)


def render_frames(frames: list[dict], out_dir: Path) -> list[str]:
    """Write frame_000.jpg ... (exactly the frame size, no upscaling). Returns file names.

    Colour like Backyard Worlds: red = D4-D6, blue = D1-D3, green = their
    average; each channel has its own brightness scale shared by all frames.
    A frame missing one channel is drawn grey from the other.
    """
    stretch = {c: _shared_stretch([f[c] for f in frames]) for c in ("blue", "red", "all")}
    names = []
    for i, f in enumerate(frames):
        if COLOR_FRAMES and (f["blue"] is not None or f["red"] is not None):
            def channel(name):
                if f[name] is None:
                    return None
                # per channel: a star can saturate in one channel only
                return fill_holes(np.asarray(stretch[name](f[name]), float))
            blue, red = channel("blue"), channel("red")
            blue = red if blue is None else blue
            red = blue if red is None else red
            img01 = np.stack([red, (red + blue) / 2, blue], axis=-1)
        else:
            img01 = fill_holes(np.asarray(stretch["all"](f["all"]), float))
        img01 = np.nan_to_num(img01, nan=0.0)  # not covered by any image: black
        name = f"frame_{i:03d}.jpg"
        _save_jpg(img01, out_dir / name)
        names.append(name)
    return names


# Before subtracting, frames are blurred by this much (Gaussian sigma, pixels).
# SPHEREx pixels are coarse compared to a star, so the same star lands slightly
# differently on the pixel grid in each pass; unblurred, almost every star would
# leave a small black/white pair. 0 = no blur.
DIFF_SMOOTH_PX = 1.5


def _smooth(img: np.ndarray, sigma: float) -> np.ndarray:
    """Gaussian blur that ignores NaN (normalised convolution); NaN stays NaN."""
    if sigma <= 0:
        return img
    ok = np.isfinite(img)
    num = gaussian_filter(np.where(ok, img, 0.0), sigma)
    den = gaussian_filter(ok.astype(float), sigma)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = num / den
    out[~ok] = np.nan
    return out


def render_differences(frames: list[dict], out_dir: Path) -> list[str]:
    """Write diff/diff_000.jpg ...: each frame minus the average of all frames.

    Like Backyard Worlds' difference images: stars that don't change cancel to
    grey; a moving object is white where it is in this frame and black where it
    is in the others (a "dipole"); a variable star is white or black.
    """
    if len(frames) < 2:
        return []
    smoothed = [_smooth(f["all"], DIFF_SMOOTH_PX) for f in frames]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        reference = np.nanmean(smoothed, axis=0)
    diffs = [s - reference for s in smoothed]
    spread = np.concatenate([np.abs(d[np.isfinite(d)]) for d in diffs])
    limit = float(np.percentile(spread, 99.5)) if spread.size else 1.0
    limit = limit if limit > 0 else 1.0

    (out_dir / "diff").mkdir(exist_ok=True)
    names = []
    for i, d in enumerate(diffs):
        img01 = np.nan_to_num(0.5 + d / (2 * limit), nan=0.5)  # grey = no change
        name = f"diff/diff_{i:03d}.jpg"
        _save_jpg(img01, out_dir / name)
        names.append(name)
    return names


def catalogue_links(ra: float, dec: float, pixels: int) -> dict[str, str]:
    """Look up what's known in the field (Backyard Worlds' volunteers check these)."""
    radius_arcmin = pixels * PIXEL_SCALE_ARCSEC / 60 / 2 * math.sqrt(2)
    coord = f"{ra:.6f} {dec:+.6f}"
    q = urllib.parse.urlencode
    return {
        "simbad": "https://simbad.cds.unistra.fr/simbad/sim-coo?"
                  + q({"Coord": coord, "Radius": f"{radius_arcmin:.2f}", "Radius.unit": "arcmin"}),
        "vizier": "https://vizier.cds.unistra.fr/viz-bin/VizieR?"
                  + q({"-c": coord, "-c.rs": f"{radius_arcmin * 60:.0f}"}),
        "aladin": "https://aladin.cds.unistra.fr/AladinLite/?"
                  + q({"target": coord, "fov": f"{pixels * PIXEL_SCALE_ARCSEC / 3600:.3f}"}),
    }


# --------------------------------------------------------------------------- #
# Workers: download one image (thread), then line it up (process)
# --------------------------------------------------------------------------- #
def _download_one(exp: Exposure, ra: float, dec: float, half: int, collection: str) -> tuple:
    """Runs in a download thread: make sure the square is in the cache.

    -> ("ok",) | ("off_chip",) | ("failed", reason)
    """
    try:
        cut = read_cutout(exp, ra, dec, half, collection)
    except DownloadError as exc:
        return ("failed", str(exc))
    return ("off_chip",) if cut is None else ("ok",)


def _download_then_align(downloads: ThreadPoolExecutor, aligners: ProcessPoolExecutor,
                         exp: Exposure, job: tuple) -> Future:
    """Queue one image: download it in a thread, and as soon as that is done line it
    up in a process. Returns a future with _load_one's result (always set, also on errors).
    """
    result: Future = Future()

    def fail(exc: BaseException) -> None:
        if not result.done():
            result.set_result(("failed", f"{type(exc).__name__}: {exc}"))

    def after_align(f: Future) -> None:
        try:
            result.set_result(f.result())
        except BaseException as exc:
            fail(exc)

    def after_download(f: Future) -> None:
        try:
            status = f.result()
            if status[0] != "ok":
                result.set_result(status)
            else:
                aligners.submit(_load_one, exp).add_done_callback(after_align)
        except BaseException as exc:
            fail(exc)

    downloads.submit(_download_one, exp, *job[:4]).add_done_callback(after_download)
    return result


_JOB: dict = {}  # per worker process: what every task of this job needs


def _init_worker(ra: float, dec: float, half: int, collection: str, pixels: int) -> None:
    """Runs once when each worker process starts.

    Rebuilding the alignment grid here is cheaper than sending it with every task.
    """
    _JOB.update(ra=ra, dec=dec, half=half, collection=collection,
                align=make_alignment_grid(make_grid_wcs(ra, dec, pixels), pixels))


def _load_one(exp: Exposure) -> tuple:
    """Runs in a worker process: load the square (from the cache), remove its sky
    background and line it up.

    -> ("ok", exposure, lined-up image, flags, noise) | ("off_chip",) | ("failed", reason)
    Only the lined-up PIXELS x PIXELS image is sent back, so memory stays small.
    """
    j = _JOB
    try:
        cut = read_cutout(exp, j["ra"], j["dec"], j["half"], j["collection"])
    except DownloadError as exc:
        return ("failed", str(exc))
    if cut is None:
        return ("off_chip",)
    data, noise = subtract_background(cut.data)
    return ("ok", exp, reproject_cutout(cut, j["align"], data), cut.flags_near_target, noise)


# --------------------------------------------------------------------------- #
# The job: build all frames into out_dir
# --------------------------------------------------------------------------- #
def build_frames(ra: float, dec: float, pixels: int, out_dir: Path,
                 wanted_detectors=None) -> dict:
    """Write frame_*.jpg into out_dir and return the metadata (without status)."""
    use_detectors = detectors.parse_detectors(wanted_detectors)  # None -> all six
    collection = latest_collection()
    exposures = detectors.select(find_exposures(ra, dec, collection), use_detectors)
    if not exposures:
        raise BackendError(EXIT_NO_DATA, f"no {collection} images cover RA={ra:.5f} Dec={dec:+.5f}")

    half = math.ceil(pixels / 2 * math.sqrt(2)) + 2  # covers the grid at any rotation
    passes = group_into_passes(exposures)
    coverage = coverage_of(exposures, ra, dec, pixels, collection)
    n_full = sum(1 for c in coverage.values() if c is not None and c >= FULL_COVERAGE)
    log.info("%d of %d images cover the whole frame", n_full, len(exposures))
    time_bins = plan_frames(passes, coverage=coverage)
    # passes skipped: too few images per detector to remove glitches
    bins_skipped = sum(1 for p in passes if not detectors.pick(p))
    if not time_bins:  # nothing has enough images: better a glitchy video than none
        time_bins = [b for b in (detectors.pick(p, min_per_detector=1) for p in passes) if b]
        bins_skipped = 0

    job = (ra, dec, half, collection, pixels)
    # How many images may be queued ahead (their lined-up results wait in RAM).
    max_queued = max(2 * DOWNLOAD_THREADS, QUEUE_AHEAD_BYTES // (pixels * pixels * 4))
    log.info("%d images available, using %d in %d time bins (%d skipped: too few images), "
             "%dx%d px squares, %d download threads, %d processes",
             len(exposures), sum(map(len, time_bins)), len(time_bins), bins_skipped,
             2 * half + 1, 2 * half + 1, DOWNLOAD_THREADS, WORKER_PROCESSES)
    frames, frame_meta, n_failed = [], [], 0
    with ThreadPoolExecutor(DOWNLOAD_THREADS) as downloads, \
            ProcessPoolExecutor(WORKER_PROCESSES, initializer=_init_worker, initargs=job) as aligners:
        queued: deque[list[Future]] = deque()  # one list of futures per queued time bin
        next_bin = 0
        for _ in range(len(time_bins)):
            # Queue further bins while there's room (always at least the next one).
            # Downloads run in queue order, so the early bins finish first.
            while next_bin < len(time_bins) and (
                    not queued or sum(map(len, queued)) + len(time_bins[next_bin]) <= max_queued):
                queued.append([_download_then_align(downloads, aligners, e, job)
                               for e in time_bins[next_bin]])
                next_bin += 1
            results = [f.result() for f in queued.popleft()]
            for r in results:
                if r[0] == "failed":
                    n_failed += 1
                    log.warning("skipping %s", r[1])
            loaded = [r[1:] for r in results if r[0] == "ok"]
            if not loaded:
                continue
            by_detector: dict[int, list[np.ndarray]] = {}
            noise_by_detector: dict[int, list[float]] = {}
            flags = 0
            for exp, img, f, noise in loaded:
                by_detector.setdefault(exp.detector, []).append(img)
                noise_by_detector.setdefault(exp.detector, []).append(noise)
                flags |= f
            frame = detectors.combine(by_detector, noise_by_detector)
            if not np.isfinite(frame["all"]).any():
                continue
            mjds = [exp.mjd for exp, *_ in loaded]
            frames.append(frame)
            frame_meta.append({
                "timestamp": Time(float(np.mean(mjds)), format="mjd").isot,
                "mjd_mean": float(np.mean(mjds)),
                "date_start": Time(min(mjds), format="mjd").iso[:10],
                "date_end": Time(max(mjds), format="mjd").iso[:10],
                "n_images": len(loaded),
                "detectors": sorted(by_detector),
                "channels": [c for c in ("blue", "red") if frame[c] is not None],
                "flag_bits_near_target": ([b for b in range(32) if flags >> b & 1]
                                          if USE_FLAGS else None),  # None = FLAGS not read
            })

    if not frames:
        if n_failed:
            raise BackendError(EXIT_DOWNLOAD_FAILED,
                               f"{n_failed} images found but none could be downloaded - try again later")
        raise BackendError(EXIT_NO_DATA, f"no {collection} images cover RA={ra:.5f} Dec={dec:+.5f}")

    names = render_frames(frames, out_dir)
    diff_names = render_differences(frames, out_dir)
    for i, (meta, name) in enumerate(zip(frame_meta, names)):
        meta["image"] = name
        meta["difference_image"] = diff_names[i] if diff_names else None

    return {
        "collection": collection,
        "pixel_scale_arcsec": PIXEL_SCALE_ARCSEC,
        "detectors": list(use_detectors),
        "colour": ({"red": "D4-D6 (2.4-5.0 um)", "green": "average of red and blue",
                    "blue": "D1-D3 (0.75-2.4 um)"} if COLOR_FRAMES else None),
        "images": names,
        "timestamps": [m["timestamp"] for m in frame_meta],
        "difference_images": diff_names,
        "frames": frame_meta,
        "images_failed": n_failed,
        "time_bins_skipped": bins_skipped,
        "links": catalogue_links(ra, dec, pixels),
    }


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)

    try:
        dec, ra, pixels, user_id = parse_args(argv)
    except BackendError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code

    final = USR_DIR / user_id
    pending = USR_DIR / f".pending_{user_id}"
    USR_DIR.mkdir(parents=True, exist_ok=True)
    if final.exists():
        print(f"error: {final} already exists (not picked up by the middleware yet)", file=sys.stderr)
        return EXIT_FOLDER_EXISTS
    try:
        pending.mkdir()  # fails if another job for this uuid is running / crashed
    except FileExistsError:
        print(f"error: {pending} already exists (job running or crashed)", file=sys.stderr)
        return EXIT_FOLDER_EXISTS

    t0 = time.time()
    try:
        meta = build_frames(ra, dec, pixels, pending)
        meta = {"status": "ok", "exit_code": EXIT_OK, **meta}
    except BackendError as exc:
        meta = {"status": "error", "exit_code": exc.code, "error": str(exc)}
    except MemoryError:
        meta = {"status": "error", "exit_code": EXIT_UNEXPECTED,
                "error": f"not enough memory for PIXELS={pixels}"}
    except Exception as exc:
        log.exception("unexpected error")
        meta = {"status": "error", "exit_code": EXIT_UNEXPECTED,
                "error": f"{type(exc).__name__}: {exc}"}
    meta.update(dec=dec, ra=ra, pixels=pixels, uuid=user_id, seconds=round(time.time() - t0, 1))
    _write_json(pending / "meta.json", meta)

    if final.exists():  # appeared while we were working - don't overwrite it
        print(f"error: {final} appeared during the job; result left in {pending}", file=sys.stderr)
        return EXIT_FOLDER_EXISTS
    os.rename(pending, final)

    if meta["status"] != "ok":
        print(f"error: {meta['error']}", file=sys.stderr)
    else:
        log.info("%d frames in %.1fs -> %s", len(meta["images"]), meta["seconds"], final)
    return meta["exit_code"]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
