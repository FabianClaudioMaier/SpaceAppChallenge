// Layers: the list in the left panel saying which catalogues are drawn over the
// frame, and how (star-overlay.js draws what visibleLayers() returns).
//
//   built in   Gaia DR3, CatWISE2020, SIMBAD's fast movers (python-backend/overlays.py),
//              fetched together (POST /api/catalog "builtin", catalogs.builtin)
//   extra      2MASS, unWISE, known asteroids, galaxies, variables, X-ray, radio, ...
//              ("+ Add catalogue"; POST /api/catalog, python-backend/catalogs.py)
// Both are fetched when switched on, for the job on screen - never during the job,
// so the frames don't wait for catalogue servers. A layer that failed gets a
// reload button (↻) instead of its chevron; "Reload" in the options asks again anyway.
// The whole list folds into its heading (chevron left of "Layers", remembered in
// "layersFolded"; in the one-column layout it starts folded).
//
// Per layer (remembered in localStorage "layers"): on/off (the eye), and behind its
// chevron (or a click on its name): a magnitude range, all / brightest N / faintest N, labels, marker size,
// hide artifacts (catalogues with artifact flags). Presets switch whole sets.
// "S" next to the eye (or shift+click on the eye): solo, like in a mixer - only
// the soloed layers are drawn (several can be), the other switches stay as they
// are and count again when the last solo ends. The solos aren't remembered.
//
// Uses app.js / star-overlay.js names: currentVideoUuid, shownMetadata,
// buildStarOverlay, prepareCatalog, svgElement, formatNumber.

const BUILTIN_LAYERS = {
  // (shapes and colours: see catalogs.py LAYERS - one shape per kind of object)
  gaia_dr3: { label: "Gaia DR3", group: "stars", shape: "star", color: "#4fe3ff" },
  catwise2020: { label: "CatWISE2020", group: "stars", shape: "star", color: "#ffa53d" },
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
const soloLayers = new Set(); // the layers soloed ("S"); empty = no solo

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

// ---- Data: fetched per job when switched on ----
function layerCatalog(key, metadata) {
  return layerData.get(`${metadata?.uuid}|${key}`)?.catalog ?? null;
}

function layerStatus(key, metadata) {
  if (!metadata) return { text: "–" };
  const entry = layerData.get(`${metadata.uuid}|${key}`);
  if (!entry) return { text: layerState.options[key]?.on || soloLayers.has(key) ? "…" : "off" };
  if (entry.status === "loading") return { text: "loading…" };
  if (entry.status === "failed") return { text: "failed", title: entry.error };
  return { ok: true };
}

// POST /api/catalog for the job on screen; refresh = the server asks the catalogue
// servers again even if it has the layer cached
async function requestCatalog(name, metadata, refresh) {
  const response = await fetch("/api/catalog", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name, ra: metadata.ra, dec: metadata.dec, pixels: metadata.pixels,
      frame_mjds: metadata.frames.map((f) => f.mjd_mean), refresh,
    }),
  });
  const result = await response.json().catch(() => null);
  if (!response.ok) throw new Error(result?.error ?? `HTTP ${response.status}`);
  return result;
}

// One layer, or for a built-in one all three (they come together: catalogs.builtin)
async function fetchLayer(key, metadata, refresh = false) {
  const keys = BUILTIN_LAYERS[key] ? Object.keys(BUILTIN_LAYERS) : [key];
  const ids = keys.map((k) => `${metadata.uuid}|${k}`);
  if (ids.some((id) => layerData.has(id))) return; // (loading or loaded; reloadLayer clears first)
  for (const id of ids) layerData.set(id, { status: "loading" });
  renderLayerList();
  try {
    const result = await requestCatalog(BUILTIN_LAYERS[key] ? "builtin" : key, metadata, refresh);
    const catalogs = BUILTIN_LAYERS[key] ? result.layers : { [key]: result };
    keys.forEach((k, i) => {
      const catalog = catalogs[k];
      if (catalog) {
        prepareCatalog(catalog, metadata); // (star-overlay.js) positions per frame, links
        layerData.set(ids[i], { status: "ok", catalog });
      } else { // (a built-in one whose server failed)
        layerData.set(ids[i], { status: "failed", error: "the catalogue server didn't answer" });
      }
    });
  } catch (error) {
    console.warn(`Layer ${key} could not be loaded:`, error);
    for (const id of ids) layerData.set(id, { status: "failed", error: error.message });
  }
  layersVersion += 1;
  renderLayerList();
}

// ↻ (failed): ask again, what the server has cached is reused (failures never are);
// "Reload" (options, refresh): the server asks the catalogue servers again.
// Either way the old answer is dropped right away (the row shows "loading…").
function reloadLayer(key, refresh = false) {
  const metadata = shownMetadata();
  if (!metadata) return;
  const keys = BUILTIN_LAYERS[key] ? Object.keys(BUILTIN_LAYERS) : [key];
  for (const k of keys) layerData.delete(`${metadata.uuid}|${k}`);
  void fetchLayer(key, metadata, refresh);
}

