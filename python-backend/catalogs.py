"""
Extra overlay layers, fetched when the user switches one on (frontend layer list,
POST /api/catalog), not during the job - so adding catalogues never slows jobs.

The job itself still brings Gaia DR3, CatWISE2020 and SIMBAD's fast movers
(overlays.py). Everything here comes in the same shape as those ("overlays" in
meta.json), so the frontend draws them the same way:
    {label, source, group, shape, color, epoch, sort_mag, mag_system, vizier_link,
     id_prefix, faintest, selection, stars: [{id, name, simbad_id, simbad_type,
     reason, ra, dec, mag: {band: value}, pm_mas_yr, info: {label: value}}]}
Stars with "positions" (one [ra, dec] or null per frame) instead of a proper
motion are drawn there as they are (the known asteroids: each frame at its date).

Sources (checked 2026-11-15 at three test fields, see the draft's table):
    VizieR    every table in LAYERS ("table"), CDS Strasbourg and the CfA mirror
              (overlays._vizier; CDS was down that day, the mirror carried on)
    SkyBoT    IMCCE's known asteroids and comets at a given date, one query per frame
    SIMBAD    every object SIMBAD knows in the field (names and types)

Cached in Cache/catalogs/<layer>/ (never expires: catalogues don't change; the
asteroid positions are per frame date, which doesn't change either).

Super Intelligence (SI) was used in creating this file.
"""

from __future__ import annotations

import math
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import requests

import overlays
from overlays import (CatalogError, SIMBAD_HOSTS, TIMEOUT_S, _first_ok, _float, _pretty_name,
                      _read_json, _round, _vizier, _write_json, search_radius_arcmin)

MAX_ROWS = 3000  # per layer and field, brightest first where the table has a magnitude
VIZIER_LINK = "https://vizier.cds.unistra.fr/viz-bin/VizieR-5?"

