// Star overlays: the layers switched on in the layer list (layers.js) drawn over
// the GIF - the job's own (meta.json "overlays": every Gaia DR3 and CatWISE2020
// source in the frame and SIMBAD's fast movers, python-backend/overlays.py) and
// the extra ones fetched on demand (python-backend/catalogs.py). Each layer has its
// shape and colour; its options (layers.js) pick which of its stars are drawn.
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
// Uses app.js's top-level variables (classic scripts): image, currentFrameIndex,
// loadGifFrames, currentVideoUuid, zoomDialog; and layers.js's visibleLayers,
// anyLayerOn, layersVersion, shapeElement.

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

let starMarkers = [];     // { star, marker, label, average } of the stars drawn now
let starOverlayKey = "";  // what the drawn overlay was built for (rebuilt when it changes)
let drawnFrame;           // frame index the markers are placed for (null = average)

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
  if (!metadata?.overlays || !metadata.uuid || metadata.uuid !== currentVideoUuid) return null;
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

// Every star's position per frame ("xy") and its SIMBAD / VizieR links, once per
// job (the backend sends neither: for thousands of stars that would be megabytes).
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
  const frame = drawnFrame;
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

// Rebuild the SVG: called when the GIF, the switches or the displayed size change.
function buildStarOverlay() {
  const metadata = shownMetadata();
  const width = image.clientWidth;
  const key = metadata
    ? `${metadata.uuid}|${image.currentSrc || image.src}|${width}|${layersVersion}`
    : "";
  if (key === starOverlayKey) return;
  starOverlayKey = key;
  starOverlay.replaceChildren();
  starMarkers = [];
  drawnFrame = undefined;
  if (selectedStar && selectedStar.uuid !== metadata?.uuid) { // another job: its card is stale
    selectedStar = null;
    starInfo.hidden = true;
  }
  if (!metadata || !anyLayerOn()) {
    starOverlay.toggleAttribute("hidden", true); // (an <svg> has no .hidden property)
    return;
  }

  starMarkers = drawStarLayers(starOverlay, metadata, width / image.naturalWidth, showStarInfo);
  starOverlay.toggleAttribute("hidden", starMarkers.length === 0);
  placeStarMarkers();
}

// Draws the switched-on layers into svg (the page's overlay or the zoom's) and
// returns their markers, for placeMarkers(). pxPerGifPx: screen px per GIF px
// there - markers and labels keep their size on screen, but a marker always
// surrounds at least a 3 px star. onPick(star, catalog): a star was clicked.
function drawStarLayers(svg, metadata, pxPerGifPx, onPick) {
  svg.replaceChildren();
  svg.setAttribute("viewBox", `0 0 ${image.naturalWidth} ${image.naturalHeight}`);
  const radius = Math.max(STAR_MARKER_PX / pxPerGifPx, 1.5);
  const fontSize = STAR_LABEL_PX / pxPerGifPx;
  const markers = [];

  for (const { key, catalog, stars, options, style } of visibleLayers(metadata)) { // (layers.js)
    const layer = svgElement("g", { class: `star-layer star-layer-${key}` });
    layer.style.setProperty("--layer-color", style.color);
    const sizes = markerSizes(catalog, stars, options.size);
    // brightest drawn last = on top
    for (const star of [...stars].reverse()) {
      const points = star.xy.filter(Boolean);
      if (!points.length) continue;
      const average = [
        points.reduce((sum, p) => sum + p[0], 0) / points.length,
        points.reduce((sum, p) => sum + p[1], 0) / points.length,
      ];
      const fast = star.reason.includes("fast");
      const size = sizes.get(star);
      const group = svgElement("g", {
        class: `star${fast ? " star-fast" : ""}${size < 0.75 ? " star-faint" : ""}`, tabindex: "0",
      });
      const title = svgElement("title");
      title.textContent = starTitle(star, catalog.label);
      group.append(title);

      if (fast && points.length > 1) {
        group.append(svgElement("polyline", {
          class: "star-track",
          points: points.map((p) => p.join(",")).join(" "),
        }));
      }
      const r = radius * size;
      // the layer's shape, centred on (0, 0) and moved to the star (placeMarkers)
      const marker = svgElement("g", { class: "star-marker" });
      marker.append(shapeElement(style.shape, r)); // (layers.js)
      // invisible, bigger target for hover / click
      const hit = svgElement("circle", { class: "star-hit", r: Math.max(r * 1.6, radius * 0.8) });
      group.append(marker, hit);
      let label = null;
      if (star.name && (options.labels === "named" || (options.labels === "fast" && fast))) {
        label = svgElement("text", { class: "star-label", "font-size": fontSize });
        label.textContent = star.name;
        group.append(label);
      }
      group.addEventListener("click", (event) => {
        event.stopPropagation();
        onPick(star, catalog);
      });
      group.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onPick(star, catalog);
        }
      });
      // a press on a star must not start the zoom selection on the image below
      group.addEventListener("pointerdown", (event) => event.stopPropagation());
      layer.append(group);
      markers.push({ star, marker, hit, label, average, radius: r, fontSize, group });
    }
    svg.append(layer);
  }
  return markers;
}

function placeStarMarkers() {
  if (!starMarkers.length) return;
  const frame = currentFrameIndex(); // (app.js)
  if (frame === drawnFrame) return;
  drawnFrame = frame;
  placeMarkers(starMarkers, frame);
  updateStarInfoFrame();
}

// ---- The same stars in the zoom (app.js calls these while it is open) ----
const zoomStarOverlay = document.querySelector("#zoom-star-overlay");
let zoomMarkers = [];
let zoomDrawnFrame;

// When the zoom opens: the same stars, at the zoom's scale
function buildZoomStarOverlay(pxPerGifPx) {
  const metadata = shownMetadata();
  zoomMarkers = metadata && anyLayerOn()
    ? drawStarLayers(zoomStarOverlay, metadata, pxPerGifPx, (star, catalog) => {
      zoomDialog.close(); // the star card is on the page, under the zoom
      showStarInfo(star, catalog);
    })
    : [];
  if (!zoomMarkers.length) zoomStarOverlay.replaceChildren();
  zoomStarOverlay.toggleAttribute("hidden", zoomMarkers.length === 0);
  zoomDrawnFrame = undefined;
  placeZoomStarMarkers();
}

function placeZoomStarMarkers() {
  const frame = currentFrameIndex();
  if (!zoomMarkers.length || frame === zoomDrawnFrame) return;
  zoomDrawnFrame = frame;
  placeMarkers(zoomMarkers, frame);
}

// Puts the markers on a frame (null = at their average positions).
function placeMarkers(markers, frame) {
  for (const { star, marker, hit, label, average, radius, fontSize, group } of markers) {
    const position = frame === null ? average : star.xy[frame];
    group.classList.toggle("star-outside", !position); // outside the image in this frame
    if (!position) continue;
    const [x, y] = position;
    marker.setAttribute("transform", `translate(${x} ${y})`); // (its shape is centred on 0, 0)
    hit.setAttribute("cx", x);
    hit.setAttribute("cy", y);
    if (label) {
      label.setAttribute("x", x + radius * 1.5);
      label.setAttribute("y", y - radius * 1.2);
      label.setAttribute("font-size", fontSize);
    }
  }
}

// Every animation frame: rebuild if needed (cheap check), then follow the GIF.
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
