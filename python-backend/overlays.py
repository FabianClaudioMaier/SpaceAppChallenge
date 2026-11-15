"""
Star overlays: the brightest and the fastest-moving stars of a frame, from
Gaia DR3 and CatWISE2020, placed on every frame at that frame's date.

The images are never changed. Instead every catalogue position is moved from
the catalogue's epoch to each frame's date with the star's proper motion
(Barnard's star: ~99" = 16 px between Gaia's 2016.0 and the 2025 frames), then
turned into JPG pixel coordinates with the frame's own grid (make_grid_wcs).

Sources (all independent of IRSA, which also serves the SPHEREx images):
    VizieR    Gaia DR3 (I/355/gaiadr3) and CatWISE2020 (II/365/catwise),
              CDS Strasbourg first, the CfA (Harvard) mirror as backup
    SIMBAD    common names ("Barnard's star", "HD 163774") and object types,
<<<<<<< HEAD
              same two hosts
=======
              same two hosts; and the known fast movers (proper motion >=
              FAST_MAS_YR) with literature motions - right also where Gaia sees
              nothing and CatWISE's fit fails (WISE 0855-0714: CatWISE says
              0.27"/yr, really 8.1"/yr)
>>>>>>> origin/star-overlays

Which stars:
    bright    the N_BRIGHT brightest in the frame (Gaia: G, CatWISE: W1)
    fast      up to N_FAST with proper motion >= FAST_MAS_YR, however faint.
              CatWISE motions of faint sources are noisy and the glow of a
              bright star produces fake fast movers around it (at Barnard's star:
              dozens within ~4', "moving" 0.5-9"/yr). So a CatWISE fast mover
              needs CATWISE_MIN_PM_SIGMA, must lie outside every bright Gaia
              star's glow, and must be red (W1-W2 >= CATWISE_MIN_FAST_COLOR):
              anything bluer that bright is a normal star, which Gaia sees with
              far better motions - CatWISE adds what Gaia can't see (brown dwarfs).

fetch() talks to the servers (cached, never expires: the catalogues don't
<<<<<<< HEAD
change), place() puts the stars on the frames (fast, no network).
=======
change; Gaia/CatWISE files carry the row limit in their name, so a new N_BRIGHT
fetches again), place() puts the stars on the frames (fast, no network).
>>>>>>> origin/star-overlays
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import urllib.parse
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, wait
from pathlib import Path

import numpy as np
import requests
from astropy.wcs import WCS

log = logging.getLogger("spherex_backend")

VIZIER_HOSTS = ["https://vizier.cds.unistra.fr", "https://vizier.cfa.harvard.edu"]
SIMBAD_HOSTS = ["https://simbad.cds.unistra.fr", "https://simbad.cfa.harvard.edu"]
LINK_VIZIER = VIZIER_HOSTS[0]  # links for the user: the main site
LINK_SIMBAD = SIMBAD_HOSTS[0]

<<<<<<< HEAD
N_BRIGHT = 30               # brightest stars per catalogue in the frame
N_FAST = 10                 # fast movers per catalogue in the frame
=======
N_BRIGHT = 50               # brightest stars per catalogue in the frame
N_FAST = 30                 # fast movers per catalogue in the frame
>>>>>>> origin/star-overlays
FAST_MAS_YR = 200.0         # "fast" from this proper motion on
CATWISE_MIN_PM_SIGMA = 10.0  # CatWISE fast movers: motion >= this many sigma
CATWISE_MIN_FAST_COLOR = 0.4  # W1-W2: redder than normal stars (brown dwarfs: 0.3 .. 3+)
GLOW_ARCMIN_AT_8 = 2.5      # glow radius of an RP = 8 star, x0.63 per magnitude fainter
GLOW_MAX_MAG = 12.0         # (RP, or G if RP is missing) fainter: no glow worth avoiding
MATCH_ARCSEC = 3.0          # CatWISE <-> Gaia: same star within this distance
TWIN_ARCSEC = 10.0          # CatWISE lists some fast movers twice, a few " apart: show one
MARGIN_ARCMIN = 2.0         # search beyond the frame: fast stars may move into it
QUERY_ROWS = 2 * N_BRIGHT   # the search circle is ~2x the frame's area
<<<<<<< HEAD
=======
ROW_LIMITED = ("gaia_dr3", "catwise2020")  # their "brightest" query stops at QUERY_ROWS
OLD_QUERY_ROWS = 60         # cache files without "_r<rows>" in their name were fetched with this
>>>>>>> origin/star-overlays
HEDGE_AFTER_S = 4.0         # no answer yet -> also ask the next host
TIMEOUT_S = (5.0, 30.0)     # connect, read
PIXEL_SCALE_ARCSEC = 6.15   # SPHEREx (same as backend.PIXEL_SCALE_ARCSEC)

GAIA_COLUMNS = "Source,RA_ICRS,DE_ICRS,Gmag,BPmag,RPmag,Plx,e_Plx,pmRA,pmDE,PM"
<<<<<<< HEAD
=======
SIMBAD_BANDS = ("J", "H", "K", "G")  # magnitudes taken from SIMBAD for its fast movers
>>>>>>> origin/star-overlays

MJD_J2000 = 51544.5
DAYS_PER_YEAR = 365.25

CATALOGS = {
    "gaia_dr3": {
        "label": "Gaia DR3",
        "source": "VizieR I/355/gaiadr3",
        "table": "I/355/gaiadr3",
        "epoch": 2016.0,
        "epoch_mjd": MJD_J2000 + 16.0 * DAYS_PER_YEAR,
        "mag_system": "Vega",
        "sort_mag": "G",
    },
    "catwise2020": {
        "label": "CatWISE2020",
        "source": "VizieR II/365/catwise",
        "table": "II/365/catwise",
        "epoch": 2015.4,
        "epoch_mjd": 57170.0,  # ReadMe: RAPMdeg/DEPMdeg are at MJD 57170 (2015.4)
        "mag_system": "Vega",
        "sort_mag": "W1",
    },
<<<<<<< HEAD
=======
    "simbad_movers": {
        "label": "Known fast movers (SIMBAD)",
        "source": "SIMBAD (proper motion >= 200 mas/yr)",
        "epoch": 2000.0,
        "epoch_mjd": MJD_J2000,  # SIMBAD's positions are for J2000 (checked against Gaia DR3)
        "mag_system": "Vega",
        "sort_mag": "J",
        "n_bright": 0,  # fast movers only, no "brightest N"
    },
>>>>>>> origin/star-overlays
}


class CatalogError(Exception):
    pass


class EmptyAnswer(Exception):
    """No star at all in the search circle: a broken query (never cached), or
    really empty (tiny frame) - shown as an empty overlay either way."""


# --------------------------------------------------------------------------- #
# Small helpers (no import from backend: it imports this module)
# --------------------------------------------------------------------------- #
def _in_background(call: Callable[[], object]) -> Future:
    """Run call in a daemon thread (a hanging server never keeps the backend alive)."""
    future: Future = Future()

    def run() -> None:
        try:
            future.set_result(call())
        except BaseException as exc:
            future.set_exception(exc)

    threading.Thread(target=run, daemon=True).start()
    return future


def _first_ok(calls: list[Callable[[], object]]):
    """calls[0]; if it fails or hasn't answered after HEDGE_AFTER_S, also the next one."""
    rest = list(calls)
    running = {_in_background(rest.pop(0))}
    errors = []
    while running:
        done, running = wait(running, timeout=HEDGE_AFTER_S, return_when=FIRST_COMPLETED)
        for f in done:
            if f.exception() is None:
                return f.result()
            errors.append(f"{type(f.exception()).__name__}: {f.exception()}")
        if rest:
            running.add(_in_background(rest.pop(0)))
    raise CatalogError(" | ".join(errors))


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("ignoring damaged cache file %s (%s)", path, exc)
        return None


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(obj, indent=1))
    os.replace(tmp, path)


