# PlanetX Finder: SPHEREx time-lapse viewer

NASA Space Apps Challenge 2026, "Planet X and SPHEREx".

Pick a spot on the sky (RA/Dec) and the app builds a time-lapse from every
SPHEREx visit to that spot. Difference images show what changed between the
visits, so moving objects such as asteroids, high proper-motion stars, or maybe
a distant planet stand out. Known stars from Gaia DR3 and CatWISE2020 are
placed on every frame at that frame's date.

## Requirements

- **Python 3.11 or newer** (tested with 3.14). The minimum comes from astropy
  ≥ 7.0; all other dependencies also run on 3.11. Check with `python3 --version`.
- **Linux or macOS** with bash. On Windows, use WSL.
- **Internet access.** Images and catalogues are downloaded when needed.
- **Disk space.** The download cache can grow up to 128 GiB and then trims
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

## Railway deployment

The root `Dockerfile` installs the Python dependencies during the image build
and runs `./start.sh` when the container starts. Railway can deploy it from the
GitHub `main` branch; enable auto deploys in the service's Source settings.

Set the service variable `PORT=5173` and set the public domain's target port to
`5173`. The container listens on `0.0.0.0` on that port. Visitors use the
Railway HTTPS domain without a port number, for example
`https://spaceappchallenge-production.up.railway.app/`.

The generated cache, videos, and `saves.csv` live in the container filesystem.
Railway replaces that filesystem on deployment, so those files do not persist
unless you attach persistent storage.

## Usage

1. Enter **Right Ascension** and **Declination**: decimal degrees
   (`269.45404`, `4.693388`) or sexagesimal (`17:57:48.97`, `+04:41:36.2`;
   with colons RA is in hours). You can also click the globe; turn that on in
   Settings.
2. **Settings:** speed (ms per frame), cutout size (50–500 px, 1 px = 6.15″),
   invert, show difference images.
3. **Submit.** A new spot can take a few minutes (downloads). The same request
   again is instant (cache).
4. **Player:** stop, play and step frame by frame. Click or drag on the image
   to zoom.
5. **Stars:** "Gaia stars", "WISE sources" and "Fast movers" in Settings. They
   follow the frame on screen; fast movers get their track and name. Click a star for
   details: magnitudes, distance, SIMBAD/VizieR links, and its position, date
   and movement in the frame on screen.

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
| `POST /api/save` | appends a result to `middleware/saves.csv` |

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
| Star overlays | Gaia DR3 (`I/355/gaiadr3`) and CatWISE2020 (`II/365/catwise`) via VizieR |
| Star names, types | SIMBAD |
| Known fast movers | SIMBAD (proper motion ≥ 200 mas/yr) |
| Earth texture | three.js example textures |

VizieR and SIMBAD are run by CDS (Strasbourg); the backend falls back to their
CfA (Harvard) mirror automatically.

## Cache

Everything downloaded is kept in `python-backend/Cache/`: image lists,
headers, image cutouts, star catalogues and finished results (an identical
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
- **`overlays` is `null` in meta.json:** VizieR/SIMBAD were unreachable. The
  frames are still fine; the next request tries again.

## Project structure

```
frontend/        index.html, app.js, style.css (UI, globe, player)
middleware/      server.py (HTTP server + API), middleware.py (runs the backend,
                 builds GIFs), clean_cache.py, saves.csv
python-backend/  backend.py (search, download, align, stack, render),
                 detectors.py (stacking), overlays.py (Gaia/CatWISE stars),
                 requirements.txt, setup_venv.sh, Cache/
start.sh         sets up the venv and starts server + cache cleaner
```

## Acknowledgements

SPHEREx data from NASA/IPAC Infrared Science Archive (IRSA). Gaia DR3 by ESA
(Gaia/DPAC). CatWISE2020 (Marocco et al. 2021, ApJS 253, 8). This research
used the SIMBAD database and the VizieR catalogue access tool, CDS,
Strasbourg, France. Earth texture from the three.js examples.
