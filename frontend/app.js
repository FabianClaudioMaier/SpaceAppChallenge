const settingsButton = document.querySelector("#settings-button");
const settingsPanel = document.querySelector("#settings-panel");
const invertButton = document.querySelector("#invert-pixel");
const speedInput = document.querySelector("#speed");
const speedValue = document.querySelector("#speed-value");
const image = document.querySelector("form img");
const zoomButton = document.querySelector("#zoom-button");
const zoomInfo = document.querySelector("#zoom-info");

settingsButton.addEventListener("click", () => {
  const open = settingsPanel.hidden;
  settingsPanel.hidden = !open;
  settingsButton.setAttribute("aria-expanded", String(open));
});

invertButton.addEventListener("click", () => {
  const wasOn = invertButton.getAttribute("aria-pressed") === "true";
  invertButton.setAttribute("aria-pressed", String(!wasOn));
  invertButton.textContent = wasOn ? "Aus" : "An";
  image.style.filter = wasOn ? "none" : "invert(1)";
});

speedInput.addEventListener("input", () => {
  speedValue.textContent = `${speedInput.value} ms`;
});

const zoomLevels = [1, 1.5, 2, 3, 4, 6, 8];
let zoomIndex = 0;
let zoomDirection = 1;
let offsetX = 0;
let offsetY = 0;
let activePointerId = null;
let dragged = false;
let startX = 0;
let startY = 0;
let lastX = 0;
let lastY = 0;

function zoomEnabled() {
  return zoomButton.getAttribute("aria-pressed") === "true";
}

function showZoom() {
  const scale = zoomLevels[zoomIndex];

  image.style.transform = scale === 1
    ? ""
    : `translate(${offsetX}px, ${offsetY}px) scale(${scale})`;

  image.classList.toggle("zoomed", scale > 1);
}

zoomButton.addEventListener("click", () => {
  const enabled = !zoomEnabled();
  zoomButton.setAttribute("aria-pressed", String(enabled));
  zoomInfo.hidden = !enabled;
  image.classList.toggle("zoom-ready", enabled);

  if (!enabled) {
    zoomIndex = 0;
    zoomDirection = 1;
    offsetX = 0;
    offsetY = 0;
    activePointerId = null;
    image.classList.remove("dragging");
    showZoom();
  }
});

function zoomAt(x, y) {
  const currentScale = zoomLevels[zoomIndex];
  const nextIndex = zoomIndex + zoomDirection;
  const nextScale = zoomLevels[nextIndex];

  if (nextIndex === 0) {
    offsetX = 0;
    offsetY = 0;
  } else {
    const rect = image.getBoundingClientRect();
    const factor = nextScale / currentScale;

    offsetX -= (x - rect.left) * (factor - 1);
    offsetY -= (y - rect.top) * (factor - 1);
  }

  zoomIndex = nextIndex;

  if (zoomIndex === 0 || zoomIndex === zoomLevels.length - 1) {
    zoomDirection *= -1;
  }

  showZoom();
}

image.addEventListener("pointerdown", (event) => {
  if (!zoomEnabled() || activePointerId !== null) return;

  activePointerId = event.pointerId;
  dragged = false;
  startX = lastX = event.clientX;
  startY = lastY = event.clientY;
  image.setPointerCapture(event.pointerId);
});

image.addEventListener("pointermove", (event) => {
  if (event.pointerId !== activePointerId) return;

  const dx = event.clientX - lastX;
  const dy = event.clientY - lastY;

  if (Math.hypot(event.clientX - startX, event.clientY - startY) > 5) {
    dragged = true;
  }

  if (dragged && zoomIndex > 0) {
    offsetX += dx;
    offsetY += dy;
    showZoom();
    image.classList.add("dragging");
  }

  lastX = event.clientX;
  lastY = event.clientY;
});

image.addEventListener("pointerup", (event) => {
  if (event.pointerId !== activePointerId) return;

  activePointerId = null;
  image.classList.remove("dragging");
  image.releasePointerCapture(event.pointerId);

  if (!dragged) {
    zoomAt(event.clientX, event.clientY);
  }
});

image.addEventListener("pointercancel", () => {
  activePointerId = null;
  image.classList.remove("dragging");
});