# Layers, grouped as in the layer list. Per VizieR table: its id column (+ prefix
# for the shown id), magnitudes {band: column} (flux_mags: {band: column} of fluxes
# in Vega nanomaggies, mag = 22.5 - 2.5 log10), the band to sort by (brightest
# first), proper motion columns (mas/yr; positions are VizieR's J2000 ones at
# epoch 2000), extra values for the star card {label: column}.
LAYERS: dict[str, dict] = {
    # -- stars and infrared
    "twomass": dict(group="stars", label="2MASS", table="II/246/out", id="2MASS", prefix="2MASS J",
                    mags={"J": "Jmag", "H": "Hmag", "K": "Kmag"}, sort="K",
                    shape="circle", color="#b48cff", note="J, H, K (1.2-2.2 µm), 1997-2001"),
    "unwise": dict(group="stars", label="unWISE", table="II/363/unwise", id="objID", prefix="unWISE ",
                   flux_mags={"W1": "FW1", "W2": "FW2"}, sort="W1", sort_column="-FW1",
                   shape="square", color="#ffd84d", note="the deepest WISE catalogue, W1/W2"),
    "allwise": dict(group="stars", label="AllWISE", table="II/328/allwise", id="AllWISE", prefix="AllWISE ",
                    mags={"W1": "W1mag", "W2": "W2mag", "W3": "W3mag", "W4": "W4mag"}, sort="W1",
                    info={"Artifact flags": "ccf"}, artifacts="ccf",
                    shape="square", color="#ff8c42", note="adds W3/W4 (12/22 µm)"),
    "ps1": dict(group="stars", label="Pan-STARRS1", table="II/349/ps1", id="objID", prefix="PS1 ",
                mags={"g": "gmag", "r": "rmag", "i": "imag", "z": "zmag", "y": "ymag"}, sort="r",
                shape="circle", color="#7cf0a0", note="deep optical, Dec > -30°"),
    "skymapper": dict(group="stars", label="SkyMapper DR4", table="II/379/smssdr4", id="ObjectId", prefix="SMSS ",
                      mags={"g": "gPSF", "r": "rPSF", "i": "iPSF", "z": "zPSF"}, sort="r",
                      shape="circle", color="#7cf0a0", note="optical, southern sky"),
    "vhs": dict(group="stars", label="VISTA VHS", table="II/367/vhs_dr5", id="SrcID", prefix="VHS ",
                mags={"J": "Jpmag", "H": "Hpmag", "Ks": "Kspmag"}, sort="Ks",
                shape="square", color="#7cf0a0", note="deep near-IR, southern sky"),
    # -- moving and nearby things
    "asteroids": dict(group="movers", label="Known asteroids & comets", special="skybot",
                      shape="cross", color="#ff6b6b", note="IMCCE SkyBoT, positions at each frame's date"),
    "bd20pc": dict(group="movers", label="Brown dwarfs ≤ 20 pc", table="J/ApJS/253/7", id="Name", prefix="",
                   mags={"J": "Jmag", "H": "Hmag", "K": "Ksmag", "W1": "W1mag", "W2": "W2mag"}, sort="W2",
                   pm=("pmRA", "pmDE"), info={"Parallax (mas)": "plx", "Spectral type code (IR)": "SpTIR"},
                   shape="diamond", color="#ff9cf0", note="Kirkpatrick+ 2021"),
    "gcns": dict(group="movers", label="Nearby stars ≤ 100 pc", table="J/A+A/649/A6", id="GaiaEDR3",
                 prefix="Gaia EDR3 ", mags={"G": "Gmag", "BP": "BPmag", "RP": "RPmag"}, sort="G",
                 pm=("pmRA", "pmDE"), info={"Parallax (mas)": "Plx", "Distance (kpc)": "Dist50"},
                 shape="diamond", color="#c79cff", note="Gaia Catalogue of Nearby Stars"),
    # -- galaxies and quasars
    "twomassx": dict(group="galaxies", label="2MASS galaxies (XSC)", table="VII/233/xsc", id="2MASX",
                     prefix="2MASX J", mags={"J": "J.ext", "H": "H.ext", "K": "K.ext"}, sort="K",
                     shape="triangle", color="#f5f5f5", note="extended sources"),
    "gladep": dict(group="galaxies", label="GLADE+ galaxies", table="VII/291/gladep", id="GLADE+", prefix="GLADE+ ",
                   mags={"B": "Bmag", "J": "Jmag", "K": "Kmag", "W1": "W1mag"}, sort="K",
                   info={"Redshift": "zcmb", "Type": "Type"},
                   shape="triangle", color="#dcdcdc", note="galaxies and quasars with distances"),
    "wiseagn": dict(group="galaxies", label="WISE AGN (R90)", table="J/ApJS/234/23/r90cat", id="WISEA",
                    prefix="WISEA ", mags={"W1": "W1mag", "W2": "W2mag", "W3": "W3mag"}, sort="W1",
                    shape="triangle", color="#ff9f43", note="mid-IR selected AGN (red, variable)"),
    "gaiaqso": dict(group="galaxies", label="Gaia DR3 quasar candidates", table="I/356/qsocand", id="Source",
                    prefix="Gaia DR3 ", info={"Redshift": "z", "Quasar probability": "PQSO"},
                    shape="triangle", color="#ffb86b", note=""),
    "milliquas": dict(group="galaxies", label="Milliquas quasars", table="VII/294/catalog", id="Name", prefix="",
                      mags={"R": "Rmag", "B": "Bmag"}, sort="R", info={"Redshift": "z", "Type": "Type"},
                      shape="triangle", color="#ffd2a1", note="Flesch 2023"),
    # -- variables
    "vsx": dict(group="variables", label="VSX variable stars", table="B/vsx/vsx", id="Name", prefix="",
                mags={"max": "max"}, sort="max", info={"Type": "Type", "Period (d)": "Period"},
                shape="circle", color="#ff5c8a", note="AAVSO"),
    "gaiavar": dict(group="variables", label="Gaia DR3 variables", table="I/358/vclassre", id="Source",
                    prefix="Gaia DR3 ", info={"Class": "Class"},
                    shape="circle", color="#ff7aa8", note="variability classes"),
    # -- x-ray and radio
    "erass1": dict(group="xray_radio", label="eROSITA eRASS1 (X-ray)", table="J/A+A/682/A34", id="IAUName",
                   prefix="", info={"Flux 0.2-2.3 keV (erg/s/cm²)": "MLFlux1"},
                   shape="cross", color="#9fd0ff", note="western Galactic half"),
    "csc": dict(group="xray_radio", label="Chandra CSC 2 (X-ray)", table="IX/57/csc2master", id="2CXO",
                prefix="2CXO ", shape="cross", color="#9fd0ff", note="pointed observations"),
    "nvss": dict(group="xray_radio", label="NVSS (radio)", table="VIII/65/nvss", id="NVSS", prefix="NVSS ",
                 info={"1.4 GHz (mJy)": "S1.4"}, shape="cross", color="#c8ff9f", note="Dec > -40°"),
    "sumss": dict(group="xray_radio", label="SUMSS (radio)", table="VIII/81B/sumss212", id="recno",
                  prefix="SUMSS #", info={"843 MHz (mJy)": "St"}, shape="cross", color="#c8ff9f",
                  note="Dec < -30°"),
    "vlass": dict(group="xray_radio", label="VLASS (radio)", table="J/ApJS/255/30", id="CompName", prefix="",
                  info={"3 GHz (mJy)": "Ftot"}, shape="cross", color="#c8ff9f", note="Dec > -40°"),
    # -- names
    "simbad_all": dict(group="names", label="SIMBAD: every object", special="simbad",
                       shape="circle", color="#ffffff", note="names and types of everything known"),
}

