// Star overlays: the layers switched on in the layer list (layers.js) drawn over
// the GIF - all fetched on demand for the job on screen (POST /api/catalog,
// python-backend/catalogs.py; the built-in Gaia DR3, CatWISE2020 and SIMBAD fast
// movers come from python-backend/overlays.py). Each layer has its shape and
// colour; its options (layers.js) pick which of its stars are drawn.
//
// prepareCatalog moves every star to each frame's date (proper motion; or takes
// its position per frame, "positions", e.g. the known asteroids) and turns that
// into GIF pixels from the top left ("xy", null = outside the image), with the
// frame's grid (the same TAN projection as the backend's make_grid_wcs).
// After that this file only has to know which frame is on screen:
//   stopped / stepping  shownFrame (app.js)
//   playing             frameShowingNow() (app.js), from the time since the GIF started
//   not known yet       (frames still being decoded, or no ImageDecoder in this
//                       browser) every star at its average position
// Fast movers also get their whole track as a dashed line.
//
// The zoom (app.js openZoom) gets the same markers at its own scale, on the same frame.
//
// The markers are drawn on a <canvas>, not as SVG elements: with every catalogue
// on there are 10,000+ of them, and as SVG (5 elements each, a CSS drop-shadow on
// every marker) each frame change took ~0.5 s. On the canvas each marker is a
// copy of a small pre-drawn image (markerSprite). Hover and click find the star under
// the mouse by distance (starAt), the hover box replaces the SVG <title>.
//
// Uses app.js's top-level variables (classic scripts): image, currentFrameIndex,
// loadGifFrames, currentVideoUuid, zoomDialog, zoomView, zoomRegion; viewer.js's
// activeTool; and layers.js's visibleLayers, anyLayerOn, layersVersion, traceShape.

const starOverlay = document.querySelector("#star-overlay");
const starInfo = document.querySelector("#star-info");

const SVG_NS = "http://www.w3.org/2000/svg";
const STAR_MARKER_PX = 6;  // marker radius on screen, whatever the frame size
const STAR_LABEL_PX = 11;  // font size of the fast movers' names on screen
// Marker size by brightness, like a star chart: the STAR_SIZE_FULL_RANK brightest
// of a catalogue full size, down to STAR_SIZE_FAINTEST x that for its faintest -
// with thousands of sources shown, same-size markers covered the whole image.
// Fast movers always full size.
const STAR_SIZE_FULL_RANK = 20;
const STAR_SIZE_FAINTEST = 0.4;
const STAR_SHAPE_MIN_SIZE = 0.7; // the star shape (★) smaller than this is a blob

// Per star of a layer: its marker size (0..1, see STAR_SIZE_FULL_RANK; "same": all 1)
function markerSizes(catalog, stars, mode = "brightness") {
  if (mode === "same") return new Map(stars.map((s) => [s, 1]));
  const band = catalog.sort_mag;
  const mags = stars.map((s) => s.mag?.[band]).filter((m) => m !== null && m !== undefined).sort((a, b) => a - b);
  const bright = mags[Math.min(STAR_SIZE_FULL_RANK, mags.length - 1)];
  const faint = mags[mags.length - 1];
  return new Map(stars.map((s) => {
    const m = s.mag?.[band];
    if (s.reason.includes("fast") || !mags.length || faint <= bright) return [s, 1];
    if (m === null || m === undefined) return [s, STAR_SIZE_FAINTEST];
    const t = Math.min(Math.max((m - bright) / (faint - bright), 0), 1);
    return [s, 1 - (1 - STAR_SIZE_FAINTEST) * t];
  }));
}

let starOverlayKey = "";  // what the page's markers were collected for (again when it changes)

// localStorage.videoMetadata parsed once per job (it is ~130 kB, and this is
// asked every animation frame); app.js stores it before switching to a new job.
let parsedMetadata = null;
let parsedForUuid = null;

// The metadata of the job whose GIF is showing (null if there is none / another job's)
function shownMetadata() {
  if (parsedForUuid !== currentVideoUuid) {
    parsedForUuid = currentVideoUuid;
    try {
      parsedMetadata = jobMetadataInMemory?.uuid === currentVideoUuid
        ? jobMetadataInMemory.metadata // (complete even if localStorage had no room for the stars)
        : JSON.parse(localStorage.getItem("videoMetadata") ?? "null");
      prepareOverlays(parsedMetadata);
    } catch {
      parsedMetadata = null;
    }
  }
  const metadata = parsedMetadata;
  const src = image.currentSrc || image.src;
  if (!metadata?.frames || !metadata.uuid || metadata.uuid !== currentVideoUuid) return null;
  if (!src.includes(metadata.uuid) || !image.naturalWidth) return null;
  if (image.naturalWidth !== metadata.pixels) return null; // not the size the positions are for
  return metadata;
}

function svgElement(name, attributes = {}) {
  const element = document.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, value);
  return element;
}

function formatNumber(value, digits = 2) {
  return value === null || value === undefined ? "–" : Number(value).toFixed(digits);
}

