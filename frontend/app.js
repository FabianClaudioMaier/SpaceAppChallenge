const settingsButton = document.querySelector("#settings-button");
const settingsPanel = document.querySelector("#settings-panel");
const invertButton = document.querySelector("#invert-pixel");
const diffButton = document.querySelector("#show-diff");
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
const submitButton = document.querySelector("#submit-button");
const throbber = document.querySelector("#throbber");
const throbberText = document.querySelector("#throbber-text");
const applySettingsButton = document.querySelector("#apply-settings");
let appliedPixelSize = Number(pixelSizeInput.value);
let appliedSpeed = Number(speedInput.value);
const coordinateGlobe = document.querySelector("#coordinate-globe");
const globeStatus = document.querySelector("#globe-status");
const globeContext = coordinateGlobe.getContext("2d");
const movingYesButton = document.querySelector("#moving-yes");
const movingNoButton = document.querySelector("#moving-no");
const movingSaveButton = document.querySelector("#moving-save");
const pausedFrame = document.querySelector("#paused-frame");
const playPauseButton = document.querySelector("#play-pause-gif");
const gifControls = document.querySelector(".gif-controls");
const prevFrameButton = document.querySelector("#prev-frame");
const nextFrameButton = document.querySelector("#next-frame");

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

// "Show diffs" (difference images): the middleware saves two GIFs per job,
// <uuid>.gif (the frames) and <uuid>_diff.gif (the difference images).
let showDiff = localStorage.getItem("showDiff") === "true";
diffButton.setAttribute("aria-checked", String(showDiff));

function videoUrlFor(uuid, diff) {
  return `/api/videos/${encodeURIComponent(uuid)}${diff ? "_diff" : ""}.gif`;
}

function stopVideoHeartbeat() {
  if (videoHeartbeatTimer !== null) {
    clearInterval(videoHeartbeatTimer);
    videoHeartbeatTimer = null;
  }
}

