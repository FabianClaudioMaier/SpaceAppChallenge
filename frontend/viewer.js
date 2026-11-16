// The viewer around the GIF:
//   tools      pointer (a click on a star opens its card, other clicks and drags zoom);
//              box (drag a box around something) - "Yes" switches to it, No / Save back
//   rulers     RA above and Dec left of the image (and of the zoom), white with black text
//   frame time the date of the frame on screen, under the image
//   box        the marked box, drawn over the image (and the zoom)
//   verdict    "Is something moving?" Yes / No, and Save (-> /api/save, middleware.save)
//   download   "Download GIF" (-> /api/export, middleware.exportView): the GIF on
//              screen with the rulers, dates, box and the layers' markers burned in
//
// The GIF itself is pixels only (python-backend/backend.py _save_jpg): everything
// with text is drawn here, so it stays sharp at any size. Saved GIFs get the same
// things burned in by the middleware (middleware/export.py).
//
// "Frame pixels" are measured from the frame's top-left corner, one per SPHEREx
// pixel, like the star overlays' "xy" (python-backend/overlays.py _xy).
//
// Uses app.js / star-overlay.js top-level names (all classic scripts): image, clamp,
// currentFrameIndex, loadGifFrames, currentVideoUuid, zoomDialog, zoomView, zoomRegion,
// zoomBoxOverlay, moving*Button, shownMetadata, parsedMetadata, svgElement, SVG_NS,
// pageStars, STAR_KINDS, coordinateGrid.

const ZOOM_RULER_LEFT_PX = 84; // room for the zoom's rulers (app.js openZoom)
const ZOOM_RULER_TOP_PX = 42;
// Ruler steps in degrees (the same in middleware/export.py):
//  page  PAGE_RULER_STEP_DEG (a 250 px frame: Dec 0.05°, RA 0.05° or 0.1°); coarser
//        only where the labels would collide, finer only where a small frame
//        would get fewer than RULER_MIN_TICKS ticks
//  zoom  about one label per ZOOM_RULER_LABEL_PX screen pixels (finer as you zoom in)
const RULER_STEPS_DEG = [0.0005, 0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5,
  10, 15, 30, 45, 90]; // (the big ones: RA right at a pole, where it spans 90° and more)
const PAGE_RULER_STEP_DEG = 0.05;
const RULER_MIN_TICKS = 3;
const RULER_LABEL_GAP_PX = { top: 16, left: 10 }; // free space between neighbouring labels
const ZOOM_RULER_LABEL_PX = { top: 110, left: 60 };
const RULER_TICK_PX = 6;
// The spacing (the same in exported GIFs, middleware/export.py): label - 3 px - tick
// (6 px) - 4 px - image (on the page the frame's border, style.css; in the zoom
// the rulers leave it, RULER_IMAGE_GAP_PX); "Dec" 6 px from the
// widest Dec label (its sign). The page's left ruler is made just wide enough
// for that (--ruler-left-page); the zoom's has a fixed width, so its "Dec" moves.
const RULER_LABEL_TICK_PX = 3;
const RULER_IMAGE_GAP_PX = 4;
const RULER_TITLE_GAP_PX = 6;
const RULER_TITLE_BASELINE_PX = 15; // "RA": its baseline from the top; "Dec" (upright): from the left
const TOOLS = ["pointer", "box"]; // pointer: a star click opens its card, other clicks and drags zoom

const resultFrameElement = document.querySelector(".result-frame");
const rulerFrame = document.querySelector(".ruler-frame");
const pageRulerSet = {
  top: document.querySelector("#ruler-top"),
  left: document.querySelector("#ruler-left"),
};
const zoomRulerFrame = document.querySelector(".zoom-ruler-frame");
const zoomRulerSet = {
  top: document.querySelector("#zoom-ruler-top"),
  left: document.querySelector("#zoom-ruler-left"),
};
const boxOverlay = document.querySelector("#box-overlay");
const frameTime = document.querySelector("#frame-time");
const saveStatus = document.querySelector("#save-status");
const downloadGifButton = document.querySelector("#download-gif");

