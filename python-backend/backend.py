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
         frame_000.jpg, frame_001.jpg, ...   oldest first, one per SPHEREx visit,
                                             PIXELS wide, PIXELS high + a timestamp strip,
                                             colour (blue = D1-D3 0.75-2.4 um, red = D4-D6 2.4-5 um)
         diff/diff_000.jpg, ...              each frame minus the median of the frames taken
                                             with the same detectors (grey = unchanged,
                                             white = only here, e.g. an asteroid) - a subfolder,
                                             so the middleware's *.jpg list doesn't pick it up
         meta.json                           status, timestamps, frame info, catalogue links,
                                             star overlays (Gaia DR3, CatWISE2020 - overlays.py)
    3. the folder is renamed to Cache/usr/<UUID>/ and the program exits
    The backend never deletes anything in Cache/usr/ - the middleware does that.
    On an error the folder is renamed as well, but without meta.json.

Exit codes (the description goes to stderr as "error: ..."):
    0  success
    1  invalid arguments
    2  no SPHEREx images cover this position
    3  images exist but none could be downloaded (archive/network trouble)
    4  anything else unexpected (e.g. out of memory for a huge PIXELS)
    5  Cache/usr/<UUID>/ or Cache/usr/.pending_<UUID>/ already exists

Pipeline:
    build_frames_cached  an identical earlier request is just copied (Cache/results/)
    find_all_exposures   IRSA query: every SPHEREx image that covers (ra, dec), in the
                         newest data release + all quick releases (the newest data)
    coverage_of          which images cover the whole frame (headers only)
    plan_visit_frames    one frame per SPHEREx visit
    read_cutout          download only the part of each image the frame needs
                         (frame_footprint; IRSA multi-range, AWS whole rows as backup)
    subtract_background  local sky model per image (removes gradients/bands) + noise
    reproject_cutout     line every image up on one common grid
    detectors.combine    kappa-sigma stack per colour channel, noise-weighted
    render_frames        same brightness scale for every frame -> colour JPGs + timestamp
    render_differences   frame minus reference -> movers stand out


    Super Intelligence (SI) was used in creating this file. It was used to help write docstrings and comments, and to suggest code structure. The code was written by a human developer, and the SI's suggestions were reviewed and edited by the developer as needed. The final code is the result of the developer's work, with the SI serving as a tool to assist in the development process.
