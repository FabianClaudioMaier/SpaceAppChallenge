# PlanetX Finder: SPHEREx time-lapse viewer

NASA Space Apps Challenge 2026, "Planet X and SPHEREx".

Pick a spot on the sky (RA/Dec) and the app builds a time-lapse from every
SPHEREx visit to that spot. Difference images show what changed between the
visits, so moving objects such as asteroids, high proper-motion stars, or maybe
a distant planet stand out. Known objects from up to 25 catalogues (stars,
asteroids, galaxies, variables, X-ray and radio sources) are placed on every
frame at that frame's date.

## Requirements

- **Python 3.11 or newer** (tested with 3.14). The minimum comes from astropy
  ≥ 7.0; all other dependencies also run on 3.11. Check with `python3 --version`.
- **Linux or macOS** with bash. On Windows, use WSL.
- **Internet access.** Images and catalogues are downloaded when needed.
- **Disk space.** The download cache grows up to 128 GiB and then trims
  itself automatically.

## Install and run

```bash
git clone https://github.com/FabianClaudioMaier/SpaceAppChallenge.git
cd SpaceAppChallenge
./start.sh
```

Then open the provided link. Stop with `Ctrl+C`.

On the first start, `start.sh` creates `python-backend/.venv` with `python3`
and installs `python-backend/requirements.txt`, which takes a few minutes.
After that it only reinstalls when the requirements change.

- **Another port:** `PORT=8080 ./start.sh`
- **Your `python3` is older than 3.11:** create the venv with a newer Python
  once, then start as usual:
  ```bash
  python3.12 -m venv python-backend/.venv
  ./start.sh
  ```

The server only listens on `127.0.0.1` (your own computer).

## Usage

1. Enter **Right Ascension** and **Declination**: decimal degrees
   (`269.45404`, `4.693388`) or sexagesimal (`17:57:48.97`, `+04:41:36.2`;
   with colons RA is in hours), then **Submit**. A new spot can take a few
   minutes (downloads); the same request again takes about a second (cache).
2. **Player:** play and stop (`Space`), one frame back or forward (`,` / `.`).
   Under the image: the date of the frame on screen. Around it: RA and Dec
   rulers.
3. **On the image:** click a star for its details (magnitudes, distance,
   SIMBAD/VizieR links, its position and movement in the frame on screen).
   Click elsewhere or drag to zoom, down to a few pixels.
4. **Layers** (left panel): one row per catalogue.
   - eye: show or hide; **S**: solo (only the soloed layers are shown)
   - chevron or a click on the name: options (magnitude range, brightest or
     faintest N, labels, marker size, hide artifacts, reload)
   - **Select catalogue:** add one of 22 more catalogues; presets switch whole
     sets
   - a layer that failed to load gets a reload button (↻)
   - each kind of object has its own shape: star = stars and infrared,
     diamond = fast movers and nearby stars, cross = asteroids and comets,
     triangle = galaxies and quasars, circle = variable stars, square = X-ray,
     plus = radio, hexagon = SIMBAD names