// The job whose GIF is on screen (like shownMetadata, but also without star overlays)
function jobMetadata() {
  shownMetadata(); // (star-overlay.js) parses localStorage.videoMetadata once per job
  const metadata = parsedMetadata;
  const src = image.currentSrc || image.src;
  if (!metadata?.frames || !metadata.uuid || metadata.uuid !== currentVideoUuid) return null;
  if (!src.includes(metadata.uuid) || image.naturalWidth !== metadata.pixels) return null;
  return metadata;
}

// ---- Sky coordinates ----
// Frame pixels -> [RA, Dec] in degrees. The backend's grid (make_grid_wcs): TAN
// projection, north up, east left, pixel_scale_arcsec per pixel, field centre in
// the middle. (Checked against astropy: same to 1e-10 arcsec.)
function frameToSky(x, y, metadata) {
  const rad = Math.PI / 180;
  const s = metadata.pixel_scale_arcsec / 3600;
  const n = metadata.pixels;
  const xi = -s * (x - n / 2) * rad;
  const eta = s * (n / 2 - y) * rad;
  const d0 = metadata.dec * rad;
  const den = Math.cos(d0) - eta * Math.sin(d0);
  const ra = (metadata.ra + Math.atan2(xi, den) / rad + 360) % 360;
  const dec = Math.atan2(Math.sin(d0) + eta * Math.cos(d0), Math.hypot(xi, den)) / rad;
  return [ra, dec];
}

function decimalsOf(step) {
  for (let d = 0; d < 8; d++) {
    if (Math.abs(step * 10 ** d - Math.round(step * 10 ** d)) < 1e-9) return d;
  }
  return 8;
}

// The ticks of one ruler: starting at step RULER_STEPS_DEG[first], coarser while
// labels (labelSize(step): screen px one takes along the ruler) would collide,
// finer (if `finer`) while there would be fewer than RULER_MIN_TICKS. Measured on
// the ticks themselves, not on the value range: along an edge the values need not
// rise or fall steadily (at a pole the Dec on the left edge rises to the middle and
// falls again, so every value is crossed twice).
function chooseTicks(samples, first, finer, pxPerUnit, labelSize, format) {
  const ticksFor = (i) => rulerTicks(samples, RULER_STEPS_DEG[i], format);
  const fits = (i) => {
    const at = ticksFor(i).map((t) => t.position * pxPerUnit).sort((a, b) => a - b);
    return at.every((p, j) => j === 0 || p - at[j - 1] >= labelSize(RULER_STEPS_DEG[i]));
  };
  let i = first;
  while (i < RULER_STEPS_DEG.length - 1 && !fits(i)) i++; // crowded: coarser
  while (finer && i > 0 && ticksFor(i).length < RULER_MIN_TICKS && fits(i - 1)) i--; // few: finer
  return ticksFor(i);
}

// The zoom's first step to try: about `wanted` labels over the value range
function zoomStepIndex(range, wanted) {
  let best = 0;
  RULER_STEPS_DEG.forEach((s, i) => {
    if (Math.abs(range / s - wanted) < Math.abs(range / RULER_STEPS_DEG[best] - wanted)) best = i;
  });
  return best;
}

function valueRange(samples) {
  const values = samples.map(([, v]) => v);
  return Math.max(...values) - Math.min(...values);
}

// Size of a ruler label in an SVG (measured, so the fonts can change)
function labelBox(svg, label) {
  const text = svgElement("text");
  text.textContent = label;
  svg.append(text);
  const box = { width: text.getComputedTextLength(), height: text.getBBox().height };
  text.remove();
  return box;
}

