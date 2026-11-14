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
    2. the frames are built into it (or, for a request identical to an earlier
       one, copied from Cache/results/ - see build_frames_cached):
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

import functools
import hashlib
import json
import logging
import math
import os
import re
import shutil
import sys
import threading
import time
import urllib.parse
import uuid as uuidlib
import warnings
from collections import Counter, deque
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TypeVar

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
from PIL import Image, ImageDraw, ImageFont
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
#     cutouts/<collection>/<obs_id>/D<n>_<coord>_p<pixels>.npz
#                                                 the part of one image a frame needs
#                                                 (_h<half>.npz: older squares, still reused)
#     coverage/<collection>/<coord>_p<pixels>.json
#                                                 how much of the frame each image covers
#     results/<collection>/<coord>_p<pixels>/     finished frames of an earlier identical
#                                                 request (+ result.json), see build_frames_cached
#     results/.staging_<uuid>/                    a stored result being copied out (temporary)
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

# What one frame of the video is:
#   "visits": one SPHEREx visit (its ~6 images are taken within minutes, visits
#             are ~11 h apart). Nothing moves noticeably during a visit, so moving
#             things survive the glitch removal: asteroids (2-6 px/hour) jump
#             between frames, TNOs (~14 px/day) crawl, a distant planet (~1 px/day
#             at 500 AU) drifts. ~25 frames, each ~2x noisier than a "passes" frame.
#   "passes": a third of a pass each (24 images over ~2 weeks): deep, clean
#             frames, but anything moving more than ~1 px in 2 weeks is removed
#             as a glitch or smeared (that includes a planet closer than ~1000 AU).
FRAME_MODE = "visits"
VISIT_GAP_H = 1.0           # images closer in time than this belong to one visit
MIN_IMAGES_PER_CHANNEL = 3  # a visit is used if it has this many blue AND red images
                            # covering the whole frame (median of 3 removes glitches)
MAX_VISIT_FRAMES = 30       # at most this many visits (evenly spread over time)
MAX_IMAGES_PER_VISIT = 12   # (visits near the ecliptic poles can have more)

# Sky level is estimated per tile of this many pixels (~3.3') and smoothed, so
# large-scale gradients and stray-light bands are removed but stars are kept.
BACKGROUND_TILE = 32

# Frames in colour (blue = D1-D3, red = D4-D6, see detectors.CHANNELS) or grey.
COLOR_FRAMES = True
# Two kinds of workers (see build_frames):
#  - DOWNLOAD_THREADS threads fetch images into the cache. Waiting for the
#    network needs almost no CPU, so many at once is cheap. The downloads are
#    many small requests (see MULTIRANGE_ROWS) whose time is mostly waiting for
#    IRSA's answer (~0.5 s each). Measured for a new 255 px spot: with IRSA
#    answering quickly, 32 -> 14.1 s of downloading, 96 -> 8.8 s; with IRSA
#    busy it's the other way round (96 queued up: median request 3.2 s vs 1.4 s
#    with 32). 64 is the compromise - and puts less load on IRSA when several
#    users run jobs at once.
#  - WORKER_PROCESSES processes line the images up. That's number crunching,
#    which threads can't spread over cores (Python's GIL) but processes can.
#    Each process uses ~130 MB of RAM.
DOWNLOAD_THREADS = 64
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


COVERAGE_TASK_IMAGES = 10  # images per worker-process task in coverage_of


def coverage_of(exposures: list[Exposure], ra: float, dec: float, pixels: int, collection: str,
                threads: ThreadPoolExecutor, processes: ProcessPoolExecutor
                ) -> dict[tuple[str, int], float | None]:
    """How much of the frame each image covers (None = header not available).

    Only the headers are needed (cached per image, see image_header) - much
    cheaper than downloading pixels that turn out unusable. Each answer needs a
    WCS built from the header (~13 ms, the lens-distortion terms make it slow),
    ~4 s for a typical spot in one process, so it is spread over the worker
    processes (`processes`, started with _init_worker for this frame), while
    missing headers are fetched in `threads`: images whose header is cached are
    computed at once, the others as soon as their header has arrived.
    The answers are cached per position + frame size in Cache/coverage/; only
    images not in it are computed. "Header not available" is not cached (retried).
    """
    path = CACHE_DIR / "coverage" / collection / f"{_coord_key(ra, dec)}_p{pixels}.json"
    known: dict[tuple[str, int], float] = {(o, d): c for o, d, c in (_read_json(path) or [])}
    todo = [e for e in exposures if (e.obs_id, e.detector) not in known]
    if todo:
        def fetch_header(e: Exposure) -> bool:
            try:
                image_header(e, collection)
                return True
            except Exception as exc:
                log.warning("no header for %s D%d: %s", e.obs_id, e.detector, exc)
                return False

        def submit(images: list[Exposure]) -> list[tuple[list[Exposure], Future]]:
            n = COVERAGE_TASK_IMAGES
            return [(images[i:i + n], processes.submit(_coverage_task, images[i:i + n]))
                    for i in range(0, len(images), n)]

        cached = [e for e in todo if _header_path(e, collection).exists()]
        missing = [e for e in todo if not _header_path(e, collection).exists()]
        tasks = submit(cached)  # computed while the missing headers download
        tasks += submit([e for e, ok in zip(missing, threads.map(fetch_header, missing)) if ok])
        new = {(e.obs_id, e.detector): c for images, f in tasks
               for e, c in zip(images, f.result()) if c is not None}
        if new:
            known.update(new)
            _write_json(path, [[o, d, c] for (o, d), c in sorted(known.items())])
    return {(e.obs_id, e.detector): known.get((e.obs_id, e.detector)) for e in exposures}


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


def group_into_visits(exposures: list[Exposure]) -> list[list[Exposure]]:
    """Group the images by SPHEREx visit: images less than VISIT_GAP_H apart."""
    visits: list[list[Exposure]] = []
    for e in sorted(exposures, key=lambda e: e.mjd):
        if visits and (e.mjd - visits[-1][-1].mjd) * 24 < VISIT_GAP_H:
            visits[-1].append(e)
        else:
            visits.append([e])
    return visits


