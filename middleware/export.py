"""
Exported GIFs (written when the user saves a finding, see middleware.save, and
for "Download GIF", middleware.exportView).

The GIFs the viewer shows are pixels only: the frontend draws the timestamp,
the RA/Dec rulers and the selection box on top, so they stay sharp at any size.
An exported GIF has to stand on its own, so here everything is burned in,
enlarged to about EXPORT_TARGET_PX per frame (export_scale: a 250 px frame 4x,
500 px 2x, 1000 px 1x; every SPHEREx pixel becomes a block, no blurring). At 1x
the ruler text of a small frame was too small to read (a 6 looked like an 8), at
2x the markers' shapes (star, triangle, ...) were hard to tell apart. Text,
markers, lines and gaps grow with the scale (_detail), so a 4x GIF looks like
the 2x one, with twice the pixels.

    white strip, RA ruler (black text)
    Dec ruler | frame, with the green selection box (and for "Download GIF" the
              | star markers of the switched-on layers)
              | black strip, timestamp (white text)

Coordinates: "frame pixels" are measured from the frame's top-left corner, one
per SPHEREx pixel, like the star overlays' "xy" (python-backend/overlays.py _xy).
The sky grid is the backend's own make_grid_wcs, so both always agree.

Super Intelligence (SI) was used in creating this file.
"""

import os
import sys
import threading
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageSequence

BACKEND_DIR = Path(__file__).resolve().parent.parent / "python-backend"

EXPORT_TARGET_PX = 1000  # about this wide per frame (without the rulers)
EXPORT_MAX_SCALE = 4
BOX_COLOUR = (0, 255, 0)
# Ruler steps in degrees, chosen like the frontend's page rulers (viewer.js):
# RULER_STEP_DEG (a 250 px frame: Dec 0.05, RA 0.05 or 0.1), coarser only where
# the labels would collide, finer only where there would be fewer than
# RULER_MIN_TICKS ticks.
RULER_STEPS_DEG = [0.0005, 0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0,
                   10.0, 15.0, 30.0, 45.0, 90.0]  # (the big ones: RA right at a pole)
RULER_STEP_DEG = 0.05
RULER_MIN_TICKS = 3
WHITE, BLACK = (255, 255, 255), (0, 0, 0)
# Ruler spacing as on the page (viewer.js), for its 13 px labels: label - 3 px -
# tick (6 px) - 4 px - image; "Dec" 6 px from the widest Dec label (its sign).
# Scaled with the GIF's label size (_spacing), so a GIF looks like the page enlarged.
PAGE_LABEL_PX = 13
RULER_TICK_PX = 6
RULER_LABEL_TICK_PX = 3
RULER_IMAGE_GAP_PX = 4
RULER_TITLE_GAP_PX = 6
# The coordinate grid (star-overlay.js coordinateGrid sends it): thin white lines
GRID_LINE = (255, 255, 255, 105)

# Star markers ("Download GIF"): the viewer's layers, drawn like star-overlay.js
# draws them - the layer's shape and colour on a dark edge, the size by
# brightness. The page sends each star's position per frame.
MARKER_SHAPES = ("circle", "square", "diamond", "triangle", "cross", "star", "plus", "hexagon")
MARKER_KINDS = {"faint": 1, "normal": 1.5, "fast": 2.5}  # line width on screen (px)
MARKER_PX = 6       # radius of a size-1 marker in output px (STAR_MARKER_PX in star-overlay.js)
STAR_INNER = 0.45   # the star shape's inner corners (layers.js SHAPE_TRACERS)
TRACK_COLOUR = (200, 200, 200)
TRACK_MIN_PX = 6  # a shorter track (output px, at 2x detail) isn't drawn - it was a dot in the marker


def _font(size, bold=False):
    '''DejaVu Sans (comes with matplotlib, a backend requirement): sturdier than
    PIL's thin default font, which is the fallback.'''
    try:
        import matplotlib
        name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
        return ImageFont.truetype(str(Path(matplotlib.get_data_path()) / "fonts" / "ttf" / name), size)
    except (ImportError, OSError):
        return ImageFont.load_default(size=size)


