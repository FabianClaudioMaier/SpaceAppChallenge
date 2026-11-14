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
const zoomView = document.querySelector("#zoom-view");
const zoomImage = document.querySelector("#zoom-image");
const zoomClose = document.querySelector("#zoom-close");
const clickSelectToggle = document.querySelector("#click-select-toggle");
const pinArrowToggle = document.querySelector("#pin-arrow-toggle");
const applyGlobeCoordinatesButton = document.querySelector("#apply-globe-coordinates");
const coordinateForm = document.querySelector("#coordinate-form");
const rightAscensionInput = document.querySelector("#fname");
const declinationInput = document.querySelector("#lname");
const submitButton = coordinateForm.querySelector('input[type="submit"]');
const applySettingsButton = document.querySelector("#apply-settings");
let appliedPixelSize = Number(pixelSizeInput.value);
let appliedSpeed = Number(speedInput.value);
const coordinateGlobe = document.querySelector("#coordinate-globe");
const globeStatus = document.querySelector("#globe-status");
const globeContext = coordinateGlobe.getContext("2d");

const angleComponent = String.raw`(?:\d+(?:\.\d*)?|\.\d+)`;
const decimalAnglePattern = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i;
let globeTarget = null;
let globeYaw = -0.48;
let globePitch = 0.32;
let globeDrag = null;
let globeInteractionMode = "rotate";
let pinnedArrowUv = null;
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
earthTextureImage.src = "./earth.jpg";

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

function createGlobeProjection(width, height) {
  const centerX = width / 2;
  const centerY = height / 2;
  const radius = Math.min(width * 0.29, height * 0.4);
  const yawCos = Math.cos(globeYaw);
  const yawSin = Math.sin(globeYaw);
  const pitchCos = Math.cos(globePitch);
  const pitchSin = Math.sin(globePitch);

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

  function unproject(x, y) {
    const cameraX = (x - centerX) / radius;
    const cameraY = (centerY - y) / radius;
    const depthSquared = 1 - cameraX * cameraX - cameraY * cameraY;
    if (depthSquared < -1e-8) return null;

    const cameraZ = Math.sqrt(Math.max(0, depthSquared));
    const rotatedY = pitchCos * cameraY + pitchSin * cameraZ;
    const rotatedZ = -pitchSin * cameraY + pitchCos * cameraZ;
    return {
      x: yawCos * cameraX - yawSin * rotatedZ,
      y: rotatedY,
      z: yawSin * cameraX + yawCos * rotatedZ,
    };
  }

  return {
    width,
    height,
    centerX,
    centerY,
    radius,
    yawCos,
    yawSin,
    pitchCos,
    pitchSin,
    project,
    unproject,
  };
}

