"""
Which SPHEREx detectors go into a frame, and how they are squashed into one image.

SPHEREx has 6 detector arrays. Each one sees a different slice of infrared
light (and inside a detector the wavelength changes slowly across the chip,
which is where the 102 "bands" come from: 17 per detector).

By default all six are used and averaged into a single picture. To use only
some of them later, pass e.g. parse_detectors("56") - brown dwarfs are much
brighter in D5/D6 (3.8-5.0 um) than in D1.
"""

from __future__ import annotations

import warnings

import numpy as np

ALL_DETECTORS: tuple[int, ...] = (1, 2, 3, 4, 5, 6)

# Wavelength range of each detector in micrometres (from the IRSA metadata).
WAVELENGTH_UM: dict[int, tuple[float, float]] = {
    1: (0.75, 1.12),
    2: (1.10, 1.65),
    3: (1.63, 2.44),
    4: (2.40, 3.85),
    5: (3.81, 4.43),
    6: (4.41, 5.01),
}


def parse_detectors(wanted=None) -> tuple[int, ...]:
    """None/"" -> all six. Also accepts [5, 6], (5, 6), "56" or "5,6"."""
    if wanted is None or wanted == "":
        return ALL_DETECTORS
    if isinstance(wanted, str):
        wanted = [int(ch) for ch in wanted if ch.isdigit()]
    detectors = tuple(sorted({int(d) for d in wanted}))
    invalid = [d for d in detectors if d not in ALL_DETECTORS]
    if not detectors or invalid:
        raise ValueError(f"detectors must be from {ALL_DETECTORS}, got {wanted!r}")
    return detectors


def select(exposures: list, detectors: tuple[int, ...] = ALL_DETECTORS) -> list:
    """Keep only the exposures taken with one of the wanted detectors."""
    return [e for e in exposures if e.detector in detectors]


# At most this many images go into one frame. Fewer = faster (less to download
# and align) but noisier. A frame often has 50+ images available.
# 18 = 3 per detector when all 6 are there: with >= 3 images the median throws
# out glitch pixels (cosmic rays etc.), which matters because FLAGS isn't read.
MAX_IMAGES_PER_FRAME = 18


# A detector needs at least this many images in a frame: the median of 3 throws
# out glitch pixels (cosmic-ray hits etc.) that appear in only one image. With
# 1-2 images a glitch survives as a white dot that is only in that one frame.
MIN_IMAGES_PER_DETECTOR = 3


def pick(exposures: list, max_images: int = MAX_IMAGES_PER_FRAME,
         min_per_detector: int = MIN_IMAGES_PER_DETECTOR) -> list:
    """Choose at most max_images of one time bin, fairly spread over detectors and time.

    0. only detectors with >= min_per_detector images in this bin are used;
       if no detector has that many, the bin gets no images (-> no frame)
    1. hand out the slots round-robin: one per detector, then a second each, ...
       (a detector with fewer images than its share just gets all of them)
    2. inside each detector take images evenly spaced in time
    Always picks the same images for the same input, so the cutout cache is reused.
    """
    by_det: dict[int, list] = {}
    for e in sorted(exposures, key=lambda e: e.mjd):
        by_det.setdefault(e.detector, []).append(e)
    by_det = {d: items for d, items in by_det.items() if len(items) >= min_per_detector}
    if sum(map(len, by_det.values())) <= max_images:
        return sorted((e for items in by_det.values() for e in items), key=lambda e: (e.mjd, e.detector))

    quota = dict.fromkeys(by_det, 0)
    left = max_images
    while left:
        for d in sorted(by_det):
            if left and quota[d] < len(by_det[d]):
                quota[d] += 1
                left -= 1

    chosen = []
    for d, items in by_det.items():
        n, k = len(items), quota[d]
        chosen += [items[int((i + 0.5) * n / k)] for i in range(k)]  # evenly spaced, centred
    return sorted(chosen, key=lambda e: (e.mjd, e.detector))


def combine(images_by_detector: dict[int, list[np.ndarray]]) -> np.ndarray:
    """Squash all images of one time bin into one picture.

    1. per detector: robust median of its images (throws out glitch pixels,
       see robust_median_stack)
    2. average the detectors together
    NaN pixels (bad or not covered) are ignored.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN pixels are fine
        per_detector = [robust_median_stack(imgs) for imgs in images_by_detector.values()]
        return np.nanmean(per_detector, axis=0)


def robust_median_stack(images: list[np.ndarray]) -> np.ndarray:
    """Pixel-wise median of a few images, ignoring NaN.

    Same as np.nanmedian, except where a pixel has exactly 2 valid values: then
    the smaller one is taken instead of their average. Glitches are always too
    bright, so the smaller value is the clean one (happens e.g. at a detector's
    edge, where not all images cover the pixel).

    Faster than np.nanmedian, which falls back to a slow per-pixel loop when
    there are NaNs (28 of 38 s for 2000x2000 frames). Instead: sort each pixel's
    values in one go (NaNs end up last), count the valid ones, take the middle.
    """
    stack = np.sort(np.asarray(images), axis=0)
    n_valid = np.isfinite(stack).sum(axis=0)
    last = stack.shape[0] - 1
    lo = np.clip((n_valid - 1) // 2, 0, last)  # n odd: lo == hi
    hi = np.clip(n_valid // 2, 0, last)
    hi = np.where(n_valid == 2, lo, hi)[None]  # 2 values: smaller one only
    lo = lo[None]
    median = (np.take_along_axis(stack, lo, axis=0)[0] + np.take_along_axis(stack, hi, axis=0)[0]) / 2
    median[n_valid == 0] = np.nan
    return median
