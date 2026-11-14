"""
SPHEREx time-lapse backend - NASA Space Apps 2026 "Planet X and SPHEREx".

main.py = data side (SPHEREx/Gaia -> JPG frames + timestamps).
gif.py  = turns those frames into the video.

Pipeline behind get_timelapse(ra, dec):
  1. find_exposures      IRSA query: every SPHEREx image that covers (ra, dec)
  2. group_into_frames   split the exposures into time bins (1 bin = 1 video frame)
  3. read_cutout         read only a small window of each image (public AWS copy)
  4. coadd               align all cutouts on one grid, stack them per time bin
  5. render              shared brightness stretch -> frame_XXX.jpg + diff_XXX.jpg
  6. gaia_sources        Gaia DR3 stars in the field, moved to each frame's date

Everything is cached under Cache/ (see CACHE LAYOUT below), so a second request
for the same patch of sky is instant and works offline.

Try it:
    python main.py "Barnard's Star"
    python main.py 133.795 -7.245 --fov 6 --detectors 5 6
    python main.py "08h55m10.831s -7d14m42.53s"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import re
import time
import urllib.parse
import warnings
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

import astropy.units as u
import numpy as np
import requests
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.stats import sigma_clipped_stats
from astropy.time import Time
from astropy.visualization import AsinhStretch, ImageNormalize
from astropy.wcs import WCS, FITSFixedWarning
from astroquery.gaia import Gaia
from astroquery.ipac.irsa import Irsa
from PIL import Image
from scipy.ndimage import map_coordinates

warnings.simplefilter("ignore", FITSFixedWarning)
log = logging.getLogger("spherex_backend")

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
BACKEND_DIR = Path(__file__).resolve().parent

# CACHE LAYOUT
#   Cache/
#     queries/collections.json                         newest SPHEREx release (24h TTL)
#     queries/<collection>/<coord>.json                image list for a position (24h TTL)
#     queries/gaia/<coord>_r<radius>.json              Gaia DR3 stars in a field
#     cutouts/<collection>/<obs_id>/D<n>_<coord>_h<half>.npz
#                                                      raw pixel window of one exposure
#     timelapses/<collection>/<coord>_fov.._bin.._det.._<data-hash>/
#         frame_000.jpg ...  diff_000.jpg ...  meta.json  (+ preview.gif from gif.py)
# <data-hash> is computed from the exposure list, so when new SPHEREx data
# arrives a fresh folder is built automatically and old ones stay valid.
CACHE_DIR = BACKEND_DIR / "Cache"
QUERY_TTL_S = 24 * 3600

# Used if IRSA can't be asked for the newest "spherex_drN" collection.
DEFAULT_COLLECTION = "spherex_dr1"
COLLECTION_RE = re.compile(r"^spherex_dr(\d+)$")

PIXEL_SCALE_ARCSEC = 6.15
PIXEL_SR = ((PIXEL_SCALE_ARCSEC * u.arcsec) ** 2).to_value(u.sr)

# Named targets are moved by their proper motion to this date (Julian year).
# Matters for Barnard's Star (~10"/yr): its J2000 catalogue position is ~4.5'
# away from where SPHEREx sees it.
TARGET_EPOCH = 2026.0
GAIA_EPOCH = 2016.0  # reference epoch of Gaia DR3 positions

# FLAGS extension bits treated as bad pixels. Bits 0-2 are confirmed
# (TRANSIENT, OVERFLOW, SUR_ERROR). The full list is in Table 10 of the SPHEREx
# Explanatory Supplement - add hot/cold/persistence/outlier bits once verified.
# Bit 21 marks pixels on a *source*; never mask that one.
BAD_FLAG_BITS = (0, 1, 2)
BAD_FLAG_MASK = sum(1 << b for b in BAD_FLAG_BITS)

MAX_FOV_ARCMIN = 30.0  # bigger = slower (more pixels to fetch per exposure)
OUTPUT_SIZE_PX = 480   # rendered JPGs are upscaled (nearest-neighbour) to ~this


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
# 0. Input
# --------------------------------------------------------------------------- #
def validate_inputs(ra: float, dec: float, fov_arcmin: float) -> tuple[float, float, float]:
    ra, dec, fov = float(ra) % 360.0, float(dec), float(fov_arcmin)
    if not -90.0 <= dec <= 90.0:
        raise ValueError(f"dec must be in [-90, 90], got {dec}")
    if not 0.5 <= fov <= MAX_FOV_ARCMIN:
        raise ValueError(f"fov_arcmin must be in [0.5, {MAX_FOV_ARCMIN}], got {fov}")
    return ra, dec, fov


def resolve_target(text: str, epoch: float = TARGET_EPOCH) -> tuple[float, float]:
    """'133.79 -7.24', '08h55m10.8s -7d14m42s', '08:55:10.8 -07:14:42' or a name.

    Names go through CDS Sesame, and are moved by their proper motion to `epoch`.
    """
    s = text.strip()
    parts = s.replace(",", " ").split()
    if len(parts) == 2:
        try:
            return float(parts[0]), float(parts[1])
        except ValueError:
            pass
        try:
            c = SkyCoord(parts[0], parts[1], unit=(u.hourangle, u.deg))
            return c.ra.deg, c.dec.deg
        except ValueError:
            pass

    url = "https://cds.unistra.fr/cgi-bin/nph-sesame/-oxp/SNV?" + urllib.parse.quote(s)
    root = ET.fromstring(requests.get(url, timeout=30).content)

    def first(tag):
        el = next(root.iter(tag), None)
        return float(el.text) if el is not None and el.text else None

    ra, dec = first("jradeg"), first("jdedeg")
    if ra is None:
        raise ValueError(f"could not resolve '{text}'")
    pmra, pmdec = first("pmRA") or 0.0, first("pmDE") or 0.0  # mas/yr, pmRA = mu_a*cos(d)
    dt = epoch - 2000.0
    dec2 = dec + pmdec * dt / 3.6e6
    ra2 = ra + pmra * dt / 3.6e6 / math.cos(math.radians(dec2))
    log.info("resolved %r -> %.5f %+.5f (moved %.1f\" for proper motion)",
             text, ra2, dec2, math.hypot(pmra, pmdec) * dt / 1000)
    return ra2 % 360.0, dec2


# --------------------------------------------------------------------------- #
# 1. Which SPHEREx exposures cover this position?
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Exposure:
    obs_id: str
    detector: int  # 1..6 (D1-D3: 0.75-2.4 um, D4-D6: 2.4-5.0 um)
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

    t = Irsa.query_sia(pos=(SkyCoord(ra * u.deg, dec * u.deg), 1 * u.arcsec),
                       collection=collection, maxrec=20000)
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
def group_into_frames(exposures: list[Exposure], bin_days: float) -> list[list[Exposure]]:
    """A new frame starts once an exposure is > bin_days after the frame's first one.

    SPHEREx sees a given spot for a few days-weeks every ~6 months, so with the
    default 10 days you get ~2-6 frames per year of data.
    """
    frames: list[list[Exposure]] = []
    for e in sorted(exposures, key=lambda e: e.mjd):
        if frames and e.mjd - frames[-1][0].mjd <= bin_days:
            frames[-1].append(e)
        else:
            frames.append([e])
    return frames


# --------------------------------------------------------------------------- #
# 3. Read a small window of one exposure (only those bytes are downloaded)
# --------------------------------------------------------------------------- #
@dataclass
class Cutout:
    exposure: Exposure
    data: np.ndarray       # MJy/sr, background-subtracted, bad pixels = NaN
    x0: int                # position of data[0, 0] in the full 2040x2040 image
    y0: int
    header: str            # IMAGE header (for the WCS)
    wavelength_um: float   # SPHEREx wavelength *at the target pixel*
    bandwidth_um: float
    flags_near_target: int  # OR of FLAGS bits within 2 px of the target

    @property
    def wcs(self) -> WCS:
        return WCS(fits.Header.fromstring(self.header))


_SIP_KEY = re.compile(r"^(A|B|AP|BP)_(ORDER|\d+_\d+)$")


def _strip_sip(hdr: fits.Header) -> fits.Header:
    h = hdr.copy()
    for k in [k for k in h if _SIP_KEY.match(k)]:
        del h[k]
    return h


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
            return None  # target is off this detector

        data = np.array(h["IMAGE"].section[y0:y1, x0:x1], dtype=np.float32)
        flags = np.array(h["FLAGS"].section[y0:y1, x0:x1]).astype(np.int64) & 0xFFFFFFFF
        data[(flags & BAD_FLAG_MASK) != 0] = np.nan

        wl = bw = float("nan")
        flags_near = 0
        if 0 <= xc < nx and 0 <= yc < ny:
            lx, ly = xc - x0, yc - y0
            win = flags[max(ly - 2, 0):ly + 3, max(lx - 2, 0):lx + 3]
            flags_near = int(np.bitwise_or.reduce(win.ravel()))
            # Every SPHEREx pixel sees a different wavelength; the lookup table
            # lives in the WCS-WAVE extension (alternate WCS "W").
            wave = WCS(_strip_sip(hdr), fobj=h, key="W")
            wl, bw = (float(v) for v in wave.pixel_to_world_values(x, y))

    good = data[np.isfinite(data)]
    median = sigma_clipped_stats(good)[1] if good.size else float("nan")
    if np.isfinite(median):
        data -= median  # remove zodiacal light / sky level of this exposure
    return Cutout(exp, data, x0, y0, hdr.tostring(), wl, bw, flags_near)


def read_cutout(exp: Exposure, ra: float, dec: float, half: int, collection: str) -> Cutout | None:
    path = (CACHE_DIR / "cutouts" / collection / exp.obs_id
            / f"D{exp.detector}_{_coord_key(ra, dec)}_h{half}.npz")
    if path.exists():
        z = np.load(path)
        meta = json.loads(str(z["meta"]))
        if meta.get("off_chip"):
            return None
        return Cutout(exp, z["data"], meta["x0"], meta["y0"], meta["header"],
                      meta["wavelength_um"], meta["bandwidth_um"], meta["flags_near_target"])

    cut, last_exc = None, None
    for url in (exp.s3_url, exp.url):  # AWS first, IRSA as fallback
        try:
            cut = _read_cutout_remote(url, exp, ra, dec, half)
            break
        except Exception as exc:
            last_exc = exc
    else:
        log.warning("skipping %s D%d: %s", exp.obs_id, exp.detector, last_exc)
        return None  # not cached -> retried next time

    if cut is None:
        meta, data = {"off_chip": True}, np.zeros((0, 0), np.float32)
    else:
        meta = {"x0": cut.x0, "y0": cut.y0, "header": cut.header,
                "wavelength_um": cut.wavelength_um, "bandwidth_um": cut.bandwidth_um,
                "flags_near_target": cut.flags_near_target}
        data = cut.data
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.stem}.{os.getpid()}.tmp.npz")
    np.savez_compressed(tmp, data=data, meta=json.dumps(meta))
    os.replace(tmp, path)
    return cut


# --------------------------------------------------------------------------- #
# 4. Align + stack
# --------------------------------------------------------------------------- #
def make_grid_wcs(ra: float, dec: float, npix: int) -> WCS:
    """North up, east left, SPHEREx pixel scale, target in the middle."""
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crval = [ra, dec]
    w.wcs.crpix = [(npix + 1) / 2, (npix + 1) / 2]
    w.wcs.cdelt = [-PIXEL_SCALE_ARCSEC / 3600, PIXEL_SCALE_ARCSEC / 3600]
    return w


def reproject_cutout(cut: Cutout, grid_ra: np.ndarray, grid_dec: np.ndarray) -> np.ndarray:
    x, y = cut.wcs.world_to_pixel_values(grid_ra, grid_dec)
    return map_coordinates(cut.data, [y - cut.y0, x - cut.x0], order=1,
                           mode="constant", cval=np.nan)


def coadd(images_by_detector: dict[int, list[np.ndarray]]) -> np.ndarray:
    """Median per detector (rejects cosmic rays when >= 3 images), then average detectors."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN pixels are fine
        per_det = [np.nanmedian(imgs, axis=0) if len(imgs) >= 3 else np.nanmean(imgs, axis=0)
                   for imgs in images_by_detector.values()]
        return np.nanmean(per_det, axis=0)