async function sendVideoHeartbeat(uuid) {
  if (uuid !== currentVideoUuid) return;

  try {
    // Keep both GIFs alive (a HEAD request refreshes a file in the video cache),
    // so switching to the difference images later still works.
    const response = await fetch(videoUrlFor(uuid, false), { method: "HEAD", cache: "no-store" });
    if (response.status === 404) {
      if (uuid === currentVideoUuid) stopVideoHeartbeat();
      return;
    }
    if (!response.ok) {
      console.warn(`Video heartbeat failed: HTTP ${response.status}`);
    }
    await fetch(videoUrlFor(uuid, true), { method: "HEAD", cache: "no-store" });
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

// A stored video that is gone (the middleware deletes GIFs after 30 min unused)
// is given up after this many "not found" answers in a row (x 500 ms = 10 s),
// instead of asking the server for it forever.
const MAX_MISSING_POLLS = 20;

async function displayVideo(uuid) {
  const poll = ++activeVideoPoll;
  const framesUrl = videoUrlFor(uuid, false);
  let missing = 0;

  while (poll === activeVideoPoll) {
    try {
      // Wait for the frames GIF (the middleware saves it last, after the difference GIF)
      const response = await fetch(framesUrl, { method: "HEAD", cache: "no-store" });
      if (response.ok) {
        let videoUrl = framesUrl;
        if (showDiff) {
          const diffUrl = videoUrlFor(uuid, true);
          const diff = await fetch(diffUrl, { method: "HEAD", cache: "no-store" });
          if (diff.ok) {
            videoUrl = diffUrl;
          } else {
            console.info("No difference images for this video, showing the frames");
          }
        }
        if (poll === activeVideoPoll) {
          image.src = videoUrl;
          image.alt = videoUrl === framesUrl
            ? "Generated astronomy animation"
            : "Difference images: what changed between the frames";
          startVideoHeartbeat(uuid);
        }
        return;
      }
      if (response.status === 404) {
        missing += 1;
        if (missing >= MAX_MISSING_POLLS) {
          console.info("The video no longer exists on the server (cleaned up), not asking again");
          if (uuid === currentVideoUuid) {
            currentVideoUuid = null;
            localStorage.removeItem("videoUuid");
          }
          if (loadingUuid === uuid) setLoading(false);
          return;
        }
      } else {
        missing = 0;
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

const MAX_TRANSIENT_RETRIES = 3;
const MAX_RETRY_DELAY_MS = 30_000;

function retryDelayMs(response, retry) {
  const fallback = Math.min(2000 * 2 ** retry, MAX_RETRY_DELAY_MS);
  const retryAfter = response.headers.get("Retry-After");
  if (!retryAfter) return fallback;

  const seconds = Number(retryAfter);
  const suggested = Number.isFinite(seconds)
    ? seconds * 1000
    : Date.parse(retryAfter) - Date.now();
  return Number.isFinite(suggested)
    ? Math.max(0, Math.min(suggested, MAX_RETRY_DELAY_MS))
    : fallback;
}

async function requestVideoUuid() {
  for (let retry = 0; ; retry += 1) {
    const response = await fetch("/api/send-to-backend", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        DEC: declinationInput.value,
        RA: rightAscensionInput.value,
        PIXELS: Number(pixelSizeInput.value),
        MS: Number(speedInput.value),
      }),
    });
    const text = await response.text();

    if ((response.status === 502 || response.status === 503) && retry < MAX_TRANSIENT_RETRIES) {
      const delayMs = retryDelayMs(response, retry);
      loadingPhase = `IRSA unavailable (HTTP ${response.status}); retry ${retry + 1}/${MAX_TRANSIENT_RETRIES}…`;
      throbberText.textContent = loadingPhase;
      console.warn(`${loadingPhase} Waiting ${Math.ceil(delayMs / 1000)} s.`);
      await new Promise((resolve) => setTimeout(resolve, delayMs));
      loadingPhase = "Working…";
      throbberText.textContent = loadingPhase;
      continue;
    }

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
    // The submit handler keeps storing the UUID. Keep the matching metadata
    // for the next step of processing, without changing the current GIF flow.
    if (result.json !== undefined) {
      if (result.json === null || typeof result.json !== "object" || Array.isArray(result.json)) {
        throw new Error("Middleware returned invalid JSON metadata");
      }
      localStorage.setItem("videoMetadata", JSON.stringify(result.json));
    } else {
      localStorage.removeItem("videoMetadata");
    }
    return result.uuid;
  }
}

coordinateForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  // A job is running: Enter in a coordinate field or "Apply" in the settings
  // also submit this form - don't start a second job.
  if (submitButton.disabled) return;

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

  setLoading(true);
  try {
    const uuid = await requestVideoUuid();
    stopVideoHeartbeat();
    currentVideoUuid = uuid;
    loadingUuid = uuid; // the throbber stays until this job's GIF has loaded
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
    setLoading(false);
    alert(`Submission failed: ${error.message}`);
  }
});

// Throbber over the GIF (and Submit greyed out), from submitting until the
// new GIF is on screen (the request itself only returns when the backend is done).
let loadingTimer = null;
let loadingUuid = null;
let loadingPhase = "Working…";

function setLoading(on) {
  clearInterval(loadingTimer);
  loadingTimer = null;
  if (!on) loadingUuid = null;
  throbber.hidden = !on;
  image.parentElement.classList.toggle("loading", on);
  submitButton.disabled = on; // greyed out while a job runs (see style.css)
  applySettingsButton.disabled = on;
  if (on) {
    const started = Date.now();
    loadingPhase = "Working…";
    throbberText.textContent = `${loadingPhase} 0 s`;
    loadingTimer = setInterval(() => {
      throbberText.textContent = `${loadingPhase} ${Math.floor((Date.now() - started) / 1000)} s`;
    }, 1000);
  }
}

function stopLoadingIfDone() {
  // Only when this job's GIF has arrived (e.g. not when "Show diffs" swaps
  // the previous result's GIF meanwhile)
  if (loadingUuid && image.src.includes(loadingUuid)) setLoading(false);
}

image.addEventListener("load", stopLoadingIfDone);
image.addEventListener("error", stopLoadingIfDone);

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
  image.style.filter = isOn ? "none" : "invert(1)";  pausedFrame.style.filter = image.style.filter;
});

// Switches the shown GIF; "Invert GIF" is a filter on the image itself,
// so it applies to both.
diffButton.addEventListener("click", () => {
  showDiff = diffButton.getAttribute("aria-checked") !== "true";
  diffButton.setAttribute("aria-checked", String(showDiff));
  localStorage.setItem("showDiff", String(showDiff));
  if (currentVideoUuid) {
    void displayVideo(currentVideoUuid);
  }
});