// Every place where the values along an edge (samples [position, value]) cross a
// multiple of step -> [{ position, value, label }]. A value crossed twice (at a
// pole) gets two ticks; only the same crossing found in two neighbouring sample
// pairs (a sample right on the value) counts once.
function rulerTicks(samples, step, format) {
  const ticks = [];
  for (let i = 0; i < samples.length - 1; i++) {
    const [p0, v0] = samples[i];
    const [p1, v1] = samples[i + 1];
    if (v0 === v1) continue;
    for (let k = Math.ceil(Math.min(v0, v1) / step); k <= Math.floor(Math.max(v0, v1) / step); k++) {
      const v = k * step;
      const position = p0 + ((v - v0) / (v1 - v0)) * (p1 - p0);
      if (ticks.some((t) => Math.abs(t.value - v) < step / 2 && Math.abs(t.position - position) < 1e-6)) continue;
      ticks.push({ position, value: v, label: format(v, decimalsOf(step)) });
    }
  }
  return ticks;
}

// Draws the rulers ({ top, left }) for a view: the part of the frame
// { x0, y0, x1, y1 } (frame pixels) shown in the screen rectangle `screen`;
// mode "page" or "zoom" (how the steps are chosen, see RULER_STEPS_DEG).
// Titles outside the numbers, centred on the frame: "RA" on its own line above
// the top ruler's labels, "Dec" upright along the left ruler's outer edge - so
// every tick keeps its label. Only redrawn when something changed.
// (Drawn in the SVGs' own CSS pixels, without a viewBox: a viewBox gives an SVG
// an aspect ratio of its own, which kept the left ruler - and with it the frame -
// tall after the window got smaller.)
function drawRulers({ top, left }, metadata, view, screen, mode) {
  const topRect = top.getBoundingClientRect();
  const leftRect = left.getBoundingClientRect();
  const key = [metadata.uuid, view.x0, view.y0, view.x1, view.y1, screen.left, screen.top, screen.width,
    screen.height, topRect.left, topRect.top, topRect.width, leftRect.top, leftRect.width, leftRect.height].join("|");
  if (top.dataset.key === key) return;
  top.dataset.key = key;
  const n = metadata.pixels;
  const scale = screen.width / (view.x1 - view.x0); // screen px per frame pixel
  const xs = [Math.max(view.x0, 0), Math.min(view.x1, n)];
  const ys = [Math.max(view.y0, 0), Math.min(view.y1, n)];

  // RA along the top edge (the values belong to that edge: away from the equator
  // lines of constant RA are tilted)
  const raSamples = [];
  const count = clamp(Math.round(screen.width / 3), 20, 400);
  for (let i = 0; i < count; i++) {
    const x = xs[0] + ((xs[1] - xs[0]) * i) / (count - 1);
    raSamples.push([x, frameToSky(x, ys[0], metadata)[0]]);
  }
  const ra0 = raSamples[0][1];
  for (const sample of raSamples) sample[1] = ra0 + ((sample[1] - ra0 + 540) % 360) - 180; // no jump at 0/360
  const raFormat = (v, d) => `${(((v % 360) + 360) % 360).toFixed(d)}°`;
  const page = mode === "page";
  const raTicks = chooseTicks(raSamples,
    page ? RULER_STEPS_DEG.indexOf(PAGE_RULER_STEP_DEG)
      : zoomStepIndex(valueRange(raSamples), Math.max(2, screen.width / ZOOM_RULER_LABEL_PX.top)),
    page, scale, (step) => labelBox(top, raFormat(ra0, decimalsOf(step))).width + RULER_LABEL_GAP_PX.top, raFormat);

  const decSamples = [];
  for (let i = 0; i < count; i++) {
    const y = ys[0] + ((ys[1] - ys[0]) * i) / (count - 1);
    decSamples.push([y, frameToSky(xs[0], y, metadata)[1]]);
  }
  const decFormat = (v, d) => `${v >= 0 ? "+" : "−"}${Math.abs(v).toFixed(d)}°`;
  const decHeight = labelBox(left, "0°").height + RULER_LABEL_GAP_PX.left;
  const decTicks = chooseTicks(decSamples,
    page ? RULER_STEPS_DEG.indexOf(PAGE_RULER_STEP_DEG)
      : zoomStepIndex(valueRange(decSamples), Math.max(2, screen.height / ZOOM_RULER_LABEL_PX.left)),
    page, scale, () => decHeight, decFormat);

  const h = topRect.height;
  const w = leftRect.width;
  const gap = page ? 0 : RULER_IMAGE_GAP_PX; // (the page's frame has a white border for it)

  const xMin = screen.left - topRect.left;
  const xMax = xMin + (xs[1] - view.x0) * scale; // right end of the frame's part shown
  const raTitle = rulerTitle("RA", (Math.max(xMin, 0) + Math.min(xMax, topRect.width)) / 2, RULER_TITLE_BASELINE_PX);
  raTitle.setAttribute("text-anchor", "middle");
  top.replaceChildren(raTitle);
  for (const { position, label } of raTicks) {
    const x = xMin + (position - view.x0) * scale;
    top.append(svgElement("line", { x1: x, y1: h - gap - RULER_TICK_PX, x2: x, y2: h - gap }));
    const text = svgElement("text", { x, y: h - gap - RULER_TICK_PX - RULER_LABEL_TICK_PX, "text-anchor": "middle" });
    text.textContent = label;
    top.append(text);
    const halfLabel = text.getComputedTextLength() / 2; // keep the label inside the image's width
    text.setAttribute("x", clamp(x, xMin + halfLabel, xMax - halfLabel));
  }

  const yMin = screen.top - leftRect.top;
  const yMax = yMin + (ys[1] - view.y0) * scale; // bottom of the frame's part shown
  // "Dec" reading upwards, centred on that part (rotated: x along it, y = its baseline)
  const decTitle = rulerTitle("Dec", -(Math.max(yMin, 0) + Math.min(yMax, leftRect.height)) / 2, RULER_TITLE_BASELINE_PX);
  decTitle.setAttribute("text-anchor", "middle");
  decTitle.setAttribute("transform", "rotate(-90)");
  left.replaceChildren(decTitle);
  let widest = 0;
  for (const { position, label } of decTicks) {
    const y = yMin + (position - view.y0) * scale;
    left.append(svgElement("line", { x1: w - gap - RULER_TICK_PX, y1: y, x2: w - gap, y2: y }));
    const text = svgElement("text", {
      x: w - gap - RULER_TICK_PX - RULER_LABEL_TICK_PX, y: clamp(y, yMin + 8, yMax - 8),
      "text-anchor": "end", "dominant-baseline": "middle",
    });
    text.textContent = label;
    left.append(text);
    widest = Math.max(widest, text.getComputedTextLength());
  }
  // "Dec" RULER_TITLE_GAP_PX left of the widest label (upright, its letters end at
  // the baseline): the page's ruler gets just that wide (redrawn at the new width),
  // in the zoom the title moves instead
  const labelsLeft = w - gap - RULER_TICK_PX - RULER_LABEL_TICK_PX - widest;
  if (page) {
    const needed = Math.ceil(RULER_TITLE_BASELINE_PX + RULER_TITLE_GAP_PX + widest + RULER_LABEL_TICK_PX + RULER_TICK_PX);
    if (Math.abs(needed - w) >= 1) document.documentElement.style.setProperty("--ruler-left-page", `${needed}px`);
  } else {
    decTitle.setAttribute("y", Math.max(RULER_TITLE_BASELINE_PX, labelsLeft - RULER_TITLE_GAP_PX));
  }
}