function starTitle(star, catalogLabel) {
  const name = star.name ?? star.id;
  const mags = Object.entries(star.mag)
    .filter(([, v]) => v !== null)
    .map(([band, v]) => `${band} ${formatNumber(v)}`)
    .join(", ");
  const motion = star.pm_mas_yr ? `, moves ${formatNumber(star.pm_mas_yr.total / 1000, 3)}″/yr` : "";
  return `${name} (${catalogLabel}) – ${mags}${motion}`;
}

// ---- The info card's "In this frame" part, updated whenever the frame changes ----
let selectedStar = null; // { star, catalog, uuid, cells: { date, pixel, sky, moved } }

const MJD_J2000 = 51544.5;

// Position at a date (MJD): the catalogue position moved with the proper motion,
// the same straight line the backend uses for "xy" (python-backend/overlays.py _move).
function skyPositionAt(star, catalog, mjd) {
  const years = (mjd - (MJD_J2000 + (catalog.epoch - 2000) * 365.25)) / 365.25;
  const pmRa = star.pm_mas_yr?.ra ?? 0; // mas/yr, includes cos(dec)
  const pmDec = star.pm_mas_yr?.dec ?? 0;
  const cosDec = Math.max(Math.cos((star.dec * Math.PI) / 180), 1e-6);
  return {
    ra: (((star.ra + (pmRa * years) / 3.6e6 / cosDec) % 360) + 360) % 360,
    dec: star.dec + (pmDec * years) / 3.6e6,
  };
}

// Sky position -> [x, y] in GIF pixels from the frame's top left: the TAN
// projection of the backend's make_grid_wcs (north up, east left, field centre in
// the middle; viewer.js frameToSky is the way back). null if behind the tangent point.
function skyToFrame(ra, dec, metadata) {
  const rad = Math.PI / 180;
  const s = (metadata.pixel_scale_arcsec / 3600) * rad; // radians per pixel
  const n = metadata.pixels;
  const d = dec * rad;
  const d0 = metadata.dec * rad;
  const dra = (ra - metadata.ra) * rad;
  const cosc = Math.sin(d0) * Math.sin(d) + Math.cos(d0) * Math.cos(d) * Math.cos(dra);
  if (cosc <= 0) return null;
  const xi = (Math.cos(d) * Math.sin(dra)) / cosc; // towards east
  const eta = (Math.cos(d0) * Math.sin(d) - Math.sin(d0) * Math.cos(d) * Math.cos(dra)) / cosc; // north
  return [n / 2 - xi / s, n / 2 - eta / s];
}

// ---- The coordinate grid: lines of constant RA and Dec over the frame ----
// Settings "Coordinate grid": "poles" (default: where the edge rulers don't tell
// the whole story - the pole in the frame, or the RA across the frame spanning more
// than GRID_POLES_RA_SPAN_DEG, so RA lines visibly tilt and Dec lines bend),
// "always", "off". In GIF pixels, like the stars' "xy": drawn by drawStarView (the
// page and the zoom), sent along with "Download GIF" and Save (middleware/export.py).
// Its values are written on the lines only with the pole in the frame, or with the
// switch "Grid labels away from the pole" (localStorage "gridLabels").
const GRID_POLES_RA_SPAN_DEG = 2;
const GRID_MAX_LINES = { dec: 10, ra: 9 }; // at most this many lines each way (steps as the rulers')
const GRID_LINE_POINTS = 400;
const GRID_LABEL_PX = 11;
let gridMode = localStorage.getItem("grid") ?? "poles";
let gridLabelsAway = localStorage.getItem("gridLabels") === "true";
let gridCache = { key: "", grid: null };
const gridModeInput = document.querySelector("#grid-mode");
gridModeInput.value = gridMode;
gridModeInput.addEventListener("change", () => {
  gridMode = gridModeInput.value;
  localStorage.setItem("grid", gridMode);
  buildStarOverlay(); // (its key includes the mode; the zoom gets it when opened next)
});
const gridLabelsButton = document.querySelector("#grid-labels");
gridLabelsButton.setAttribute("aria-checked", String(gridLabelsAway));
gridLabelsButton.addEventListener("click", () => {
  gridLabelsAway = !gridLabelsAway;
  gridLabelsButton.setAttribute("aria-checked", String(gridLabelsAway));
  if (gridLabelsAway) localStorage.setItem("gridLabels", "true");
  else localStorage.removeItem("gridLabels");
  buildStarOverlay();
});

// {lines: [[[x, y], ...]], labels: [[x, y, text]], dec_step, ra_step} or null (no grid)
function coordinateGrid(metadata) {
  if (!metadata || gridMode === "off") return null;
  const key = `${metadata.uuid}|${gridMode}|${gridLabelsAway}`;
  if (gridCache.key !== key) gridCache = { key, grid: buildGrid(metadata) };
  return gridCache.grid;
}

