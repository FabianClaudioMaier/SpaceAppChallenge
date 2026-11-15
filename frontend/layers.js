// Layers: the list in the left panel saying which catalogues are drawn over the
// frame, and how (star-overlay.js draws what visibleLayers() returns).
//
//   built in   every job brings them (python-backend/overlays.py): Gaia DR3,
//              CatWISE2020, SIMBAD's fast movers
//   extra      fetched when switched on, for the job on screen (POST /api/catalog,
//              python-backend/catalogs.py): 2MASS, unWISE, known asteroids, galaxies,
//              variables, X-ray, radio, ... ("+ Add catalogue")
//
// Per layer (remembered in localStorage "layers"): on/off (the eye), and behind
// "⋯": a magnitude range, all / brightest N / faintest N, labels, marker size,
// hide artifacts (catalogues with artifact flags). Presets switch whole sets.
//
// Uses app.js / star-overlay.js names: currentVideoUuid, shownMetadata,
// buildStarOverlay, prepareCatalog, svgElement, formatNumber.

const BUILTIN_LAYERS = {
  gaia_dr3: { label: "Gaia DR3", group: "stars", shape: "circle", color: "#4fe3ff" },
  catwise2020: { label: "CatWISE2020", group: "stars", shape: "square", color: "#ffa53d" },
  simbad_movers: { label: "Fast movers (SIMBAD)", group: "movers", shape: "diamond", color: "#ff5cf0" },
};
const DEFAULT_LAYERS = ["gaia_dr3", "catwise2020", "simbad_movers", "asteroids", "twomass"];
const DEFAULT_ON = ["gaia_dr3", "catwise2020", "simbad_movers", "asteroids"];
const DEFAULT_OPTIONS = {
  on: false, magMin: null, magMax: null, show: "all", count: 100,
  labels: "fast", size: "brightness", hideArtifacts: true,
};
const PRESETS = {
  discovery: () => true, // everything in the list
  stars: (info) => info.group === "stars",
  movers: (info) => info.group === "movers",
  none: () => false,
};

const layerListElement = document.querySelector("#layer-list");
const layerPresetInput = document.querySelector("#layer-preset");
const layerAddButton = document.querySelector("#layer-add-button");
const layerAddPanel = document.querySelector("#layer-add-panel");
const layerSearchInput = document.querySelector("#layer-search");
const layerAddList = document.querySelector("#layer-add-list");

let layerRegistry = { groups: {}, layers: {} }; // extra layers (GET /api/catalogs)
let layersVersion = 0; // changes whenever what is drawn changes (star-overlay.js rebuilds)
const layerData = new Map(); // "<uuid>|<layer>" -> { status: "loading" | "ok" | "failed", catalog, error }
let openLayer = null; // the layer whose "⋯" options are open

// ---- State (remembered) ----
const layerState = (() => {
  let saved = null;
  try {
    saved = JSON.parse(localStorage.getItem("layers") ?? "null");
  } catch {
    saved = null;
  }
  const order = Array.isArray(saved?.order) ? saved.order : [...DEFAULT_LAYERS];
  const options = {};
  for (const key of order) {
    options[key] = { ...DEFAULT_OPTIONS, on: DEFAULT_ON.includes(key), ...(saved?.options?.[key] ?? {}) };
  }
  return { order, options };
})();

function saveLayers() {
  localStorage.setItem("layers", JSON.stringify(layerState));
}

function layerInfo(key) {
  return BUILTIN_LAYERS[key] ?? layerRegistry.layers[key] ?? { label: key, group: "other", shape: "circle", color: "#ffffff" };
}

function layerChanged() {
  layersVersion += 1;
  saveLayers();
  buildStarOverlay(); // (star-overlay.js)
}

// ---- Data: built-in layers come with the job, extra ones are fetched ----
function layerCatalog(key, metadata) {
  if (BUILTIN_LAYERS[key]) return metadata?.overlays?.[key] ?? null;
  return layerData.get(`${metadata?.uuid}|${key}`)?.catalog ?? null;
}

function layerStatus(key, metadata) {
  if (!metadata) return { text: "–" };
  if (BUILTIN_LAYERS[key]) {
    return metadata.overlays?.[key] ? { ok: true } : { text: "not available" };
  }
  const entry = layerData.get(`${metadata.uuid}|${key}`);
  if (!entry) return { text: layerState.options[key]?.on ? "…" : "off" };
  if (entry.status === "loading") return { text: "loading…" };
  if (entry.status === "failed") return { text: "failed", title: entry.error };
  return { ok: true };
}