function rulerTitle(label, x, y) {
  const text = svgElement("text", { x, y, "text-anchor": "end", class: "ruler-title" });
  text.textContent = label;
  return text;
}

// The whole frame as shown on the page
function pageRulers(metadata) {
  const rect = image.getBoundingClientRect();
  const scale = image.clientWidth / image.naturalWidth;
  drawRulers(pageRulerSet, metadata, { x0: 0, y0: 0, x1: metadata.pixels, y1: metadata.pixels }, {
    left: rect.left + image.clientLeft,
    top: rect.top + image.clientTop,
    width: image.clientWidth,
    height: metadata.pixels * scale,
  }, "page");
}

// ---- The date of the frame on screen ----
function frameDate(timestamp) {
  return `${timestamp.slice(0, 16).replace("T", " ")} UTC`; // as on saved GIFs
}

function updateFrameTime(metadata) {
  let text = "";
  if (metadata) {
    const frames = metadata.frames;
    const i = currentFrameIndex(); // (app.js) null until the frames are decoded
    text = i === null
      ? `${frames[0].timestamp.slice(0, 10)} – ${frames[frames.length - 1].timestamp.slice(0, 10)} · ${frames.length} frames`
      : `${frameDate(frames[i].timestamp)} · frame ${i + 1} of ${frames.length}`;
  }
  if (frameTime.textContent !== text) frameTime.textContent = text;
}