function buildGrid(metadata) {
  const n = metadata.pixels;
  const ra0 = metadata.ra;
  const samples = [];
  for (let i = 0; i <= 30; i++) {
    for (let j = 0; j <= 30; j++) samples.push(frameToSky((i / 30) * n, (j / 30) * n, metadata)); // (viewer.js)
  }
  const poleDec = metadata.dec >= 0 ? 90 : -90;
  const pole = skyToFrame(0, poleDec, metadata);
  const poleInside = Boolean(pole) && pole[0] >= 0 && pole[0] <= n && pole[1] >= 0 && pole[1] <= n;
  let decMin = Math.min(...samples.map((s) => s[1]));
  let decMax = Math.max(...samples.map((s) => s[1]));
  if (poleInside && poleDec > 0) decMax = 90;
  if (poleInside && poleDec < 0) decMin = -90;
  const raRel = samples.map((s) => ((s[0] - ra0 + 540) % 360) - 180); // around the centre: no jump at 0/360
  const raMin = Math.min(...raRel);
  const raMax = Math.max(...raRel);
  const raSpan = poleInside ? 360 : raMax - raMin;
  if (gridMode === "poles" && !poleInside && raSpan <= GRID_POLES_RA_SPAN_DEG) return null;

  // the rulers' steps (viewer.js RULER_STEPS_DEG): coarser while too many lines, finer while < 3
  const step = (span, first, most) => {
    let i = first;
    while (i < RULER_STEPS_DEG.length - 1 && span / RULER_STEPS_DEG[i] > most) i++;
    while (i > 0 && span / RULER_STEPS_DEG[i] < 3) i--;
    return RULER_STEPS_DEG[i];
  };
  const decStep = step(decMax - decMin, RULER_STEPS_DEG.indexOf(PAGE_RULER_STEP_DEG), GRID_MAX_LINES.dec);
  const raStep = step(raSpan, 0, GRID_MAX_LINES.ra);
  const lines = [];
  const labels = [];
  const round = (v) => Math.round(v * 100) / 100;
  const near = (p) => p && p[0] >= -1 && p[0] <= n + 1 && p[1] >= -1 && p[1] <= n + 1;
  // a line's parts inside the frame -> lines; its points (null = outside) for the label
  const addLine = (points) => {
    let part = [];
    for (const p of points) {
      if (near(p)) part.push([round(p[0]), round(p[1])]);
      else {
        if (part.length > 1) lines.push(part);
        part = [];
      }
    }
    if (part.length > 1) lines.push(part);
  };
  // labels keep off the edges (the right one leaves room for the text)
  const labelArea = (p) => p && p[0] > 0.02 * n && p[0] < 0.88 * n && p[1] > 0.05 * n && p[1] < 0.97 * n;

  // constant Dec (around the pole: circles), labelled down and to the right of the
  // pole between two RA lines, else near the right edge
  const raFrom = poleInside ? 0 : ra0 + raMin - 0.1 * raSpan;
  const raTo = poleInside ? 360 : ra0 + raMax + 0.1 * raSpan;
  const decDigits = decimalsOf(decStep); // (viewer.js)
  for (let k = Math.ceil(decMin / decStep); k * decStep < decMax - 1e-9; k++) {
    const dec = k * decStep;
    if (Math.abs(dec) >= 90) continue;
    const points = Array.from({ length: GRID_LINE_POINTS + 1 },
      (_, i) => skyToFrame(raFrom + ((raTo - raFrom) * i) / GRID_LINE_POINTS, dec, metadata));
    addLine(points);
    let best = null;
    let bestScore = Infinity;
    for (const p of points) {
      if (!labelArea(p)) continue;
      const score = poleInside
        ? Math.abs(((((Math.atan2(p[0] - pole[0], p[1] - pole[1]) * 180) / Math.PI - 22.5) % 360) + 540) % 360 - 180)
        : Math.abs(p[0] - 0.86 * n);
      if (score < bestScore) [best, bestScore] = [p, score];
    }
    if (best) labels.push([round(best[0]), round(best[1]), `${dec >= 0 ? "+" : "−"}${Math.abs(dec).toFixed(decDigits)}°`]);
  }

  // constant RA (around the pole: spokes), labelled where they come in from the edge
  const raDigits = decimalsOf(raStep);
  const raValues = [];
  if (poleInside) for (let a = 0; a < 360 - 1e-9; a += raStep) raValues.push(a);
  else for (let k = Math.ceil((ra0 + raMin) / raStep); k * raStep < ra0 + raMax; k++) raValues.push(k * raStep);
  const decRange = decMax - decMin;
  const decFrom = poleDec > 0 ? Math.max(-90, decMin - 0.1 * decRange) : (poleInside ? -89.99999 : decMin - 0.1 * decRange);
  const decTo = poleDec > 0 ? (poleInside ? 89.99999 : Math.min(90, decMax + 0.1 * decRange)) : Math.min(90, decMax + 0.1 * decRange);
  for (const value of raValues) {
    const ra = ((value % 360) + 360) % 360;
    const points = Array.from({ length: GRID_LINE_POINTS + 1 },
      (_, i) => skyToFrame(ra, decFrom + ((decTo - decFrom) * i) / GRID_LINE_POINTS, metadata));
    addLine(points);
    // its far end from the pole: the low |Dec| end of the list (north) or the high end (south)
    const order = poleDec > 0 ? points : [...points].reverse();
    const first = order.find(labelArea);
    if (first) labels.push([round(first[0]), round(first[1]), `${ra.toFixed(raDigits)}°`]);
  }
  return { lines, labels: poleInside || gridLabelsAway ? labels : [], dec_step: decStep, ra_step: raStep };
}