def plan_visit_frames(exposures: list[Exposure], coverage: dict[tuple[str, int], float | None],
                      min_frames: int = MIN_FRAMES, max_frames: int = MAX_VISIT_FRAMES
                      ) -> tuple[list[list[Exposure]], int]:
    """FRAME_MODE "visits": one frame per visit -> (frames, number of visits not used).

    Used are the visits with >= MIN_IMAGES_PER_CHANNEL blue AND red images that
    cover the whole frame (enough for the median to remove glitches in both
    colours); all images of such a visit go into its frame. If there are fewer
    than min_frames of them, the visits with the most images are added (glitches
    may survive there). At most max_frames visits, evenly spread over time.
    """
    def covers(e: Exposure) -> bool:
        return (coverage.get((e.obs_id, e.detector)) or 0.0) >= FULL_COVERAGE

    def complete(v: list[Exposure]) -> bool:
        return all(sum(1 for e in v if e.detector in dets and covers(e)) >= MIN_IMAGES_PER_CHANNEL
                   for dets in detectors.CHANNELS.values())

    visits = group_into_visits(exposures)
    chosen = [v for v in visits if complete(v)]
    if len(chosen) < min_frames:
        rest = sorted((v for v in visits if not complete(v)), key=len, reverse=True)
        chosen = sorted(chosen + rest[:min_frames - len(chosen)], key=lambda v: v[0].mjd)
    if len(chosen) > max_frames:
        chosen = [chosen[round(i * (len(chosen) - 1) / (max_frames - 1))] for i in range(max_frames)]
    frames = [detectors.pick(v, MAX_IMAGES_PER_VISIT, min_per_detector=1) for v in chosen]
    return frames, len(visits) - len(frames)


# --------------------------------------------------------------------------- #
# 3. Read the part of one image the frame needs (only those bytes are downloaded)
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
HEADER_PROBE_BYTES = 32 * 1024  # first bytes of the file: PRIMARY + IMAGE header (25.9-28.8 KB measured)
FITS_BLOCK = 2880

# Whole rows are 2040 px wide, so for a small square most of that request is
# thrown away (200 px frame: ~7x more than needed). Instead, only the square's
# part of each row can be asked for - one byte range per row, many ranges per
# request. AWS S3 doesn't support several ranges in one request (it sends the
# whole file), IRSA does. Measured at IRSA: 100 ranges per request work; 200 got
# the connection reset; 300-400 were answered with the whole 65 MB file; 600 were
# refused (header too long). Hence MULTIRANGE_ROWS rows per request: the most
# that worked (fewer requests = faster, each one is mostly waiting for IRSA).
# Used when it downloads at least MULTIRANGE_MIN_SAVING x less than whole rows;
# otherwise (or if it fails) whole rows from AWS as before.
MULTIRANGE_ROWS = 100
MULTIRANGE_MIN_SAVING = 2.0
# After this many failed multi-range downloads in one run, stop trying them
# (IRSA down or refusing): each failure costs retries before the AWS fallback.
MULTIRANGE_MAX_FAILURES = 5
_multirange_failures = 0
_multirange_lock = threading.Lock()

# Slow requests: most answers take ~0.5-1 s, but every run had a few taking up
# to 10 s, and the job waits for the slowest. So a header request that hasn't
# answered after HEDGE_AFTER_S is also sent to the other server (AWS -> IRSA)
# and whichever answers first is used. Only to the OTHER server: repeating a
# request to the same busy server made things worse (see _read_footprint).
HEDGE_AFTER_S = 2.0
T = TypeVar("T")
def _in_background(call: Callable[[], T]) -> Future:
    """Run call in a daemon thread. (Not a ThreadPoolExecutor: Python waits for
    those at exit, so a dropped slow request would keep the backend - and the
    user - waiting after the frames are done. Daemon threads are just ended.)"""
    future: Future = Future()

    def run() -> None:
        try:
            future.set_result(call())
        except BaseException as exc:
            future.set_exception(exc)

    threading.Thread(target=run, daemon=True).start()
    return future


def _first_success(calls: list[Callable[[], T]], hedge_after: float | None = None,
                   on_next: Callable[[], None] | None = None) -> T:
    """Start calls[0]; whenever nothing has answered for `hedge_after` seconds (or
    an attempt failed), also start the next call. -> the first successful result;
    raises DownloadError with all errors if every call fails. Slower attempts
    still running are left to finish in the background (their answer is dropped)."""
    hedge_after = HEDGE_AFTER_S if hedge_after is None else hedge_after
    remaining = list(calls)
    running: set[Future] = {_in_background(remaining.pop(0))}
    errors: list[str] = []
    while running:
        done, running = wait(running, timeout=hedge_after, return_when=FIRST_COMPLETED)
        for f in done:
            exc = f.exception()
            if exc is None:
                return f.result()
            errors.append(f"{type(exc).__name__}: {exc}")
        if remaining:  # too slow, or an attempt failed: start the next one
            running.add(_in_background(remaining.pop(0)))
            if on_next:
                on_next()
    raise DownloadError(" | ".join(errors))

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


