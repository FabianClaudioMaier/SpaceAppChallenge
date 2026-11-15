"""
How the images of one frame (one SPHEREx visit) are squashed into one picture.

SPHEREx has 6 detector arrays. Each one sees a different slice of infrared
light (and inside a detector the wavelength changes slowly across the chip,
which is where the 102 "bands" come from: 17 per detector).

All six are used. Like Backyard Worlds (WISE W1 = blue, W2 = red), the
short-wavelength detectors D1-D3 become the blue channel and the long ones
D4-D6 the red channel of a colour picture, so objects that are much brighter at
long wavelengths - brown dwarfs, cold planets - stand out red.

Stacking follows DeepSkyStacker's "median kappa-sigma": per colour channel,
values far from the median (glitches) are thrown out, the rest are averaged,
and noisier images count less (weights 1 / noise^2).

per Intelligence (SI) was used in creating this file. It was used to help write docstrings and comments, and to suggest code structure. The code was written by a human developer, and the SI's suggestions were reviewed and edited by the developer as needed. The final code is the result of the developer's work, with the SI serving as a tool to assist in the development process.
"""

from __future__ import annotations

import numpy as np

# Colour channels of the rendered frames (green = average of the two).
CHANNELS: dict[str, tuple[int, ...]] = {
    "blue": (1, 2, 3),  # 0.75 - 2.4 um
    "red": (4, 5, 6),   # 2.4  - 5.0 um
}


def pick(exposures: list, max_images: int) -> list:
    """Choose at most max_images of one frame's images, fairly spread over detectors and time.

    1. hand out the slots round-robin: one per detector, then a second each, ...
       (a detector with fewer images than its share just gets all of them)
    2. inside each detector take images evenly spaced in time
    Always picks the same images for the same input, so the cutout cache is reused.
    """
    by_det: dict[int, list] = {}
    for e in sorted(exposures, key=lambda e: e.mjd):
        by_det.setdefault(e.detector, []).append(e)
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


# Kappa-sigma rejection: a value is thrown out if it is further than
#   KAPPA x (that image's noise) + RELATIVE_TOLERANCE x |median|
# from the median of its colour channel. The relative part keeps bright stars, whose
# brightness differs a bit between images because each image sees them at a
# slightly different wavelength.
KAPPA = 4.0
RELATIVE_TOLERANCE = 0.25

# A channel's stack is only trusted at a pixel where at least this many of its
# images had data there: with a single value a glitch can't be recognised. (At
# the edges of images that only partly cover the frame this happens a lot.)
# If neither channel reaches it at a pixel, both are used there anyway.
MIN_VALID_PER_PIXEL = 2


def combine(images_by_detector: dict[int, list[np.ndarray]],
            noise_by_detector: dict[int, list[float]]) -> dict[str, np.ndarray | None]:
    """Squash all images of one frame (one SPHEREx visit).

    1. per colour channel: median kappa-sigma stack of all its images
       (stack_detector) - a visit has ~1 image per detector but ~3 per
       channel, taken minutes apart, so glitches can be recognised
    2. "all": noise-weighted mean of the two channel stacks, per pixel only of
       stacks with enough images there (weighted_mean)
    -> {"blue": ..., "red": ..., "all": ...}; a channel is None when none of
       its detectors is in this frame. NaN pixels (not covered) are ignored.
    """
    stacks = {}
    for name, dets in CHANNELS.items():
        present = [d for d in dets if d in images_by_detector]
        if present:
            stacks[name] = stack_detector(
                [img for d in present for img in images_by_detector[d]],
                [n for d in present for n in noise_by_detector[d]])
    result: dict[str, np.ndarray | None] = {
        name: weighted_mean([stacks[name]]) if name in stacks else None for name in CHANNELS}
    result["all"] = weighted_mean(list(stacks.values()))
    return result


def stack_detector(images: list[np.ndarray], noises: list[float]) -> tuple[np.ndarray, float, np.ndarray]:
    """Median kappa-sigma stack of some images (a channel's) -> (image, its noise, valid count).

    Glitches (cosmic rays etc.) appear in one image only, far from the median,
    and are thrown out; the remaining values are averaged with weights
    1 / noise^2, which is less noisy than the median alone. "valid count" is,
    per pixel, how many images had data there.
    """
    stack = np.asarray(images, dtype=np.float32)
    n_valid = np.isfinite(stack).sum(axis=0)
    sigma = np.asarray(noises, dtype=np.float32)
    usable = np.isfinite(sigma) & (sigma > 0)
    sigma = np.where(usable, sigma, np.median(sigma[usable]) if usable.any() else 1.0)
    sigma = sigma.reshape(-1, 1, 1)

    centre = robust_median_stack(stack)
    with np.errstate(invalid="ignore", divide="ignore"):
        keep = np.abs(stack - centre) <= KAPPA * sigma + RELATIVE_TOLERANCE * np.abs(centre)
        weight = np.where(keep, 1.0 / sigma**2, 0.0).astype(np.float32)
        total = weight.sum(axis=0)
        mean = np.where(keep, stack * weight, 0.0).sum(axis=0) / total
    mean[total == 0] = np.nan
    good = total[total > 0]
    noise = float(1.0 / np.sqrt(np.median(good))) if good.size else float("inf")
    return mean.astype(np.float32), noise, n_valid


def weighted_mean(stacks: list[tuple[np.ndarray, float, np.ndarray]]) -> np.ndarray | None:
    """Pixel-wise mean of several (image, noise, valid count) stacks, weighted 1 / noise^2.

    Per pixel, stacks with fewer than MIN_VALID_PER_PIXEL images there are left
    out if another stack has enough (see MIN_VALID_PER_PIXEL). None if empty.
    """
    if not stacks:
        return None
    trusted = [count >= MIN_VALID_PER_PIXEL for _, _, count in stacks]
    any_trusted = np.logical_or.reduce(trusted)
    num = np.zeros_like(stacks[0][0], dtype=np.float64)
    den = np.zeros_like(num)
    for (img, noise, _), trust in zip(stacks, trusted):
        w = 1.0 / noise**2 if np.isfinite(noise) and noise > 0 else 0.0
        ok = np.isfinite(img) & (trust | ~any_trusted)
        num += np.where(ok, img, 0.0) * w
        den += ok * w
    with np.errstate(invalid="ignore", divide="ignore"):
        out = num / den
    out[den == 0] = np.nan
    return out.astype(np.float32)


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