"""

from __future__ import annotations

import contextlib
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
from collections.abc import Callable, Iterator, Mapping
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
import overlays

warnings.simplefilter("ignore", FITSFixedWarning)
log = logging.getLogger("spherex_backend")

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
BACKEND_DIR = Path(__file__).resolve().parent

# CACHE LAYOUT
#   Cache/
#     queries/collections.json                    which IRSA collections to search (24h TTL)
#     queries/<collection>/<coord>.json           image list for a position (24h TTL)
#     headers/<collection>/<obs_id>/D<n>.json      IMAGE header of one image (any position)
#     cutouts/<collection>/<obs_id>/D<n>_<coord>_p<pixels>.npz
#                                                 the part of one image a frame needs
#                                                 (_h<half>.npz: older squares, still reused)
<<<<<<< HEAD
#     catalogs/<catalog>/<coord>_p<pixels>.json  candidate stars for the overlays (overlays.py,
#                                                 never expires: the catalogues don't change)
=======
#     catalogs/<catalog>/<coord>_p<pixels>[_r<rows>].json  candidate stars for the overlays
#                                                 (overlays.py, never expires: the catalogues
#                                                 don't change; _r = Gaia/CatWISE row limit)
>>>>>>> origin/star-overlays
#     coverage/<coord>_p<pixels>.json
#                                                 how much of the frame each image covers
#     results/<collections>/<coord>_p<pixels>/    finished frames of an earlier identical
#                                                 request (+ result.json), see build_frames_cached
#     results/.staging_<uuid>/                    a stored result being copied out (temporary)
#     usr/.pending_<uuid>/                        job still running
#     usr/<uuid>/                                 finished job, picked up + deleted by the middleware
CACHE_DIR = BACKEND_DIR / "Cache"
USR_DIR = CACHE_DIR / "usr"
QUERY_TTL_S = 24 * 3600

# Which IRSA collections are searched: the newest data release ("spherex_drN":
# reprocessed, complete up to some date) plus all quick releases ("spherex_qrN":
# the newest data, published as it comes in). E.g. at Barnard's star DR1 ends on
# 2026-05-05 and QR3 has 2026-08-16 .. 09-22 - without QR3 a whole pass is missing.
# ("_deep" / "_cube" collections are other products and not used.)
DEFAULT_COLLECTIONS = ["spherex_dr1"]  # if IRSA can't be asked (and nothing is cached)
RELEASE_RE = re.compile(r"^spherex_(dr|qr)(\d+)$")

PIXEL_SCALE_ARCSEC = 6.15
MIN_PIXELS, MAX_PIXELS = 3, 2040  # 2040 = width of one SPHEREx detector

# One frame of the video = one SPHEREx visit: its ~6 images are taken within
# minutes, visits are ~11 h apart (passes over a spot ~6 months apart). Nothing
# moves noticeably during a visit, so moving things survive the glitch removal:
# asteroids (2-6 px/hour) jump between frames, TNOs (~14 px/day) crawl, a distant
# planet (~1 px/day at 500 AU) drifts, Barnard's star (1.7 px/year) creeps.
# (Frames stacked over ~2 weeks were cleaner, but removed or smeared anything
# moving more than ~1 px in that time - see git history for that mode.)
VISIT_GAP_H = 1.0           # images closer in time than this belong to one visit
MIN_IMAGES_PER_CHANNEL = 3  # a visit is used if it has this many blue AND red images
                            # covering the whole frame (median of 3 removes glitches)
MAX_VISIT_FRAMES = 30       # at most this many visits (evenly spread over time)
MAX_IMAGES_PER_VISIT = 12   # (visits near the ecliptic poles can have more)
MIN_FRAMES = 6              # fewer complete visits: the next-best ones are added

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

# The FLAGS extension (bad-pixel mask) is not read: it would double the download.
# Glitches (cosmic rays) are removed by the median of each visit's images instead.

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
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _write_json(path: Path, obj) -> None:
    _atomic_write_bytes(path, json.dumps(obj, indent=1).encode())


def _touch(path: Path) -> None:
    """Mark a cache file as just used (its mtime), so trim_cache deletes it last."""
    try:
        os.utime(path)
    except OSError:
        pass


def _read_json(path: Path, max_age_s: float | None = None):
    """Cached JSON, or None if missing, too old or damaged (then it is simply rebuilt)."""
    try:
        if max_age_s is not None and time.time() - path.stat().st_mtime > max_age_s:
            return None
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("ignoring damaged cache file %s (%s)", path, exc)
        return None


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

    return dec + 0.0, ra % 360.0 + 0.0, pixels, user_id  # (+ 0.0 turns -0.0 into 0.0: one cache key)


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
    collection: str  # IRSA collection it was found in (data release or quick release)


def collections() -> list[str]:
    """IRSA collections to search (see RELEASE_RE): the newest data release first,
    then the quick releases. Looked up once a day, so new releases are picked up."""
    path = CACHE_DIR / "queries" / "collections.json"
    cached = _read_json(path, QUERY_TTL_S)
    if cached and cached.get("use"):
        return cached["use"]
    try:
        names = [str(n) for n in Irsa.list_collections(servicetype="SIA")["collection"]]
        found = [(m[1], int(m[2]), n) for n in names if (m := RELEASE_RE.match(n))]
        newest_dr = max(((num, n) for kind, num, n in found if kind == "dr"), default=None)
        quick = [n for _, n in sorted((num, n) for kind, num, n in found if kind == "qr")]
        use = ([newest_dr[1]] if newest_dr else []) + quick or DEFAULT_COLLECTIONS
    except Exception as exc:  # network trouble: the last known list (even if old), don't cache
        use = (_read_json(path) or {}).get("use") or DEFAULT_COLLECTIONS
        log.warning("could not list IRSA collections (%s); using %s", exc, use)
        return use
    _write_json(path, {"use": use, "checked": Time.now().isot})
    return use


def find_all_exposures(ra: float, dec: float) -> tuple[list[Exposure], list[str]]:
    """Every image covering (ra, dec) in all collections() -> (images, collections).

    An image in several collections (a quick-release image later reprocessed
    into a data release) is kept once, from the first one: the data release.
    A collection whose search fails is skipped, as long as another one answered.
    The collections are searched at the same time (each search is a ~2-3 s wait
    for IRSA; one after the other, every quick release would add that).
    """
    names = collections()
    seen: set[tuple[str, int]] = set()
    found: list[Exposure] = []
    errors: list[BackendError] = []
    with ThreadPoolExecutor(len(names)) as pool:
        futures = [pool.submit(find_exposures, ra, dec, name) for name in names]
    for name, future in zip(names, futures):  # in order: the data release first
        try:
            exposures = future.result()
        except BackendError as exc:
            log.warning("searching %s failed, using the other collections: %s", name, exc)
            errors.append(exc)
            continue
        for e in exposures:
            if (e.obs_id, e.detector) not in seen:
                seen.add((e.obs_id, e.detector))
                found.append(e)
    if errors and len(errors) == len(names):
        raise errors[0]
    found.sort(key=lambda e: (e.mjd, e.detector))
    return found, names


def find_exposures(ra: float, dec: float, collection: str) -> list[Exposure]:
    path = CACHE_DIR / "queries" / collection / f"{_coord_key(ra, dec)}.json"
    cached = _read_json(path, QUERY_TTL_S)
    if cached is not None:
        return [Exposure(**{**e, "collection": collection}) for e in cached]

    try:
        t = Irsa.query_sia(pos=(SkyCoord(ra * u.deg, dec * u.deg), 1 * u.arcsec),
                           collection=collection, maxrec=20000)
    except Exception as exc:
        # The AWS bucket can't be searched (no position index, only the files), so
        # the fallback is the last list IRSA gave for this spot, even if older than
        # QUERY_TTL_S - only images newer than that are missing. The files themselves
        # then come from AWS as usual (see image_header, _read_whole_rows).
        stale = _read_json(path)
        if stale is not None:
            log.warning("IRSA image search for %s failed (%s); using the last list IRSA gave",
                        collection, exc)
            return [Exposure(**{**e, "collection": collection}) for e in stale]
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
            collection=collection,
        ))
    exposures.sort(key=lambda e: (e.mjd, e.detector))
    _write_json(path, [asdict(e) for e in exposures])
    return exposures


# --------------------------------------------------------------------------- #
# 2. Which images go into which frame
# --------------------------------------------------------------------------- #
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


def coverage_of(exposures: list[Exposure], ra: float, dec: float, pixels: int,
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
    path = CACHE_DIR / "coverage" / f"{_coord_key(ra, dec)}_p{pixels}.json"
    known: dict[tuple[str, int], float] = {(o, d): c for o, d, c in (_read_json(path) or [])}
    _touch(path)
    todo = [e for e in exposures if (e.obs_id, e.detector) not in known]
    if todo:
        def fetch_header(e: Exposure) -> bool:
            try:
                image_header(e)
                return True
            except Exception as exc:
                log.warning("no header for %s D%d: %s", e.obs_id, e.detector, exc)
                return False

        def submit(images: list[Exposure]) -> list[tuple[list[Exposure], Future]]:
            n = COVERAGE_TASK_IMAGES
            return [(images[i:i + n], processes.submit(_coverage_task, images[i:i + n]))
                    for i in range(0, len(images), n)]

        cached = [e for e in todo if _header_path(e).exists()]
        missing = [e for e in todo if not _header_path(e).exists()]
        tasks = submit(cached)  # computed while the missing headers download
        tasks += submit([e for e, ok in zip(missing, threads.map(fetch_header, missing)) if ok])
        new = {(e.obs_id, e.detector): c for images, f in tasks
               for e, c in zip(images, f.result()) if c is not None}
        if new:
            known.update(new)
            _write_json(path, [[o, d, c] for (o, d), c in sorted(known.items())])
    return {(e.obs_id, e.detector): known.get((e.obs_id, e.detector)) for e in exposures}


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
    """One frame per SPHEREx visit -> (frames, number of visits not used).

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
    frames = [detectors.pick(v, MAX_IMAGES_PER_VISIT) for v in chosen]
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

    @property
    def wcs(self) -> WCS:
        return WCS(fits.Header.fromstring(self.header))