async function fetchLayer(key, metadata) {
  const id = `${metadata.uuid}|${key}`;
  if (layerData.has(id)) return;
  layerData.set(id, { status: "loading" });
  renderLayerList();
  try {
    const response = await fetch("/api/catalog", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: key, ra: metadata.ra, dec: metadata.dec, pixels: metadata.pixels,
        frame_mjds: metadata.frames.map((f) => f.mjd_mean),
      }),
    });
    const result = await response.json().catch(() => null);
    if (!response.ok) throw new Error(result?.error ?? `HTTP ${response.status}`);
    prepareCatalog(result, metadata); // (star-overlay.js) positions per frame, links
    layerData.set(id, { status: "ok", catalog: result });
  } catch (error) {
    console.warn(`Layer ${key} could not be loaded:`, error);
    layerData.set(id, { status: "failed", error: error.message });
  }
  layersVersion += 1;
  renderLayerList();
}

// Fetch the switched-on extra layers of the job on screen that aren't there yet
function loadLayers(metadata) {
  if (!metadata) return;
  for (const key of layerState.order) {
    if (!BUILTIN_LAYERS[key] && layerState.options[key]?.on && layerRegistry.layers[key]) {
      void fetchLayer(key, metadata);
    }
  }
}

// ---- What star-overlay.js draws ----
function anyLayerOn() {
  return layerState.order.some((key) => layerState.options[key]?.on);
}

// The stars of a layer after its options (magnitude range, brightest / faintest N,
// artifacts); fast movers always stay
function filterLayerStars(catalog, options) {
  const band = catalog.sort_mag;
  const magOf = (s) => (band ? s.mag?.[band] : null);
  let stars = catalog.stars.filter((s) => !(options.hideArtifacts && s.artifact));
  if (band && (options.magMin !== null || options.magMax !== null)) {
    stars = stars.filter((s) => {
      const m = magOf(s);
      if (m === null || m === undefined) return s.reason.includes("fast");
      return (options.magMin === null || m >= options.magMin) && (options.magMax === null || m <= options.magMax)
        || s.reason.includes("fast");
    });
  }
  if (options.show !== "all" && band) {
    const measured = stars.filter((s) => magOf(s) !== null && magOf(s) !== undefined);
    const picked = new Set(options.show === "brightest"
      ? measured.slice(0, options.count) : measured.slice(-options.count));
    stars = stars.filter((s) => picked.has(s) || s.reason.includes("fast"));
  }
  return stars;
}

// [{ key, catalog, stars, options, style }] of the switched-on layers, bottom first
// (the top of the list is drawn last, on top)
function visibleLayers(metadata) {
  const out = [];
  for (const key of [...layerState.order].reverse()) {
    const options = layerState.options[key];
    const catalog = options?.on ? layerCatalog(key, metadata) : null;
    if (!catalog) continue;
    const info = layerInfo(key);
    out.push({ key, catalog, options, stars: filterLayerStars(catalog, options),
      style: { shape: catalog.shape ?? info.shape, color: catalog.color ?? info.color } });
  }
  return out;
}

// ---- The list ----
const SHAPE_PATHS = {
  circle: (r) => svgElement("circle", { r }),
  square: (r) => svgElement("rect", { x: -0.9 * r, y: -0.9 * r, width: 1.8 * r, height: 1.8 * r }),
  diamond: (r) => svgElement("rect", { x: -0.75 * r, y: -0.75 * r, width: 1.5 * r, height: 1.5 * r, transform: "rotate(45)" }),
  triangle: (r) => svgElement("path", { d: `M0 ${-r}L${0.87 * r} ${0.5 * r}L${-0.87 * r} ${0.5 * r}Z` }),
  cross: (r) => svgElement("path", { d: `M${-0.8 * r} ${-0.8 * r}L${0.8 * r} ${0.8 * r}M${0.8 * r} ${-0.8 * r}L${-0.8 * r} ${0.8 * r}` }),
};

// A marker shape centred on (0, 0) (also used on the frame, star-overlay.js)
function shapeElement(shape, r) {
  return (SHAPE_PATHS[shape] ?? SHAPE_PATHS.circle)(r);
}

function symbolSvg(shape, color) {
  const svg = svgElement("svg", { class: "layer-symbol", viewBox: "-8 -8 16 16", "aria-hidden": "true" });
  const mark = shapeElement(shape, 5.5);
  mark.setAttribute("stroke", color);
  svg.append(mark);
  return svg;
}

function element(tag, attributes = {}, text = null) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attributes)) e.setAttribute(k, v);
  if (text !== null) e.textContent = text;
  return e;
}

function selectElement(options, value, label) {
  const select = element("select", { "aria-label": label });
  for (const [v, text] of options) select.append(element("option", { value: v }, text));
  select.value = value;
  return select;
}

function optionRow(label, control) {
  const row = element("label", { class: "layer-option" });
  row.append(element("span", {}, label), control);
  return row;
}