// The frame on screen is only known once the GIF's frames are decoded (app.js
// does that lazily); the date under the image needs it right away.
image.addEventListener("load", () => {
  if ("ImageDecoder" in window && jobMetadata()) {
    loadGifFrames().catch((error) => console.warn("Could not decode the GIF frames:", error));
  }
});

// ---- Tools (no buttons: "Yes" switches to the box, No / Save / a new job back) ----
let activeTool = "pointer"; // (app.js's image handlers ask this)

function setTool(tool) {
  activeTool = TOOLS.includes(tool) ? tool : "pointer";
  resultFrameElement.dataset.tool = activeTool; // the cursor (style.css)
}
setTool("pointer");

// ---- The marked box ----
let markedBox = null; // { uuid, x1, y1, x2, y2 }: corners (top left, bottom right) in frame pixels

// app.js: a box was dragged with the Box tool (screen coordinates; rect = the image's)
function markBoxOnScreen(left, top, width, height, rect) {
  const metadata = jobMetadata();
  if (!metadata) return;
  const n = metadata.pixels;
  const perScreen = image.naturalWidth / image.clientWidth; // frame pixels per screen px
  const x0 = rect.left + image.clientLeft;
  const y0 = rect.top + image.clientTop;
  const x1 = clamp(Math.floor((left - x0) * perScreen), 0, n - 1);
  const y1 = clamp(Math.floor((top - y0) * perScreen), 0, n - 1);
  const x2 = clamp(Math.ceil((left + width - x0) * perScreen), x1 + 1, n);
  const y2 = clamp(Math.ceil((top + height - y0) * perScreen), y1 + 1, n);
  markedBox = { uuid: metadata.uuid, x1, y1, x2, y2 };
  setVerdict("moving");
  const [ra, dec] = frameToSky((x1 + x2) / 2, (y1 + y2) / 2, metadata);
  saveStatus.textContent =
    `Box x ${x1}–${x2}, y ${y1}–${y2} px, centre RA ${ra.toFixed(5)}°, ` +
    `Dec ${dec < 0 ? "−" : "+"}${Math.abs(dec).toFixed(5)}°. Save to keep it.`;
}

// The box over the whole frame, or (region: the zoom's part, in frame pixels) over that part
function drawBox(svg, box, region = null) {
  const view = region ? [region.x0, region.y0, region.x1 - region.x0, region.y1 - region.y0]
    : [0, 0, image.naturalWidth, image.naturalHeight];
  const key = box ? `${view.join(",")}|${box.x1},${box.y1},${box.x2},${box.y2}` : "";
  if (svg.dataset.key === key) return;
  svg.dataset.key = key;
  svg.replaceChildren();
  svg.toggleAttribute("hidden", !box);
  if (!box) return;
  svg.setAttribute("viewBox", view.join(" "));
  svg.setAttribute("preserveAspectRatio", "none");
  svg.append(svgElement("rect", { x: box.x1, y: box.y1, width: box.x2 - box.x1, height: box.y2 - box.y1 }));
}