class DownloadError(Exception):
    pass


# ---- exact byte-range downloads -------------------------------------------
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

# Idle requests.Sessions (each keeps its connections open), shared by all threads.
# Not one per thread: most requests run in short-lived background threads (see
# _in_background), and a new session each time meant a new connection + TLS
# handshake for every image and header.
_idle_sessions: list[requests.Session] = []
_sessions_lock = threading.Lock()
_speeds_lock = threading.Lock()
_recent_speeds: deque[float] = deque(maxlen=50)  # bytes/s of recently finished big requests


@contextlib.contextmanager
def _session() -> Iterator[requests.Session]:
    """A session for one request, taken from the shared pool and put back afterwards."""
    with _sessions_lock:
        session = _idle_sessions.pop() if _idle_sessions else requests.Session()
    try:
        yield session
    finally:
        with _sessions_lock:
            _idle_sessions.append(session)


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
              judge_speed: bool = True) -> tuple[bytes, Mapping[str, str]]:
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
            with _session() as session, session.get(
                    url, headers={"Range": range_spec}, stream=True,
                    timeout=(HTTP_CONNECT_TIMEOUT_S, HTTP_STALL_TIMEOUT_S)) as r:
                if r.status_code != 206:  # 206 = "here is the part you asked for"
                    raise DownloadError(f"HTTP {r.status_code} (expected 206)")
                headers = r.headers  # case-insensitive ("content-type" == "Content-Type")
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