// Lines, then labels (one that would overlap another or leave the canvas is left out);
// drawn last, in front of the markers
function drawGrid(context, view) {
  const { grid, scale, offsetX, offsetY } = view;
  context.beginPath();
  for (const line of grid.lines) {
    line.forEach(([x, y], i) => {
      if (i === 0) context.moveTo(x * scale + offsetX, y * scale + offsetY);
      else context.lineTo(x * scale + offsetX, y * scale + offsetY);
    });
  }
  context.lineWidth = 1;
  context.strokeStyle = "rgba(255, 255, 255, 0.4)";
  context.stroke();
  context.font = `${GRID_LABEL_PX}px "DM Sans", sans-serif`;
  context.lineWidth = 3;
  context.strokeStyle = "#000";
  context.fillStyle = "white";
  const placed = [];
  for (const [x, y, text] of grid.labels) {
    const left = x * scale + offsetX + 3;
    const baseline = y * scale + offsetY - 3;
    const box = [left - 2, baseline - GRID_LABEL_PX, left + context.measureText(text).width + 2, baseline + 3];
    if (box[0] < 0 || box[1] < 0 || box[2] > view.width || box[3] > view.height) continue;
    if (placed.some((b) => box[0] < b[2] && b[0] < box[2] && box[1] < b[3] && b[1] < box[3])) continue;
    placed.push(box);
    context.strokeText(text, left, baseline);
    context.fillText(text, left, baseline);
  }
}

// Every star's position per frame ("xy") and its SIMBAD / VizieR links, once per
// job (the backend sends neither: for thousands of stars that would be megabytes).
// (Only results from before the layers were fetched on demand have "overlays".)
function prepareOverlays(metadata) {
  if (!metadata?.overlays || !metadata.frames) return;
  for (const catalog of Object.values(metadata.overlays)) {
    if (catalog) prepareCatalog(catalog, metadata);
  }
}

// The same for one layer (layers.js calls it for the extra ones)
function prepareCatalog(catalog, metadata) {
  const n = metadata.pixels;
  for (const star of catalog.stars ?? []) {
    star.xy ??= metadata.frames.map((frame, i) => {
      const sky = skyAt(star, catalog, i, frame.mjd_mean);
      const p = sky && skyToFrame(sky.ra, sky.dec, metadata);
      return p && p[0] >= 0 && p[0] <= n && p[1] >= 0 && p[1] <= n
        ? [Math.round(p[0] * 100) / 100, Math.round(p[1] * 100) / 100] : null;
    });
    star.links ??= {
      simbad: star.simbad_id
        ? `https://simbad.cds.unistra.fr/simbad/sim-id?Ident=${encodeURIComponent(star.simbad_id)}`
        : `https://simbad.cds.unistra.fr/simbad/sim-coo?Coord=${encodeURIComponent(`${star.ra} ${star.dec}`)}` +
          "&Radius=5&Radius.unit=arcsec",
      vizier: catalog.vizier_link && star.id.startsWith(catalog.id_prefix ?? "")
        ? catalog.vizier_link + encodeURIComponent(star.id.slice((catalog.id_prefix ?? "").length))
        : null,
    };
  }
}

// A star's sky position on frame i (at that frame's date): its own position for
// that frame if it has them (known asteroids; null = not there), else the
// catalogue position moved with the proper motion
function skyAt(star, catalog, i, mjd) {
  if (star.positions) {
    const p = star.positions[i];
    return p ? { ra: p[0], dec: p[1] } : null;
  }
  return skyPositionAt(star, catalog, mjd);
}

// "north-west" etc. for a move in image pixels (north up, east left - see make_grid_wcs)
function compassDirection(dx, dy) {
  const names = ["north", "north-west", "west", "south-west", "south", "south-east", "east", "north-east"];
  // angle from north, counter-clockwise towards west (= right in the image)
  const angle = (Math.atan2(dx, -dy) * 180) / Math.PI;
  return names[((Math.round(angle / 45) % 8) + 8) % 8];
}

function updateStarInfoFrame() {
  if (!selectedStar || starInfo.hidden) return;
  const { star, catalog, cells } = selectedStar;
  const metadata = shownMetadata();
  const frame = pageStars.frame;
  if (!metadata || frame === null || frame === undefined) {
    cells.date.textContent = "– (frame not known yet; stop the GIF for exact values)";
    for (const key of ["pixel", "sky", "moved"]) cells[key].textContent = "–";
    return;
  }
  const info = metadata.frames[frame];
  cells.date.textContent =
    `${info.timestamp.slice(0, 16).replace("T", " ")} UTC (frame ${frame + 1} of ${metadata.frames.length})`;
  const sky = skyAt(star, catalog, frame, info.mjd_mean);
  cells.sky.textContent = sky ? `RA ${formatNumber(sky.ra, 5)}°, Dec ${formatNumber(sky.dec, 5)}°` : "–";
  const here = star.xy[frame];
  if (!here) {
    cells.pixel.textContent = "outside the image in this frame";
    cells.moved.textContent = "–";
    return;
  }
  cells.pixel.textContent = `x ${formatNumber(here[0], 1)}, y ${formatNumber(here[1], 1)} px (from top left)`;
  const firstIndex = star.xy.findIndex(Boolean);
  const first = star.xy[firstIndex];
  const dx = here[0] - first[0];
  const dy = here[1] - first[1];
  const pixels = Math.hypot(dx, dy);
  cells.moved.textContent = frame === firstIndex
    ? "– (first frame it is in)"
    : `${formatNumber(pixels, 2)} px = ${formatNumber(pixels * metadata.pixel_scale_arcsec, 1)}″` +
      (pixels >= 0.01 ? ` ${compassDirection(dx, dy)}` : "") +
      ` since ${metadata.frames[firstIndex].timestamp.slice(0, 10)}`;
}