# ---- per-job statistics (logged at the end of build_frames) -----------------
class JobStats:
    """Counts and timings of one job, so the log shows where the time went.

    Only the main process counts (the download threads); the worker processes
    read images from the cache and aren't counted. Thread-safe.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counts: Counter[str] = Counter()
        self.requests: dict[str, list[tuple[float, int]]] = {}  # kind -> [(seconds, bytes)]
        self.image_seconds: list[float] = []  # per downloaded image: how long it took
        self.phases: dict[str, float] = {}

    def count(self, key: str, n: int = 1) -> None:
        with self._lock:
            self.counts[key] += n

    def request(self, kind: str, seconds: float, nbytes: int) -> None:
        with self._lock:
            self.requests.setdefault(kind, []).append((seconds, nbytes))

    def image(self, seconds: float) -> None:
        with self._lock:
            self.image_seconds.append(seconds)

    def log(self, n_used: int, n_failed: int) -> None:
        def spread(values: list[float]) -> str:
            v = np.asarray(values)
            return (f"median {np.median(v):.2f} s, p90 {np.percentile(v, 90):.2f} s, "
                    f"max {v.max():.2f} s") if v.size else "-"

        p, c = self.phases, self.counts
        log.info("job stats: total %.1f s = image search %.1f s + headers/coverage %.1f s "
                 "(%d headers fetched, %d also asked from IRSA because AWS was slow) + images %.1f s",
                 p.get("total", 0), p.get("search", 0), p.get("coverage", 0),
                 c["headers_fetched"], c["header_second_server"], p.get("images", 0))
        log.info("  images: %d used, %d failed | %d from cache, %d cut from a bigger cached one, "
                 "%d downloaded (%d only the needed columns from IRSA, %d whole rows; "
                 "AWS backup started for %d, used for %d) | per downloaded image: %s",
                 n_used, n_failed, c["cutout_cached"], c["cutout_cut_from_bigger"],
                 c["cutout_downloaded"], c["columns_only_used"], c["whole_rows_used"],
                 c["backup_started"], c["backup_used"], spread(self.image_seconds))
        for kind, reqs in sorted(self.requests.items()):
            log.info("  requests %-10s %4d, %6.1f MB, %s%s", kind, len(reqs),
                     sum(b for _, b in reqs) / 1e6, spread([s for s, _ in reqs]),
                     f", {c['failed_' + kind]} failed attempts (retried)" if c["failed_" + kind] else "")


_stats = JobStats()


def _request_kind(url: str, range_spec: str) -> str:
    if "," in range_spec:
        return "columns"  # multi-range: only the needed columns (IRSA)
    server = "aws" if "amazonaws.com" in url else "irsa"
    return f"header_{server}" if range_spec.startswith("bytes=0-") else f"rows_{server}"


def _http_range(url: str, start: int, end: int) -> bytes:
    """Download bytes start..end (inclusive) of url, retrying stuck/failed attempts."""
    return _http_get(url, f"bytes={start}-{end}", want=end - start + 1)[0]


def _http_get(url: str, range_spec: str, want: int | None = None,
              judge_speed: bool = True) -> tuple[bytes, dict[str, str]]:
    """GET with a Range header -> (body, response headers), retrying stuck/failed attempts.

    Only a 206 answer is accepted. Anything else (e.g. 200 = the whole 65 MB file,
    which servers send when they don't like the Range) is aborted before its
    body is downloaded. want: expected body size, if known.
    judge_speed=False: don't abort for being slow (requests that are mostly
    waiting time, see _http_multirange) - a stalled connection still is.
    """
    errors = []
    for attempt in range(1, HTTP_ATTEMPTS + 1):
        t0 = time.monotonic()
        try:
            with _session().get(url, headers={"Range": range_spec}, stream=True,
                                timeout=(HTTP_CONNECT_TIMEOUT_S, HTTP_STALL_TIMEOUT_S)) as r:
                if r.status_code != 206:  # 206 = "here is the part you asked for"
                    raise DownloadError(f"HTTP {r.status_code} (expected 206)")
                headers = dict(r.headers)
                buf = bytearray()
                for chunk in r.iter_content(64 * 1024):
                    buf += chunk
                    elapsed = time.monotonic() - t0
                    # (the limit is re-read each time: early downloads start before
                    # anything has finished, so "typical speed" isn't known yet)
                    if (judge_speed and attempt < HTTP_ATTEMPTS and elapsed > HTTP_SLOW_AFTER_S
                            and len(buf) / elapsed < _too_slow_below()):
                        raise DownloadError(f"too slow ({len(buf) / elapsed / 1000:.0f} kB/s)")
            if want is not None and len(buf) != want:
                raise DownloadError(f"got {len(buf)} of {want} bytes")
            if judge_speed and len(buf) >= 256 * 1024:  # small requests are mostly waiting time
                with _speeds_lock:
                    _recent_speeds.append(len(buf) / max(time.monotonic() - t0, 1e-3))
            _stats.request(_request_kind(url, range_spec), time.monotonic() - t0, len(buf))
            return bytes(buf), headers
        except (requests.RequestException, DownloadError) as exc:
            errors.append(f"try {attempt}: {type(exc).__name__}: {exc}")
            _stats.count("failed_" + _request_kind(url, range_spec))
    raise DownloadError("; ".join(errors))


_CONTENT_RANGE_RE = re.compile(rb"content-range:[ \t]*bytes[ \t]+(\d+)-(\d+)/", re.IGNORECASE)


def _http_multirange(url: str, ranges: list[tuple[int, int]]) -> list[bytes]:
    """Several byte ranges (start, end inclusive) of url in ONE request -> their bytes, same order.

    The server answers with "multipart/byteranges": the parts one after the
    other, each with a small header saying which bytes it holds. IRSA does
    this, AWS S3 doesn't (see MULTIRANGE_ROWS). The server may also merge
    neighbouring ranges into one part, so each range is looked up by position.
    """
    body, headers = _http_get(url, "bytes=" + ",".join(f"{s}-{e}" for s, e in ranges),
                              judge_speed=False)
    parts = _split_byteranges(body, headers)
    out = []
    for s, e in ranges:
        for start, data in parts:
            if start <= s and e < start + len(data):
                out.append(data[s - start:e - start + 1])
                break
        else:
            raise DownloadError(f"bytes {s}-{e} missing from the multipart answer")
    return out


def _split_byteranges(body: bytes, headers: dict[str, str]) -> list[tuple[int, bytes]]:
    """Body of a 206 answer -> [(first byte position, bytes), ...] of its parts."""
    ctype = headers.get("Content-Type", "")
    if not ctype.lower().startswith("multipart/byteranges"):
        # one part only (a single range, or the server merged them all)
        m = _CONTENT_RANGE_RE.match(b"content-range: " + headers.get("Content-Range", "").encode())
        if not m:
            raise DownloadError(f"206 answer without Content-Range ({ctype!r})")
        return [(int(m[1]), body)]
    boundary = ctype.split("boundary=", 1)[1].split(";")[0].strip().strip('"')
    delimiter = b"--" + boundary.encode()
    parts, pos = [], 0
    while True:
        pos = body.find(delimiter, pos)
        if pos < 0:
            raise DownloadError("multipart answer ends early")
        pos += len(delimiter)
        if body.startswith(b"--", pos):  # closing delimiter
            return parts
        head_end = body.find(b"\r\n\r\n", pos)
        m = _CONTENT_RANGE_RE.search(body, pos, head_end) if head_end >= 0 else None
        if m is None:
            raise DownloadError("multipart part without Content-Range")
        start, end = int(m[1]), int(m[2])
        data_start = head_end + 4
        data = body[data_start:data_start + end - start + 1]
        if len(data) != end - start + 1:
            raise DownloadError(f"multipart part {start}-{end} is cut off")
        parts.append((start, data))
        pos = data_start + len(data)  # jump over the data: it may contain anything


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
    """IMAGE header + the byte offset where its data starts, from the file's first bytes.

    HEADER_PROBE_BYTES covers all headers seen so far; a longer one is fetched again bigger.
    """
    for probe in (HEADER_PROBE_BYTES, 8 * HEADER_PROBE_BYTES):
        buf = _http_range(url, 0, probe - 1)
        primary = _parse_header(buf, 0)
        image = primary and _parse_header(buf, primary[1] + _data_bytes(primary[0]))
        if image is not None:
            break
    if image is None or image[0].get("EXTNAME") != "IMAGE":
        raise DownloadError("unexpected file layout (IMAGE is not the first extension)")
    hdr, data_offset = image
    if hdr.get("BITPIX") != -32 or hdr.get("NAXIS") != 2:
        raise DownloadError(f"unexpected IMAGE format BITPIX={hdr.get('BITPIX')} NAXIS={hdr.get('NAXIS')}")
    return hdr, data_offset


def _header_path(exp: Exposure, collection: str) -> Path:
    return CACHE_DIR / "headers" / collection / exp.obs_id / f"D{exp.detector}.json"


def image_header(exp: Exposure, collection: str, fetch: bool = True) -> tuple[fits.Header, int] | None:
    """IMAGE header of one exposure + its data offset, cached in Cache/headers/.

    A header doesn't depend on the position looked at, so once fetched it is
    reused for every request (and saves that request when downloading pixels).
    fetch=False: only look in the cache (None if not there).
    """
    path = _header_path(exp, collection)
    cached = _read_json(path)
    if cached is not None:
        return fits.Header.fromstring(cached["header"]), cached["data_offset"]
    if not fetch:
        return None
    try:  # AWS first; IRSA too if AWS fails or is slow (see HEDGE_AFTER_S)
        hdr, data_offset = _first_success([functools.partial(_fetch_image_header, exp.s3_url),
                                           functools.partial(_fetch_image_header, exp.url)],
                                          on_next=lambda: _stats.count("header_second_server"))
    except DownloadError as exc:
        raise DownloadError(f"{exp.obs_id} D{exp.detector} header: {exc}") from None
    _stats.count("headers_fetched")
    _write_json(path, {"header": hdr.tostring(), "data_offset": data_offset})
    return hdr, data_offset


# ---- which part of an image a frame needs ----------------------------------
# The frame is a north-up square, the images are tilted against it (~73 deg at
# the spots measured), so in an image the frame covers a tilted square. Only
# that is downloaded: per image row just the columns the tilted square crosses,
# plus FOOTPRINT_MARGIN pixels for the interpolation when lining up. A square
# that fits the frame at any tilt (the old way) is ~2x as many pixels.
FOOTPRINT_MARGIN = 3     # image pixels around the frame
# Frame pixels between the points traced along the frame's edge. Per row, ring
# points within 1 + this many rows count; on the shallow edges of the tilted
# square every extra row there widens the row by ~3 px, so keep it small.
FOOTPRINT_RING_STEP = 1


@dataclass
class Footprint:
    """The image pixels a frame needs: rows y0..y0+len-1, in row i the columns
    x_lo[i]..x_hi[i] (inclusive; x_lo > x_hi = nothing needed in that row)."""
    y0: int
    x_lo: np.ndarray
    x_hi: np.ndarray

    @property
    def rows(self) -> int:
        return len(self.x_lo)

    @property
    def used(self) -> np.ndarray:
        return np.flatnonzero(self.x_lo <= self.x_hi)

    @property
    def x0(self) -> int:
        return int(self.x_lo[self.used].min())

    @property
    def x1(self) -> int:  # exclusive
        return int(self.x_hi[self.used].max()) + 1

    @property
    def n_pixels(self) -> int:
        return int(np.clip(self.x_hi - self.x_lo + 1, 0, None).sum())


@functools.lru_cache(maxsize=4)
def frame_ring(ra: float, dec: float, pixels: int) -> tuple[np.ndarray, np.ndarray]:
    """Sky positions of points along the frame's edge, one pixel outside it."""
    edge = np.append(np.arange(-1.0, pixels, FOOTPRINT_RING_STEP), float(pixels))
    lo, hi = np.full_like(edge, -1.0), np.full_like(edge, float(pixels))
    px = np.concatenate([edge, hi, edge, lo])
    py = np.concatenate([lo, edge, hi, edge])
    return make_grid_wcs(ra, dec, pixels).pixel_to_world_values(px, py)