function drawCelestialGlobe() {
  const bounds = coordinateGlobe.getBoundingClientRect();
  if (!bounds.width || !bounds.height) return;

  const pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
  coordinateGlobe.width = Math.round(bounds.width * pixelRatio);
  coordinateGlobe.height = Math.round(bounds.height * pixelRatio);
  globeContext.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);

  const projection = createGlobeProjection(bounds.width, bounds.height);
  const {
    width,
    height,
    centerX,
    centerY,
    radius,
    yawCos,
    yawSin,
    pitchCos,
    pitchSin,
  } = projection;
  const context = globeContext;

  function project(vector) {
    return projection.project(vector);
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
    const isPinned = globeInteractionMode === "pin-arrow" && pinnedArrowUv;
    const surfacePoint = isPinned
      ? { x: pinnedArrowUv.u * width, y: pinnedArrowUv.v * height }
      : project(globeTarget.vector);
    const endpoint = isPinned
      ? {
          x: centerX + (surfacePoint.x - centerX) * 1.08,
          y: centerY + (surfacePoint.y - centerY) * 1.08,
        }
      : project({
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

function globeInteractionHint() {
  if (globeInteractionMode === "click-select") {
    return "Click a visible point to select coordinates.";
  }
  if (globeInteractionMode === "pin-arrow") {
    return "Drag the globe under the arrow, then apply the coordinates.";
  }
  return "Drag the sphere to rotate it.";
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
      : "") + globeInteractionHint();
  drawCelestialGlobe();
}

function vectorToCoordinates(vector) {
  return {
    raDegrees: ((Math.atan2(vector.z, vector.x) * 180 / Math.PI) + 360) % 360,
    decDegrees: Math.asin(Math.max(-1, Math.min(1, vector.y))) * 180 / Math.PI,
  };
}

function setInputsFromGlobeVector(vector) {
  const { raDegrees, decDegrees } = vectorToCoordinates(vector);
  rightAscensionInput.value = raDegrees.toFixed(4);
  declinationInput.value = decDegrees.toFixed(4);
  rightAscensionInput.dispatchEvent(new Event("input", { bubbles: true }));
  declinationInput.dispatchEvent(new Event("input", { bubbles: true }));
  updateCelestialTarget(raDegrees, decDegrees);
}

function setGlobeInteractionMode(mode) {
  if (mode === "pin-arrow") {
    try {
      updateCelestialTarget(
        parseBackendAngle(rightAscensionInput.value, "RA"),
        parseBackendAngle(declinationInput.value, "DEC")
      );
    } catch (error) {
      globeStatus.textContent = `Enter valid coordinates before pinning the arrow: ${error.message}`;
      return;
    }
  }

  globeInteractionMode = mode;
  const clickSelectEnabled = mode === "click-select";
  const pinArrowEnabled = mode === "pin-arrow";
  clickSelectToggle.setAttribute("aria-checked", String(clickSelectEnabled));
  pinArrowToggle.setAttribute("aria-checked", String(pinArrowEnabled));
  applyGlobeCoordinatesButton.hidden = !pinArrowEnabled;
  coordinateGlobe.classList.toggle("selecting-coordinates", clickSelectEnabled);

  pinnedArrowUv = null;
  if (pinArrowEnabled && globeTarget) {
    const bounds = coordinateGlobe.getBoundingClientRect();
    const projection = createGlobeProjection(bounds.width, bounds.height);
    const point = projection.project(globeTarget.vector);
    pinnedArrowUv = {
      u: point.x / projection.width,
      v: point.y / projection.height,
    };
  }

  if (globeTarget) {
    updateCelestialTarget(globeTarget.raDegrees, globeTarget.decDegrees);
  } else {
    globeStatus.textContent = clickSelectEnabled
      ? "Click a visible point on the globe to select coordinates."
      : "Enter coordinates to point the globe.";
    drawCelestialGlobe();
  }
}

function selectGlobePoint(clientX, clientY) {
  const bounds = coordinateGlobe.getBoundingClientRect();
  const projection = createGlobeProjection(bounds.width, bounds.height);
  const vector = projection.unproject(clientX - bounds.left, clientY - bounds.top);
  if (!vector) {
    globeStatus.textContent = "Click inside the visible globe to select coordinates.";
    return;
  }
  setInputsFromGlobeVector(vector);
}

function applyPinnedGlobePoint() {
  if (!pinnedArrowUv) return;

  const bounds = coordinateGlobe.getBoundingClientRect();
  const projection = createGlobeProjection(bounds.width, bounds.height);
  const vector = projection.unproject(
    pinnedArrowUv.u * projection.width,
    pinnedArrowUv.v * projection.height
  );
  if (!vector) {
    globeStatus.textContent = "The pinned arrow is outside the visible globe.";
    return;
  }
  setInputsFromGlobeVector(vector);
}

clickSelectToggle.addEventListener("click", () => {
  setGlobeInteractionMode(
    globeInteractionMode === "click-select" ? "rotate" : "click-select"
  );
});

pinArrowToggle.addEventListener("click", () => {
  setGlobeInteractionMode(
    globeInteractionMode === "pin-arrow" ? "rotate" : "pin-arrow"
  );
});

applyGlobeCoordinatesButton.addEventListener("click", applyPinnedGlobePoint);

coordinateGlobe.addEventListener("pointerdown", (event) => {
  if (globeInteractionMode === "click-select") return;
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
});

coordinateGlobe.addEventListener("pointerup", () => {
  globeDrag = null;
});

coordinateGlobe.addEventListener("pointercancel", () => {
  globeDrag = null;
});

coordinateGlobe.addEventListener("click", (event) => {
  if (globeInteractionMode === "click-select") {
    selectGlobePoint(event.clientX, event.clientY);
  }
});

if ("ResizeObserver" in window) {
  new ResizeObserver(drawCelestialGlobe).observe(coordinateGlobe);
} else {
  window.addEventListener("resize", drawCelestialGlobe);
}
drawCelestialGlobe();
setInterval(drawCelestialGlobe, 10000);

let currentVideoUuid = localStorage.getItem("videoUuid");
let activeVideoPoll = 0;
let videoHeartbeatTimer = null;
const VIDEO_HEARTBEAT_INTERVAL_MS = 5 * 60 * 1000;

function stopVideoHeartbeat() {
  if (videoHeartbeatTimer !== null) {
    clearInterval(videoHeartbeatTimer);
    videoHeartbeatTimer = null;
  }
}

async function sendVideoHeartbeat(uuid) {
  if (uuid !== currentVideoUuid) return;
  const videoUrl = `/api/videos/${encodeURIComponent(uuid)}.gif`;

  try {
    const response = await fetch(videoUrl, { method: "HEAD", cache: "no-store" });
    if (response.status === 404) {
      if (uuid === currentVideoUuid) stopVideoHeartbeat();
      return;
    }
    if (!response.ok) {
      console.warn(`Video heartbeat failed: HTTP ${response.status}`);
    }
  } catch (error) {
    console.warn("Could not refresh the video cache:", error);
  }
}

function startVideoHeartbeat(uuid) {
  stopVideoHeartbeat();
  videoHeartbeatTimer = window.setInterval(
    () => void sendVideoHeartbeat(uuid),
    VIDEO_HEARTBEAT_INTERVAL_MS
  );
}

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
          startVideoHeartbeat(uuid);
        }
        return;
      }
      if (response.status !== 404) {
        console.warn(`Video request failed: HTTP ${response.status}`);
      }
    } catch (error) {
      console.warn("Waiting for the video server:", error);
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
}