5. **Is something moving?** **Yes** (or `B`), then drag a box around it (or
   don't), or **No**. **Save** adds a row to `middleware/saves/saves.csv`
   (Settings → Download saves); for "Yes" it also writes the GIFs, see below.
6. **Download GIF** (next to the player): the GIF on screen with the rulers,
   each frame's UTC date, the box, the coordinate grid and the shown layers
   burned in. Exported GIFs are enlarged to about 1000 px per frame (a 250 px
   field 4×) and named after the box centre (or the field) and the time, e.g.
   `J220253.5-565201_20261115T194337Z.gif` (+ `_diff.gif`).
7. **Settings:** invert, difference images, coordinate grid (near the poles,
   always or off), globe, speed (ms per frame), cutout size (50–1000 px,
   1 px = 6.15″), contrast and brightness (view only), **Reset to defaults**.
   The panels left and right can be resized by dragging their inner edge.

Example: Barnard's star, RA `269.45404`, Dec `4.693388`, 250 px. It creeps
about 1.7 px north per year across the frames.

## How it works

```
browser (frontend/)
  └─ POST /api/send-to-backend ─► middleware/server.py ─► middleware.py
                                                            └─ runs python-backend/backend.py
                                                               writes Cache/usr/<uuid>/ (JPGs + meta.json)
  ◄─ {uuid, json: meta.json} ──── GIFs built into middleware/video_cache/
  └─ GET /api/videos/<uuid>.gif, <uuid>_diff.gif
```

| Endpoint | |
|---|---|
| `POST /api/send-to-backend` | `{DEC, RA, PIXELS, MS}` → `{uuid, json}` (`json` = the job's `meta.json`) |
| `GET /api/videos/<uuid>.gif` | the frames; `<uuid>_diff.gif` the difference images |
| `GET /api/catalogs` | the catalogue layers that can be added |
| `POST /api/catalog` | `{name, ra, dec, pixels, frame_mjds, refresh}` → one layer's sources in the frame; `name` `builtin` = Gaia DR3, CatWISE2020 and the fast movers together |
| `POST /api/export` | the GIF on screen with everything shown (Download GIF) |
| `POST /api/save` | `{uuid, ra, dec, pixels, collections, verdict, box, frame_shown, view, frame_utc, detectors, grid}` → a row in `middleware/saves/saves.csv` + the exported GIFs |
| `GET /api/saves.csv` | the saves so far |

The frames never wait for the catalogues: the page fetches each layer when it
is switched on. Exported GIFs are drawn by worker processes
(`middleware/export.py`, one per CPU core but one), at most 10 exports at a
time. Backend jobs run one at a time; further requests wait for their turn.

The server only answers requests addressed to `127.0.0.1:<port>` / `localhost:<port>`,
and API calls only from its own page (JSON, no foreign `Origin`), so other web pages
open in the same browser can't use it.

GIFs not accessed for 30 minutes are deleted by `middleware/clean_cache.py`,
which `start.sh` starts along with the server.

### Backend on its own

```bash
cd python-backend
.venv/bin/python backend.py <DEC> <RA> <PIXELS> <UUID>
# e.g. .venv/bin/python backend.py 4.693388 269.45404 250 "$(python3 -c 'import uuid; print(uuid.uuid4())')"
```

Output goes to `Cache/usr/<UUID>/`: `frame_000.jpg …` (oldest first),
`diff/diff_000.jpg …` and `meta.json` (only on success). See
`python-backend/example_meta_barnard.json` for a full example.

| Exit code | Meaning |
|---|---|
| 0 | success |
| 1 | invalid arguments |
| 2 | no SPHEREx images cover this position |
| 3 | images exist but none could be downloaded (archive or network trouble) |
| 4 | anything else unexpected (e.g. out of memory for a huge PIXELS) |
| 5 | `Cache/usr/<UUID>/` already exists |

## Data sources

| What | From |
|---|---|
| SPHEREx images: which ones cover a spot | IRSA (NASA/IPAC) image search |
| SPHEREx images: the files | AWS bucket `nasa-irsa-spherex`, IRSA as fallback |
| Built-in layers | Gaia DR3 (`I/355/gaiadr3`) and CatWISE2020 (`II/365/catwise`) via VizieR |
| Star names, types | SIMBAD |
| Known fast movers | SIMBAD (proper motion ≥ 200 mas/yr) |
| More layers | 2MASS, unWISE, AllWISE, Pan-STARRS1, SkyMapper, VISTA VHS, galaxies, quasars, variables, X-ray and radio catalogues via VizieR (`python-backend/catalogs.py`) |
| Known asteroids and comets | IMCCE SkyBoT, one query per frame date |
| Earth texture | three.js example textures |

VizieR and SIMBAD are run by CDS (Strasbourg); the backend falls back to their
CfA (Harvard) mirror automatically.

## Cache

Everything downloaded is kept in `python-backend/Cache/`: image lists,
headers, image cutouts, catalogue layers and finished results (an identical
request is copied instead of rebuilt). Over 128 GiB, the entries used longest
ago are deleted. It is safe to delete the cache while nothing is running; it
is rebuilt on demand.

## Troubleshooting

- **"IRSA unavailable (HTTP 502)":** IRSA's image search is down. Spots
  searched before still work from the cache; new spots need IRSA. The
  frontend retries 3 times.
- **Frontend changes don't show:** hard-refresh (`Ctrl+Shift+R`); static
  files are served without cache headers.
- **Port already in use:** start on a port other than 5173, e.g.
  `PORT=5174 ./start.sh`
- **A layer shows "failed":** its catalogue server didn't answer. The frames
  are not affected; click ↻ to try again.

## Project structure

```
frontend/        index.html, app.js, style.css (UI, globe, player, zoom),
                 layers.js (layer list), star-overlay.js (markers, coordinate grid),
                 viewer.js (rulers, box, Save, Download GIF, panel widths)
middleware/      server.py (HTTP server + API), middleware.py (runs the backend,
                 builds GIFs, saves), export.py (exported GIFs), clean_cache.py,
                 saves/ (saves.csv + exported GIFs)
python-backend/  backend.py (search, download, align, stack, render),
                 detectors.py (stacking), overlays.py (built-in layers),
                 catalogs.py (more layers), requirements.txt, setup_venv.sh, Cache/
start.sh         sets up the venv and starts server + cache cleaner
```

## Acknowledgements

SPHEREx data from NASA/IPAC Infrared Science Archive (IRSA). Gaia DR3 by ESA
(Gaia/DPAC). CatWISE2020 (Marocco et al. 2021, ApJS 253, 8). This research
used the SIMBAD database and the VizieR catalogue access tool, CDS,
Strasbourg, France. Known asteroids and comets from SkyBoT (IMCCE,
Observatoire de Paris). Earth texture from the three.js examples.