// The "⋯" options of one layer
function layerOptionsElement(key, metadata) {
  const options = layerState.options[key];
  const catalog = layerCatalog(key, metadata);
  const box = element("div", { class: "layer-options" });
  const band = catalog?.sort_mag;
  const mags = (catalog?.stars ?? []).map((s) => s.mag?.[band]).filter((m) => m !== null && m !== undefined);

  if (band && mags.length) {
    const lo = Math.floor(Math.min(...mags) * 2) / 2;
    const hi = Math.ceil(Math.max(...mags) * 2) / 2;
    const from = element("input", { type: "range", min: lo, max: hi, step: 0.1, "aria-label": `${band} from` });
    const to = element("input", { type: "range", min: lo, max: hi, step: 0.1, "aria-label": `${band} to` });
    from.value = options.magMin ?? lo;
    to.value = options.magMax ?? hi;
    const text = element("span", { class: "layer-range-text" });
    const update = () => {
      const a = Math.min(Number(from.value), Number(to.value));
      const b = Math.max(Number(from.value), Number(to.value));
      options.magMin = a <= lo ? null : a;
      options.magMax = b >= hi ? null : b;
      text.textContent = `${band} ${a.toFixed(1)} – ${b.toFixed(1)}`;
      layerChanged();
      updateLayerCounts();
    };
    from.addEventListener("input", update);
    to.addEventListener("input", update);
    text.textContent = `${band} ${Number(from.value).toFixed(1)} – ${Number(to.value).toFixed(1)}`;
    const range = element("div", { class: "layer-range" });
    range.append(text, from, to);
    box.append(range);

    const show = selectElement([["all", "All"], ["brightest", "Brightest"], ["faintest", "Faintest"]], options.show, "Show");
    const count = element("input", { type: "number", min: 1, max: 10000, step: 10, value: options.count, "aria-label": "How many" });
    count.disabled = options.show === "all";
    show.addEventListener("change", () => {
      options.show = show.value;
      count.disabled = options.show === "all";
      layerChanged();
      updateLayerCounts();
    });
    count.addEventListener("input", () => {
      options.count = Math.max(1, Number(count.value) || 1);
      layerChanged();
      updateLayerCounts();
    });
    const showRow = element("div", { class: "layer-option" });
    showRow.append(element("span", {}, "Show"), show, count);
    box.append(showRow);
  }

  const labels = selectElement([["fast", "Movers only"], ["named", "All named"], ["none", "None"]], options.labels, "Labels");
  labels.addEventListener("change", () => { options.labels = labels.value; layerChanged(); });
  box.append(optionRow("Labels", labels));

  const size = selectElement([["brightness", "By brightness"], ["same", "Same size"]], options.size, "Marker size");
  size.addEventListener("change", () => { options.size = size.value; layerChanged(); });
  box.append(optionRow("Marker size", size));

  if (catalog?.has_artifacts) {
    const artifacts = element("input", { type: "checkbox" });
    artifacts.checked = options.hideArtifacts;
    artifacts.addEventListener("change", () => {
      options.hideArtifacts = artifacts.checked;
      layerChanged();
      updateLayerCounts();
    });
    box.append(optionRow("Hide artifacts (spikes, ghosts)", artifacts));
  }

  if (catalog?.source) box.append(element("p", { class: "layer-source" }, `${catalog.source}. ${catalog.selection ?? ""}`));
  if (!BUILTIN_LAYERS[key]) {
    const remove = element("button", { type: "button", class: "layer-remove" }, "Remove from the list");
    remove.addEventListener("click", () => {
      layerState.order = layerState.order.filter((k) => k !== key);
      delete layerState.options[key];
      openLayer = null;
      layerChanged();
      renderLayerList();
    });
    box.append(remove);
  }
  return box;
}

const layerCountCells = new Map(); // layer -> its count element (updated without rebuilding the list)

function countText(key, metadata) {
  const status = layerStatus(key, metadata);
  if (!status.ok) return status;
  const catalog = layerCatalog(key, metadata);
  const total = catalog.stars.length;
  if (!total) return { text: "none here" };
  const shown = filterLayerStars(catalog, layerState.options[key]).length;
  const fmt = (n) => n.toLocaleString("en-US");
  return { text: shown === total ? fmt(total) : `${fmt(shown)} / ${fmt(total)}`,
    title: `${shown} shown of ${total} in the frame` };
}

function updateLayerCounts() {
  const metadata = shownMetadata();
  for (const [key, cell] of layerCountCells) {
    const { text, title } = countText(key, metadata);
    cell.textContent = text;
    cell.title = title ?? "";
  }
}