if (currentVideoUuid) {
  void displayVideo(currentVideoUuid);
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && videoHeartbeatTimer !== null) {
    void sendVideoHeartbeat(currentVideoUuid);
  }
});

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
  const text = await response.text();
  let result;
  try {
    result = JSON.parse(text);
  } catch {
    throw new Error(`Server antwortete nicht mit JSON (HTTP ${response.status}: ${text.slice(0, 80)}). Läuft start.sh?`);
  }
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
    stopVideoHeartbeat();
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
});document.addEventListener("pointerdown", (event) => {
  if (settingsPanel.hidden) return;                    // Panel ist schon zu → nichts tun
  if (event.target.closest(".settings")) return;       // Klick war im Panel oder auf dem Button → offen lassen
  settingsPanel.hidden = true;
  settingsButton.setAttribute("aria-expanded", "false");
});

invertButton.addEventListener("click", () => {
  const isOn = invertButton.getAttribute("aria-checked") === "true";
  invertButton.setAttribute("aria-checked", String(!isOn));
  image.style.filter = isOn ? "none" : "invert(1)";
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
  // The image area inside the 4px border
  const left = imageRect.left + image.clientLeft;
  const top = imageRect.top + image.clientTop;
  const width = image.clientWidth;
  const height = image.clientHeight;

  const availableWidth = Math.min(window.innerWidth - 32, 1000);
  const availableHeight = Math.min(window.innerHeight - 64, 800);
  const scale = Math.min(availableWidth / region.width, availableHeight / region.height);

  zoomView.style.width = `${Math.round(region.width * scale)}px`;
  zoomView.style.height = `${Math.round(region.height * scale)}px`;

  zoomImage.src = image.currentSrc || image.src;
  zoomImage.style.width = `${width * scale}px`;
  zoomImage.style.height = `${height * scale}px`;
  zoomImage.style.left = `${(left - region.left) * scale}px`;
  zoomImage.style.top = `${(top - region.top) * scale}px`;
  zoomImage.style.filter = image.style.filter;
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