speedInput.addEventListener("input", () => {
  speedValue.textContent = `${speedInput.value} ms`;
});
pixelSizeInput.addEventListener("input", () => {
  pixelSizeValue.textContent = `${pixelSizeInput.value} px`;
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
movingYesButton.addEventListener("click", () => {
  movingSaveButton.hidden = false;   // Yes → Save anzeigen
});

movingNoButton.addEventListener("click", () => {
  movingSaveButton.hidden = true;    // No → Save wieder verstecken
});

movingSaveButton.addEventListener("click", async () => {
  movingSaveButton.disabled = true;
  try {
    const metadata = localStorage.getItem("videoMetadata");
    if (!metadata) {
      throw new Error("No video metadata available to save");
    }
    const response = await fetch("/api/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: metadata,
    });
    if (!response.ok) {
      const result = await response.json().catch(() => null);
      throw new Error(result?.error ?? `HTTP ${response.status}`);
    }
    movingSaveButton.hidden = true;
  } catch (error) {
    console.error("Saving failed:", error);
    alert(`Saving failed: ${error.message}`);
  } finally {
    movingSaveButton.disabled = false;
  }
});
// Stop/Play: an <img> can't pause a GIF. So stopping splits the GIF into its
// frames (ImageDecoder) and shows the current one on a canvas over the GIF;
// playing on then runs the frames on that canvas, from where it stopped.
let gifFrames = [];      // the frames of the GIF that is showing: { bitmap, duration }
let gifFramesSrc = "";   // which GIF gifFrames belongs to
let gifStartTime = 0;    // when the <img> started playing it
let shownFrame = 0;      // number of the frame on the canvas
let canvasTimer = null;  // runs while the canvas plays
let isPaused = false;

gifControls.hidden = !("ImageDecoder" in window); // only works on localhost / https

function showPaused(paused) {
  isPaused = paused;
  playPauseButton.classList.toggle("paused", paused); // CSS swaps the icon
  playPauseButton.setAttribute("aria-label", paused ? "Play" : "Stop");
  playPauseButton.dataset.tooltip = paused ? "Play" : "Pause"; // text of the hover box
}

image.addEventListener("load", () => {
  gifStartTime = performance.now();
  clearTimeout(canvasTimer);
  pausedFrame.hidden = true; // a new GIF always starts playing in the <img>
  showPaused(false);
});

async function loadGifFrames() {
  const src = image.currentSrc || image.src;
  if (src === gifFramesSrc) return; // already split
  const data = await (await fetch(src)).arrayBuffer();
  const decoder = new ImageDecoder({ data, type: "image/gif" });
  await decoder.tracks.ready;
  const frames = [];
  for (let i = 0; i < decoder.tracks.selectedTrack.frameCount; i++) {
    const { image: frame } = await decoder.decode({ frameIndex: i });
    frames.push({
      bitmap: await createImageBitmap(frame),
      duration: (frame.duration ?? 100_000) / 1000, // microseconds -> ms
    });
    frame.close();
  }
  gifFrames = frames;
  gifFramesSrc = src;
}

// Which frame the <img> shows right now, worked out from the time since it started.
function frameShowingNow() {
  const total = gifFrames.reduce((sum, f) => sum + f.duration, 0);
  let t = (performance.now() - gifStartTime) % total;
  let i = 0;
  while (t >= gifFrames[i].duration) {
    t -= gifFrames[i].duration;
    i++;
  }
  return i;
}

function drawFrame(i) {
  shownFrame = i;
  const bitmap = gifFrames[i].bitmap;
  pausedFrame.width = bitmap.width;
  pausedFrame.height = bitmap.height;
  pausedFrame.getContext("2d").drawImage(bitmap, 0, 0);
}

// Freezes the GIF on the frame that is showing now (nothing to do if already stopped).
async function pauseGif() {
  clearTimeout(canvasTimer);
  if (pausedFrame.hidden) { // the <img> is playing: freeze its current frame
    await loadGifFrames();
    drawFrame(frameShowingNow());
    pausedFrame.style.filter = image.style.filter; // keep "Invert GIF"
    pausedFrame.hidden = false;
  }
  showPaused(true);
}

playPauseButton.addEventListener("click", async () => {
  if (!image.complete || image.naturalWidth === 0) return; // no GIF loaded yet
  if (isPaused) { // ▶ pressed: play on from the frame that is shown
    showPaused(false);
    const next = () => {
      drawFrame((shownFrame + 1) % gifFrames.length);
      canvasTimer = setTimeout(next, gifFrames[shownFrame].duration);
    };
    canvasTimer = setTimeout(next, gifFrames[shownFrame].duration);
    return;
  }
  await pauseGif(); // ⏸ pressed
});

// ⏮ / ⏭: stop and go one frame back / forward (after the last comes the first again)
async function stepFrame(step) {
  if (!image.complete || image.naturalWidth === 0) return; // no GIF loaded yet
  await pauseGif();
  drawFrame((shownFrame + step + gifFrames.length) % gifFrames.length);
}

prevFrameButton.addEventListener("click", () => stepFrame(-1));
nextFrameButton.addEventListener("click", () => stepFrame(1));