def aperture_flux_mjy(cut: Cutout, ra: float, dec: float, radius_px: float = 2.0) -> float:
    """Rough flux at the target (no aperture correction) -> for the on-the-fly spectrum."""
    x, y = cut.wcs.world_to_pixel_values(ra, dec)
    yy, xx = np.mgrid[0:cut.data.shape[0], 0:cut.data.shape[1]]
    inside = (xx - (x - cut.x0)) ** 2 + (yy - (y - cut.y0)) ** 2 <= radius_px ** 2
    vals = cut.data[inside]
    if vals.size == 0 or not np.all(np.isfinite(vals)):
        return float("nan")
    return float(vals.sum() * PIXEL_SR * 1e9)  # MJy/sr * sr = MJy -> mJy


# --------------------------------------------------------------------------- #
# 5. Rendering (same brightness scale for every frame, or blinking is useless)
# --------------------------------------------------------------------------- #
def _to_jpg(img01: np.ndarray, path: Path, scale: int) -> None:
    arr = (np.clip(np.nan_to_num(img01, nan=0.0), 0, 1) * 255).astype(np.uint8)
    im = Image.fromarray(np.flipud(arr), mode="L")  # FITS row 0 is the bottom
    im = im.resize((im.width * scale, im.height * scale), Image.NEAREST)
    im.save(path, quality=92)