function renderLayerList() {
  const metadata = shownMetadata();
  layerListElement.replaceChildren();
  layerCountCells.clear();
  for (const key of layerState.order) {
    const options = layerState.options[key];
    const info = layerInfo(key);
    const row = element("li", { class: `layer-row${options.on ? "" : " layer-off"}${openLayer === key ? " layer-open" : ""}` });
    const eye = element("button", { type: "button", class: "layer-eye", "aria-pressed": String(options.on),
      "aria-label": `Show ${info.label}`, title: options.on ? "Hide" : "Show" });
    eye.addEventListener("click", () => {
      options.on = !options.on;
      layerPresetInput.value = matchingPreset();
      layerChanged();
      loadLayers(shownMetadata());
      renderLayerList();
    });
    const name = element("span", { class: "layer-name", title: info.note ?? "" }, info.label);
    const count = element("span", { class: "layer-count" });
    layerCountCells.set(key, count);
    const more = element("button", { type: "button", class: "layer-more", "aria-expanded": String(openLayer === key),
      "aria-label": `${info.label} options`, title: "Options" }, "⋯");
    more.addEventListener("click", () => {
      openLayer = openLayer === key ? null : key;
      renderLayerList();
    });
    row.append(eye, symbolSvg(info.shape, info.color), name, count, more);
    layerListElement.append(row);
    if (openLayer === key) {
      const item = element("li", { class: "layer-options-item" });
      item.append(layerOptionsElement(key, metadata));
      layerListElement.append(item);
    }
  }
  updateLayerCounts();
}

// ---- Presets ----
function matchingPreset() {
  for (const [name, test] of Object.entries(PRESETS)) {
    if (layerState.order.every((key) => layerState.options[key].on === test(layerInfo(key)))) return name;
  }
  return "custom";
}

layerPresetInput.addEventListener("change", () => {
  const test = PRESETS[layerPresetInput.value];
  if (!test) return;
  for (const key of layerState.order) layerState.options[key].on = test(layerInfo(key));
  layerChanged();
  loadLayers(shownMetadata());
  renderLayerList();
});

// ---- "+ Add catalogue…" ----
function renderAddList() {
  const query = layerSearchInput.value.trim().toLowerCase();
  layerAddList.replaceChildren();
  const groups = layerRegistry.groups ?? {};
  let any = false;
  for (const [group, groupLabel] of Object.entries(groups)) {
    const items = Object.entries(layerRegistry.layers)
      .filter(([key, info]) => info.group === group && !layerState.order.includes(key))
      .filter(([, info]) => !query || `${info.label} ${info.note ?? ""} ${info.source}`.toLowerCase().includes(query));
    if (!items.length) continue;
    any = true;
    layerAddList.append(element("li", { class: "layer-add-group" }, groupLabel));
    for (const [key, info] of items) {
      const item = element("li");
      const button = element("button", { type: "button", class: "layer-add-item", title: info.source });
      button.append(symbolSvg(info.shape, info.color), element("span", {}, info.label),
        element("small", {}, info.note ?? ""));
      button.addEventListener("click", () => {
        layerState.order.push(key);
        layerState.options[key] = { ...DEFAULT_OPTIONS, on: true };
        layerPresetInput.value = matchingPreset();
        layerChanged();
        loadLayers(shownMetadata());
        renderLayerList();
        renderAddList();
      });
      item.append(button);
      layerAddList.append(item);
    }
  }
  if (!any) layerAddList.append(element("li", { class: "layer-add-group" }, query ? "Nothing found" : "All catalogues are in the list"));
}

layerAddButton.addEventListener("click", () => {
  layerAddPanel.hidden = !layerAddPanel.hidden;
  layerAddButton.setAttribute("aria-expanded", String(!layerAddPanel.hidden));
  if (!layerAddPanel.hidden) {
    renderAddList();
    layerSearchInput.focus();
  }
});
layerSearchInput.addEventListener("input", renderAddList);

// ---- Start, and follow the job on screen ----
fetch("/api/catalogs")
  .then((response) => (response.ok ? response.json() : Promise.reject(new Error(`HTTP ${response.status}`))))
  .then((registry) => {
    layerRegistry = registry;
    renderLayerList();
    loadLayers(shownMetadata());
  })
  .catch((error) => console.warn("Catalogue list not available:", error));

layerPresetInput.value = matchingPreset();
renderLayerList();

let layersForJob = null;
function layersTick() {
  const metadata = shownMetadata();
  const job = metadata?.uuid ?? null;
  if (job !== layersForJob) { // another job (or its metadata just arrived): its layers
    layersForJob = job;
    loadLayers(metadata);
    renderLayerList();
  }
  requestAnimationFrame(layersTick);
}
requestAnimationFrame(layersTick);