function showStarInfo(star, catalog) {
  const rows = [
    ["Catalogue", catalog.label],
    ["ID", star.id],
    ["Type (SIMBAD)", star.simbad_type],
    ["Magnitudes", Object.entries(star.mag).filter(([, v]) => v !== null)
      .map(([band, v]) => `${band} ${formatNumber(v)}`).join(" · ")],
    ["Proper motion", star.pm_mas_yr
      ? `${formatNumber(star.pm_mas_yr.total, 1)} mas/yr (RA ${formatNumber(star.pm_mas_yr.ra, 1)}, Dec ${formatNumber(star.pm_mas_yr.dec, 1)})`
      : null],
    ["Distance", star.distance_ly ? `${formatNumber(star.distance_ly, 2)} light years` : null],
    // per catalogue: variable type, redshift, X-ray / radio flux, artifact flags, ...
    ...Object.entries(star.info ?? {}),
    ["Catalogue position", `RA ${formatNumber(star.ra, 5)}°, Dec ${formatNumber(star.dec, 5)}°` +
      (catalog.epoch ? ` (epoch ${catalog.epoch})` : " (first frame it is in)")],
    ["Selected as", star.reason.join(" + ")],
  ].filter(([, value]) => value !== null && value !== undefined && value !== "");

  starInfo.replaceChildren();
  const close = document.createElement("button");
  close.type = "button";
  close.className = "star-info-close";
  close.setAttribute("aria-label", "Close star info");
  close.textContent = "×";
  close.addEventListener("click", () => { starInfo.hidden = true; });
  const heading = document.createElement("h2");
  heading.textContent = star.name ?? star.id;
  const list = document.createElement("dl");
  for (const [term, value] of rows) {
    const dt = document.createElement("dt");
    dt.textContent = term;
    const dd = document.createElement("dd");
    dd.textContent = value;
    list.append(dt, dd);
  }
  // Live part: filled by updateStarInfoFrame for the frame on screen
  const frameHeading = document.createElement("h3");
  frameHeading.textContent = "In this frame";
  const frameList = document.createElement("dl");
  const cells = {};
  for (const [key, term] of [["date", "Date"], ["pixel", "Position in image"],
    ["sky", "Sky position then"], ["moved", "Moved"]]) {
    const dt = document.createElement("dt");
    dt.textContent = term;
    cells[key] = document.createElement("dd");
    frameList.append(dt, cells[key]);
  }
  const links = document.createElement("p");
  links.className = "star-info-links";
  for (const [label, url] of [["SIMBAD", star.links.simbad], ["VizieR", star.links.vizier]]) {
    if (!url) continue; // SIMBAD-only objects have no VizieR entry
    const link = document.createElement("a");
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = label;
    links.append(link);
  }
  starInfo.append(close, heading, list, frameHeading, frameList, links);
  starInfo.hidden = false;
  selectedStar = { star, catalog, uuid: shownMetadata()?.uuid, cells };
  updateStarInfoFrame();
}

// ---- Drawing: a "view" is one canvas with its markers (the page's or the zoom's) ----
// Line widths on screen, per kind of marker (as the SVG version's CSS had them)
const STAR_KINDS = [ // drawn in this order: faint ones at the bottom
  { kind: "faint", lineWidth: 1, alpha: 0.75 },
  { kind: "normal", lineWidth: 1.5, alpha: 1 },
  { kind: "fast", lineWidth: 2.5, alpha: 1 },
];
const STAR_OUTLINE = "rgba(0, 0, 0, 0.65)"; // dark edge under each line, so it shows on bright stars
const STAR_TRACK = "rgba(255, 255, 255, 0.75)";
// A track shorter than this on screen isn't drawn: a fast mover that moves only a
// fraction of a pixel over the frames showed it as a white dot inside its marker
const TRACK_MIN_PX = 6;
const STAR_HOVER = "white";
// Markers bigger than this (radius, screen px) are stroked directly instead of copied
// from a pre-drawn image: in a zoom onto a few pixels markers get hundreds of px big,
// and an image per size made a 2 px zoom take over a second to open
const SPRITE_MAX_RADIUS_PX = 48;

function createStarView(canvas, onPick) {
  return {
    canvas, onPick,
    layers: [],      // [{ style, kinds: { faint: [], normal: [], fast: [] } }], bottom first
    markers: [],     // all of them, in drawing order (last = on top)
    scale: 1,        // screen px per GIF px
    offsetX: 0,      // screen position of GIF pixel (0, 0)
    offsetY: 0,
    width: 0,        // canvas size on screen (CSS px)
    height: 0,
    frame: undefined, // frame index drawn (null = average positions)
    grid: null,      // the coordinate grid (coordinateGrid), or null
    hovered: null,   // the marker under the mouse
    pointer: null,   // last mouse position over it { x, y } (client px), to re-check hover per frame
    dirty: false,    // redraw on the next animation frame
  };
}