// ---- "Is something moving?" ----
let verdict = null; // "moving" (Yes or a box) / "nothing" (No) / null

function setVerdict(value) {
  verdict = value;
  setTool(value === "moving" ? "box" : "pointer"); // "Yes": mark it with a box
  movingYesButton.setAttribute("aria-pressed", String(value === "moving"));
  movingNoButton.setAttribute("aria-pressed", String(value === "nothing"));
  movingSaveButton.hidden = value === null;
}

movingYesButton.addEventListener("click", () => {
  if (!jobMetadata()) {
    saveStatus.textContent = "Load a field first.";
    return;
  }
  setVerdict("moving");
  saveStatus.textContent = markedBox
    ? "Drag a new box to replace it, or Save."
    : "Drag a box around it on the image, or Save without one.";
});

movingNoButton.addEventListener("click", () => {
  if (!jobMetadata()) {
    saveStatus.textContent = "Load a field first.";
    return;
  }
  markedBox = null;
  setVerdict("nothing");
  saveStatus.textContent = "Save to note that nothing moves in this field.";
});

movingSaveButton.addEventListener("click", async () => {
  const metadata = jobMetadata();
  if (!metadata || verdict === null) return;
  movingSaveButton.disabled = true;
  try {
    const src = image.currentSrc || image.src;
    const box = verdict === "moving" && markedBox?.uuid === metadata.uuid ? markedBox : null;
    const response = await fetch("/api/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        uuid: metadata.uuid,
        ra: metadata.ra,
        dec: metadata.dec,
        pixels: metadata.pixels,
        collections: metadata.collections,
        verdict,
        box: box && { x1: box.x1, y1: box.y1, x2: box.x2, y2: box.y2 },
        frame_shown: currentFrameIndex(),
        view: src.includes("_diff.gif") ? "diff" : "frames",
        frame_utc: metadata.frames.map((f) => f.timestamp),
        detectors: metadata.frames.map((f) => f.detectors),
        grid: coordinateGrid(metadata), // (star-overlay.js) burned into the saved GIFs too, if shown
      }),
    });
    const result = await response.json().catch(() => null);
    if (!response.ok) throw new Error(result?.error ?? `HTTP ${response.status}`);
    const gifs = [result.gif, result.diff_gif].filter(Boolean);
    saveStatus.textContent = `Saved: ${result.name}` + (verdict === "moving"
      ? (gifs.length ? ` (${gifs.length} GIF${gifs.length > 1 ? "s" : ""} exported)` : " (GIFs no longer in the cache, row only)")
      : "");
    setVerdict(null);
  } catch (error) {
    console.error("Saving failed:", error);
    saveStatus.textContent = `Saving failed: ${error.message}`;
  } finally {
    movingSaveButton.disabled = false;
  }
});

