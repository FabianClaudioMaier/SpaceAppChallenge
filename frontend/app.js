const settingsButton = document.querySelector("#settings-button");
const settingsPanel = document.querySelector("#settings-panel");
const invertButton = document.querySelector("#invert-pixel");
const speedInput = document.querySelector("#speed");
const speedValue = document.querySelector("#speed-value");
const image = document.querySelector("#result-image");
const pixelSizeInput = document.querySelector("#pixel-size");
const pixelSizeValue = document.querySelector("#pixel-size-value");
const zoomSelection = document.querySelector("#zoom-selection");
const zoomDialog = document.querySelector("#zoom-dialog");
const zoomCanvas = document.querySelector("#zoom-canvas");
const zoomClose = document.querySelector("#zoom-close");
const coordinateForm = document.querySelector("#coordinate-form");
const rightAscensionInput = document.querySelector("#fname");
const declinationInput = document.querySelector("#lname");
const submitButton = coordinateForm.querySelector('input[type="submit"]');

let currentVideoUuid = localStorage.getItem("videoUuid");
let activeVideoPoll = 0;

async function displayVideo(uuid) {
  const poll = ++activeVideoPoll;
  const videoUrl = `/frontend/video_cache/${encodeURIComponent(uuid)}.gif`;

  while (poll === activeVideoPoll) {
    try {
      const response = await fetch(videoUrl, { method: "HEAD", cache: "no-store" });
      if (response.ok) {
        if (poll === activeVideoPoll) {
          image.src = videoUrl;
          image.alt = "Generated astronomy animation";
        }
        return;
      }
      if (response.status !== 404) {
        console.warn(`Video request failed: HTTP ${response.status}`);
      }
    } catch (error) {
      console.warn("Waiting for the video server:", error);
    }
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
}

if (currentVideoUuid) {
  void displayVideo(currentVideoUuid);
}

rightAscensionInput.value = localStorage.getItem("rightAscension") ?? "";
declinationInput.value = localStorage.getItem("declination") ?? "";

rightAscensionInput.addEventListener("input", () => {
  localStorage.setItem("rightAscension", rightAscensionInput.value);
});

declinationInput.addEventListener("input", () => {
  localStorage.setItem("declination", declinationInput.value);
});

async function requestVideoUuid(DEC, RA, PIXELS, MS) {
  const response = await fetch("/api/send-to-backend", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ DEC, RA, PIXELS, MS }),
  });
  const result = await response.json();

  if (!response.ok) {
    throw new Error(result.error ?? `HTTP ${response.status}`);
  }
  if (typeof result.uuid !== "string" || !result.uuid) {
    throw new Error("Middleware returned no UUID");
  }
  return result.uuid;
}

coordinateForm.addEventListener("submit", async (event) => {
  event.preventDefault();

  submitButton.disabled = true;
  try {
    const uuid = await requestVideoUuid(
      declinationInput.value,
      rightAscensionInput.value,
      Number(pixelSizeInput.value),
      Number(speedInput.value)
    );
    currentVideoUuid = uuid;
    localStorage.setItem("videoUuid", uuid);
    image.removeAttribute("src");
    image.alt = "Astronomy preview while the video is created";
    void displayVideo(uuid);
    console.info("Middleware returned UUID:", currentVideoUuid);
  } catch (error) {
    console.error("Submission failed:", error);
    alert(`Submission failed: ${error.message}`);
  } finally {
    submitButton.disabled = false;
  }
});

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
pixelSizeInput.addEventListener("input", () => {
  pixelSizeValue.textContent = pixelSizeInput.value;
});

image.classList.add("zoom-ready");

let selectionStart = null;
let activePointerId = null;

function clamp(value, min, max) {
  return Math.min(Math.max(value, min), max);
}