// The markers of the switched-on layers: { star, catalog, style, size, kind, average, track, label }.
// Positions stay in GIF pixels - the view's scale and offset put them on its canvas.
function setStarViewMarkers(view, metadata) {
  view.layers = [];
  view.markers = [];
  view.hovered = null;
  view.dirty = true;
  if (!metadata || !anyLayerOn()) return;
  for (const { catalog, stars, options, style } of visibleLayers(metadata)) { // (layers.js)
    const layer = { style, kinds: { faint: [], normal: [], fast: [] } };
    const sizes = markerSizes(catalog, stars, options.size);
    // brightest drawn last = on top (and found first by starAt)
    for (const star of [...stars].reverse()) {
      const points = star.xy.filter(Boolean);
      if (!points.length) continue;
      const fast = star.reason.includes("fast");
      const size = style.shape === "star" ? Math.max(sizes.get(star), STAR_SHAPE_MIN_SIZE) : sizes.get(star);
      const marker = {
        star, catalog, style, size,
        kind: fast ? "fast" : size < 0.75 ? "faint" : "normal",
        average: [
          points.reduce((sum, p) => sum + p[0], 0) / points.length,
          points.reduce((sum, p) => sum + p[1], 0) / points.length,
        ],
        track: fast && points.length > 1 ? points : null,
        label: star.name && (options.labels === "named" || (options.labels === "fast" && fast)) ? star.name : null,
      };
      layer.kinds[marker.kind].push(marker);
    }
    view.layers.push(layer);
  }
  // drawing order for hit testing: per layer faint, normal, fast (as drawStarView)
  for (const layer of view.layers) {
    for (const { kind } of STAR_KINDS) view.markers.push(...layer.kinds[kind]);
  }
}

// Marker radius on screen for size 1; a marker always surrounds at least a 3 px star
function markerRadius(view) {
  return Math.max(STAR_MARKER_PX, 1.5 * view.scale);
}

// [x, y] on the view's canvas on its frame, or null (outside the image then)
function markerOnScreen(view, marker) {
  const p = view.frame === null || view.frame === undefined ? marker.average : marker.star.xy[view.frame];
  return p ? [p[0] * view.scale + view.offsetX, p[1] * view.scale + view.offsetY] : null;
}

// A marker drawn once (dark edge, then the layer's colour), centred in a small
// canvas: { canvas, size, half } in CSS px. Radii are rounded to 1/4 px, so a
// layer needs a few dozen of them at most.
const markerSprites = new Map();

function markerSprite(style, { kind, lineWidth, alpha }, radius, ratio) {
  const r = Math.round(radius * 4) / 4;
  const key = `${style.shape}|${style.color}|${kind}|${r}|${ratio}`;
  let sprite = markerSprites.get(key);
  if (sprite) return sprite;
  if (markerSprites.size > 4000) markerSprites.clear(); // (many sizes over time: start again)
  const half = Math.ceil(r * 1.1 + lineWidth + 2); // the diamond reaches 1.06 r
  const size = 2 * half;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = Math.ceil(size * ratio);
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.lineJoin = "round";
  context.globalAlpha = alpha;
  const path = new Path2D();
  traceShape(path, style.shape, half, half, r); // (layers.js)
  context.lineWidth = lineWidth + 2;
  context.strokeStyle = STAR_OUTLINE;
  context.stroke(path);
  context.lineWidth = lineWidth;
  context.strokeStyle = style.color;
  context.stroke(path);
  sprite = { canvas, size, half };
  markerSprites.set(key, sprite);
  return sprite;
}