// ---- "Download GIF": the GIF on screen as it looks here, made by the middleware ----
// The star markers go along as drawn (pageStars, star-overlay.js): per layer its
// shape and colour, per star its size, kind, name and position in every frame.
downloadGifButton.addEventListener("click", async () => {
  const metadata = jobMetadata();
  if (!metadata) {
    saveStatus.textContent = "Load a field first.";
    return;
  }
  downloadGifButton.disabled = true;
  saveStatus.textContent = "Making the GIF…";
  try {
    const src = image.currentSrc || image.src;
    const box = markedBox?.uuid === metadata.uuid ? markedBox : null;
    const layers = pageStars.markers.length ? pageStars.layers.map((layer) => ({
      shape: layer.style.shape,
      color: layer.style.color,
      stars: STAR_KINDS.flatMap(({ kind }) => layer.kinds[kind].map((m) => ({
        size: Math.round(m.size * 100) / 100,
        kind,
        xy: m.star.xy,
        label: m.label,
        track: Boolean(m.track),
      }))),
    })) : [];
    const response = await fetch("/api/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        uuid: metadata.uuid,
        ra: metadata.ra,
        dec: metadata.dec,
        pixels: metadata.pixels,
        view: src.includes("_diff.gif") ? "diff" : "frames",
        frame_utc: metadata.frames.map((f) => f.timestamp),
        box: box && { x1: box.x1, y1: box.y1, x2: box.x2, y2: box.y2 },
        layers,
        grid: coordinateGrid(metadata), // (star-overlay.js) null where none is shown
      }),
    });
    if (!response.ok) {
      const result = await response.json().catch(() => null);
      throw new Error(result?.error ?? `HTTP ${response.status}`);
    }
    const name = /filename="([^"]+)"/.exec(response.headers.get("Content-Disposition") ?? "")?.[1] ?? "spherex.gif";
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = name;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 10_000);
    saveStatus.textContent = `Downloaded ${name}`;
  } catch (error) {
    console.error("Download failed:", error);
    saveStatus.textContent = `Download failed: ${error.message}`;
  } finally {
    downloadGifButton.disabled = false;
  }
});

// ---- Keys: B = "Yes" (box), V (or Z) back to the pointer, Space stops / plays the GIF,
// "," / "." one frame back / forward (like YouTube; also in the zoom) ----
document.addEventListener("keydown", (event) => {
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  if (event.target.closest("input, textarea, select, [contenteditable]")) return;
  if (event.key === " ") {
    // also when a button has the focus (Space would press that button instead)
    event.preventDefault();
    if (!event.repeat && !playPauseButton.hidden && !gifControls.hidden) playPauseButton.click();
    return;
  }
  if ((event.key === "," || event.key === ".") && !gifControls.hidden) {
    event.preventDefault();
    void stepFrame(event.key === "," ? -1 : 1); // (app.js: stops the GIF first)
    return;
  }
  const key = event.key.toLowerCase();
  if (key === "b") movingYesButton.click();
  if (key === "v" || key === "z") setTool("pointer"); // (Z: the old zoom tool)
});

// ---- Layout: the tallest frame that fits (style.css makes the viewer just wide
// enough for it, the right panel takes the rest, so both gaps are the same) ----
const frameStage = document.querySelector(".frame-stage");
new ResizeObserver(() => {
  const free = frameStage.clientHeight - pageRulerSet.top.getBoundingClientRect().height;
  document.documentElement.style.setProperty("--frame-max", `${Math.max(0, Math.floor(free))}px`);
}).observe(frameStage);

// ---- Panel widths: drag the handle on a panel's edge (double-click: default) ----
// Kept as a share of the window's width, so a dragged panel grows and shrinks with
// the window like the default ones do. Remembered in localStorage "panelWidths"
// ({side, right}: 0..1, missing = default).
const appLayout = document.querySelector(".app-layout");
const sidePanel = document.querySelector(".side-panel");
const rightPanel = document.querySelector(".right-panel");
const panelResizers = [...document.querySelectorAll(".panel-resizer")];
const PANEL_MIN_PX = { side: 220, right: 180 };
const PANEL_MAX_SHARE = 0.45; // of the window's width
let panelWidths = {};
try {
  panelWidths = JSON.parse(localStorage.getItem("panelWidths") ?? "{}") ?? {};
} catch {
  panelWidths = {};
}
for (const [panel, value] of Object.entries(panelWidths)) { // (saved in px before)
  if (value > 1) panelWidths[panel] = value / window.innerWidth;
}