def render_frames(frames: list[np.ndarray], out_dir: Path, scale: int) -> tuple[list[str], list[str]]:
    finite = np.concatenate([f[np.isfinite(f)] for f in frames]) if frames else np.array([])
    if finite.size == 0:
        finite = np.array([0.0, 1.0])
    vmin, vmax = np.percentile(finite, [0.5, 99.8])
    norm = ImageNormalize(vmin=vmin, vmax=vmax, stretch=AsinhStretch(0.1), clip=True)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        reference = np.nanmedian(frames, axis=0)
    diffs = [f - reference for f in frames]  # mover = bright now, dark where it was/will be
    dfin = np.concatenate([np.abs(d[np.isfinite(d)]) for d in diffs])
    dlim = float(np.percentile(dfin, 99.5)) if dfin.size else 1.0
    dlim = dlim or 1.0

    frame_paths, diff_paths = [], []
    for i, (f, d) in enumerate(zip(frames, diffs)):
        fp, dp = out_dir / f"frame_{i:03d}.jpg", out_dir / f"diff_{i:03d}.jpg"
        _to_jpg(np.asarray(norm(f)), fp, scale)
        _to_jpg((d + dlim) / (2 * dlim), dp, scale)
        frame_paths.append(str(fp))
        diff_paths.append(str(dp))
    return frame_paths, diff_paths