def frame_footprint(hdr: fits.Header, ring: tuple[np.ndarray, np.ndarray]) -> Footprint | None:
    """Which pixels of this image the frame needs (see Footprint); None if it's off the detector.

    The frame's edge, traced into the image, is a (slightly distorted) tilted
    square. Lining up reads, for every frame pixel inside it, the 2x2 image
    pixels around its position. The left/right ends of a row are where the
    edge crosses it, so per row the ring points within a band of rows give the
    needed columns (the ring is dense enough that no crossing is missed).
    """
    x, y = WCS(hdr).world_to_pixel_values(*ring)
    ok = np.isfinite(x) & np.isfinite(y)
    if not ok.any():
        return None
    x, y = x[ok], y[ok]
    nx, ny = hdr["NAXIS1"], hdr["NAXIS2"]
    m = FOOTPRINT_MARGIN
    r0 = max(int(np.floor(y.min())) - m, 0)
    r1 = min(int(np.floor(y.max())) + 1 + m, ny - 1)
    if r1 - r0 + 1 < 3:
        return None
    rows = np.arange(r0, r1 + 1)
    near = np.abs(y[None, :] - rows[:, None]) <= 1 + FOOTPRINT_RING_STEP
    with np.errstate(invalid="ignore"):
        lo = np.where(near, x[None, :], np.inf).min(axis=1)
        hi = np.where(near, x[None, :], -np.inf).max(axis=1)
    found = np.isfinite(lo)
    x_lo = np.where(found, np.clip(np.floor(np.where(found, lo, 0)) - m, 0, nx - 1), 1).astype(int)
    x_hi = np.where(found, np.clip(np.floor(np.where(found, hi, 0)) + 1 + m, 0, nx - 1), 0).astype(int)
    used = np.flatnonzero(x_lo <= x_hi)
    if len(used) < 3 or (x_hi - x_lo + 1)[used].max() < 3:
        return None
    first, last = used[0], used[-1]
    return Footprint(int(r0 + first), x_lo[first:last + 1], x_hi[first:last + 1])


def _read_footprint(url: str, hdr: fits.Header, data_offset: int, fp: Footprint,
                    multirange: bool, on_request: Callable[[], None] | None = None) -> np.ndarray:
    """Download the footprint's pixels -> (fp.rows, fp.x1 - fp.x0) array, NaN outside it.

    multirange=False: the footprint's rows in full (1 request, only a single range)
    multirange=True: only the needed columns of each row, MULTIRANGE_ROWS rows per
    request (the server must support multipart/byteranges, i.e. IRSA).
    Both give exactly the same array. on_request: called before each request
    (lets _read_footprint_racing see when a request is stuck).
    """
    nx = hdr["NAXIS1"]
    row_bytes = nx * 4
    x0, used = fp.x0, fp.used
    out = np.full((fp.rows, fp.x1 - x0), np.nan, np.float32)
    if multirange:
        ranges = [(data_offset + (fp.y0 + i) * row_bytes + int(fp.x_lo[i]) * 4,
                   data_offset + (fp.y0 + i) * row_bytes + (int(fp.x_hi[i]) + 1) * 4 - 1)
                  for i in used]
        parts = []
        for k in range(0, len(ranges), MULTIRANGE_ROWS):
            if on_request:
                on_request()
            parts += _http_multirange(url, ranges[k:k + MULTIRANGE_ROWS])
        for i, raw in zip(used, parts):
            out[i, fp.x_lo[i] - x0:fp.x_hi[i] + 1 - x0] = np.frombuffer(raw, dtype=">f4")
    else:
        raw = _http_range(url, data_offset + fp.y0 * row_bytes,
                          data_offset + (fp.y0 + fp.rows) * row_bytes - 1)
        full = np.frombuffer(raw, dtype=">f4").reshape(fp.rows, nx)
        for i in used:
            out[i, fp.x_lo[i] - x0:fp.x_hi[i] + 1 - x0] = full[i, fp.x_lo[i]:fp.x_hi[i] + 1]
    if hdr.get("BSCALE", 1) != 1 or hdr.get("BZERO", 0) != 0:
        out = out * np.float32(hdr.get("BSCALE", 1)) + np.float32(hdr.get("BZERO", 0))
    return out