def _grid_wcs(ra, dec, pixels):
    """The frame's sky grid (backend.make_grid_wcs; imported when first needed:
    loading the backend module takes a moment)."""
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
    from backend import make_grid_wcs
    return make_grid_wcs(ra, dec, pixels)


def frame_px_to_sky(x, y, ra, dec, pixels):
    '''
    A point in frame pixels (from the top-left corner; pixel i spans i..i+1)
    -> (RA, Dec) in degrees. The inverse of overlays._xy.
    '''
    r, d = _grid_wcs(ra, dec, pixels).pixel_to_world_values(x - 0.5, pixels - 0.5 - y)
    return float(r), float(d)


def j_name(ra, dec):
    '''
    IAU-style designation of a position, e.g. "J220322.8-565122" (J2000,
    RA hhmmss.s, Dec +-ddmmss): says where the object is, sorts by RA, and can
    be pasted into SIMBAD or Aladin.
    '''
    import astropy.units as u
    from astropy.coordinates import SkyCoord
    c = SkyCoord(ra * u.deg, dec * u.deg)
    return ("J" + c.ra.to_string(unit=u.hourangle, sep="", precision=1, pad=True)
            + c.dec.to_string(sep="", precision=0, alwayssign=True, pad=True))


def _ticks(values, step):
    '''(position along the edge in pixels, value) wherever the values cross a multiple
    of step. A value crossed twice (at a pole the Dec along the left edge rises to
    the middle and falls again) gets two ticks; the same crossing found in two
    neighbouring pixel pairs (a pixel right on the value) counts once.'''
    out = []
    for i in range(len(values) - 1):
        a, b = values[i], values[i + 1]
        if a == b:
            continue
        for k in range(int(np.ceil(min(a, b) / step)), int(np.floor(max(a, b) / step)) + 1):
            v = k * step
            pos = i + (v - a) / (b - a)
            if not any(abs(v - t) < step / 2 and abs(p - pos) < 1e-9 for p, t in out):
                out.append((pos, v))
    return out


def _decimals(step):
    return next(d for d in range(8) if abs(step * 10**d - round(step * 10**d)) < 1e-9)


def _ra_label(v, step):
    return f"{v % 360:.{_decimals(step)}f}°"


def _dec_label(v, step):
    return f"{v:+.{_decimals(step)}f}°".replace("-", "−")


def _step(values, scale, label_size):
    '''RULER_STEP_DEG, unless labels (label_size(step): pixels one takes along the
    ruler; scale: ruler pixels per frame pixel) would collide or be too few.
    Measured on the ticks themselves (the values along an edge need not rise or
    fall steadily, see _ticks).'''
    def fits(step):
        at = sorted(p * scale for p, _ in _ticks(values, step))
        return all(b - a >= label_size(step) for a, b in zip(at, at[1:]))

    i = RULER_STEPS_DEG.index(RULER_STEP_DEG)
    while i < len(RULER_STEPS_DEG) - 1 and not fits(RULER_STEPS_DEG[i]):
        i += 1  # crowded: coarser
    while i > 0 and len(_ticks(values, RULER_STEPS_DEG[i])) < RULER_MIN_TICKS and fits(RULER_STEPS_DEG[i - 1]):
        i -= 1  # few: finer
    return RULER_STEPS_DEG[i]


def ruler_ticks(ra, dec, pixels, scale=1, font=None):
    '''
    Ticks of the two rulers -> ([(x, "330.8°"), ...] along the top edge,
    [(y, "−56.75°"), ...] along the left edge), positions in frame pixels.
    The values are taken along the edge itself (away from the equator lines of
    constant RA are tilted, so the bottom edge would give other RAs). scale,
    font: how the labels are drawn (for the crowding check).
    '''
    font = font or _font(12)
    grid = _grid_wcs(ra, dec, pixels)
    edge = np.arange(pixels, dtype=float)
    top_ra, _ = grid.pixel_to_world_values(edge, np.full(pixels, pixels - 1.0))
    _, left_dec = grid.pixel_to_world_values(np.zeros(pixels), pixels - 1.0 - edge)
    top_ra = top_ra[0] + (top_ra - top_ra[0] + 180) % 360 - 180  # no jump at RA 0/360
    gap = 4 * scale
    rs = _step(top_ra, scale, lambda step: font.getlength(_ra_label(top_ra[0], step)) + 2 * gap)
    ds = _step(left_dec, scale, lambda step: sum(font.getmetrics()) + gap)
    return ([(x + 0.5, _ra_label(v, rs)) for x, v in _ticks(top_ra, rs)],
            [(y + 0.5, _dec_label(v, ds)) for y, v in _ticks(left_dec, ds)])