# --------------------------------------------------------------------------- #
# 6. Gaia overlay
# --------------------------------------------------------------------------- #
def gaia_sources(ra: float, dec: float, radius_deg: float, g_max: float = 19.0,
                 limit: int = 300) -> list[dict]:
    path = CACHE_DIR / "queries" / "gaia" / f"{_coord_key(ra, dec)}_r{radius_deg:.4f}.json"
    cached = _read_json(path)
    if cached is not None:
        return cached
    query = f"""
        SELECT TOP {int(limit)} source_id, ra, dec, pmra, pmdec, parallax, phot_g_mean_mag
        FROM gaiadr3.gaia_source
        WHERE 1 = CONTAINS(POINT(ra, dec), CIRCLE({ra:.8f}, {dec:.8f}, {radius_deg:.6f}))
          AND phot_g_mean_mag < {float(g_max)}
        ORDER BY phot_g_mean_mag"""
    t = Gaia.launch_job_async(query).get_results()
    t.rename_columns(t.colnames, [c.lower() for c in t.colnames])

    def col(name):
        return np.ma.filled(np.ma.asarray(t[name], dtype=float), np.nan)

    pmra, pmdec, plx = (np.nan_to_num(col(c)) for c in ("pmra", "pmdec", "parallax"))
    out = [{"source_id": int(sid), "ra": float(r), "dec": float(d), "pmra": float(pa),
            "pmdec": float(pd), "parallax": float(p), "g_mag": float(g)}
           for sid, r, d, pa, pd, p, g in zip(t["source_id"], col("ra"), col("dec"),
                                              pmra, pmdec, plx, col("phot_g_mean_mag"))]
    _write_json(path, out)
    return out