def _float(text: str | None) -> float | None:
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


# --------------------------------------------------------------------------- #
# Talking to VizieR and SIMBAD
# --------------------------------------------------------------------------- #
def _vizier(table: str, ra: float, dec: float, radius_arcmin: float, params: dict,
            columns: str | None = None) -> list[dict]:
    """Rows of a VizieR cone search (ASU, tab-separated) -> list of {column: text}.

    columns: "-out" list, or None for all columns. (Tested 2026-11: CatWISE
    returns no rows at all with a -out list, nor with "-out.all=" - only the bare
    "-out.all" works. Gaia is the other way round: -out.all takes 8-22 s, a
    column list ~3 s.)
    """
    query = {"-source": table, "-c": f"{ra:.6f} {dec:+.6f}", "-c.rm": f"{radius_arcmin:.2f}", **params}
    path = "/viz-bin/asu-tsv?" + urllib.parse.urlencode(
        {**query, "-out": columns} if columns else query) + ("" if columns else "&-out.all")

    def ask(host: str) -> list[dict]:
        r = requests.get(host + path, timeout=TIMEOUT_S)
        r.raise_for_status()
        if not r.text.startswith("#"):  # an HTML error page with status 200
            raise CatalogError(f"{host}: not a VizieR answer")
        lines = [l for l in r.text.splitlines() if l.strip() and not l.startswith("#")]
        if not lines:
            return []  # nothing in the circle
        head = lines[0].split("\t")
        return [dict(zip(head, (v.strip() for v in l.split("\t")))) for l in lines[3:]]  # units, ----

    return _first_ok([lambda h=h: ask(h) for h in VIZIER_HOSTS])