def export_scale(pixels):
    '''How much a frame is enlarged: to about EXPORT_TARGET_PX, at most EXPORT_MAX_SCALE x'''
    return max(1, min(EXPORT_MAX_SCALE, EXPORT_TARGET_PX // pixels))


def _detail(scale):
    '''How much text, markers and lines grow: 1 up to 2x (the sizes below are made for
    that), 2 at 4x - so a bigger GIF looks the same, only sharper'''
    return max(scale, 2) / 2


def _fonts(pixels, scale):
    '''(label font, title font) for a frame drawn pixels * scale wide'''
    size = max(12, min(round(22 * _detail(scale)), round(scale * pixels / 28)))
    return _font(size), _font(size + 3, bold=True)


def _trace(draw, shape, c, r, width, colour):
    '''A marker shape centred on (c, c), as layers.js traceShape draws it'''
    if shape == "circle":
        draw.ellipse([c - r, c - r, c + r, c + r], outline=colour, width=width)
    elif shape == "square":
        draw.rectangle([c - 0.9 * r, c - 0.9 * r, c + 0.9 * r, c + 0.9 * r], outline=colour, width=width)
    elif shape in ("cross", "plus"):
        if shape == "cross":
            d = 0.8 * r
            lines = [((c - d, c - d), (c + d, c + d)), ((c + d, c - d), (c - d, c + d))]
        else:
            lines = [((c - r, c), (c + r, c)), ((c, c - r), (c, c + r))]
        for line in lines:
            draw.line(line, fill=colour, width=width)
    else:
        if shape == "diamond":
            d = 0.75 * np.sqrt(2) * r
            points = [(c, c - d), (c + d, c), (c, c + d), (c - d, c)]
        elif shape == "star":  # five points, the inner corners at STAR_INNER x r
            points = [(c + (r if k % 2 == 0 else STAR_INNER * r) * np.sin(np.pi * k / 5),
                       c - (r if k % 2 == 0 else STAR_INNER * r) * np.cos(np.pi * k / 5)) for k in range(10)]
        elif shape == "hexagon":
            points = [(c + r * np.cos(np.pi / 3 * k + np.pi / 6), c + r * np.sin(np.pi / 3 * k + np.pi / 6))
                      for k in range(6)]
        else:  # triangle
            points = [(c, c - r), (c + 0.87 * r, c + 0.5 * r), (c - 0.87 * r, c + 0.5 * r)]
        draw.line(points + points[:1], fill=colour, width=width, joint="curve")


def _sprite(shape, colour, kind, r, k, cache):
    '''A marker drawn once (dark edge, then its colour) on a transparent square:
    (image, half its size). Stamping copies is much faster than drawing thousands.
    k: _detail, the line widths grow with it.'''
    key = (shape, colour, kind, r, k)
    if key not in cache:
        width = max(1, round(MARKER_KINDS[kind] * k))
        edge = max(2, round(2 * k))
        half = int(np.ceil(r * 1.1 + width + edge))
        sprite = Image.new("RGBA", (2 * half + 1, 2 * half + 1), (0, 0, 0, 0))
        draw = ImageDraw.Draw(sprite)
        _trace(draw, shape, half, r, width + edge, (0, 0, 0, 255))
        _trace(draw, shape, half, r, width, colour + (255,))
        cache[key] = (sprite, half)
    return cache[key]


def _draw_markers(big, layers, i, scale, cache, font):
    '''The star markers of frame i on the scaled frame: fast movers' tracks
    (dashed), the markers, then the names.'''
    draw = ImageDraw.Draw(big)
    draw.fontmode = "1"
    k = _detail(scale)
    names = []
    for layer in layers:
        for star in layer["stars"]:
            if star["track"]:
                points = [(x * scale, y * scale) for x, y in (p for p in star["xy"] if p)]
                if len(points) < 2 or np.hypot(points[-1][0] - points[0][0], points[-1][1] - points[0][1]) < TRACK_MIN_PX * k:
                    continue
                for a, b in zip(points, points[1:]):  # dashes of 3 px every 6 px, like the page
                    length = np.hypot(b[0] - a[0], b[1] - a[1])
                    for t in np.arange(0, length, 6 * k):
                        e = min(t + 3 * k, length)
                        draw.line([(a[0] + (b[0] - a[0]) * t / length, a[1] + (b[1] - a[1]) * t / length),
                                   (a[0] + (b[0] - a[0]) * e / length, a[1] + (b[1] - a[1]) * e / length)],
                                  fill=TRACK_COLOUR, width=max(1, round(k)))
    for layer in layers:
        for star in layer["stars"]:
            p = star["xy"][i]
            if not p:
                continue
            r = round(max(MARKER_PX * k, 1.5 * scale) * star["size"] * 2) / 2
            sprite, half = _sprite(layer["shape"], layer["colour"], star["kind"], r, k, cache)
            x, y = round(p[0] * scale), round(p[1] * scale)
            big.paste(sprite, (x - half, y - half), sprite)
            if star["label"]:
                names.append((x + r * 1.5, y - r * 1.2, star["label"]))
    for x, y, text in names:
        draw.text((x, y), text, font=font, fill=WHITE, anchor="ls", stroke_width=round(2 * k), stroke_fill=BLACK)


def _draw_grid(big, grid, scale, font):
    '''The coordinate grid on the scaled frame: lines of constant RA and Dec (thin,
    half transparent), then their labels - one that would overlap another is left out.'''
    layer = Image.new("RGBA", big.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.fontmode = "1"
    k = _detail(scale)
    for line in grid["lines"]:
        draw.line([(x * scale, y * scale) for x, y in line], fill=GRID_LINE, width=max(1, round(k)))
    placed = []
    for x, y, text in grid["labels"]:
        at = (x * scale + 3 * k, y * scale - 3 * k)
        box = draw.textbbox(at, text, font=font, anchor="ls", stroke_width=round(2 * k))
        if any(box[0] < b[2] and b[0] < box[2] and box[1] < b[3] and b[1] < box[3] for b in placed):
            continue
        placed.append(box)
        draw.text(at, text, font=font, fill=WHITE + (255,), anchor="ls",
                  stroke_width=round(2 * k), stroke_fill=BLACK + (255,))
    big.paste(Image.alpha_composite(big.convert("RGBA"), layer).convert("RGB"))


def _spacing(font):
    '''The page's ruler spacing scaled to this label font: (tick, label-tick, tick-image,
    "Dec"-label) in px'''
    f = font.size / PAGE_LABEL_PX
    return tuple(max(1, round(v * f)) for v in (RULER_TICK_PX, RULER_LABEL_TICK_PX, RULER_IMAGE_GAP_PX,
                                                 RULER_TITLE_GAP_PX))


def _decorate(frame, ticks, fonts, pixels, label, box, scale, markers=None, grid=None):
    '''One frame (pixels x pixels, RGB) -> scale x bigger, rulers above and left
    (titles as in the frontend, outside the numbers and centred on the frame:
    "RA" on its own line above the RA labels, "Dec" upright along the left
    ruler's outer edge - so no tick label ever has to make room for them), the
    box, the timestamp strip below. markers: (layers, frame index, sprite cache)
    for the star markers, or None. grid: the coordinate grid (its steps go into
    the strip, a second line under the timestamp), or None.'''
    big = frame.resize((pixels * scale, pixels * scale), Image.NEAREST)
    font, title_font = fonts
    if markers:
        _draw_markers(big, *markers[:2], scale, markers[2], font)
    if grid:  # (in front of the markers, as on the page)
        _draw_grid(big, grid, scale, _font(max(11, round(font.size * 0.72))))
    ascent, descent = font.getmetrics()
    title_height = sum(title_font.getmetrics())
    ra_ticks, dec_ticks = ticks
    measure = ImageDraw.Draw(big)
    tick, label_tick, gap, title_gap = _spacing(font)
    pad = 2 * scale  # (the outer margin)
    widest = max([measure.textlength(t, font=font) for _, t in dec_ticks] + [0])
    # the upright "Dec" (letters ending at its baseline, title_ascent from the left)
    # title_gap left of the widest label
    title_ascent = title_font.getmetrics()[0]
    dx = pad + title_ascent + title_gap + int(np.ceil(widest)) + label_tick + tick + gap
    dy = pad + title_height + ascent + descent + tick + gap
    baseline = pad + title_height + ascent  # of the RA labels (the "RA" title is the line above)
    text_right = dx - gap - tick - label_tick  # right edge of the Dec labels
    w = h = pixels * scale
    strip_lines = 2 if grid else 1
    out = Image.new("RGB", (dx + w, dy + h + strip_lines * (ascent + descent) + (strip_lines + 1) * pad), WHITE)
    out.paste(big, (dx, dy))
    draw = ImageDraw.Draw(out)
    # No anti-aliasing: soft grey letter edges get mapped to the nearest colours
    # of the GIF's 256-colour palette (made from the orange-brown sky), which
    # tints the text. Pure black and white stay pure.
    draw.fontmode = "1"
    draw.rectangle([0, dy + h, dx + w, out.height], fill=BLACK)
    draw.text((dx + pad, dy + h + pad), label, font=font, fill=WHITE, anchor="la")
    if grid:
        draw.text((dx + pad, dy + h + 2 * pad + ascent + descent), grid["text"], font=font, fill=WHITE, anchor="la")
    draw.text((dx + w // 2, pad), "RA", font=title_font, fill=BLACK, anchor="mt")  # centred on the frame
    # "Dec" upright (reading upwards), centred on the frame's height
    title = Image.new("L", (int(np.ceil(draw.textlength("Dec", font=title_font))), title_height), 0)
    title_draw = ImageDraw.Draw(title)
    title_draw.fontmode = "1"
    title_draw.text((0, 0), "Dec", font=title_font, fill=255)
    title = title.rotate(90, expand=True)
    out.paste(BLACK, (pad, dy + (h - title.height) // 2), title)
    half_height = (ascent + descent) / 2
    for x, text in ra_ticks:
        xs = dx + x * scale
        draw.line([(xs, dy - gap - tick), (xs, dy - gap - 1)], fill=BLACK, width=max(1, scale // 2))
        half = draw.textlength(text, font=font) / 2
        draw.text((min(max(xs, dx + half), dx + w - half), baseline), text, font=font, fill=BLACK, anchor="ms")
    for y, text in dec_ticks:  # every tick gets its label (kept inside the frame's height)
        ys = dy + y * scale
        draw.line([(dx - gap - tick, ys), (dx - gap - 1, ys)], fill=BLACK, width=max(1, scale // 2))
        draw.text((text_right, min(max(ys, dy + half_height), dy + h - half_height)), text,
                  font=font, fill=BLACK, anchor="rm")
    if box:
        x1, y1, x2, y2 = box
        draw.rectangle([dx + x1 * scale, dy + y1 * scale, dx + x2 * scale - 1, dy + y2 * scale - 1],
                       outline=BOX_COLOUR, width=scale)
    return out


def _to_palette(frame, fixed):
    '''An RGB frame -> a 256-colour frame whose palette holds the fixed colours
    exactly (text, box, the layers' marker colours). A palette made from the
    frame alone is mostly sky: a few thin marker lines got the nearest sky colour
    (orange came out pink, violet grey).'''
    fixed = list(dict.fromkeys(fixed))[:64]
    adaptive = frame.quantize(colors=256 - len(fixed), method=Image.Quantize.MEDIANCUT)
    palette = adaptive.getpalette()[:3 * (256 - len(fixed))]
    palette += [v for colour in fixed for v in colour]
    target = Image.new("P", (1, 1))
    target.putpalette(palette + [0] * (768 - len(palette)))
    return frame.quantize(palette=target, dither=Image.Dither.NONE)


# The frames of an export are drawn in worker processes, a share each (_render_frames):
# one export ~4x faster on 8 cores, and several exports at once really run at once
# (threads in the server can't: Python runs one at a time). Started with the server
# (warm_up), so the first export doesn't wait for them.
EXPORT_WORKERS = max(1, (os.cpu_count() or 2) - 1)  # (one core stays for the server)
_pool = None
_pool_lock = threading.Lock()


def _workers():
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ProcessPoolExecutor(EXPORT_WORKERS)
        return _pool


def _load_fonts(_):
    _fonts(250, 4)


def warm_up():
    '''Start the worker processes and let them load their libraries and fonts.'''
    list(_workers().map(_load_fonts, range(EXPORT_WORKERS)))


def _render_frames(job):
    '''In a worker: a share of an export's frames -> [(frame index, palette image)].'''
    pixels, scale = job["pixels"], job["scale"]
    fonts = _fonts(pixels, scale)
    sprites = {}
    out = []
    for i, rgb, label in job["frames"]:
        frame = Image.frombytes("RGB", (pixels, pixels), rgb)
        markers = (job["layers"], i, sprites) if job["layers"] else None
        out.append((i, _to_palette(_decorate(frame, job["ticks"], fonts, pixels, label, job["box"], scale,
                                             markers, job["grid"]), job["fixed"])))
    return out


def export_gif(gif_bytes, ra, dec, pixels, labels, box=None, scale=None, layers=None, grid=None):
    '''
    The viewer's GIF -> the exported GIF (bytes), see the module docstring.

    Parameters:
    gif_bytes: a GIF from middleware/video_cache (frames or difference images)
    ra, dec, pixels: the job's field centre and frame size
    labels: text per frame for the strip below it, e.g. "2025-06-07 12:53 UTC"
    box: (x1, y1, x2, y2) corners in frame pixels (top left, bottom right), or None
    scale: None = export_scale(pixels) (about EXPORT_TARGET_PX per frame)
    layers: star markers to draw, bottom layer first, or None:
        [{shape, colour: (r, g, b), stars: [{size, kind, xy: per frame [x, y] | None,
          label, track}]}] (positions in frame pixels, see middleware.validateExport)
    grid: the coordinate grid shown on the page, or None:
        {lines: [[(x, y), ...]], labels: [(x, y, text)], text: "Grid: Dec every ..."}
    '''
    if scale is None:
        scale = export_scale(pixels)
    fonts = _fonts(pixels, scale)
    ticks = ruler_ticks(ra, dec, pixels, scale, fonts[0])
    src = Image.open(BytesIO(gif_bytes))
    fixed = [WHITE, BLACK, BOX_COLOUR, TRACK_COLOUR] + [layer["colour"] for layer in layers or []]
    todo, durations = [], []
    for i, f in enumerate(ImageSequence.Iterator(src)):
        # (GIFs from before the frontend drew the timestamps have a strip below the frame)
        todo.append((i, f.convert("RGB").crop((0, 0, pixels, pixels)).tobytes(), labels[i] if i < len(labels) else ""))
        durations.append(f.info.get("duration", 250))
    share = -(-len(todo) // EXPORT_WORKERS)  # frames per worker (rounded up)
    jobs = [{"frames": todo[k:k + share], "pixels": pixels, "scale": scale, "ticks": ticks, "box": box,
             "layers": layers, "grid": grid, "fixed": fixed} for k in range(0, len(todo), share)]
    try:
        done = [frame for part in _workers().map(_render_frames, jobs) for frame in part]
    except (BrokenProcessPool, OSError):  # the workers are gone: draw them here, as before
        done = [frame for job in jobs for frame in _render_frames(job)]
    frames = [image for _, image in sorted(done, key=lambda d: d[0])]
    out = BytesIO()
    frames[0].save(out, format="GIF", save_all=True, append_images=frames[1:],
                   duration=durations, loop=0)
    return out.getvalue()