function openZoom(region, imageRect) {
  const scaleX = image.naturalWidth / imageRect.width;
  const scaleY = image.naturalHeight / imageRect.height;
  const sourceX = (region.left - imageRect.left) * scaleX;
  const sourceY = (region.top - imageRect.top) * scaleY;
  const sourceWidth = region.width * scaleX;
  const sourceHeight = region.height * scaleY;

  zoomCanvas.width = Math.max(1, Math.round(sourceWidth));
  zoomCanvas.height = Math.max(1, Math.round(sourceHeight));

  const context = zoomCanvas.getContext("2d");
  context.drawImage(
    image,
    sourceX, sourceY, sourceWidth, sourceHeight,
    0, 0, zoomCanvas.width, zoomCanvas.height
  );

  const availableWidth = Math.min(window.innerWidth - 32, 1000);
  const availableHeight = Math.min(window.innerHeight - 64, 800);
  const displayScale = Math.min(
    availableWidth / zoomCanvas.width,
    availableHeight / zoomCanvas.height
  );

  zoomCanvas.style.width = `${Math.round(zoomCanvas.width * displayScale)}px`;
  zoomCanvas.style.height = `${Math.round(zoomCanvas.height * displayScale)}px`;
  zoomCanvas.style.filter = image.style.filter;
  zoomDialog.showModal();
}

image.addEventListener("pointerdown", (event) => {
  if (activePointerId !== null || !image.naturalWidth) return;

  activePointerId = event.pointerId;
  selectionStart = {
    x: event.clientX,
    y: event.clientY,
    rect: image.getBoundingClientRect(),
  };
  image.setPointerCapture(event.pointerId);
  event.preventDefault();
});

image.addEventListener("pointermove", (event) => {
  if (event.pointerId !== activePointerId) return;

  const { x, y, rect } = selectionStart;
  const endX = clamp(event.clientX, rect.left, rect.right);
  const endY = clamp(event.clientY, rect.top, rect.bottom);
  const width = Math.abs(endX - x);
  const height = Math.abs(endY - y);

  zoomSelection.hidden = width < 6 && height < 6;
  zoomSelection.style.left = `${Math.min(x, endX)}px`;
  zoomSelection.style.top = `${Math.min(y, endY)}px`;
  zoomSelection.style.width = `${width}px`;
  zoomSelection.style.height = `${height}px`;
});

image.addEventListener("pointerup", (event) => {
  if (event.pointerId !== activePointerId) return;

  const { x, y, rect } = selectionStart;
  activePointerId = null;
  selectionStart = null;
  zoomSelection.hidden = true;

  if (image.hasPointerCapture(event.pointerId)) {
    image.releasePointerCapture(event.pointerId);
  }

  const endX = clamp(event.clientX, rect.left, rect.right);
  const endY = clamp(event.clientY, rect.top, rect.bottom);
  const width = Math.abs(endX - x);
  const height = Math.abs(endY - y);

  if (width >= 8 && height >= 8) {
    openZoom({
      left: Math.min(x, endX),
      top: Math.min(y, endY),
      width,
      height,
    }, rect);
  } else {
    const cropWidth = rect.width * 0.3;
    const cropHeight = rect.height * 0.3;

    openZoom({
      left: clamp(endX - cropWidth / 2, rect.left, rect.right - cropWidth),
      top: clamp(endY - cropHeight / 2, rect.top, rect.bottom - cropHeight),
      width: cropWidth,
      height: cropHeight,
    }, rect);
  }
});

image.addEventListener("pointercancel", (event) => {
  if (event.pointerId !== activePointerId) return;
  activePointerId = null;
  selectionStart = null;
  zoomSelection.hidden = true;
});

zoomClose.addEventListener("click", () => zoomDialog.close());

zoomDialog.addEventListener("click", (event) => {
  const rect = zoomDialog.getBoundingClientRect();

  if (
    event.clientX < rect.left || event.clientX > rect.right ||
    event.clientY < rect.top || event.clientY > rect.bottom
  ) {
    zoomDialog.close();
  }
});