function drawStarView(view) {
  view.dirty = false;
  const { canvas } = view;
  const ratio = window.devicePixelRatio || 1;
  const pixelWidth = Math.max(1, Math.round(view.width * ratio));
  const pixelHeight = Math.max(1, Math.round(view.height * ratio));
  if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
    canvas.width = pixelWidth;
    canvas.height = pixelHeight;
  }
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, view.width, view.height);
  context.lineJoin = "round";
  const base = markerRadius(view);

  // the fast movers' whole tracks, dashed, under the markers
  context.beginPath();
  for (const marker of view.markers) {
    if (!marker.track) continue;
    const [first, last] = [marker.track[0], marker.track[marker.track.length - 1]];
    if (Math.hypot(last[0] - first[0], last[1] - first[1]) * view.scale < TRACK_MIN_PX) continue;
    marker.track.forEach(([x, y], i) => {
      const sx = x * view.scale + view.offsetX;
      const sy = y * view.scale + view.offsetY;
      if (i === 0) context.moveTo(sx, sy); else context.lineTo(sx, sy);
    });
  }
  context.setLineDash([3, 3]);
  context.lineWidth = 1;
  context.strokeStyle = STAR_TRACK;
  context.stroke();
  context.setLineDash([]);

  // the markers: copies of a small pre-drawn image per shape, colour, kind and size
  // (stroking 10,000 shapes per frame took ~150 ms; copying them a few ms). Only
  // the ones on the canvas (in a zoom most are outside); very big ones stroked directly.
  const snap = (value) => Math.round(value * ratio) / ratio; // whole device pixels: sharp copies
  for (const layer of view.layers) {
    for (const kindStyle of STAR_KINDS) {
      const big = new Path2D();
      let anyBig = false;
      for (const marker of layer.kinds[kindStyle.kind]) {
        const at = markerOnScreen(view, marker);
        if (!at) continue;
        const r = base * marker.size;
        const reach = r * 1.1 + kindStyle.lineWidth + 2;
        if (at[0] < -reach || at[1] < -reach || at[0] > view.width + reach || at[1] > view.height + reach) continue;
        if (r > SPRITE_MAX_RADIUS_PX) {
          traceShape(big, layer.style.shape, at[0], at[1], r); // (layers.js)
          anyBig = true;
          continue;
        }
        const sprite = markerSprite(layer.style, kindStyle, r, ratio);
        context.drawImage(sprite.canvas, snap(at[0]) - sprite.half, snap(at[1]) - sprite.half, sprite.size, sprite.size);
      }
      if (anyBig) { // (like markerSprite draws them)
        context.globalAlpha = kindStyle.alpha;
        context.lineWidth = kindStyle.lineWidth + 2;
        context.strokeStyle = STAR_OUTLINE;
        context.stroke(big);
        context.lineWidth = kindStyle.lineWidth;
        context.strokeStyle = layer.style.color;
        context.stroke(big);
        context.globalAlpha = 1;
      }
    }
  }

  const hovered = view.hovered && markerOnScreen(view, view.hovered);
  if (hovered) {
    const path = new Path2D();
    traceShape(path, view.hovered.style.shape, hovered[0], hovered[1], base * view.hovered.size);
    context.lineWidth = 4.5;
    context.strokeStyle = STAR_OUTLINE;
    context.stroke(path);
    context.lineWidth = 2.5;
    context.strokeStyle = STAR_HOVER;
    context.stroke(path);
  }

  // names, right above the marker (left of it where they would leave the canvas)
  context.font = `${STAR_LABEL_PX}px "DM Sans", sans-serif`;
  context.textBaseline = "alphabetic";
  context.lineWidth = 3;
  context.strokeStyle = "#000";
  context.fillStyle = "white";
  for (const marker of view.markers) {
    if (!marker.label) continue;
    const at = markerOnScreen(view, marker);
    if (!at) continue;
    const r = base * marker.size;
    const textWidth = context.measureText(marker.label).width;
    let x = at[0] + r * 1.5;
    if (x + textWidth > view.width) x = at[0] - r * 1.5 - textWidth;
    const y = at[1] - r * 1.2;
    context.strokeText(marker.label, x, y);
    context.fillText(marker.label, x, y);
  }

  if (view.grid) drawGrid(context, view); // (in front of the markers and names)
}

// The marker under a mouse position (client px): the nearest within its hit
// radius (a bit bigger than the marker), the top one if they are equally near
function starAt(view, clientX, clientY) {
  if (!view.markers.length || view.canvas.hidden) return null;
  const rect = view.canvas.getBoundingClientRect();
  const x = clientX - rect.left;
  const y = clientY - rect.top;
  const base = markerRadius(view);
  let best = null;
  let bestDistance = Infinity;
  for (const marker of view.markers) {
    const at = markerOnScreen(view, marker);
    if (!at) continue;
    const distance = Math.hypot(at[0] - x, at[1] - y);
    if (distance <= Math.max(base * marker.size * 1.6, base * 0.8) && distance <= bestDistance) {
      best = marker;
      bestDistance = distance;
    }
  }
  return best;
}

// ---- Hover box (replaces the SVG markers' <title>) ----
const starTooltip = document.createElement("div");
starTooltip.className = "star-tooltip";
starTooltip.setAttribute("role", "tooltip");
starTooltip.hidden = true;
document.body.append(starTooltip);

function setHovered(view, marker, clientX, clientY) {
  if (marker !== view.hovered) {
    view.hovered = marker;
    view.dirty = true;
  }
  view.canvas.parentElement.classList.toggle("over-star", Boolean(marker));
  if (!marker) {
    starTooltip.hidden = true;
    return;
  }
  // in the zoom the box must be inside the dialog (the top layer covers the page)
  const host = zoomDialog.open ? zoomDialog : document.body;
  if (starTooltip.parentElement !== host) host.append(starTooltip);
  starTooltip.textContent = starTitle(marker.star, marker.catalog.label);
  starTooltip.hidden = false;
  const left = Math.min(clientX + 14, window.innerWidth - starTooltip.offsetWidth - 8);
  const top = clientY + 18 + starTooltip.offsetHeight > window.innerHeight
    ? clientY - 10 - starTooltip.offsetHeight : clientY + 18;
  starTooltip.style.left = `${Math.max(8, left)}px`;
  starTooltip.style.top = `${Math.max(8, top)}px`;
}

// A press that moves less than this (both ways) is a click, not a drag (app.js uses it too)
const STAR_CLICK_MOVE_PX = 6;

