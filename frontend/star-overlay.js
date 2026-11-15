// Star overlays: the stars from the backend's meta.json ("overlays": Gaia DR3,
// CatWISE2020 and SIMBAD's known fast movers, see python-backend/overlays.py) drawn over the GIF.
//
// The backend already moved every star to each frame's date (proper motion) and
// gives its position per frame in GIF pixels from the top left ("xy", null =
// outside the image). So this file only has to know which frame is on screen:
//   stopped / stepping  shownFrame (app.js)
//   playing             frameShowingNow() (app.js), from the time since the GIF started
//   not known yet       (frames still being decoded, or no ImageDecoder in this
//                       browser) every star at its average position
// Fast movers also get their whole track as a dashed line.
//
// Uses app.js's top-level variables (both are classic scripts): image, pausedFrame,
// gifFrames, gifFramesSrc, shownFrame, frameShowingNow, loadGifFrames, currentVideoUuid.

const starOverlay = document.querySelector("#star-overlay");
const starInfo = document.querySelector("#star-info");
const starToggles = {
  gaia_dr3: document.querySelector("#show-gaia"),
  catwise2020: document.querySelector("#show-catwise"),
  simbad_movers: document.querySelector("#show-movers"),
};

const SVG_NS = "http://www.w3.org/2000/svg";
const STAR_MARKER_PX = 6;  // marker radius on screen, whatever the frame size
const STAR_LABEL_PX = 11;  // font size of the fast movers' names on screen

// Which catalogues are switched on: on unless switched off before (remembered, like "Show diffs")
const starLayersOn = {};
for (const [catalog, toggle] of Object.entries(starToggles)) {
  starLayersOn[catalog] = localStorage.getItem(`showStars_${catalog}`) !== "false";
  toggle.setAttribute("aria-checked", String(starLayersOn[catalog]));
  toggle.addEventListener("click", () => {
    starLayersOn[catalog] = !starLayersOn[catalog];
    toggle.setAttribute("aria-checked", String(starLayersOn[catalog]));
    localStorage.setItem(`showStars_${catalog}`, String(starLayersOn[catalog]));
    buildStarOverlay();
  });
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
      parsedMetadata = JSON.parse(localStorage.getItem("videoMetadata") ?? "null");
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
  const sky = skyPositionAt(star, catalog, info.mjd_mean);
  cells.sky.textContent = `RA ${formatNumber(sky.ra, 5)}°, Dec ${formatNumber(sky.dec, 5)}°`;
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
    ["Catalogue position", `RA ${formatNumber(star.ra, 5)}°, Dec ${formatNumber(star.dec, 5)}° (epoch ${catalog.epoch})`],
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
    ? `${metadata.uuid}|${image.currentSrc || image.src}|${width}|${JSON.stringify(starLayersOn)}`
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
  if (!metadata || !Object.values(starLayersOn).some(Boolean)) {
    starOverlay.toggleAttribute("hidden", true); // (an <svg> has no .hidden property)
    return;
  }

  starOverlay.setAttribute("viewBox", `0 0 ${image.naturalWidth} ${image.naturalHeight}`);
  const scale = width / image.naturalWidth; // screen px per GIF px
  const radius = STAR_MARKER_PX / scale;
  const fontSize = STAR_LABEL_PX / scale;

  for (const [catalogName, catalog] of Object.entries(metadata.overlays)) {
    if (!catalog || !starLayersOn[catalogName]) continue; // failed in the backend, or switched off
    const layer = svgElement("g", { class: `star-layer star-layer-${catalogName}` });
    // brightest drawn last = on top
    for (const star of [...catalog.stars].reverse()) {
      const points = star.xy.filter(Boolean);
      if (!points.length) continue;
      const average = [
        points.reduce((sum, p) => sum + p[0], 0) / points.length,
        points.reduce((sum, p) => sum + p[1], 0) / points.length,
      ];
      const fast = star.reason.includes("fast");
      const group = svgElement("g", { class: `star${fast ? " star-fast" : ""}`, tabindex: "0" });
      const title = svgElement("title");
      title.textContent = starTitle(star, catalog.label);
      group.append(title);

      if (fast && points.length > 1) {
        group.append(svgElement("polyline", {
          class: "star-track",
          points: points.map((p) => p.join(",")).join(" "),
        }));
      }
      const marker = catalogName === "gaia_dr3"
        ? svgElement("circle", { class: "star-marker", r: radius })
        : svgElement("rect", { class: "star-marker", width: 2 * radius * 0.9, height: 2 * radius * 0.9 });
      if (catalogName === "simbad_movers") marker.classList.add("star-diamond"); // square turned 45°
      // invisible, bigger target for hover / click
      const hit = svgElement("circle", { class: "star-hit", r: radius * 1.6 });
      group.append(marker, hit);
      let label = null;
      if (fast && star.name) {
        label = svgElement("text", { class: "star-label", "font-size": fontSize });
        label.textContent = star.name;
        group.append(label);
      }
      group.addEventListener("click", (event) => {
        event.stopPropagation();
        showStarInfo(star, catalog);
      });
      group.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          showStarInfo(star, catalog);
        }
      });
      // a press on a star must not start the zoom selection on the image below
      group.addEventListener("pointerdown", (event) => event.stopPropagation());
      layer.append(group);
      starMarkers.push({ star, marker, hit, label, average, radius, fontSize, group });
    }
    starOverlay.append(layer);
  }
  starOverlay.toggleAttribute("hidden", starMarkers.length === 0);
  placeStarMarkers();
}

// Which frame is on screen; null if not known (then the average positions are used).
function overlayFrameIndex() {
  if (!pausedFrame.hidden) return shownFrame; // stopped, stepping, or playing on the canvas
  if (gifFrames.length && gifFramesSrc === (image.currentSrc || image.src)) return frameShowingNow();
  return null;
}

function placeStarMarkers() {
  if (!starMarkers.length) return;
  const frame = overlayFrameIndex();
  if (frame === drawnFrame) return;
  drawnFrame = frame;
  for (const { star, marker, hit, label, average, radius, fontSize, group } of starMarkers) {
    const position = frame === null ? average : star.xy[frame];
    group.classList.toggle("star-outside", !position); // outside the image in this frame
    if (!position) continue;
    const [x, y] = position;
    if (marker.tagName === "rect") {
      const size = Number(marker.getAttribute("width"));
      marker.setAttribute("x", x - size / 2);
      marker.setAttribute("y", y - size / 2);
      if (marker.classList.contains("star-diamond")) marker.setAttribute("transform", `rotate(45 ${x} ${y})`);
    } else {
      marker.setAttribute("cx", x);
      marker.setAttribute("cy", y);
    }
    hit.setAttribute("cx", x);
    hit.setAttribute("cy", y);
    if (label) {
      label.setAttribute("x", x + radius * 1.5);
      label.setAttribute("y", y - radius * 1.2);
      label.setAttribute("font-size", fontSize);
    }
  }
  updateStarInfoFrame();
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
  if ("ImageDecoder" in window && Object.values(starLayersOn).some(Boolean) && shownMetadata()) {
    loadGifFrames().catch((error) => console.warn("Could not decode the GIF frames:", error));
  }
});
for (const toggle of Object.values(starToggles)) {
  toggle.addEventListener("click", () => {
    if ("ImageDecoder" in window && image.complete && shownMetadata()) {
      loadGifFrames().catch((error) => console.warn("Could not decode the GIF frames:", error));
    }
  });
}

requestAnimationFrame(starOverlayTick);