def _split_byteranges(body: bytes, headers: Mapping[str, str]) -> list[tuple[int, bytes]]:
    """Body of a 206 answer -> [(first byte position, bytes), ...] of its parts."""
    headers = requests.structures.CaseInsensitiveDict(headers)
    ctype = headers.get("Content-Type", "")
    if not ctype.lower().startswith("multipart/byteranges"):
        # one part only (a single range, or the server merged them all)
        m = _CONTENT_RANGE_RE.match(b"content-range: " + headers.get("Content-Range", "").encode())
        if not m:
            raise DownloadError(f"206 answer without Content-Range ({ctype!r})")
        return [(int(m[1]), body)]
    if "boundary=" not in ctype:
        raise DownloadError(f"multipart answer without boundary ({ctype!r})")
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


def _header_path(exp: Exposure) -> Path:
    return CACHE_DIR / "headers" / exp.collection / exp.obs_id / f"D{exp.detector}.json"


def image_header(exp: Exposure, fetch: bool = True) -> tuple[fits.Header, int] | None:
    """IMAGE header of one exposure + its data offset, cached in Cache/headers/.

    A header doesn't depend on the position looked at, so once fetched it is
    reused for every request (and saves that request when downloading pixels).
    fetch=False: only look in the cache (None if not there).
    """
    path = _header_path(exp)
    cached = _read_json(path)
    if cached is not None:
        _touch(path)
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


def _subtract_sky_level(data: np.ndarray) -> np.ndarray:
    """Remove the zodiacal light / sky level of this image (one number)."""
    sample = data[::4, ::4] if data.size > 250_000 else data  # a sample is plenty
    good = sample[np.isfinite(sample)]
    median = sigma_clipped_stats(good)[1] if good.size else float("nan")
    if np.isfinite(median):
        data -= median
    return data


def _download_cutout(exp: Exposure, ra: float, dec: float, pixels: int) -> Cutout | None:
    """Download the part of one image the frame needs; None if the frame is off this detector."""
    ring = frame_ring(ra, dec, pixels)
    hdr, data_offset = image_header(exp)  # usually cached already (coverage_of)
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
    return Cutout(exp, _subtract_sky_level(data), fp.x0, fp.y0, hdr.tostring())


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
    return Cutout(exp, z["data"], meta["x0"], meta["y0"], meta["header"])


def _save_cutout_file(path: Path, cut: Cutout | None) -> None:
    if cut is None:
        meta, data = {"off_chip": True}, np.zeros((0, 0), np.float32)
    else:
        meta = {"x0": cut.x0, "y0": cut.y0, "header": cut.header}
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


