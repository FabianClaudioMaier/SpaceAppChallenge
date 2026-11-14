"""
SPHEREx time-lapse backend - NASA Space Apps 2026 "Planet X and SPHEREx".

Called by the middleware:

    python backend.py <DEC> <RA> <PIXELS> <UUID>

    DEC, RA  target position in degrees
    PIXELS   width = height of the frames in SPHEREx pixels (6.15" each), 3..2040
    UUID     the user's id

What happens:
    1. Cache/usr/.pending_<UUID>/ is created
    2. the frames are built into it:
         frame_000.jpg, frame_001.jpg, ...   oldest first, PIXELS x PIXELS, grayscale
         meta.json                           status, timestamps, frame info
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
    find_exposures     IRSA query: every SPHEREx image that covers (ra, dec)
    group_into_frames  split the images into time bins (1 bin = 1 video frame)
    read_cutout        read only a small square of each image (public AWS copy)
    reproject_cutout   line every square up on one common grid
    detectors.combine  squash each bin into one picture
    render_frames      same brightness scale for every frame -> JPGs
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sys
import time
import uuid as uuidlib
import warnings
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

import astropy.units as u
import numpy as np
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.stats import sigma_clipped_stats
from astropy.time import Time
from astropy.visualization import AsinhStretch, ImageNormalize
from astropy.wcs import WCS, FITSFixedWarning
from astroquery.ipac.irsa import Irsa
from PIL import Image
from scipy.ndimage import map_coordinates

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
BIN_DAYS = 10.0                   # images closer together than this form one frame
DOWNLOAD_WORKERS = 16

# FLAGS extension bits treated as bad pixels. Bits 0-2 are confirmed
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


def parse_args(argv: list[str]) -> tuple[float, float, int, str]:
    """argv without the program name -> (dec, ra, pixels, uuid). Raises BackendError(1)."""
    if len(argv) != 4:
        raise BackendError(EXIT_BAD_ARGS, f"expected 4 arguments, got {len(argv)}. {USAGE}")
    dec_s, ra_s, pixels_s, uuid_s = argv

    try:
        dec, ra = float(dec_s), float(ra_s)
    except ValueError:
        raise BackendError(EXIT_BAD_ARGS, f"DEC and RA must be numbers, got {dec_s!r} {ra_s!r}")
    if not (math.isfinite(dec) and math.isfinite(ra)):
        raise BackendError(EXIT_BAD_ARGS, "DEC and RA must be finite numbers")
    if not -90.0 <= dec <= 90.0:
        raise BackendError(EXIT_BAD_ARGS, f"DEC must be in [-90, 90], got {dec}")

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
# 2. Time bins -> video frames
# --------------------------------------------------------------------------- #
def group_into_frames(exposures: list[Exposure], bin_days: float = BIN_DAYS) -> list[list[Exposure]]:
    """A new frame starts once an image is > bin_days after the frame's first one.

    SPHEREx sees a given spot for a few days-weeks every ~6 months, so you get
    ~2-6 frames per year of data.
    """
    frames: list[list[Exposure]] = []
    for e in sorted(exposures, key=lambda e: e.mjd):
        if frames and e.mjd - frames[-1][0].mjd <= bin_days:
            frames[-1].append(e)
        else:
            frames.append([e])
    return frames


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


def _read_cutout_remote(url: str, exp: Exposure, ra: float, dec: float, half: int) -> Cutout | None:
    with fits.open(url, use_fsspec=True,
                   fsspec_kwargs={"block_size": 1_000_000, "cache_type": "bytes"}) as h:
        hdr = h["IMAGE"].header
        x, y = (float(v) for v in WCS(hdr).world_to_pixel_values(ra, dec))
        nx, ny = hdr["NAXIS1"], hdr["NAXIS2"]
        xc, yc = round(x), round(y)
        x0, x1 = max(xc - half, 0), min(xc + half + 1, nx)
        y0, y1 = max(yc - half, 0), min(yc + half + 1, ny)
        if x1 - x0 < 3 or y1 - y0 < 3:
            return None  # the square doesn't overlap this detector

        data = np.array(h["IMAGE"].section[y0:y1, x0:x1], dtype=np.float32)
        flags = np.array(h["FLAGS"].section[y0:y1, x0:x1]).astype(np.int64) & 0xFFFFFFFF
    data[(flags & BAD_FLAG_MASK) != 0] = np.nan

    flags_near = 0
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

    errors = []
    for url in (exp.s3_url, exp.url):  # AWS first, IRSA as fallback
        try:
            cut = _read_cutout_remote(url, exp, ra, dec, half)
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


def reproject_cutout(cut: Cutout, align: AlignmentGrid) -> np.ndarray:
    """Resample one cutout onto the common output grid."""
    x, y = cut.wcs.world_to_pixel_values(align.coarse_ra, align.coarse_dec)  # exact, few points
    x_full = map_coordinates(x, align.fine_index, order=1)  # in between: interpolated
    y_full = map_coordinates(y, align.fine_index, order=1)
    return map_coordinates(cut.data, [y_full - cut.y0, x_full - cut.x0], order=1,
                           mode="constant", cval=np.nan)


# --------------------------------------------------------------------------- #
# 5. Rendering (same brightness scale for every frame, or blinking is useless)
# --------------------------------------------------------------------------- #
def render_frames(frames: list[np.ndarray], out_dir: Path) -> list[str]:
    """Write frame_000.jpg ... (exactly the frame size, no upscaling). Returns file names."""
    finite = np.concatenate([f[np.isfinite(f)] for f in frames])
    if finite.size == 0:
        finite = np.array([0.0, 1.0])
    vmin, vmax = np.percentile(finite, [0.5, 99.8])
    norm = ImageNormalize(vmin=vmin, vmax=vmax, stretch=AsinhStretch(0.1), clip=True)

    names = []
    for i, frame in enumerate(frames):
        img01 = np.nan_to_num(np.asarray(norm(frame), dtype=float), nan=0.0)
        arr = (np.clip(img01, 0, 1) * 255).astype(np.uint8)
        name = f"frame_{i:03d}.jpg"
        Image.fromarray(np.flipud(arr), mode="L").save(out_dir / name, quality=92)  # FITS row 0 = bottom
        names.append(name)
    return names


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
    grid = make_grid_wcs(ra, dec, pixels)
    align = make_alignment_grid(grid, pixels)
    time_bins = [detectors.pick(group) for group in group_into_frames(exposures)]

    def load(exp: Exposure):
        """Download (or load from cache) one square and line it up; runs in a worker thread.

        Only the lined-up PIXELS x PIXELS image is kept, so memory stays small.
        """
        try:
            cut = read_cutout(exp, ra, dec, half, collection)
        except DownloadError as exc:
            log.warning("skipping %s", exc)
            return "failed"
        if cut is None:
            return None
        return exp, reproject_cutout(cut, align), cut.flags_near_target

    log.info("%d images available, using %d in %d time bins, %dx%d px squares",
             len(exposures), sum(map(len, time_bins)), len(time_bins), 2 * half + 1, 2 * half + 1)
    frames, frame_meta, n_failed = [], [], 0
    with ThreadPoolExecutor(DOWNLOAD_WORKERS) as pool:
        for group in time_bins:  # one time bin at a time
            results = list(pool.map(load, group))
            n_failed += results.count("failed")
            loaded = [r for r in results if r is not None and r != "failed"]
            if not loaded:
                continue
            by_detector: dict[int, list[np.ndarray]] = {}
            flags = 0
            for exp, img, f in loaded:
                by_detector.setdefault(exp.detector, []).append(img)
                flags |= f
            frame = detectors.combine(by_detector)
            if not np.isfinite(frame).any():
                continue
            mjds = [exp.mjd for exp, _, _ in loaded]
            frames.append(frame)
            frame_meta.append({
                "timestamp": Time(float(np.mean(mjds)), format="mjd").isot,
                "mjd_mean": float(np.mean(mjds)),
                "date_start": Time(min(mjds), format="mjd").iso[:10],
                "date_end": Time(max(mjds), format="mjd").iso[:10],
                "n_images": len(loaded),
                "detectors": sorted(by_detector),
                "flag_bits_near_target": [b for b in range(32) if flags >> b & 1],
            })

    if not frames:
        if n_failed:
            raise BackendError(EXIT_DOWNLOAD_FAILED,
                               f"{n_failed} images found but none could be downloaded - try again later")
        raise BackendError(EXIT_NO_DATA, f"no {collection} images cover RA={ra:.5f} Dec={dec:+.5f}")

    names = render_frames(frames, out_dir)
    for meta, name in zip(frame_meta, names):
        meta["image"] = name

    return {
        "collection": collection,
        "pixel_scale_arcsec": PIXEL_SCALE_ARCSEC,
        "detectors": list(use_detectors),
        "images": names,
        "timestamps": [m["timestamp"] for m in frame_meta],
        "frames": frame_meta,
        "images_failed": n_failed,
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