def gaia_overlay(stars: list[dict], grid: WCS, npix: int, scale: int,
                 mjds: list[float]) -> list[dict]:
    """Each star's position in JPG pixel coordinates for every frame (proper motion applied)."""
    out = []
    for s in stars:
        xy = []
        for mjd in mjds:
            dt = Time(mjd, format="mjd").jyear - GAIA_EPOCH
            d = s["dec"] + s["pmdec"] * dt / 3.6e6
            r = s["ra"] + s["pmra"] * dt / 3.6e6 / math.cos(math.radians(d))
            x, y = grid.world_to_pixel_values(r, d)
            xy.append([round((float(x) + 0.5) * scale, 1),
                       round((npix - 1 - float(y) + 0.5) * scale, 1)])
        out.append({**s, "xy_per_frame": xy})
    return out


# --------------------------------------------------------------------------- #
# Public entry point for the middleware
# --------------------------------------------------------------------------- #
def get_timelapse(ra: float, dec: float, fov_arcmin: float = 4.0, bin_days: float = 10.0,
                  detectors: tuple[int, ...] = (1, 2, 3, 4, 5, 6), collection: str | None = None,
                  with_gaia: bool = True, max_workers: int = 16) -> dict:
    """Build (or load from Cache/) the SPHEREx time-lapse for one patch of sky.

    Returns a JSON-safe dict. The middleware mainly needs:
        result["images"]      list of frame JPG paths (oldest first)
        result["timestamps"]  matching UTC ISO timestamps
    """
    ra, dec, fov_arcmin = validate_inputs(ra, dec, fov_arcmin)
    detectors = tuple(sorted({int(d) for d in detectors} & {1, 2, 3, 4, 5, 6})) or (1, 2, 3, 4, 5, 6)
    collection = collection or latest_collection()

    exposures = [e for e in find_exposures(ra, dec, collection) if e.detector in detectors]
    if not exposures:
        raise LookupError(f"no {collection} images cover RA={ra:.4f} Dec={dec:+.4f}")

    data_hash = hashlib.sha1("|".join(f"{e.obs_id}D{e.detector}" for e in exposures)
                            .encode()).hexdigest()[:10]
    det_key = "".join(map(str, detectors))
    out_dir = (CACHE_DIR / "timelapses" / collection /
               f"{_coord_key(ra, dec)}_fov{fov_arcmin:g}_bin{bin_days:g}_det{det_key}_{data_hash}")
    cached = _read_json(out_dir / "meta.json")
    if cached is not None and (cached["gaia"] is not None or not with_gaia):
        log.info("cache hit: %s", out_dir)
        return cached

    npix = max(5, math.ceil(fov_arcmin * 60 / PIXEL_SCALE_ARCSEC)) | 1  # odd -> target centred
    half = math.ceil(npix / 2 * math.sqrt(2)) + 2  # cover the grid at any rotation
    scale = max(1, OUTPUT_SIZE_PX // npix)

    log.info("%d exposures, reading %dx%d px cutouts ...", len(exposures), 2 * half + 1, 2 * half + 1)
    with ThreadPoolExecutor(max_workers) as pool:
        cutouts = list(pool.map(lambda e: read_cutout(e, ra, dec, half, collection), exposures))
    by_exposure = {(c.exposure.obs_id, c.exposure.detector): c for c in cutouts if c is not None}

    grid = make_grid_wcs(ra, dec, npix)
    gx, gy = np.meshgrid(np.arange(npix), np.arange(npix))
    grid_ra, grid_dec = grid.pixel_to_world_values(gx, gy)

    frames, frame_meta = [], []
    for group in group_into_frames(exposures, bin_days):
        cuts = [by_exposure[k] for e in group if (k := (e.obs_id, e.detector)) in by_exposure]
        if not cuts:
            continue
        by_det: dict[int, list[np.ndarray]] = {}
        for c in cuts:
            by_det.setdefault(c.exposure.detector, []).append(reproject_cutout(c, grid_ra, grid_dec))
        img = coadd(by_det)
        if not np.isfinite(img).any():
            continue
        mjds = [c.exposure.mjd for c in cuts]
        flags = 0
        for c in cuts:
            flags |= c.flags_near_target
        frames.append(img)
        frame_meta.append({
            "timestamp": Time(float(np.mean(mjds)), format="mjd").isot,
            "mjd_mean": float(np.mean(mjds)),
            "date_start": Time(min(mjds), format="mjd").iso[:10],
            "date_end": Time(max(mjds), format="mjd").iso[:10],
            "n_exposures": len(cuts),
            "detectors": sorted(by_det),
            "flag_bits_near_target": [b for b in range(32) if flags >> b & 1],
        })

    if not frames:
        raise LookupError("images were found but none could be read - try again later")

    out_dir.mkdir(parents=True, exist_ok=True)
    frame_paths, diff_paths = render_frames(frames, out_dir, scale)

    spectrum = sorted(
        ({"mjd": c.exposure.mjd, "detector": c.exposure.detector,
          "wavelength_um": c.wavelength_um, "bandwidth_um": c.bandwidth_um,
          "flux_mJy": f, "flag_bits": c.flags_near_target}
         for c in by_exposure.values()
         if np.isfinite(c.wavelength_um) and np.isfinite(f := aperture_flux_mjy(c, ra, dec))),
        key=lambda p: p["wavelength_um"])

    gaia = None
    if with_gaia:
        try:
            stars = gaia_sources(ra, dec, radius_deg=fov_arcmin / 60 * 0.75)
            gaia = gaia_overlay(stars, grid, npix, scale, [m["mjd_mean"] for m in frame_meta])
        except Exception as exc:  # Gaia archive down must not kill the whole request
            log.warning("Gaia query failed: %s", exc)

    result = {
        "ra": ra, "dec": dec, "fov_arcmin": fov_arcmin, "bin_days": bin_days,
        "detectors": list(detectors), "collection": collection,
        "pixel_scale_arcsec": PIXEL_SCALE_ARCSEC,
        "image_size_px": npix * scale, "upscale": scale,
        "images": frame_paths,
        "timestamps": [m["timestamp"] for m in frame_meta],
        "difference_images": diff_paths,
        "frames": frame_meta,
        "spectrum": spectrum,
        "gaia": gaia,
        "cache_dir": str(out_dir),
    }
    _write_json(out_dir / "meta.json", result)
    return result


# --------------------------------------------------------------------------- #
# CLI for testing without the middleware
# --------------------------------------------------------------------------- #
def main() -> None:
    p = argparse.ArgumentParser(description="SPHEREx time-lapse for a patch of sky")
    p.add_argument("target", nargs="+", help="name ('R Dra') or coordinates ('133.79 -7.24')")
    p.add_argument("--fov", type=float, default=4.0, help="field of view in arcmin")
    p.add_argument("--bin-days", type=float, default=10.0)
    p.add_argument("--detectors", type=int, nargs="+", default=[1, 2, 3, 4, 5, 6])
    p.add_argument("--collection", default=None, help="default: newest spherex_drN")
    p.add_argument("--no-gaia", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    ra, dec = resolve_target(" ".join(args.target))
    t0 = time.time()
    r = get_timelapse(ra, dec, args.fov, args.bin_days, tuple(args.detectors),
                      args.collection, with_gaia=not args.no_gaia)
    print(f"\n{len(r['images'])} frames in {time.time() - t0:.1f}s  ->  {r['cache_dir']}")
    for m in r["frames"]:
        print(f"  {m['date_start']} .. {m['date_end']}  {m['n_exposures']:3d} exposures"
              f"  detectors {m['detectors']}  flags {m['flag_bits_near_target']}")
    print(f"spectrum points: {len(r['spectrum'])}   gaia stars: "
          f"{len(r['gaia']) if r['gaia'] is not None else 'n/a'}")

    from gif import make_gif  # GIF building lives in gif.py
    gif_path = make_gif(r["images"], r["timestamps"], Path(r["cache_dir"]) / "preview.gif")
    print(f"preview: {gif_path}")


if __name__ == "__main__":
    main()