def _cut_to_footprint(cut: Cutout, fp: Footprint) -> np.ndarray:
    """The footprint's pixels taken from a bigger cutout (NaN where it has none)."""
    x0 = fp.x0
    out = np.full((fp.rows, fp.x1 - x0), np.nan, np.float32)
    h, w = cut.data.shape
    for i in fp.used:
        r = fp.y0 + i - cut.y0
        lo, hi = max(int(fp.x_lo[i]), cut.x0), min(int(fp.x_hi[i]) + 1, cut.x0 + w)
        if 0 <= r < h and lo < hi:
            out[i, lo - x0:hi - x0] = cut.data[r, lo - cut.x0:hi - cut.x0]
    return out


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


def _square_with_flags(url: str, exp: Exposure, ra: float, dec: float, pixels: int) -> Cutout | None:
    """USE_FLAGS only: a square that fits the frame at any tilt, bad pixels (FLAGS) set to NaN."""
    half = math.ceil(pixels / 2 * math.sqrt(2)) + 2
    hdr, data, flags, (xc, yc, x0, y0, nx, ny) = _read_square_fits(url, ra, dec, half)
    if data is None:
        return None  # the square doesn't overlap this detector
    data[(flags & BAD_FLAG_MASK) != 0] = np.nan
    flags_near = 0
    if 0 <= xc < nx and 0 <= yc < ny:
        lx, ly = xc - x0, yc - y0
        win = flags[max(ly - 2, 0):ly + 3, max(lx - 2, 0):lx + 3]
        flags_near = int(np.bitwise_or.reduce(win.ravel()))
    return Cutout(exp, data, x0, y0, hdr.tostring(), flags_near)


def _subtract_sky_level(data: np.ndarray) -> np.ndarray:
    """Remove the zodiacal light / sky level of this image (one number)."""
    sample = data[::4, ::4] if data.size > 250_000 else data  # a sample is plenty
    good = sample[np.isfinite(sample)]
    median = sigma_clipped_stats(good)[1] if good.size else float("nan")
    if np.isfinite(median):
        data -= median
    return data


def _download_cutout(exp: Exposure, ra: float, dec: float, pixels: int,
                     collection: str) -> Cutout | None:
    """Download the part of one image the frame needs; None if the frame is off this detector."""
    ring = frame_ring(ra, dec, pixels)
    if USE_FLAGS:
        errors = []
        for url in (exp.s3_url, exp.url):  # AWS first, IRSA as fallback
            try:
                square = _square_with_flags(url, exp, ra, dec, pixels)
                break
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
        else:
            raise DownloadError(f"{exp.obs_id} D{exp.detector}: {' | '.join(errors)}")
        fp = square and frame_footprint(fits.Header.fromstring(square.header), ring)
        if fp is None:
            return None
        data = _subtract_sky_level(_cut_to_footprint(square, fp))
        return Cutout(exp, data, fp.x0, fp.y0, square.header, square.flags_near_target)

    hdr, data_offset = image_header(exp, collection)  # usually cached already (coverage_of)
    fp = frame_footprint(hdr, ring)
    if fp is None:
        return None
    # Only the needed columns, from IRSA (see MULTIRANGE_ROWS), if that saves
    # enough - with whole rows (AWS, then IRSA) as backup. Otherwise whole rows.
    needed = fp.n_pixels * 4 + 100 * fp.rows  # ~100 bytes of part header per row
    try:
        if (fp.rows * hdr["NAXIS1"] * 4 >= MULTIRANGE_MIN_SAVING * needed
                and _multirange_failures < MULTIRANGE_MAX_FAILURES):
            data = _read_footprint_racing(exp, hdr, data_offset, fp)
        else:
            data = _read_whole_rows(exp, hdr, data_offset, fp)
            _stats.count("whole_rows_used")
    except DownloadError as exc:
        raise DownloadError(f"{exp.obs_id} D{exp.detector}: {exc}") from None  # not cached
    return Cutout(exp, _subtract_sky_level(data), fp.x0, fp.y0, hdr.tostring(), 0)


def _read_whole_rows(exp: Exposure, hdr: fits.Header, data_offset: int, fp: Footprint) -> np.ndarray:
    """The footprint via whole rows: AWS first, IRSA as fallback."""
    errors = []
    for url in (exp.s3_url, exp.url):
        try:
            return _read_footprint(url, hdr, data_offset, fp, multirange=False)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
    raise DownloadError(" | ".join(errors))


# Stragglers: IRSA answers most multi-range requests in ~0.5-1 s, but every run
# had a few taking 10-20 s, and each frame waits for all its images. Sending the
# same request to IRSA again doesn't help (tried: the copy waits in the same
# busy queue and adds load - requests got slower, up to 25 s). So when one of an
# image's IRSA requests hasn't answered after STRAGGLER_AFTER_S, the image is
# also fetched as whole rows from AWS (another server, limited only by our
# bandwidth: ~3 MB for 255 px) and whichever finishes first is used.
STRAGGLER_AFTER_S = 3.0


def _read_footprint_racing(exp: Exposure, hdr: fits.Header, data_offset: int,
                           fp: Footprint) -> np.ndarray:
    """Only the needed columns from IRSA; whole rows as backup if IRSA is stuck or fails."""
    request_started = [time.monotonic()]  # when the current IRSA request began

    def columns_only() -> np.ndarray:
        global _multirange_failures
        try:
            return _read_footprint(exp.url, hdr, data_offset, fp, multirange=True,
                                   on_request=lambda: request_started.__setitem__(0, time.monotonic()))
        except Exception as exc:
            with _multirange_lock:
                _multirange_failures += 1
                if _multirange_failures == MULTIRANGE_MAX_FAILURES:
                    log.warning("multi-range downloads keep failing (%s), "
                                "using whole rows from now on", exc)
            raise

    irsa = _in_background(columns_only)
    backup: Future | None = None
    pending, errors = {irsa}, []
    while pending:
        done, pending = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
        for f in done:
            exc = f.exception()
            if exc is None:
                _stats.count("backup_used" if f is backup else "columns_only_used")
                return f.result()  # the other one (if any) finishes in the background, unused
            errors.append(f"{type(exc).__name__}: {exc}")
        stuck = time.monotonic() - request_started[0] > STRAGGLER_AFTER_S
        if backup is None and (irsa in done or stuck):  # failed or stuck -> start the backup
            backup = _in_background(lambda: _read_whole_rows(exp, hdr, data_offset, fp))
            pending.add(backup)
            _stats.count("backup_started")
    raise DownloadError(" | ".join(errors))