// Fetch the switched-on extra layers of the job on screen that aren't there yet
function loadLayers(metadata) {
  if (!metadata) return;
  for (const key of layerState.order) {
    if ((layerState.options[key]?.on || soloLayers.has(key)) && (BUILTIN_LAYERS[key] || layerRegistry.layers[key])) {
      void fetchLayer(key, metadata);
    }
  }
}

// ---- What star-overlay.js draws ----
function anyLayerOn() {
  return soloLayers.size > 0 || layerState.order.some((key) => layerState.options[key]?.on);
}

// The stars of a layer after its options (magnitude range, brightest / faintest N,
// artifacts); fast movers always stay - the ones found among a catalogue's stars
// (Gaia, CatWISE), not objects with their own position per frame (the known
// asteroids: all of them move, so the options apply to them like to any layer)
function filterLayerStars(catalog, options) {
  const band = catalog.sort_mag;
  const magOf = (s) => (band ? s.mag?.[band] : null);
  const alwaysShown = (s) => s.reason.includes("fast") && !s.positions;
  let stars = catalog.stars.filter((s) => !(options.hideArtifacts && s.artifact));
  if (band && (options.magMin !== null || options.magMax !== null)) {
    stars = stars.filter((s) => {
      const m = magOf(s);
      if (m === null || m === undefined) return alwaysShown(s);
      return (options.magMin === null || m >= options.magMin) && (options.magMax === null || m <= options.magMax)
        || alwaysShown(s);
    });
  }
  if (options.show !== "all" && band) {
    const measured = stars.filter((s) => magOf(s) !== null && magOf(s) !== undefined);
    const picked = new Set(options.show === "brightest"
      ? measured.slice(0, options.count) : measured.slice(-options.count));
    stars = stars.filter((s) => picked.has(s) || alwaysShown(s));
  }
  return stars;
}