def read_cutout(exp: Exposure, ra: float, dec: float, pixels: int) -> Cutout | None:
    """The part of one image a `pixels` frame needs (see frame_footprint), cached.

    None if the frame is off this detector; raises DownloadError if unreachable.
    Cached as Cache/cutouts/<collection>/<obs_id>/D<n>_<coord>_p<pixels>.npz.
    A cutout cached for a bigger frame at the same position (or an old-style
    square, "_h<half>") holds everything a smaller frame needs, so it is cut
    down instead of downloading again - changing the frame size to something
    smaller downloads nothing.
    """
    folder = CACHE_DIR / "cutouts" / exp.collection / exp.obs_id
    coord = _coord_key(ra, dec)
    path = folder / f"D{exp.detector}_{coord}_p{pixels}.npz"
    if path.exists():
        try:
            cut = _load_cutout_file(path, exp)
            _touch(path)
            _stats.count("cutout_cached")
            return cut
        except Exception as exc:  # damaged file: rebuilt below (and overwritten)
            log.warning("cached cutout %s not readable (%s), fetching it again", path.name, exc)

    for bigger in _bigger_cutouts(folder, exp.detector, coord, pixels):
        try:
            big = _load_cutout_file(bigger, exp)
            _touch(bigger)
        except Exception as exc:  # damaged file: try the next one / download
            log.warning("cached cutout %s not readable: %s", bigger.name, exc)
            continue
        if big is None:
            cut = None  # off the detector for the bigger frame -> for this one too
        else:
            fp = frame_footprint(fits.Header.fromstring(big.header), frame_ring(ra, dec, pixels))
            # (sky level measured again on just this footprint: same as downloading it)
            cut = None if fp is None else Cutout(
                exp, _subtract_sky_level(_cut_to_footprint(big, fp)), fp.x0, fp.y0, big.header)
        _stats.count("cutout_cut_from_bigger")
        break
    else:
        t0 = time.monotonic()
        cut = _download_cutout(exp, ra, dec, pixels)
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
    """Timestamp shown on a frame: when its images were taken (a visit takes minutes), UTC."""
    return meta["timestamp"][:16].replace("T", " ") + " UTC"


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
def _download_one(exp: Exposure, ra: float, dec: float, pixels: int) -> tuple:
    """Runs in a download thread: make sure the image's part is in the cache.

    -> ("ok",) | ("off_chip",) | ("failed", reason)
    """
    try:
        cut = read_cutout(exp, ra, dec, pixels)
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


def _init_worker(ra: float, dec: float, pixels: int) -> None:
    """Runs once when each worker process starts.

    Rebuilding the alignment grid here is cheaper than sending it with every task.
    """
    _JOB.update(ra=ra, dec=dec, pixels=pixels,
                align=make_alignment_grid(make_grid_wcs(ra, dec, pixels), pixels))


def _coverage_task(images: list[Exposure]) -> list[float | None]:
    """Runs in a worker process: coverage_of's number crunching for a few images
    (their headers are in the cache by then). None = header not readable."""
    j = _JOB
    if "test_points" not in j:
        j["test_points"] = frame_test_points(j["ra"], j["dec"], j["pixels"])
    out = []
    for e in images:
        header = image_header(e, fetch=False)
        out.append(None if header is None else frame_coverage(header[0], *j["test_points"]))
    return out


def _load_one(exp: Exposure) -> tuple:
    """Runs in a worker process: load the image's part (from the cache), remove its
    sky background and line it up.

    -> ("ok", exposure, lined-up image, noise) | ("off_chip",) | ("failed", reason)
    Only the lined-up PIXELS x PIXELS image is sent back, so memory stays small.
    """
    j = _JOB
    try:
        cut = read_cutout(exp, j["ra"], j["dec"], j["pixels"])
    except DownloadError as exc:
        return ("failed", str(exc))
    if cut is None:
        return ("off_chip",)
    data, noise = subtract_background(cut.data)
    return ("ok", exp, reproject_cutout(cut, j["align"], data), noise)