def _load_cutout_file(path: Path, exp: Exposure) -> Cutout | None:
    """A cached cutout; None if it is an "off this detector" marker."""
    z = np.load(path)
    meta = json.loads(str(z["meta"]))
    if meta.get("off_chip"):
        return None
    return Cutout(exp, z["data"], meta["x0"], meta["y0"], meta["header"], meta["flags_near_target"])


def _save_cutout_file(path: Path, cut: Cutout | None) -> None:
    if cut is None:
        meta, data = {"off_chip": True}, np.zeros((0, 0), np.float32)
    else:
        meta = {"x0": cut.x0, "y0": cut.y0, "header": cut.header,
                "flags_near_target": cut.flags_near_target}
        data = cut.data
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.stem}.{os.getpid()}.{threading.get_ident()}.tmp.npz")
    np.savez_compressed(tmp, data=data, meta=json.dumps(meta))
    os.replace(tmp, path)


_CUTOUT_NAME_RE = re.compile(r"_([ph])(\d+)\.npz$")


def _bigger_cutouts(folder: Path, detector: int, coord: str, pixels: int) -> list[Path]:
    """Cached cutouts of this image at this position that contain everything a
    `pixels` frame needs: the footprint of a bigger frame ("_p<pixels>") or an
    old-style square ("_h<half>") at least as big. Smallest (fastest to load) first."""
    half = math.ceil(pixels / 2 * math.sqrt(2)) + 2
    found = []
    for path in folder.glob(f"D{detector}_{coord}_*.npz"):
        m = _CUTOUT_NAME_RE.search(path.name)
        if not m:
            continue
        kind, n = m[1], int(m[2])
        if kind == "p" and n > pixels:
            found.append((n, path))
        elif kind == "h" and n >= half:
            found.append(((n - 2) * math.sqrt(2), path))  # roughly the frame size it fits
    return [path for _, path in sorted(found)]


def read_cutout(exp: Exposure, ra: float, dec: float, pixels: int, collection: str) -> Cutout | None:
    """The part of one image a `pixels` frame needs (see frame_footprint), cached.

    None if the frame is off this detector; raises DownloadError if unreachable.
    Cached as Cache/cutouts/<collection>/<obs_id>/D<n>_<coord>_p<pixels>.npz.
    A cutout cached for a bigger frame at the same position (or an old-style
    square, "_h<half>") holds everything a smaller frame needs, so it is cut
    down instead of downloading again - changing the frame size to something
    smaller downloads nothing.
    """
    folder = CACHE_DIR / "cutouts" / collection / exp.obs_id
    coord = _coord_key(ra, dec)
    path = folder / f"D{exp.detector}_{coord}_p{pixels}.npz"
    if path.exists():
        _stats.count("cutout_cached")
        return _load_cutout_file(path, exp)

    for bigger in _bigger_cutouts(folder, exp.detector, coord, pixels):
        try:
            big = _load_cutout_file(bigger, exp)
        except Exception as exc:  # damaged file: try the next one / download
            log.warning("cached cutout %s not readable: %s", bigger.name, exc)
            continue
        if big is None:
            cut = None  # off the detector for the bigger frame -> for this one too
        else:
            fp = frame_footprint(fits.Header.fromstring(big.header), frame_ring(ra, dec, pixels))
            # (sky level measured again on just this footprint: same as downloading it)
            cut = None if fp is None else Cutout(
                exp, _subtract_sky_level(_cut_to_footprint(big, fp)), fp.x0, fp.y0,
                big.header, big.flags_near_target)
        _stats.count("cutout_cut_from_bigger")
        break
    else:
        t0 = time.monotonic()
        cut = _download_cutout(exp, ra, dec, pixels, collection)
        if cut is not None:
            _stats.count("cutout_downloaded")
            _stats.image(time.monotonic() - t0)

    _save_cutout_file(path, cut)
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


# Black point of the frames: this many noise sigmas below the typical sky level.
# (It used to be the faintest 0.5% of all pixels. With noisier frames - one per
# visit - that is far below the sky, which then came out dark brown: R,G,B ~(32,17,3)
# instead of ~(7,4,1); the red channel is the noisiest. This gives ~(8,4,1).)
SKY_BLACK_SIGMAS = 1.0


def _shared_stretch(images: list[np.ndarray | None]) -> ImageNormalize:
    """One brightness scale for all frames, or blinking between them is useless."""
    finite = [img[np.isfinite(img)] for img in images if img is not None]
    values = np.concatenate(finite) if finite else np.array([])
    if values.size == 0:
        values = np.array([0.0, 1.0])
    sky = np.median(values)
    noise = 1.4826 * np.median(np.abs(values - sky))
    vmax = np.percentile(values, 99.8)
    vmin = sky - SKY_BLACK_SIGMAS * noise if 0 < noise and sky < vmax else np.percentile(values, 0.5)
    return ImageNormalize(vmin=vmin, vmax=vmax, stretch=AsinhStretch(0.1), clip=True)


def _save_jpg(img01: np.ndarray, path: Path, label: str | None = None) -> None:
    """(H, W) grey or (H, W, 3) colour values 0..1 -> JPG (FITS row 0 is the bottom).

    label: written into a black strip added below the image (see _add_label_strip),
    so it covers nothing of the image.
    """
    arr = (np.clip(img01, 0, 1) * 255).astype(np.uint8)
    img = Image.fromarray(np.ascontiguousarray(np.flipud(arr)))
    if label:
        img = _add_label_strip(img, label)
    img.save(path, quality=92)


def _add_label_strip(img: Image.Image, label: str) -> Image.Image:
    """The image with a black strip below it holding the label in white.

    The image itself is unchanged (same pixels, same x/y from the top left);
    the result is just taller. Text sized to the frame width; if it doesn't
    fit, only its first word (the date); if even that doesn't fit, no strip.
    """
    w, h = img.size
    font = ImageFont.load_default(size=max(8, min(20, round(w / 20))))
    pad = max(2, round(w / 85))  # 3 px around the text in a 255 px frame
    measure = ImageDraw.Draw(img)
    for text in (label, label.split()[0]):
        left, top, right, bottom = measure.textbbox((0, 0), text, font=font)
        if right - left + 2 * pad <= w:
            break
    else:
        return img
    black, white = (0, 255) if img.mode == "L" else ((0, 0, 0), (255, 255, 255))
    out = Image.new(img.mode, (w, h + (bottom - top) + 2 * pad), black)
    out.paste(img, (0, 0))
    draw = ImageDraw.Draw(out)
    # No anti-aliasing: the soft grey letter edges were mapped to the nearest
    # colours of the GIF's 256-colour palette (made from the orange-brown sky)
    # by the middleware, which tinted the text orange. Pure white stays white.
    draw.fontmode = "1"
    draw.text((pad - left, h + pad - top), text, font=font, fill=white)
    return out