// [{ key, catalog, stars, options, style }] of the switched-on layers, bottom first
// (the top of the list is drawn last, on top)
function visibleLayers(metadata) {
  const out = [];
  for (const key of [...layerState.order].reverse()) {
    const options = layerState.options[key];
    const shown = soloLayers.size ? soloLayers.has(key) : options?.on;
    const catalog = shown ? layerCatalog(key, metadata) : null;
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
  plus: (r) => svgElement("path", { d: `M${-r} 0H${r}M0 ${-r}V${r}` }),
  star: (r) => svgElement("path", { d: `M${starPoints(r).map(([x, y]) => `${x} ${y}`).join("L")}Z` }),
  hexagon: (r) => svgElement("path", { d: `M${hexagonPoints(r).map(([x, y]) => `${x} ${y}`).join("L")}Z` }),
};

// Corners of the star (five points, inner corners at 0.45 r) and the hexagon, centred on (0, 0)
function starPoints(r) {
  return Array.from({ length: 10 }, (_, k) => {
    const d = k % 2 === 0 ? r : 0.45 * r;
    return [d * Math.sin((Math.PI * k) / 5), -d * Math.cos((Math.PI * k) / 5)];
  });
}

function hexagonPoints(r) {
  return Array.from({ length: 6 }, (_, k) => [r * Math.cos((Math.PI / 3) * k + Math.PI / 6),
    r * Math.sin((Math.PI / 3) * k + Math.PI / 6)]);
}

// The same shapes on the frame's canvas (star-overlay.js), centred on (x, y):
// added to a Path2D, so a whole layer is stroked at once
const SHAPE_TRACERS = {
  circle: (path, x, y, r) => {
    path.moveTo(x + r, y);
    path.arc(x, y, r, 0, 2 * Math.PI);
  },
  square: (path, x, y, r) => path.rect(x - 0.9 * r, y - 0.9 * r, 1.8 * r, 1.8 * r),
  diamond: (path, x, y, r) => { // the 1.5 r square turned by 45°
    const d = 0.75 * Math.SQRT2 * r;
    path.moveTo(x, y - d);
    path.lineTo(x + d, y);
    path.lineTo(x, y + d);
    path.lineTo(x - d, y);
    path.closePath();
  },
  triangle: (path, x, y, r) => {
    path.moveTo(x, y - r);
    path.lineTo(x + 0.87 * r, y + 0.5 * r);
    path.lineTo(x - 0.87 * r, y + 0.5 * r);
    path.closePath();
  },
  cross: (path, x, y, r) => {
    path.moveTo(x - 0.8 * r, y - 0.8 * r);
    path.lineTo(x + 0.8 * r, y + 0.8 * r);
    path.moveTo(x + 0.8 * r, y - 0.8 * r);
    path.lineTo(x - 0.8 * r, y + 0.8 * r);
  },
  plus: (path, x, y, r) => {
    path.moveTo(x - r, y);
    path.lineTo(x + r, y);
    path.moveTo(x, y - r);
    path.lineTo(x, y + r);
  },
  star: (path, x, y, r) => tracePolygon(path, x, y, starPoints(r)),
  hexagon: (path, x, y, r) => tracePolygon(path, x, y, hexagonPoints(r)),
};

function tracePolygon(path, x, y, points) {
  points.forEach(([px, py], i) => (i === 0 ? path.moveTo(x + px, y + py) : path.lineTo(x + px, y + py)));
  path.closePath();
}

function traceShape(path, shape, x, y, r) {
  (SHAPE_TRACERS[shape] ?? SHAPE_TRACERS.circle)(path, x, y, r);
}

// A marker shape centred on (0, 0), for the list's symbols (the frame uses traceShape)
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
  const reload = element("button", { type: "button", class: "layer-reload" }, "Reload");
  reload.title = BUILTIN_LAYERS[key] ? "Ask the catalogue servers again (Gaia, CatWISE and the fast movers come together)"
    : "Ask the catalogue servers again";
  reload.addEventListener("click", () => reloadLayer(key, true));
  box.append(reload);
  if (!BUILTIN_LAYERS[key]) {
    const remove = element("button", { type: "button", class: "layer-remove" }, "Remove from the list");
    remove.addEventListener("click", () => {
      layerState.order = layerState.order.filter((k) => k !== key);
      delete layerState.options[key];
      openLayer = null;
      soloLayers.delete(key);
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
    // while a layer is solo: it stands out, the others are dimmed (their eyes keep
    // saying what comes back when the solo ends)
    const solo = soloLayers.has(key) ? " layer-solo" : soloLayers.size ? " layer-muted" : options.on ? "" : " layer-off";
    const row = element("li", { class: `layer-row${solo}${openLayer === key ? " layer-open" : ""}` });
    const eye = element("button", { type: "button", class: "layer-eye", "aria-pressed": String(options.on),
      "aria-label": `Show ${info.label}`,
      title: options.on ? "Hide" : "Show" });
    const toggleSolo = () => {
      if (!soloLayers.delete(key)) soloLayers.add(key);
      layerChanged();
      loadLayers(shownMetadata());
      renderLayerList();
    };
    eye.addEventListener("click", (event) => {
      if (event.shiftKey) {
        toggleSolo();
        return;
      }
      options.on = !options.on;
      layerPresetInput.value = matchingPreset();
      layerChanged();
      loadLayers(shownMetadata());
      renderLayerList();
    });
    const soloButton = element("button", { type: "button", class: "layer-solo-button", "aria-pressed": String(soloLayers.has(key)),
      "aria-label": `Solo ${info.label}`, title: soloLayers.has(key) ? "Solo off" : "Solo: show only the soloed layers" }, "S");
    soloButton.addEventListener("click", toggleSolo);
    const name = element("span", { class: "layer-name", title: info.note ?? "" }, info.label);
    const count = element("span", { class: "layer-count" });
    layerCountCells.set(key, count);
    const failed = layerData.get(`${metadata?.uuid}|${key}`)?.status === "failed";
    const more = failed
      ? element("button", { type: "button", class: "layer-more layer-retry", "aria-label": `Reload ${info.label}`,
        title: "Failed - reload" }, "↻")
      : element("button", { type: "button", class: "layer-more", "aria-expanded": String(openLayer === key),
        "aria-label": `${info.label} options`, title: openLayer === key ? "Close the options" : "Options" });
    if (!failed) more.append(element("span", { class: "chevron", "aria-hidden": "true" }));
    const toggleOptions = () => {
      openLayer = openLayer === key ? null : key;
      renderLayerList();
    };
    more.addEventListener("click", () => {
      if (failed) {
        reloadLayer(key); // (the row shows "loading…" and its chevron again right away)
        return;
      }
      toggleOptions();
    });
    name.addEventListener("click", toggleOptions);
    row.append(eye, soloButton, symbolSvg(info.shape, info.color), name, count, more);
    layerListElement.append(row);
    if (openLayer === key) {
      const item = element("li", { class: "layer-options-item" });
      item.append(layerOptionsElement(key, metadata));
      layerListElement.append(item);
    }
  }
  updateLayerCounts();
  updateLayersSummary();
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
  soloLayers.clear(); // a preset says what is shown
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

// ---- Folding the whole list (chevron left of "Layers") ----
const layersFoldButton = document.querySelector("#layers-fold");
const layersBody = document.querySelector("#layers-body");
const layersSummary = document.querySelector("#layers-summary");

function setLayersFolded(folded) {
  layersBody.hidden = folded;
  layersSummary.hidden = !folded;
  layersFoldButton.setAttribute("aria-expanded", String(!folded));
  layersFoldButton.setAttribute("aria-label", folded ? "Unfold the layer list" : "Fold the layer list");
  updateLayersSummary();
}

// "4 of 5 shown" (or "solo: 1 of 5") next to the folded heading
function updateLayersSummary() {
  const shown = layerState.order.filter((key) => (soloLayers.size ? soloLayers.has(key) : layerState.options[key]?.on));
  layersSummary.textContent = `${soloLayers.size ? "solo: " : ""}${shown.length} of ${layerState.order.length} shown`;
}

layersFoldButton.addEventListener("click", () => {
  const folded = !layersBody.hidden;
  localStorage.setItem("layersFolded", String(folded));
  setLayersFolded(folded);
});
{
  const saved = localStorage.getItem("layersFolded");
  setLayersFolded(saved === null ? window.matchMedia("(max-width: 900px)").matches : saved === "true");
}

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
