// Star overlays: the stars from the backend's meta.json ("overlays": Gaia DR3 and
// CatWISE2020, see python-backend/overlays.py) drawn over the GIF.
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
};

const SVG_NS = "http://www.w3.org/2000/svg";
const STAR_MARKER_PX = 6;  // marker radius on screen, whatever the frame size
const STAR_LABEL_PX = 11;  // font size of the fast movers' names on screen

// Which catalogues are switched on (remembered, like "Show diffs")
const starLayersOn = {};
for (const [catalog, toggle] of Object.entries(starToggles)) {
  starLayersOn[catalog] = localStorage.getItem(`showStars_${catalog}`) === "true";
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
    ["Position", `RA ${formatNumber(star.ra, 5)}°, Dec ${formatNumber(star.dec, 5)}° (epoch ${catalog.epoch})`],
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
  const links = document.createElement("p");
  links.className = "star-info-links";
  for (const [label, url] of [["SIMBAD", star.links.simbad], ["VizieR", star.links.vizier]]) {
    const link = document.createElement("a");
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = label;
    links.append(link);
  }
  starInfo.append(close, heading, list, links);
  starInfo.hidden = false;
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
      const marker = catalogName === "catwise2020"
        ? svgElement("rect", { class: "star-marker", width: 2 * radius * 0.9, height: 2 * radius * 0.9 })
        : svgElement("circle", { class: "star-marker", r: radius });
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