GROUPS = {"stars": "Stars & infrared", "movers": "Moving & nearby", "galaxies": "Galaxies & quasars",
          "variables": "Variable stars", "xray_radio": "X-ray & radio", "names": "Names"}


def summary() -> dict:
    """The layers the frontend can offer (GET /api/catalogs)."""
    return {"groups": GROUPS, "layers": {
        key: {k: info.get(k) for k in ("group", "label", "shape", "color", "note")}
        | {"source": "IMCCE SkyBoT" if info.get("special") == "skybot" else
           "SIMBAD" if info.get("special") == "simbad" else f"VizieR {info['table']}"}
        for key, info in LAYERS.items()}}


# --------------------------------------------------------------------------- #
# VizieR tables
# --------------------------------------------------------------------------- #
def _vizier_stars(info: dict, ra: float, dec: float, radius: float) -> list[dict]:
    params = {"-out.add": "_RAJ,_DEJ", "-out.max": MAX_ROWS}
    if info.get("sort_column") or info.get("sort"):
        params["-sort"] = info.get("sort_column") or {**info.get("mags", {})}[info["sort"]]
    stars = []
    for row in _vizier(info["table"], ra, dec, radius, params):
        s_ra, s_dec = _float(row.get("_RAJ2000")), _float(row.get("_DEJ2000"))
        if s_ra is None or s_dec is None:
            continue
        mag = {band: _round(_float(row.get(col)), 3) for band, col in info.get("mags", {}).items()}
        for band, col in info.get("flux_mags", {}).items():
            flux = _float(row.get(col))
            mag[band] = round(22.5 - 2.5 * math.log10(flux), 3) if flux and flux > 0 else None
        pm_ra = pm_dec = None
        if info.get("pm"):
            pm_ra, pm_dec = _float(row.get(info["pm"][0])), _float(row.get(info["pm"][1]))
        key = (row.get(info["id"]) or "").strip()
        flags = (row.get(info["artifacts"]) or "").strip()[:2] if info.get("artifacts") else ""
        stars.append({
            "artifact": any(c in overlays.ARTIFACT_FLAGS for c in flags),
            "id": f"{info['prefix']}{key}", "name": None, "simbad_id": None, "simbad_type": None,
            "ra": s_ra, "dec": s_dec, "pm_ra": pm_ra, "pm_dec": pm_dec, "mag": mag,
            "info": {label: (row.get(col) or "").strip() or None for label, col in info.get("info", {}).items()},
        })
    return stars