def frame_label(meta: dict) -> str:
    """Timestamp shown on a frame: when its images were taken (UTC)."""
    if FRAME_MODE == "visits":  # a visit takes minutes: one moment
        return meta["timestamp"][:16].replace("T", " ") + " UTC"
    return f"{meta['date_start']} to {meta['date_end']}"  # a frame spans ~2 weeks


def render_frames(frames: list[dict], out_dir: Path, labels: list[str] | None = None) -> list[str]:
    """Write frame_000.jpg ... (the frame size, no upscaling). Returns file names.

    labels: per frame, its timestamp (see frame_label), in a black strip below
    the image - the JPGs are that much taller than the frame.

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
        _save_jpg(img01, out_dir / name, labels[i] if labels else None)
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


def _brightness_ratio(img: np.ndarray, reference: np.ndarray) -> float:
    """How bright the stars are in img compared with reference (median of the ratio).

    Each visit sees a star at a somewhat different wavelength (where on the
    detector it lands), so all stars of a frame can come out e.g. 20% fainter
    than the reference - and leave a black dot each in the difference image.
    Measured on the pixels where the reference clearly has a star; something
    only in img (an asteroid) sits on empty sky there and doesn't count.
    """
    ok = np.isfinite(img) & np.isfinite(reference)
    if ok.sum() < 100:
        return 1.0
    r = reference[ok]
    level, spread = np.median(r), 1.4826 * np.median(np.abs(r - np.median(r)))
    star = r > level + 10 * spread
    if star.sum() < 20 or spread <= 0:
        return 1.0
    ratio = float(np.median(img[ok][star] / r[star]))
    return ratio if 0.2 < ratio < 5 else 1.0


def render_differences(frames: list[dict], out_dir: Path, labels: list[str] | None = None,
                       groups: list | None = None) -> list[str]:
    """Write diff/diff_000.jpg ...: each frame minus a reference image.

    Like Backyard Worlds' difference images: stars that don't change cancel to
    grey; something that is only in this frame (a moving object) is white; a
    variable star is white or black.

    The reference is the median of the frames in the same group (groups: per
    frame, e.g. its detectors). Each SPHEREx visit uses one detector pair
    (D1+D4, D2+D5 or D3+D6), and a star looks different in each (other
    wavelengths): compared with frames of another pair, nearly every star left a
    black or white dot. Median, so an asteroid in another frame leaves no black
    dot here (needs 3 frames in the group; with 2 the other frame is the
    reference, alone in its group: the median of all frames). The reference is
    scaled to the frame's star brightness first (see _brightness_ratio).
    """
    n = len(frames)
    if n < 2:
        return []
    smoothed = [_smooth(f["all"], DIFF_SMOOTH_PX) for f in frames]
    groups = groups if groups is not None else [None] * n
    members: dict = {}
    for i, g in enumerate(groups):
        members.setdefault(g, []).append(i)
    medians = {g: detectors.robust_median_stack([smoothed[i] for i in idx])
               for g, idx in members.items() if len(idx) >= 3}
    everything = (detectors.robust_median_stack(smoothed)
                  if any(len(idx) == 1 for idx in members.values()) else None)
    diffs = []
    for i, s in enumerate(smoothed):
        idx = members[groups[i]]
        if len(idx) >= 3:
            reference = medians[groups[i]]
        elif len(idx) == 2:
            reference = smoothed[idx[1] if idx[0] == i else idx[0]]
        else:
            reference = everything
        diffs.append(s - _brightness_ratio(s, reference) * reference)
    spread = np.concatenate([np.abs(d[np.isfinite(d)]) for d in diffs])
    limit = float(np.percentile(spread, 99.5)) if spread.size else 1.0
    limit = limit if limit > 0 else 1.0

    (out_dir / "diff").mkdir(exist_ok=True)
    names = []
    for i, d in enumerate(diffs):
        img01 = np.nan_to_num(0.5 + d / (2 * limit), nan=0.5)  # grey = no change
        name = f"diff/diff_{i:03d}.jpg"
        _save_jpg(img01, out_dir / name, labels[i] if labels else None)
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
def _download_one(exp: Exposure, ra: float, dec: float, pixels: int, collection: str) -> tuple:
    """Runs in a download thread: make sure the image's part is in the cache.

    -> ("ok",) | ("off_chip",) | ("failed", reason)
    """
    try:
        cut = read_cutout(exp, ra, dec, pixels, collection)
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

    downloads.submit(_download_one, exp, *job).add_done_callback(after_download)
    return result


_JOB: dict = {}  # per worker process: what every task of this job needs


def _init_worker(ra: float, dec: float, pixels: int, collection: str) -> None:
    """Runs once when each worker process starts.

    Rebuilding the alignment grid here is cheaper than sending it with every task.
    """
    _JOB.update(ra=ra, dec=dec, pixels=pixels, collection=collection,
                align=make_alignment_grid(make_grid_wcs(ra, dec, pixels), pixels))


def _coverage_task(images: list[Exposure]) -> list[float | None]:
    """Runs in a worker process: coverage_of's number crunching for a few images
    (their headers are in the cache by then). None = header not readable."""
    j = _JOB
    if "test_points" not in j:
        j["test_points"] = frame_test_points(j["ra"], j["dec"], j["pixels"])
    out = []
    for e in images:
        header = image_header(e, j["collection"], fetch=False)
        out.append(None if header is None else frame_coverage(header[0], *j["test_points"]))
    return out


def _load_one(exp: Exposure) -> tuple:
    """Runs in a worker process: load the image's part (from the cache), remove its
    sky background and line it up.

    -> ("ok", exposure, lined-up image, flags, noise) | ("off_chip",) | ("failed", reason)
    Only the lined-up PIXELS x PIXELS image is sent back, so memory stays small.
    """
    j = _JOB
    try:
        cut = read_cutout(exp, j["ra"], j["dec"], j["pixels"], j["collection"])
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
    """Write frame_*.jpg into out_dir and return the metadata (without status).

    Logs a summary of where the time went at the end (see JobStats).
    """
    t_start = time.monotonic()
    use_detectors = detectors.parse_detectors(wanted_detectors)  # None -> all six
    collection = latest_collection()
    exposures = detectors.select(find_exposures(ra, dec, collection), use_detectors)
    _stats.phases["search"] = time.monotonic() - t_start
    if not exposures:
        raise BackendError(EXIT_NO_DATA, f"no {collection} images cover RA={ra:.5f} Dec={dec:+.5f}")

    passes = group_into_passes(exposures)
    job = (ra, dec, pixels, collection)
    frames, frame_meta, n_failed = [], [], 0
    # The worker processes start right away: they first help with coverage_of,
    # and are up and running by the time the first images are downloaded.
    with ThreadPoolExecutor(DOWNLOAD_THREADS) as downloads, \
            ProcessPoolExecutor(WORKER_PROCESSES, initializer=_init_worker, initargs=job) as aligners:
        t_coverage = time.monotonic()
        coverage = coverage_of(exposures, ra, dec, pixels, collection, downloads, aligners)
        t_images = time.monotonic()
        _stats.phases["coverage"] = t_images - t_coverage
        n_full = sum(1 for c in coverage.values() if c is not None and c >= FULL_COVERAGE)
        log.info("%d of %d images cover the whole frame", n_full, len(exposures))
        if FRAME_MODE == "visits":
            time_bins, bins_skipped = plan_visit_frames(exposures, coverage)
        else:
            time_bins = plan_frames(passes, coverage=coverage)
            # passes skipped: too few images per detector to remove glitches
            bins_skipped = sum(1 for p in passes if not detectors.pick(p))
            if not time_bins:  # nothing has enough images: better a glitchy video than none
                time_bins = [b for b in (detectors.pick(p, min_per_detector=1) for p in passes) if b]
                bins_skipped = 0

        # How many images may be queued ahead (their lined-up results wait in RAM).
        max_queued = max(2 * DOWNLOAD_THREADS, QUEUE_AHEAD_BYTES // (pixels * pixels * 4))
        log.info("%d images available, using %d in %d frames (one per %s; %d %s not used), "
                 "%dx%d px frames, %d download threads, %d processes",
                 len(exposures), sum(map(len, time_bins)), len(time_bins),
                 "visit" if FRAME_MODE == "visits" else "part of a pass", bins_skipped,
                 "visits" if FRAME_MODE == "visits" else "passes",
                 pixels, pixels, DOWNLOAD_THREADS, WORKER_PROCESSES)
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
            frame = detectors.combine(by_detector, noise_by_detector,
                                      "channel" if FRAME_MODE == "visits" else "detector")
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

    _stats.phases["images"] = time.monotonic() - t_images
    _stats.phases["total"] = time.monotonic() - t_start
    _stats.log(sum(len(b) for b in time_bins), n_failed)

    if not frames:
        if n_failed:
            raise BackendError(EXIT_DOWNLOAD_FAILED,
                               f"{n_failed} images found but none could be downloaded - try again later")
        raise BackendError(EXIT_NO_DATA, f"no {collection} images cover RA={ra:.5f} Dec={dec:+.5f}")

    labels = [frame_label(m) for m in frame_meta]
    names = render_frames(frames, out_dir, labels)
    diff_names = render_differences(frames, out_dir, labels, [tuple(m["detectors"]) for m in frame_meta])
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
# Finished results: an identical request is copied instead of built again
# --------------------------------------------------------------------------- #
# The same position + frame size always gives the same frames - as long as the
# image list and the code are the same. So every complete result is kept in
# Cache/results/ together with a fingerprint of both (backend.py + detectors.py
# + the image list): a new image list or ANY code change makes old results count
# as missing, so they are never stale. Results with failed images aren't kept.
RESULTS_DIR = CACHE_DIR / "results"
RESULT_META = "result.json"


def _result_dir(collection: str, ra: float, dec: float, pixels: int) -> Path:
    return RESULTS_DIR / collection / f"{_coord_key(ra, dec)}_p{pixels}"


def _result_fingerprint(exposures: list[Exposure]) -> str:
    h = hashlib.sha256()
    for source in (Path(__file__), Path(detectors.__file__)):
        h.update(source.read_bytes())
    for e in sorted(exposures, key=lambda e: (e.obs_id, e.detector)):
        h.update(f"{e.obs_id}/{e.detector};".encode())
    return h.hexdigest()


def _result_files(meta: dict) -> list[str]:
    """The image files of a result, relative to its folder (e.g. "diff/diff_000.jpg")."""
    return list(meta["images"]) + list(meta.get("difference_images") or [])


def load_result(collection: str, ra: float, dec: float, pixels: int, fingerprint: str,
                out_dir: Path, user_id: str) -> dict | None:
    """Copy a stored result into out_dir -> its metadata; None if there is no valid one.

    The files are first copied to a staging folder outside Cache/usr/ and only
    then moved into out_dir, so a failed copy never leaves half a result there
    (and the backend still never deletes anything in Cache/usr/).
    """
    src = _result_dir(collection, ra, dec, pixels)
    staging = RESULTS_DIR / f".staging_{user_id}"
    try:
        stored = _read_json(src / RESULT_META)
        if not stored or stored.get("fingerprint") != fingerprint:
            return None
        meta = stored["meta"]
        files = _result_files(meta)
        shutil.rmtree(staging, ignore_errors=True)
        for name in files:
            (staging / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src / name, staging / name)
        for name in files:  # a rename (no copying) - both are inside Cache/
            (out_dir / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(staging / name, out_dir / name)
        return meta
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("stored result not usable (%s), building it again", exc)
        return None
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def save_result(meta: dict, out_dir: Path, collection: str, ra: float, dec: float,
                pixels: int, fingerprint: str) -> None:
    """Keep a finished result for identical requests. Atomic: built in a temp
    folder that is renamed into place, so readers never see half a result."""
    dest = _result_dir(collection, ra, dec, pixels)
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
    old = dest.with_name(f".{dest.name}.{os.getpid()}.old")
    try:
        shutil.rmtree(tmp, ignore_errors=True)
        for name in _result_files(meta):
            (tmp / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(out_dir / name, tmp / name)
        _write_json(tmp / RESULT_META, {"fingerprint": fingerprint, "meta": meta})
        if dest.exists():
            os.rename(dest, old)
        os.rename(tmp, dest)
    except OSError as exc:  # e.g. another job stored the same result at the same moment
        log.warning("could not store the result for reuse: %s", exc)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(old, ignore_errors=True)


def build_frames_cached(ra: float, dec: float, pixels: int, out_dir: Path, user_id: str) -> dict:
    """build_frames, but an identical earlier request is copied from Cache/results/."""
    collection = latest_collection()
    fingerprint = _result_fingerprint(find_exposures(ra, dec, collection))
    meta = load_result(collection, ra, dec, pixels, fingerprint, out_dir, user_id)
    if meta is not None:
        log.info("identical request done before: result copied from %s",
                 _result_dir(collection, ra, dec, pixels))
        return {**meta, "from_cache": True}
    meta = build_frames(ra, dec, pixels, out_dir)
    if meta["images_failed"] == 0:
        save_result(meta, out_dir, collection, ra, dec, pixels, fingerprint)
    else:
        log.info("result not kept for reuse: %d images failed", meta["images_failed"])
    return {**meta, "from_cache": False}


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
        meta = build_frames_cached(ra, dec, pixels, pending, user_id)
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