def _simbad_names(ids: list[str]) -> dict[str, tuple[str, str]]:
    """{catalogue id: (SIMBAD main name, object type)} for the ids SIMBAD knows."""
    if not ids:
        return {}
    listed = ", ".join("'" + i.replace("'", "''") + "'" for i in ids)
    adql = ("SELECT i.id, b.main_id, b.otype FROM ident AS i JOIN basic AS b ON b.oid = i.oidref "
            f"WHERE i.id IN ({listed})")
    data = {"REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "json", "QUERY": adql}

    def ask(host: str) -> dict:
        r = requests.post(host + "/simbad/sim-tap/sync", data=data, timeout=TIMEOUT_S)
        r.raise_for_status()
        return {row[0]: (row[1], row[2]) for row in r.json()["data"]}

    return _first_ok([lambda h=h: ask(h) for h in SIMBAD_HOSTS])


def _pretty_name(main_id: str) -> str:
    """SIMBAD's "NAME Barnard's star" -> "Barnard's star", "* alf Ori" -> "alf Ori",
    "TYC  425-1810-1" -> "TYC 425-1810-1" (SIMBAD pads some names with spaces)."""
    for prefix in ("NAME ", "* ", "V* "):
        if main_id.startswith(prefix):
            main_id = main_id[len(prefix):]
            break
    return " ".join(main_id.split())


# --------------------------------------------------------------------------- #
# Reading the catalogue rows
# --------------------------------------------------------------------------- #
def _gaia_star(row: dict) -> dict | None:
    ra, dec = _float(row.get("RA_ICRS")), _float(row.get("DE_ICRS"))
    if ra is None or dec is None:
        return None
    pm_ra, pm_dec = _float(row.get("pmRA")), _float(row.get("pmDE"))  # mas/yr, pmRA incl. cos(dec)
    plx, e_plx = _float(row.get("Plx")), _float(row.get("e_Plx"))
    good_plx = plx is not None and e_plx is not None and plx > 0 and plx >= 5 * e_plx
    source = row["Source"]
    return {
        "id": f"Gaia DR3 {source}",
        "ra": ra, "dec": dec,
        "pm_ra": pm_ra, "pm_dec": pm_dec,
        "mag": {"G": _round(_float(row.get("Gmag")), 3),
                "BP": _round(_float(row.get("BPmag")), 3),
                "RP": _round(_float(row.get("RPmag")), 3)},
        "parallax_mas": _round(plx, 3),
        "distance_ly": round(1000 / plx * 3.261564, 2) if good_plx else None,
        "fast_ok": True,  # Gaia's motions are reliable
        "vizier": f"{LINK_VIZIER}/viz-bin/VizieR-5?" + urllib.parse.urlencode(
            {"-source": "I/355/gaiadr3", "Source": source}),
    }


def _catwise_star(row: dict) -> dict | None:
    # RAPMdeg/DEPMdeg: position at the CatWISE epoch (MJD 57170), from the motion fit
    ra, dec = _float(row.get("RAPMdeg")), _float(row.get("DEPMdeg"))
    if ra is None or dec is None:
        return None
    pm_ra, pm_dec = _float(row.get("pmRA")), _float(row.get("pmDE"))  # arcsec/yr
    e_ra, e_dec = _float(row.get("e_pmRA")), _float(row.get("e_pmDE"))
    sigma = 0.0
    if None not in (pm_ra, pm_dec, e_ra, e_dec) and (pm_ra or pm_dec):
        total = math.hypot(pm_ra, pm_dec)
        err = math.hypot(pm_ra * e_ra, pm_dec * e_dec) / total
        sigma = total / err if err > 0 else 0.0
    w1, w2 = _float(row.get("W1mproPM")), _float(row.get("W2mproPM"))
    name = row["Name"]
    return {
        "id": f"CWISE {name}",
        "ra": ra, "dec": dec,
        "pm_ra": None if pm_ra is None else pm_ra * 1000,  # -> mas/yr
        "pm_dec": None if pm_dec is None else pm_dec * 1000,
        "mag": {"W1": _round(w1, 3), "W2": _round(w2, 3),
                "W1-W2": _round(w1 - w2, 3) if w1 is not None and w2 is not None else None},
        "fast_ok": (sigma >= CATWISE_MIN_PM_SIGMA and w1 is not None and w2 is not None
                    and w1 - w2 >= CATWISE_MIN_FAST_COLOR),
        "vizier": f"{LINK_VIZIER}/viz-bin/VizieR-5?" + urllib.parse.urlencode(
            {"-source": "II/365/catwise", "Name": name}),
    }


def _total_pm(star: dict) -> float:
    if star["pm_ra"] is None or star["pm_dec"] is None:
        return 0.0
    return math.hypot(star["pm_ra"], star["pm_dec"])


def _move(star: dict, from_mjd: float, to_mjd: float) -> tuple[float, float]:
    """Position at to_mjd: a straight line on the sky with the proper motion
    (good to << 1 SPHEREx pixel over decades, except right at a pole)."""
    dt = (to_mjd - from_mjd) / DAYS_PER_YEAR
    pm_ra, pm_dec = star["pm_ra"] or 0.0, star["pm_dec"] or 0.0
    dec = star["dec"] + pm_dec * dt / 3.6e6
    cos_dec = max(math.cos(math.radians(star["dec"])), 1e-6)
    ra = (star["ra"] + pm_ra * dt / 3.6e6 / cos_dec) % 360
    return ra, dec


def _separation_arcsec(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    dra = (ra1 - ra2 + 180) % 360 - 180
    return math.hypot(dra * math.cos(math.radians((dec1 + dec2) / 2)), dec1 - dec2) * 3600


# --------------------------------------------------------------------------- #
# fetch: the candidate stars of a frame, with names (network, cached)
# --------------------------------------------------------------------------- #
def search_radius_arcmin(pixels: int) -> float:
    return pixels * PIXEL_SCALE_ARCSEC / 60 / 2 * math.sqrt(2) + MARGIN_ARCMIN


<<<<<<< HEAD
def _cache_path(cache_dir: Path, catalog: str, ra: float, dec: float, pixels: int) -> Path:
    return cache_dir / "catalogs" / catalog / f"ra{ra:09.5f}_dec{dec:+09.5f}_p{pixels}.json"
=======
def _cache_name(ra: float, dec: float, pixels: int) -> str:
    return f"ra{ra:09.5f}_dec{dec:+09.5f}_p{pixels}"


def _cache_path(cache_dir: Path, catalog: str, ra: float, dec: float, pixels: int) -> Path:
    """Gaia/CatWISE: QUERY_ROWS is part of the name, so after a change of N_BRIGHT
    the stars are fetched again instead of reusing an answer with fewer of them."""
    rows = f"_r{QUERY_ROWS}" if catalog in ROW_LIMITED else ""
    return cache_dir / "catalogs" / catalog / f"{_cache_name(ra, dec, pixels)}{rows}.json"


def _older_cache(cache_dir: Path, catalog: str, ra: float, dec: float, pixels: int) -> list[dict] | None:
    """The same frame cached with another row limit (the most rows first), or None.
    Used when the server fails: fewer stars are better than none."""
    if catalog not in ROW_LIMITED:
        return None
    folder = cache_dir / "catalogs" / catalog
    name = _cache_name(ra, dec, pixels)

    def rows(path: Path) -> int:
        tail = path.stem[len(name):]  # "" or "_r<rows>"
        return int(tail[2:]) if tail[2:].isdigit() else OLD_QUERY_ROWS

    # "_r*" (not "*"): "..._p250*" would also match a 2500 px frame
    found = [p for p in [folder / f"{name}.json", *folder.glob(f"{name}_r*.json")] if p.exists()]
    for path in sorted(found, key=rows, reverse=True):
        stars = _read_json(path)
        if stars is not None:
            log.warning("star overlay %s: using the older cached answer (%d rows) instead",
                        catalog, rows(path))
            return stars
    return None
>>>>>>> origin/star-overlays


def _query_gaia(ra: float, dec: float, radius: float) -> list[dict]:
    t = CATALOGS["gaia_dr3"]["table"]
    bright = _in_background(lambda: _vizier(t, ra, dec, radius, {"-sort": "Gmag", "-out.max": QUERY_ROWS},
                                            GAIA_COLUMNS))
    fast = _in_background(lambda: _vizier(t, ra, dec, radius, {"PM": f">{FAST_MAS_YR:g}", "-out.max": 500},
                                          GAIA_COLUMNS))
    bright_rows = bright.result()
    if not bright_rows:
        raise EmptyAnswer("Gaia DR3: no stars in the search circle")
    stars = {}
    for row in bright_rows + fast.result():
        star = _gaia_star(row)
        if star:
            stars[star["id"]] = star
    return list(stars.values())


def _query_catwise(ra: float, dec: float, radius: float) -> list[dict]:
    t = CATALOGS["catwise2020"]["table"]
    lim = FAST_MAS_YR / 1000  # arcsec/yr
    # VizieR can't filter on the total motion: one query per direction, and only
    # sources whose motion is measured well enough to possibly pass the sigma cut.
    queries = [{"-sort": "W1mproPM", "-out.max": QUERY_ROWS}] + [
        {f"pm{d}": f"!-{lim}..{lim}", f"e_pm{d}": f"<{lim / 5:g}", "-out.max": 1000} for d in ("RA", "DE")]
    futures = [_in_background(lambda q=q: _vizier(t, ra, dec, radius, q)) for q in queries]
    if not futures[0].result():
        raise EmptyAnswer("CatWISE2020: no sources in the search circle")
    stars = {}
    for f in futures:
        for row in f.result():
            star = _catwise_star(row)
            if star:
                stars[star["id"]] = star
    return list(stars.values())


<<<<<<< HEAD
=======
def _query_simbad_movers(ra: float, dec: float, radius_arcmin: float) -> list[dict]:
    """Every object SIMBAD knows in the circle moving >= FAST_MAS_YR (often none:
    an empty list is a normal answer here and gets cached)."""
    adql = ("SELECT b.main_id, b.otype, b.ra, b.dec, b.pmra, b.pmdec, b.plx_value, f.filter, f.flux "
            "FROM basic AS b LEFT JOIN flux AS f ON f.oidref = b.oid "
            f"WHERE CONTAINS(POINT('ICRS', b.ra, b.dec), CIRCLE('ICRS', {ra:.6f}, {dec:+.6f}, "
            f"{radius_arcmin / 60:.5f})) = 1 "
            f"AND b.pmra * b.pmra + b.pmdec * b.pmdec >= {FAST_MAS_YR ** 2:g}")
    data = {"REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "json", "QUERY": adql}

    def ask(host: str) -> list:
        r = requests.post(host + "/simbad/sim-tap/sync", data=data, timeout=TIMEOUT_S)
        r.raise_for_status()
        return r.json()["data"]

    stars: dict[str, dict] = {}  # one row per object and band -> one star per object
    for main_id, otype, s_ra, s_dec, pm_ra, pm_dec, plx, band, flux in _first_ok(
            [lambda h=h: ask(h) for h in SIMBAD_HOSTS]):
        if s_ra is None or s_dec is None:
            continue
        star = stars.get(main_id)
        if star is None:
            star = stars[main_id] = {
                "id": _pretty_name(main_id),
                "ra": s_ra, "dec": s_dec,
                "pm_ra": pm_ra, "pm_dec": pm_dec,  # mas/yr, pmra incl. cos(dec)
                "mag": {b: None for b in SIMBAD_BANDS},
                "parallax_mas": _round(plx, 3),
                "distance_ly": round(1000 / plx * 3.261564, 2) if plx and plx > 0 else None,
                "fast_ok": True,  # literature motions
                "vizier": None,   # not a VizieR table
                "name": _pretty_name(main_id), "simbad_id": main_id, "simbad_type": otype or None,
            }
        if band in SIMBAD_BANDS and flux is not None:
            star["mag"][band] = _round(flux, 3)
    return list(stars.values())


>>>>>>> origin/star-overlays
def _drop_glow_fakes(catwise: list[dict], gaia: list[dict]) -> None:
    """CatWISE 'fast movers' inside a bright Gaia star's glow are fakes: not fast_ok.
    The glow is measured by RP (red): WISE sees in the infrared, where red stars
    like Barnard's (G 8.2, RP 7.0) are much brighter than in G."""
    epoch = CATALOGS["catwise2020"]["epoch_mjd"]
    bright = []
    for g in gaia:
        mag = g["mag"]["RP"] if g["mag"]["RP"] is not None else g["mag"]["G"]
        if mag is not None and mag <= GLOW_MAX_MAG:
            bright.append((*_move(g, CATALOGS["gaia_dr3"]["epoch_mjd"], epoch),
                           60 * GLOW_ARCMIN_AT_8 * 10 ** (-0.2 * (mag - 8))))
    for c in catwise:
        if c["fast_ok"] and any(_separation_arcsec(c["ra"], c["dec"], ra, dec) < r for ra, dec, r in bright):
            c["fast_ok"] = False


def _add_names(stars_by_catalog: dict[str, list[dict]]) -> None:
    """name / simbad_type / links from SIMBAD; CatWISE stars SIMBAD doesn't know
    by their CatWISE name get those of the Gaia star at the same position."""
    new = [s for stars in stars_by_catalog.values() for s in stars if "simbad_id" not in s]
    known = _simbad_names([s["id"] for s in new])  # (cached stars have their names already)
    for s in new:
        main_id, otype = known.get(s["id"], (None, None))
        s["name"] = _pretty_name(main_id) if main_id else None
        s["simbad_id"] = main_id
        s["simbad_type"] = otype or None

    gaia, catwise = stars_by_catalog.get("gaia_dr3"), stars_by_catalog.get("catwise2020")
    if gaia and catwise:
        g_epoch, c_epoch = CATALOGS["gaia_dr3"]["epoch_mjd"], CATALOGS["catwise2020"]["epoch_mjd"]
        gaia_then = [(g, *_move(g, g_epoch, c_epoch)) for g in gaia if g["simbad_id"]]
        for c in catwise:
            if c["simbad_id"]:
                continue
            for g, ra, dec in gaia_then:
                if _separation_arcsec(c["ra"], c["dec"], ra, dec) < MATCH_ARCSEC:
                    c.update(name=g["name"], simbad_id=g["simbad_id"], simbad_type=g["simbad_type"])
                    break


def fetch(ra: float, dec: float, pixels: int, cache_dir: Path) -> tuple[dict[str, list[dict] | None], bool]:
<<<<<<< HEAD
    """Candidate stars per catalogue -> ({catalog: stars, or None if its server failed},
    complete). complete = nothing failed (not even the names), so a result built
    from this may be kept for reuse. Cached per catalogue once complete."""
=======
    """Candidate stars per catalogue -> ({catalog: stars, or None if its server failed
    and nothing older is cached}, complete). complete = nothing failed (not even the
    names), so a result built from this may be kept for reuse. Cached per catalogue
    once complete."""
>>>>>>> origin/star-overlays
    radius = search_radius_arcmin(pixels)
    out: dict[str, list[dict] | None] = {}
    todo = {}
    for name in CATALOGS:
        cached = _read_json(_cache_path(cache_dir, name, ra, dec, pixels))
        if cached is not None:
            out[name] = cached
        else:
<<<<<<< HEAD
            query = _query_gaia if name == "gaia_dr3" else _query_catwise
=======
            query = {"gaia_dr3": _query_gaia, "catwise2020": _query_catwise,
                     "simbad_movers": _query_simbad_movers}[name]
>>>>>>> origin/star-overlays
            todo[name] = _in_background(lambda q=query: q(ra, dec, radius))
    if not todo:
        return out, True

    complete = True
    fresh: dict[str, list[dict]] = {}
    for name, future in todo.items():
        try:
            fresh[name] = future.result()
        except EmptyAnswer as exc:
            log.warning("star overlay %s: %s", name, exc)
<<<<<<< HEAD
            out[name] = []
            complete = False
        except Exception as exc:
            log.warning("star overlay %s: catalogue query failed (%s)", name, exc)
            out[name] = None
=======
            out[name] = _older_cache(cache_dir, name, ra, dec, pixels) or []
            complete = False
        except Exception as exc:
            log.warning("star overlay %s: catalogue query failed (%s)", name, exc)
            out[name] = _older_cache(cache_dir, name, ra, dec, pixels)
>>>>>>> origin/star-overlays
            complete = False

    gaia = out["gaia_dr3"] if "gaia_dr3" in out else fresh.get("gaia_dr3")  # cached, failed or fresh
    if "catwise2020" in fresh and gaia:
        _drop_glow_fakes(fresh["catwise2020"], gaia)
    elif "catwise2020" in fresh:  # no Gaia stars to tell real fast movers from glow fakes
        for c in fresh["catwise2020"]:
            c["fast_ok"] = False
        complete = False

    try:
        named = {**{n: s for n, s in out.items() if s is not None}, **fresh}
        _add_names(named)
    except Exception as exc:
        log.warning("star overlays: SIMBAD names failed (%s)", exc)
        for stars in fresh.values():
            for s in stars:
                s.update(name=None, simbad_id=None, simbad_type=None)
        complete = False

    for name, stars in fresh.items():
        out[name] = stars
        if complete:  # never cache anything half-done (no names, no glow check, empty answer)
            _write_json(_cache_path(cache_dir, name, ra, dec, pixels), stars)
    return out, complete


# --------------------------------------------------------------------------- #
# place: which stars are shown, and where on each frame (no network)
# --------------------------------------------------------------------------- #
<<<<<<< HEAD
def _links(star: dict) -> dict[str, str]:
=======
def _links(star: dict) -> dict[str, str | None]:
>>>>>>> origin/star-overlays
    if star.get("simbad_id"):
        simbad = f"{LINK_SIMBAD}/simbad/sim-id?" + urllib.parse.urlencode({"Ident": star["simbad_id"]})
    else:  # SIMBAD doesn't know it by name: search around its position
        simbad = f"{LINK_SIMBAD}/simbad/sim-coo?" + urllib.parse.urlencode(
            {"Coord": f"{star['ra']:.6f} {star['dec']:+.6f}", "Radius": "5", "Radius.unit": "arcsec"})
    return {"simbad": simbad, "vizier": star["vizier"]}


def _xy(star: dict, epoch_mjd: float, grid: WCS, pixels: int, frame_mjds: list[float]) -> list:
    """[x, y] per frame in JPG pixels from the top left (None = outside the image)."""
    pos = [_move(star, epoch_mjd, mjd) for mjd in frame_mjds]
    x, y = grid.world_to_pixel_values(np.array([p[0] for p in pos]), np.array([p[1] for p in pos]))
    out = []
    for xi, yi in zip(x, y):
        # FITS pixel i covers i-0.5..i+0.5, row 0 at the bottom; the JPG is flipped
        # (see backend._save_jpg) and is measured from its top-left corner.
        x_img, y_img = float(xi) + 0.5, (pixels - 1 - float(yi)) + 0.5
        inside = 0 <= x_img <= pixels and 0 <= y_img <= pixels
        out.append([round(x_img, 2), round(y_img, 2)] if inside else None)
    return out


def place(catalogs: dict[str, list[dict] | None], grid: WCS, pixels: int,
          frame_mjds: list[float]) -> dict[str, dict | None]:
    """The "overlays" part of meta.json (see the module docstring)."""
    result: dict[str, dict | None] = {}
    for name, info in CATALOGS.items():
        stars = catalogs.get(name)
        if stars is None:
            result[name] = None
            continue
        shown = []
        for s in stars:
            xy = _xy(s, info["epoch_mjd"], grid, pixels, frame_mjds)
            if any(p is not None for p in xy):
                shown.append((s, xy))

        def mag(item) -> float:
            m = item[0]["mag"][info["sort_mag"]]
            return m if m is not None else 99.0

        bright = {id(s) for s, _ in sorted((i for i in shown if i[0]["mag"][info["sort_mag"]] is not None),
<<<<<<< HEAD
                                          key=mag)[:N_BRIGHT]}
=======
                                          key=mag)[:info.get("n_bright", N_BRIGHT)]}
>>>>>>> origin/star-overlays
        fast_ids: list[int] = []
        fast_pos: list[tuple[float, float]] = []
        for s, _ in sorted((i for i in shown if i[0]["fast_ok"] and _total_pm(i[0]) >= FAST_MAS_YR),
                           key=lambda i: -_total_pm(i[0])):
            if len(fast_ids) == N_FAST:
                break
            if any(_separation_arcsec(s["ra"], s["dec"], ra, dec) < TWIN_ARCSEC for ra, dec in fast_pos):
                continue  # the same object listed twice (CatWISE): the faster entry is kept
            fast_ids.append(id(s))
            fast_pos.append((s["ra"], s["dec"]))
        fast = set(fast_ids)
        out_stars = []
        for s, xy in sorted(shown, key=mag):
            reason = (["bright"] if id(s) in bright else []) + (["fast"] if id(s) in fast else [])
            if not reason:
                continue
            pm = None if s["pm_ra"] is None or s["pm_dec"] is None else {
                "ra": round(s["pm_ra"], 2), "dec": round(s["pm_dec"], 2), "total": round(_total_pm(s), 2)}
            entry = {
                "id": s["id"],
                "name": s.get("name"),
                "simbad_type": s.get("simbad_type"),
                "reason": reason,
                "ra": round(s["ra"], 6),
                "dec": round(s["dec"], 6),
                "mag": s["mag"],
                "pm_mas_yr": pm,
            }
<<<<<<< HEAD
            if name == "gaia_dr3":
                entry.update(parallax_mas=s["parallax_mas"], distance_ly=s["distance_ly"])
            entry.update(links=_links(s), xy=xy)
            out_stars.append(entry)
=======
            if "parallax_mas" in s:  # Gaia and SIMBAD
                entry.update(parallax_mas=s["parallax_mas"], distance_ly=s["distance_ly"])
            entry.update(links=_links(s), xy=xy)
            out_stars.append(entry)
        n_bright = info.get("n_bright", N_BRIGHT)
>>>>>>> origin/star-overlays
        result[name] = {
            "label": info["label"],
            "source": info["source"],
            "epoch": info["epoch"],
            "mag_system": info["mag_system"],
<<<<<<< HEAD
            "selection": (f"{N_BRIGHT} brightest ({info['sort_mag']}) in the frame + up to {N_FAST} "
                          f"with proper motion >= {FAST_MAS_YR:g} mas/yr"),
=======
            "selection": ((f"{n_bright} brightest ({info['sort_mag']}) in the frame + " if n_bright else "")
                          + f"up to {N_FAST} with proper motion >= {FAST_MAS_YR:g} mas/yr"),
>>>>>>> origin/star-overlays
            "stars": out_stars,
        }
    return result