// Mouse handling for a view: canPick() says whether stars take the mouse now.
// The canvas itself lets the mouse through (pointer-events: none), so a press
// still reaches the image below (zoom / box selection); its parent listens.
function listenForStars(view, canPick) {
  const area = view.canvas.parentElement;
  let pressedAt = null;
  area.addEventListener("pointerdown", (event) => { pressedAt = { x: event.clientX, y: event.clientY }; });
  area.addEventListener("pointermove", (event) => {
    view.pointer = canPick() ? { x: event.clientX, y: event.clientY } : null;
    setHovered(view, view.pointer ? starAt(view, event.clientX, event.clientY) : null, event.clientX, event.clientY);
  });
  area.addEventListener("pointerleave", () => {
    view.pointer = null;
    setHovered(view, null);
  });
  area.addEventListener("click", (event) => {
    if (!canPick()) return;
    if (pressedAt && (Math.abs(event.clientX - pressedAt.x) >= STAR_CLICK_MOVE_PX ||
      Math.abs(event.clientY - pressedAt.y) >= STAR_CLICK_MOVE_PX)) return; // a drag: that zooms
    const marker = starAt(view, event.clientX, event.clientY);
    if (!marker) return;
    event.stopPropagation();
    view.onPick(marker.star, marker.catalog);
  });
}

// A new frame or new markers: the star under a still mouse may have changed
function refreshHover(view) {
  if (view.pointer) setHovered(view, starAt(view, view.pointer.x, view.pointer.y), view.pointer.x, view.pointer.y);
}

// ---- The page's overlay ----
const pageStars = createStarView(starOverlay, showStarInfo);
// (viewer.js) Box drags over the stars; while "Working…" (app.js throbber) nothing takes clicks
listenForStars(pageStars, () => activeTool === "pointer" && throbber.hidden);

// Collect the markers again: called when the GIF, the layers or the displayed size change
function buildStarOverlay() {
  const metadata = shownMetadata();
  const width = image.clientWidth;
  const height = image.clientHeight;
  const ratio = window.devicePixelRatio || 1;
  const key = metadata
    ? `${metadata.uuid}|${image.currentSrc || image.src}|${width}x${height}@${ratio}|${layersVersion}|${gridMode}|${gridLabelsAway}`
    : "";
  if (key === starOverlayKey) return;
  starOverlayKey = key;
  if (selectedStar && selectedStar.uuid !== metadata?.uuid) { // another job: its card is stale
    selectedStar = null;
    starInfo.hidden = true;
  }
  setStarViewMarkers(pageStars, metadata);
  Object.assign(pageStars, { scale: width / (image.naturalWidth || 1), width, height, frame: undefined,
    grid: coordinateGrid(metadata) });
  starOverlay.toggleAttribute("hidden", pageStars.markers.length === 0 && !pageStars.grid);
  refreshHover(pageStars);
  placeStarMarkers();
}

// Follow the frame on screen; redraw when it or anything else changed
function placeStarMarkers() {
  const frame = currentFrameIndex(); // (app.js)
  if (frame !== pageStars.frame) {
    pageStars.frame = frame;
    pageStars.dirty = true;
    refreshHover(pageStars);
    if (pageStars.markers.length) updateStarInfoFrame();
  }
  if (pageStars.dirty) drawStarView(pageStars);
}

// ---- The same stars in the zoom (app.js calls these while it is open) ----
const zoomStarOverlay = document.querySelector("#zoom-star-overlay");
const zoomStars = createStarView(zoomStarOverlay, (star, catalog) => {
  zoomDialog.close(); // the star card is on the page, under the zoom
  showStarInfo(star, catalog);
});
listenForStars(zoomStars, () => true);
zoomDialog.addEventListener("close", () => {
  zoomStars.pointer = null;
  setHovered(zoomStars, null);
});

// When the zoom opens: the same stars, at the zoom's scale. The canvas covers just
// the zoom's view; zoomRegion (app.js) is the part of the frame it shows.
function buildZoomStarOverlay(pxPerGifPx) {
  setStarViewMarkers(zoomStars, shownMetadata());
  zoomStars.grid = coordinateGrid(shownMetadata());
  Object.assign(zoomStars, {
    scale: pxPerGifPx,
    offsetX: -zoomRegion.x0 * pxPerGifPx,
    offsetY: -zoomRegion.y0 * pxPerGifPx,
    width: parseFloat(zoomView.style.width),
    height: parseFloat(zoomView.style.height),
    frame: undefined,
    pointer: null,
  });
  zoomStarOverlay.toggleAttribute("hidden", zoomStars.markers.length === 0 && !zoomStars.grid);
  placeZoomStarMarkers();
}

function placeZoomStarMarkers() {
  const frame = currentFrameIndex();
  if (frame !== zoomStars.frame) {
    zoomStars.frame = frame;
    zoomStars.dirty = true;
    refreshHover(zoomStars);
  }
  if (zoomStars.dirty && (zoomStars.markers.length || zoomStars.grid)) drawStarView(zoomStars);
}

// Every animation frame: collect again if needed (cheap check), then follow the GIF.
function starOverlayTick() {
  buildStarOverlay();
  placeStarMarkers();
  requestAnimationFrame(starOverlayTick);
}

// The playing GIF's frame is only known once its frames are decoded (app.js does
// that lazily on the first pause) - with stars switched on, decode right away.
image.addEventListener("load", () => {
  if ("ImageDecoder" in window && anyLayerOn() && shownMetadata()) {
    loadGifFrames().catch((error) => console.warn("Could not decode the GIF frames:", error));
  }
});

requestAnimationFrame(starOverlayTick);
