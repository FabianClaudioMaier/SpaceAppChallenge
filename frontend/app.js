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
const coordinateGlobe = document.querySelector("#coordinate-globe");
const globeStatus = document.querySelector("#globe-status");
const globeContext = coordinateGlobe.getContext("2d");

const angleComponent = String.raw`(?:\d+(?:\.\d*)?|\.\d+)`;
const decimalAnglePattern = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i;
let globeTarget = null;
let globeYaw = -0.48;
let globePitch = 0.32;
let globeDrag = null;
let earthTexture = null;
let earthTextureError = null;
const earthTextureImage = new Image();
earthTextureImage.crossOrigin = "anonymous";
earthTextureImage.addEventListener("load", () => {
  const textureCanvas = document.createElement("canvas");
  textureCanvas.width = earthTextureImage.naturalWidth;
  textureCanvas.height = earthTextureImage.naturalHeight;
  const textureContext = textureCanvas.getContext("2d", { willReadFrequently: true });

  if (!textureContext) {
    earthTextureError = new Error("Could not create an Earth texture canvas");
    console.error(earthTextureError);
    globeStatus.textContent = earthTextureError.message;
    return;
  }

  try {
    textureContext.drawImage(earthTextureImage, 0, 0);
    earthTexture = {
      width: textureCanvas.width,
      height: textureCanvas.height,
      pixels: textureContext.getImageData(
        0,
        0,
        textureCanvas.width,
        textureCanvas.height
      ).data,
    };
    earthTextureError = null;
    drawCelestialGlobe();
  } catch (error) {
    earthTextureError = error;
    console.error("Could not read the remote Earth texture:", error);
    globeStatus.textContent = "The Earth texture could not be read; check the network connection.";
  }
}, { once: true });
earthTextureImage.addEventListener("error", () => {
  earthTextureError = new Error("The remote Earth texture failed to load");
  console.error(earthTextureError);
  globeStatus.textContent = "The Earth texture could not be loaded; check the network connection.";
}, { once: true });
earthTextureImage.src = "https://threejs.org/examples/textures/planets/earth_atmos_2048.jpg";