# --------------------------------------------------------------------------- #
# The job: build all frames into out_dir
# --------------------------------------------------------------------------- #
def build_frames(ra: float, dec: float, pixels: int, out_dir: Path) -> tuple[dict, int, bool]:
    """Write frame_*.jpg into out_dir -> (metadata without status, number of failed
    images, whether the star overlays are complete).

    Logs a summary of where the time went at the end (see JobStats).
    """
    t_start = time.monotonic()
    # The star catalogues (overlays.py) are asked meanwhile - other servers than IRSA.
    catalogs = _in_background(lambda: overlays.fetch(ra, dec, pixels, CACHE_DIR))
    exposures, searched = find_all_exposures(ra, dec)
    collection = "+".join(searched)
    _stats.phases["search"] = time.monotonic() - t_start
    if not exposures:
        raise BackendError(EXIT_NO_DATA, f"no {collection} images cover RA={ra:.5f} Dec={dec:+.5f}")

    job = (ra, dec, pixels)
    frames, frame_meta, n_failed = [], [], 0
    # The worker processes start right away: they first help with coverage_of,
    # and are up and running by the time the first images are downloaded.
    with ThreadPoolExecutor(DOWNLOAD_THREADS) as downloads, \
            ProcessPoolExecutor(WORKER_PROCESSES, initializer=_init_worker, initargs=job) as aligners:
        t_coverage = time.monotonic()
        coverage = coverage_of(exposures, ra, dec, pixels, downloads, aligners)
        t_images = time.monotonic()
        _stats.phases["coverage"] = t_images - t_coverage
        n_full = sum(1 for c in coverage.values() if c is not None and c >= FULL_COVERAGE)
        log.info("%d of %d images cover the whole frame", n_full, len(exposures))
        time_bins, bins_skipped = plan_visit_frames(exposures, coverage)

        # How many images may be queued ahead (their lined-up results wait in RAM).
        max_queued = max(2 * DOWNLOAD_THREADS, QUEUE_AHEAD_BYTES // (pixels * pixels * 4))
        log.info("%d images available, using %d in %d frames (one per visit; %d visits not used), "
                 "%dx%d px frames, %d download threads, %d processes",
                 len(exposures), sum(map(len, time_bins)), len(time_bins), bins_skipped,
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
            for exp, img, noise in loaded:
                by_detector.setdefault(exp.detector, []).append(img)
                noise_by_detector.setdefault(exp.detector, []).append(noise)
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

    t_overlays = time.monotonic()
    try:
        found, overlays_complete = catalogs.result()
    except Exception as exc:  # a bug in overlays.py must not cost the frames
        log.warning("star overlays failed: %s", exc)
        found, overlays_complete = {}, False
    star_overlays = overlays.place(found, make_grid_wcs(ra, dec, pixels), pixels,
                                   [m["mjd_mean"] for m in frame_meta])
    log.info("star overlays: %s (waited %.1f s)", ", ".join(
        f"{n} {len(o['stars']) if o else 'failed'}" for n, o in star_overlays.items()),
        time.monotonic() - t_overlays)

    return {
        "collections": searched,
        "images_per_collection": {n: sum(e.collection == n for e in exposures) for n in searched},
        "pixel_scale_arcsec": PIXEL_SCALE_ARCSEC,
        "colour": ({"red": "D4-D6 (2.4-5.0 um)", "green": "average of red and blue",
                    "blue": "D1-D3 (0.75-2.4 um)"} if COLOR_FRAMES else None),
        "images": names,
        "timestamps": [m["timestamp"] for m in frame_meta],
        "difference_images": diff_names,
        "frames": frame_meta,
        "visits_not_used": bins_skipped,
        "links": catalogue_links(ra, dec, pixels),
        "overlays": star_overlays,
    }, n_failed, overlays_complete


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
    for source in (Path(__file__), Path(detectors.__file__), Path(overlays.__file__)):
        h.update(source.read_bytes())
    for e in sorted(exposures, key=lambda e: (e.obs_id, e.detector)):
        h.update(f"{e.collection}/{e.obs_id}/{e.detector};".encode())
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
        _touch(src / RESULT_META)
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
    exposures, names = find_all_exposures(ra, dec)
    collection = "+".join(names)  # e.g. "spherex_dr1+spherex_qr3"
    fingerprint = _result_fingerprint(exposures)
    meta = load_result(collection, ra, dec, pixels, fingerprint, out_dir, user_id)
    if meta is not None:
        log.info("identical request done before: result copied from %s",
                 _result_dir(collection, ra, dec, pixels))
        return meta
    meta, n_failed, overlays_complete = build_frames(ra, dec, pixels, out_dir)
    if n_failed:
        log.info("result not kept for reuse: %d images failed", n_failed)
    elif not overlays_complete:  # else the overlay would stay missing for this spot
        log.info("result not kept for reuse: star overlays incomplete")
    else:
        save_result(meta, out_dir, collection, ra, dec, pixels, fingerprint)
    return meta


# --------------------------------------------------------------------------- #
# Keeping the cache small
# --------------------------------------------------------------------------- #
# Without this the cache only grows (one full-size spot alone is ~3 GB of
# cutouts). After a job, at most once per CACHE_TRIM_EVERY_S: if Cache/ (without
# usr/) is over CACHE_MAX_BYTES, the entries used longest ago are deleted until it
# is under CACHE_TRIM_TO of that. "Used" = file mtime, refreshed when a cutout,
# header, coverage or stored result is read (_touch); an image list (queries/)
# counts from when it was fetched. A stored result (results/<..>/) goes as a whole.
# Leftovers of killed jobs (".<name>.<pid>.tmp" files, staging folders) older
# than STALE_TEMP_S are deleted too. Deleting is safe while other jobs run: a
# file that vanishes is simply fetched or built again.
CACHE_MAX_BYTES = 128 * 1024**3
CACHE_TRIM_TO = 0.8
CACHE_TRIM_EVERY_S = 3600
STALE_TEMP_S = 24 * 3600


def trim_cache() -> None:
    marker = CACHE_DIR / ".last_trim"
    now = time.time()
    try:
        if now - marker.stat().st_mtime < CACHE_TRIM_EVERY_S:
            return
    except FileNotFoundError:
        pass
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()

    entries: list[tuple[float, int, Path]] = []  # (last used, bytes, file or result folder)
    for top in CACHE_DIR.iterdir():
        if top.name == "usr" or not top.is_dir():
            continue
        for path in list(top.rglob("*")):  # listed first: deleting while walking is fragile
            try:
                st = path.lstat()
                if path.name.startswith("."):  # temp files / staging folders of other jobs
                    if now - st.st_mtime > STALE_TEMP_S:
                        shutil.rmtree(path) if path.is_dir() else path.unlink()
                    continue
                if top.name == "results" and (path / RESULT_META).exists():
                    files = [f for f in path.rglob("*") if f.is_file()]
                    entries.append(((path / RESULT_META).stat().st_mtime,
                                    sum(f.stat().st_size for f in files), path))
                elif path.is_file() and not (top.name == "results" and path.parent != top):
                    entries.append((st.st_mtime, st.st_size, path))
            except OSError:  # removed meanwhile by another job
                continue
    total = sum(size for _, size, _ in entries)
    if total <= CACHE_MAX_BYTES:
        return
    freed, goal = 0, total - CACHE_TRIM_TO * CACHE_MAX_BYTES
    for _, size, path in sorted(entries):
        if freed >= goal:
            break
        try:
            shutil.rmtree(path) if path.is_dir() else path.unlink()
            freed += size
        except OSError:
            continue
    log.info("cache was %.1f GB, deleted %.1f GB of entries used longest ago",
             total / 1024**3, freed / 1024**3)


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

    # meta.json is only written on success; on an error the message goes to stderr only.
    t0 = time.time()
    meta, error, exit_code = None, None, EXIT_OK
    try:
        meta = build_frames_cached(ra, dec, pixels, pending, user_id)
    except BackendError as exc:
        error, exit_code = str(exc), exc.code
    except MemoryError:
        error, exit_code = f"not enough memory for PIXELS={pixels}", EXIT_UNEXPECTED
    except Exception as exc:
        log.exception("unexpected error")
        error, exit_code = f"{type(exc).__name__}: {exc}", EXIT_UNEXPECTED
    if meta is not None:
        meta = {"status": "ok", "exit_code": EXIT_OK, **meta,
                "dec": dec, "ra": ra, "pixels": pixels, "uuid": user_id,
                "seconds": round(time.time() - t0, 1)}
        _write_json(pending / "meta.json", meta)

    if final.exists():  # appeared while we were working - don't overwrite it
        print(f"error: {final} appeared during the job; result left in {pending}", file=sys.stderr)
        return EXIT_FOLDER_EXISTS
    os.rename(pending, final)
    try:
        trim_cache()  # after handing over the result; at most once an hour
    except Exception as exc:  # never let housekeeping fail a finished job
        log.warning("cache cleanup failed: %s", exc)

    if error is not None:
        print(f"error: {error}", file=sys.stderr)
    else:
        log.info("%d frames in %.1fs -> %s", len(meta["images"]), meta["seconds"], final)
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