function applyPanelWidths() {
  const max = window.innerWidth * PANEL_MAX_SHARE;
  const width = (panel) => Math.round(clamp(panelWidths[panel] * window.innerWidth, PANEL_MIN_PX[panel],
    Math.max(PANEL_MIN_PX[panel], max)));
  if (panelWidths.side) document.documentElement.style.setProperty("--side-width", `${width("side")}px`);
  else document.documentElement.style.removeProperty("--side-width");
  appLayout.classList.toggle("right-sized", Boolean(panelWidths.right));
  if (panelWidths.right) appLayout.style.setProperty("--right-width", `${width("right")}px`);
  placePanelResizers();
}

// The handles sit on the panels' inner edges
function placePanelResizers() {
  const layout = appLayout.getBoundingClientRect();
  for (const handle of panelResizers) {
    const edge = handle.dataset.panel === "side"
      ? sidePanel.getBoundingClientRect().right : rightPanel.getBoundingClientRect().left;
    handle.style.left = `${edge - layout.left}px`;
  }
}

function setPanelWidth(panel, px) {
  if (px === null) delete panelWidths[panel];
  else panelWidths[panel] = px / window.innerWidth;
  try {
    localStorage.setItem("panelWidths", JSON.stringify(panelWidths));
  } catch {
    // (only remembering fails)
  }
  applyPanelWidths();
}

for (const handle of panelResizers) {
  const panel = handle.dataset.panel;
  const widthAt = (clientX) => (panel === "side" ? clientX - appLayout.getBoundingClientRect().left
    : appLayout.getBoundingClientRect().right - clientX);
  handle.addEventListener("pointerdown", (event) => {
    handle.setPointerCapture(event.pointerId);
    handle.classList.add("dragging");
    event.preventDefault();
  });
  handle.addEventListener("pointermove", (event) => {
    if (handle.hasPointerCapture(event.pointerId)) setPanelWidth(panel, widthAt(event.clientX));
  });
  const stop = () => handle.classList.remove("dragging");
  handle.addEventListener("pointerup", stop);
  handle.addEventListener("pointercancel", stop);
  handle.addEventListener("dblclick", () => setPanelWidth(panel, null));
  handle.addEventListener("keydown", (event) => { // arrows: 16 px wider / narrower
    const step = { ArrowLeft: -16, ArrowRight: 16 }[event.key];
    if (!step) return;
    event.preventDefault();
    const current = (panel === "side" ? sidePanel : rightPanel).getBoundingClientRect().width;
    setPanelWidth(panel, current + (panel === "side" ? step : -step));
  });
}
new ResizeObserver(placePanelResizers).observe(appLayout);
new ResizeObserver(placePanelResizers).observe(sidePanel);
new ResizeObserver(placePanelResizers).observe(rightPanel);
window.addEventListener("resize", applyPanelWidths);
applyPanelWidths();

// ---- Every animation frame: follow the job, the frame on screen and the layout ----
let viewerUuid = null;

function viewerTick() {
  if (currentVideoUuid !== viewerUuid) { // another job: its box and verdict are gone
    viewerUuid = currentVideoUuid;
    markedBox = null;
    setVerdict(null);
    saveStatus.textContent = "";
  }
  const metadata = jobMetadata();
  rulerFrame.classList.toggle("rulers-off", !metadata);
  if (metadata) pageRulers(metadata);
  updateFrameTime(metadata);
  const box = metadata && markedBox?.uuid === metadata.uuid ? markedBox : null;
  drawBox(boxOverlay, box);
  if (zoomDialog.open) {
    zoomRulerFrame.classList.toggle("rulers-off", !metadata || !zoomRegion);
    if (metadata && zoomRegion) drawRulers(zoomRulerSet, metadata, zoomRegion, zoomView.getBoundingClientRect(), "zoom");
    drawBox(zoomBoxOverlay, box, zoomRegion);
  }
  requestAnimationFrame(viewerTick);
}

requestAnimationFrame(viewerTick);