function parseBackendAngle(rawValue, coordinate) {
  const value = rawValue.trim();
  if (!value) {
    throw new Error(`${coordinate} is empty`);
  }

  let degrees;
  if (decimalAnglePattern.test(value)) {
    degrees = Number(value);
  } else {
    const normalized = value
      .replace(/[°º]/g, "d")
      .replace(/[′']/g, "m")
      .replace(/[″"]/g, "s");
    const explicitUnitPattern = new RegExp(
      `^([+-]?${angleComponent})\\s*(hourangle|hours?|hr|h|degrees?|deg|d)` +
        `\\s*(?:(${angleComponent})\\s*(?:minutes?|min|m))?` +
        `\\s*(?:(${angleComponent})\\s*(?:seconds?|sec|s))?$`,
      "i"
    );
    const explicitMatch = normalized.match(explicitUnitPattern);

    if (explicitMatch) {
      const sign = explicitMatch[1].startsWith("-") ? -1 : 1;
      const magnitude =
        Math.abs(Number(explicitMatch[1])) +
        Number(explicitMatch[3] ?? 0) / 60 +
        Number(explicitMatch[4] ?? 0) / 3600;
      const isHours = explicitMatch[2].toLowerCase().startsWith("h");
      degrees = sign * magnitude * (isHours ? 15 : 1);
    } else {
      const sexagesimalPattern = new RegExp(
        `^([+-]?${angleComponent})(?:\\s*:\\s*|\\s+)` +
          `(${angleComponent})(?:(?:\\s*:\\s*|\\s+)(${angleComponent}))?$`
      );
      const sexagesimalMatch = normalized.match(sexagesimalPattern);
      if (!sexagesimalMatch) {
        throw new Error(`${coordinate} format is not supported by the globe preview`);
      }

      // Preserve the sign separately so values such as "-00:30:00" stay negative.
      const sign = sexagesimalMatch[1].startsWith("-") ? -1 : 1;
      const magnitude =
        Math.abs(Number(sexagesimalMatch[1])) +
        Number(sexagesimalMatch[2]) / 60 +
        Number(sexagesimalMatch[3] ?? 0) / 3600;
      degrees = sign * magnitude * (coordinate === "RA" ? 15 : 1);
    }
  }

  const minimum = coordinate === "RA" ? 0 : -90;
  const maximum = coordinate === "RA" ? 360 : 90;
  if (!Number.isFinite(degrees) || degrees < minimum || degrees > maximum) {
    throw new Error(`${coordinate} must be between ${minimum}° and ${maximum}°`);
  }
  return degrees;
}

function coordinateVector(raDegrees, decDegrees) {
  const ra = (raDegrees * Math.PI) / 180;
  const dec = (decDegrees * Math.PI) / 180;
  return {
    x: Math.cos(dec) * Math.cos(ra),
    y: Math.sin(dec),
    z: Math.cos(dec) * Math.sin(ra),
  };
}

function greenwichMeanSiderealDegrees(date) {
  const julianDate = date.getTime() / 86400000 + 2440587.5;
  const centuries = (julianDate - 2451545) / 36525;
  const degrees =
    280.46061837 +
    360.98564736629 * (julianDate - 2451545) +
    0.000387933 * centuries ** 2 -
    centuries ** 3 / 38710000;
  return ((degrees % 360) + 360) % 360;
}

function drawCelestialGlobe() {
  const bounds = coordinateGlobe.getBoundingClientRect();
  if (!bounds.width || !bounds.height) return;

  const pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
  coordinateGlobe.width = Math.round(bounds.width * pixelRatio);
  coordinateGlobe.height = Math.round(bounds.height * pixelRatio);
  globeContext.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);

  const width = bounds.width;
  const height = bounds.height;
  const centerX = width / 2;
  const centerY = height / 2;
  const radius = Math.min(width * 0.29, height * 0.4);
  const yawCos = Math.cos(globeYaw);
  const yawSin = Math.sin(globeYaw);
  const pitchCos = Math.cos(globePitch);
  const pitchSin = Math.sin(globePitch);
  const context = globeContext;

  function project(vector) {
    const rotatedX = yawCos * vector.x + yawSin * vector.z;
    const rotatedZ = -yawSin * vector.x + yawCos * vector.z;
    const rotatedY = pitchCos * vector.y - pitchSin * rotatedZ;
    const depth = pitchSin * vector.y + pitchCos * rotatedZ;
    return {
      x: centerX + radius * rotatedX,
      y: centerY - radius * rotatedY,
      depth,
    };
  }

  function drawCurve(points, frontColor, backColor, lineWidth) {
    for (const frontSide of [false, true]) {
      context.beginPath();
      let drawing = false;
      for (let index = 1; index < points.length; index += 1) {
        const start = points[index - 1];
        const end = points[index];
        const isFront = (start.depth + end.depth) >= 0;
        if (isFront !== frontSide) {
          drawing = false;
          continue;
        }
        if (!drawing) {
          context.moveTo(start.x, start.y);
          drawing = true;
        }
        context.lineTo(end.x, end.y);
      }
      context.strokeStyle = frontSide ? frontColor : backColor;
      context.lineWidth = lineWidth;
      context.setLineDash(frontSide ? [] : [3, 5]);
      context.stroke();
    }
    context.setLineDash([]);
  }

  function drawPoint(vector, label, color, offsetX = 0, offsetY = 0) {
    const point = project(vector);
    context.beginPath();
    context.arc(point.x, point.y, 3.5, 0, Math.PI * 2);
    context.fillStyle = color;
    context.fill();
    context.font = "12px 'DM Sans', sans-serif";
    context.fillStyle = "#e6eeff";
    context.fillText(label, point.x + offsetX, point.y + offsetY);
  }

  function drawEarthSurface() {
    const resolution = Math.max(2, Math.round(radius * 2));
    const surfaceCanvas = document.createElement("canvas");
    surfaceCanvas.width = resolution;
    surfaceCanvas.height = resolution;
    const surfaceContext = surfaceCanvas.getContext("2d");
    if (!surfaceContext) return;

    const surfaceImage = surfaceContext.createImageData(resolution, resolution);
    const gmstDegrees = greenwichMeanSiderealDegrees(new Date());
    const textureWidth = earthTexture.width;
    const textureHeight = earthTexture.height;
    const sourcePixels = earthTexture.pixels;
    const targetPixels = surfaceImage.data;
    const halfResolution = resolution / 2;

    for (let y = 0; y < resolution; y += 1) {
      const cameraY = -(y + 0.5 - halfResolution) / halfResolution;
      for (let x = 0; x < resolution; x += 1) {
        const cameraX = (x + 0.5 - halfResolution) / halfResolution;
        const depthSquared = 1 - cameraX * cameraX - cameraY * cameraY;
        if (depthSquared <= 0) continue;

        const cameraZ = Math.sqrt(depthSquared);
        const rotatedY = pitchCos * cameraY + pitchSin * cameraZ;
        const rotatedZ = -pitchSin * cameraY + pitchCos * cameraZ;
        const worldX = yawCos * cameraX - yawSin * rotatedZ;
        const worldZ = yawSin * cameraX + yawCos * rotatedZ;
        const latitude = Math.asin(
          Math.max(-1, Math.min(1, rotatedY))
        ) * 180 / Math.PI;
        const rightAscension = ((Math.atan2(worldZ, worldX) * 180 / Math.PI) + 360) % 360;
        let longitude = rightAscension - gmstDegrees;
        longitude = ((longitude + 180) % 360 + 360) % 360 - 180;

        const longitudeU = (longitude + 180) / 360;
        const textureU = (1 - longitudeU) % 1;
        const textureX = Math.floor(textureU * textureWidth);
        const textureY = Math.min(
          textureHeight - 1,
          Math.floor(((90 - latitude) / 180) * textureHeight)
        );
        const sourceOffset = (textureY * textureWidth + textureX) * 4;
        const targetOffset = (y * resolution + x) * 4;
        targetPixels[targetOffset] = sourcePixels[sourceOffset];
        targetPixels[targetOffset + 1] = sourcePixels[sourceOffset + 1];
        targetPixels[targetOffset + 2] = sourcePixels[sourceOffset + 2];
        targetPixels[targetOffset + 3] = 238;
      }
    }

    surfaceContext.putImageData(surfaceImage, 0, 0);
    context.drawImage(
      surfaceCanvas,
      centerX - radius,
      centerY - radius,
      radius * 2,
      radius * 2
    );

    context.save();
    context.beginPath();
    context.arc(centerX, centerY, radius, 0, Math.PI * 2);
    context.clip();
    const shade = context.createRadialGradient(
      centerX - radius * 0.35,
      centerY - radius * 0.38,
      radius * 0.08,
      centerX,
      centerY,
      radius
    );
    shade.addColorStop(0, "rgba(255, 255, 255, 0.08)");
    shade.addColorStop(0.7, "rgba(0, 0, 0, 0)");
    shade.addColorStop(1, "rgba(0, 5, 20, 0.48)");
    context.fillStyle = shade;
    context.fillRect(centerX - radius, centerY - radius, radius * 2, radius * 2);
    context.restore();
  }

  context.clearRect(0, 0, width, height);
  if (earthTexture) {
    drawEarthSurface();
  } else {
    const sphereFill = context.createRadialGradient(
      centerX - radius * 0.32,
      centerY - radius * 0.38,
      radius * 0.04,
      centerX,
      centerY,
      radius
    );
    sphereFill.addColorStop(0, "rgba(90, 145, 210, 0.12)");
    sphereFill.addColorStop(1, "rgba(20, 45, 85, 0.05)");
    context.beginPath();
    context.arc(centerX, centerY, radius, 0, Math.PI * 2);
    context.fillStyle = sphereFill;
    context.fill();
  }

  for (let dec = -75; dec <= 75; dec += 15) {
    if (dec === 0) continue;
    const points = [];
    for (let ra = 0; ra <= 360; ra += 5) {
      points.push(project(coordinateVector(ra, dec)));
    }
    drawCurve(points, "rgba(145, 180, 230, 0.38)", "rgba(100, 140, 195, 0.18)", 0.8);
  }

  for (let ra = 0; ra < 360; ra += 30) {
    const points = [];
    for (let dec = -90; dec <= 90; dec += 4) {
      points.push(project(coordinateVector(ra, dec)));
    }
    drawCurve(points, "rgba(145, 180, 230, 0.38)", "rgba(100, 140, 195, 0.18)", 0.8);
  }

  const equator = [];
  for (let ra = 0; ra <= 360; ra += 4) {
    equator.push(project(coordinateVector(ra, 0)));
  }
  drawCurve(equator, "rgba(108, 169, 255, 0.95)", "rgba(108, 169, 255, 0.4)", 1.7);

  context.beginPath();
  context.arc(centerX, centerY, radius, 0, Math.PI * 2);
  context.strokeStyle = "rgba(190, 214, 255, 0.8)";
  context.lineWidth = 1.2;
  context.stroke();

  drawPoint({ x: 0, y: 1, z: 0 }, "NCP", "#a9c8ff", 6, -5);
  drawPoint({ x: 0, y: -1, z: 0 }, "SCP", "#a9c8ff", 6, 14);

  if (globeTarget) {
    const surfacePoint = project(globeTarget.vector);
    const endpoint = project({
      x: globeTarget.vector.x * 1.08,
      y: globeTarget.vector.y * 1.08,
      z: globeTarget.vector.z * 1.08,
    });
    const deltaX = endpoint.x - centerX;
    const deltaY = endpoint.y - centerY;
    const length = Math.hypot(deltaX, deltaY);

    context.beginPath();
    context.moveTo(centerX, centerY);
    context.lineTo(endpoint.x, endpoint.y);
    context.strokeStyle = "#ff4d5a";
    context.lineWidth = 2.5;
    context.setLineDash([]);
    context.stroke();

    if (length > 8) {
      const unitX = deltaX / length;
      const unitY = deltaY / length;
      const headLength = 12;
      const headWidth = 6;
      context.beginPath();
      context.moveTo(endpoint.x, endpoint.y);
      context.lineTo(
        endpoint.x - unitX * headLength - unitY * headWidth,
        endpoint.y - unitY * headLength + unitX * headWidth
      );
      context.lineTo(
        endpoint.x - unitX * headLength + unitY * headWidth,
        endpoint.y - unitY * headLength - unitX * headWidth
      );
      context.closePath();
      context.fillStyle = "#ff4d5a";
      context.fill();
    }

    context.beginPath();
    context.arc(surfacePoint.x, surfacePoint.y, 4, 0, Math.PI * 2);
    context.fillStyle = "#ff4d5a";
    context.fill();
    context.beginPath();
    context.arc(endpoint.x, endpoint.y, 7, 0, Math.PI * 2);
    context.strokeStyle = "rgba(255, 77, 90, 0.9)";
    context.lineWidth = 1.2;
    context.stroke();
  }
}

function updateCelestialTarget(raDegrees, decDegrees) {
  globeTarget = {
    raDegrees,
    decDegrees,
    vector: coordinateVector(raDegrees, decDegrees),
  };
  globeStatus.textContent =
    `Target: RA ${raDegrees.toFixed(4)}°, Dec ${decDegrees.toFixed(4)}°. ` +
    (earthTextureError
      ? "Earth texture unavailable; check the network. "
      : "") +
    "Drag the sphere to rotate it.";
  drawCelestialGlobe();
}

coordinateGlobe.addEventListener("pointerdown", (event) => {
  globeDrag = { x: event.clientX, y: event.clientY };
  coordinateGlobe.setPointerCapture(event.pointerId);
});

coordinateGlobe.addEventListener("pointermove", (event) => {
  if (!globeDrag) return;
  globeYaw += (event.clientX - globeDrag.x) * 0.01;
  globePitch = Math.max(
    -1.45,
    Math.min(1.45, globePitch + (event.clientY - globeDrag.y) * 0.01)
  );
  globeDrag = { x: event.clientX, y: event.clientY };
  drawCelestialGlobe();
  setInterval(drawCelestialGlobe, 10000);
});

coordinateGlobe.addEventListener("pointerup", () => {
  globeDrag = null;
});

coordinateGlobe.addEventListener("pointercancel", () => {
  globeDrag = null;
});

if ("ResizeObserver" in window) {
  new ResizeObserver(drawCelestialGlobe).observe(coordinateGlobe);
} else {
  window.addEventListener("resize", drawCelestialGlobe);
}
drawCelestialGlobe();

let currentVideoUuid = localStorage.getItem("videoUuid");
let activeVideoPoll = 0;

async function displayVideo(uuid) {
  const poll = ++activeVideoPoll;
  const videoUrl = `/api/videos/${encodeURIComponent(uuid)}.gif`;

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

  let previewWasUpdated = false;
  try {
    const raDegrees = parseBackendAngle(rightAscensionInput.value, "RA");
    const decDegrees = parseBackendAngle(declinationInput.value, "DEC");
    updateCelestialTarget(raDegrees, decDegrees);
    previewWasUpdated = true;
  } catch (error) {
    globeStatus.textContent =
      `Globe preview unavailable: ${error.message}. ` +
      "The original coordinates will still be sent to the backend for validation.";
  }

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
    if (!previewWasUpdated) {
      globeStatus.textContent =
        "The backend accepted the coordinates, but the globe could not parse their format.";
    }
    void displayVideo(uuid);
    console.info("Middleware returned UUID:", currentVideoUuid);
  } catch (error) {
    console.error("Submission failed:", error);
    if (!previewWasUpdated) {
      globeStatus.textContent = `Submission failed: ${error.message}`;
    }
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