# --------------------------------------------------------------------------- #
# SIMBAD: every object in the field
# --------------------------------------------------------------------------- #
def _simbad_stars(ra: float, dec: float, radius: float) -> list[dict]:
    adql = ("SELECT TOP %d b.main_id, b.otype, b.ra, b.dec, b.pmra, b.pmdec, f.flux "
            "FROM basic AS b LEFT JOIN flux AS f ON f.oidref = b.oid AND f.filter = 'V' "
            "WHERE CONTAINS(POINT('ICRS', b.ra, b.dec), CIRCLE('ICRS', %.6f, %+.6f, %.5f)) = 1"
            % (MAX_ROWS, ra, dec, radius / 60))
    data = {"REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "json", "QUERY": adql}

    def ask(host: str) -> list:
        r = requests.post(host + "/simbad/sim-tap/sync", data=data, timeout=TIMEOUT_S)
        r.raise_for_status()
        return r.json()["data"]

    stars = []
    for main_id, otype, s_ra, s_dec, pm_ra, pm_dec, v in _first_ok([lambda h=h: ask(h) for h in SIMBAD_HOSTS]):
        if s_ra is None or s_dec is None:
            continue
        stars.append({"id": _pretty_name(main_id), "name": _pretty_name(main_id), "simbad_id": main_id,
                      "simbad_type": otype or None, "ra": s_ra, "dec": s_dec, "pm_ra": pm_ra, "pm_dec": pm_dec,
                      "mag": {"V": _round(v, 3)}, "info": {}})
    return stars


# --------------------------------------------------------------------------- #
# SkyBoT: known asteroids and comets, one query per frame date
# --------------------------------------------------------------------------- #
SKYBOT_URL = "https://ssp.imcce.fr/webservices/skybot/api/conesearch.php"


def _sexagesimal(text: str, hours: bool) -> float | None:
    parts = text.split()
    if len(parts) != 3:
        return None
    sign = -1.0 if parts[0].startswith("-") else 1.0
    value = abs(float(parts[0])) + float(parts[1]) / 60 + float(parts[2]) / 3600
    return sign * value * (15.0 if hours else 1.0)


def _skybot_frame(ra: float, dec: float, radius: float, mjd: float, cache: Path) -> list[dict]:
    """Known solar-system objects in the circle at one date (cached per date)."""
    path = cache / f"mjd{mjd:.5f}.json"
    cached = _read_json(path)
    if cached is not None:
        return cached
    r = requests.get(SKYBOT_URL, timeout=60, params={
        "-ep": f"{mjd + 2400000.5:.6f}", "-ra": f"{ra:.6f}", "-dec": f"{dec:+.6f}", "-rd": f"{radius / 60:.5f}",
        "-mime": "text", "-output": "basic", "-loc": "500", "-objFilter": "111"})
    if r.status_code not in (200, 204):
        raise CatalogError(f"SkyBoT: HTTP {r.status_code}")
    objects = []
    for line in r.text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        cols = [c.strip() for c in line.split("|")]
        if len(cols) < 6:
            continue
        o_ra, o_dec = _sexagesimal(cols[2], True), _sexagesimal(cols[3], False)
        if o_ra is None or o_dec is None:
            continue
        objects.append({"number": cols[0] if cols[0] not in ("", "-") else None, "name": cols[1],
                        "ra": o_ra, "dec": o_dec, "class": cols[4], "V": _float(cols[5])})
    _write_json(path, objects)
    return objects


def _skybot_stars(ra: float, dec: float, radius: float, frame_mjds: list[float], cache: Path) -> list[dict]:
    with ThreadPoolExecutor(6) as pool:  # (one query per frame date; SkyBoT answers in ~1-3 s)
        per_frame = list(pool.map(lambda mjd: _skybot_frame(ra, dec, radius, mjd, cache), frame_mjds))
    found: dict[str, dict] = {}
    for i, objects in enumerate(per_frame):
        for o in objects:
            star = found.setdefault(o["name"], {
                "id": f"({o['number']}) {o['name']}" if o["number"] else o["name"], "name": o["name"],
                "simbad_id": None, "simbad_type": None, "ra": o["ra"], "dec": o["dec"],
                "pm_ra": None, "pm_dec": None, "mag": {"V": o["V"]}, "info": {"Class": o["class"]},
                "positions": [None] * len(frame_mjds)})
            star["positions"][i] = [round(o["ra"], 6), round(o["dec"], 6)]
    return list(found.values())


# --------------------------------------------------------------------------- #
# A layer for one frame
# --------------------------------------------------------------------------- #
def layer(name: str, ra: float, dec: float, pixels: int, frame_mjds: list[float], cache_dir: Path) -> dict:
    """One layer's sources in the frame (see the module docstring). Raises KeyError
    for an unknown layer, CatalogError if the servers can't be reached."""
    info = LAYERS[name]
    radius = search_radius_arcmin(pixels)
    folder = cache_dir / "catalogs" / f"layer_{name}"
    coord = f"ra{ra:09.5f}_dec{dec:+09.5f}_p{pixels}"
    epoch = 2000.0  # VizieR's _RAJ2000/_DEJ2000 and SIMBAD's positions
    if info.get("special") == "skybot":
        stars = _skybot_stars(ra, dec, radius, frame_mjds, folder / coord)
        epoch = None
    else:
        path = folder / f"{coord}_r{MAX_ROWS}.json"
        stars = _read_json(path)
        if stars is None:
            stars = (_simbad_stars(ra, dec, radius) if info.get("special") == "simbad"
                     else _vizier_stars(info, ra, dec, radius))
            _write_json(path, stars)

    from backend import make_grid_wcs  # (backend imports overlays, not this module)
    grid = make_grid_wcs(ra, dec, pixels)
    if epoch is None:  # positions per frame: inside the frame on at least one frame
        inside = []
        for s in stars:
            pts = [p for p in s["positions"] if p]
            x, y = grid.world_to_pixel_values(np.array([p[0] for p in pts]), np.array([p[1] for p in pts]))
            inside.append(bool(((x >= -0.5) & (x <= pixels - 0.5) & (y >= -0.5) & (y <= pixels - 0.5)).any()))
    else:
        inside = overlays._inside(stars, overlays.MJD_J2000, grid, pixels, frame_mjds)
    shown = [s for s, ok in zip(stars, inside) if ok]
    band = info.get("sort") or ("V" if info.get("special") else None)

    def mag(s) -> float:
        m = s["mag"].get(band) if band else None
        return m if m is not None else 99.0

    out = []
    for s in sorted(shown, key=mag):
        pm = None if s["pm_ra"] is None or s["pm_dec"] is None else {
            "ra": round(s["pm_ra"], 2), "dec": round(s["pm_dec"], 2),
            "total": round(math.hypot(s["pm_ra"], s["pm_dec"]), 2)}
        entry = {"id": s["id"], "name": s["name"], "simbad_id": s["simbad_id"], "simbad_type": s["simbad_type"],
                 "reason": ["catalogue"] + (["fast"] if "positions" in s else []),
                 "ra": round(s["ra"], 6), "dec": round(s["dec"], 6), "mag": s["mag"], "pm_mas_yr": pm,
                 "info": {k: v for k, v in s.get("info", {}).items() if v not in (None, "")}}
        if "positions" in s:
            entry["positions"] = s["positions"]
        if s.get("artifact"):
            entry["artifact"] = True
        out.append(entry)
    limited = epoch is not None and len(stars) >= MAX_ROWS
    mags = [mag(s) for s in stars if mag(s) < 99]
    faintest = round(max(mags), 2) if limited and mags else None
    table = info.get("table")
    return {
        "label": info["label"],
        "source": ("IMCCE SkyBoT" if info.get("special") == "skybot" else "SIMBAD" if info.get("special") == "simbad"
                   else f"VizieR {table}") + (f" ({info['note']})" if info["note"] else ""),
        "group": info["group"], "shape": info["shape"], "color": info["color"],
        "epoch": epoch, "sort_mag": band, "mag_system": "Vega",
        "vizier_link": (VIZIER_LINK + urllib.parse.urlencode({"-source": table}) + f"&{info['id']}=") if table else None,
        "id_prefix": info.get("prefix"),
        "faintest": faintest,
        "has_artifacts": bool(info.get("artifacts")),
        "selection": (f"the {MAX_ROWS} brightest ({band}) in the search circle, down to {faintest:g}"
                      if faintest is not None else "every source in the frame"),
        "stars": out,
    }
