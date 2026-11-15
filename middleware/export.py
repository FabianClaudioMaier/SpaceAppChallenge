"""
Exported GIFs (written when the user saves a finding, see middleware.save).

The GIFs the viewer shows are pixels only: the frontend draws the timestamp,
the RA/Dec rulers and the selection box on top, so they stay sharp at any size.
An exported GIF has to stand on its own, so here everything is burned in, at
EXPORT_SCALE x the size for frames up to EXPORT_SCALE_MAX_PIXELS (every SPHEREx
pixel becomes a 2x2 block, no blurring; at 1x the ruler text of a small frame
was too small to read - a 6 looked like an 8). Bigger frames stay 1x: a
1000 px frame at 2x would be 2000 x 2000 px per GIF frame.

    white strip, RA ruler (black text)
    Dec ruler | frame, with the green selection box
              | black strip, timestamp (white text)

Coordinates: "frame pixels" are measured from the frame's top-left corner, one
per SPHEREx pixel, like the star overlays' "xy" (python-backend/overlays.py _xy).
The sky grid is the backend's own make_grid_wcs, so both always agree.

Super Intelligence (SI) was used in creating this file.
"""

import sys
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageSequence

BACKEND_DIR = Path(__file__).resolve().parent.parent / "python-backend"

EXPORT_SCALE = 2
EXPORT_SCALE_MAX_PIXELS = 500
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


def _fonts(pixels, scale):
    '''(label font, title font) for a frame drawn pixels * scale wide'''
    size = max(12, min(22, round(scale * pixels / 28)))
    return _font(size), _font(size + 3, bold=True)


def _decorate(frame, ticks, fonts, pixels, label, box, scale):
    '''One frame (pixels x pixels, RGB) -> scale x bigger, rulers above and left
    (titles as in the frontend, outside the numbers and centred on the frame:
    "RA" on its own line above the RA labels, "Dec" upright along the left
    ruler's outer edge - so no tick label ever has to make room for them), the
    box, the timestamp strip below.'''
    big = frame.resize((pixels * scale, pixels * scale), Image.NEAREST)
    font, title_font = fonts
    ascent, descent = font.getmetrics()
    title_height = sum(title_font.getmetrics())
    ra_ticks, dec_ticks = ticks
    measure = ImageDraw.Draw(big)
    tick, pad = 3 * scale, 2 * scale
    widest = max([measure.textlength(t, font=font) for _, t in dec_ticks] + [0])
    dx = pad + title_height + 2 * pad + int(np.ceil(widest)) + pad + tick
    dy = pad + title_height + ascent + descent + tick
    baseline = pad + title_height + ascent  # of the RA labels (the "RA" title is the line above)
    text_right = dx - tick - pad            # right edge of the Dec labels
    w = h = pixels * scale
    out = Image.new("RGB", (dx + w, dy + h + ascent + descent + 2 * pad), WHITE)
    out.paste(big, (dx, dy))
    draw = ImageDraw.Draw(out)
    # No anti-aliasing: soft grey letter edges get mapped to the nearest colours
    # of the GIF's 256-colour palette (made from the orange-brown sky), which
    # tints the text. Pure black and white stay pure.
    draw.fontmode = "1"
    draw.rectangle([0, dy + h, dx + w, out.height], fill=BLACK)
    draw.text((dx + pad, dy + h + pad), label, font=font, fill=WHITE, anchor="la")
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
        draw.line([(xs, dy - tick), (xs, dy - 1)], fill=BLACK, width=max(1, scale // 2))
        half = draw.textlength(text, font=font) / 2
        draw.text((min(max(xs, dx + half), dx + w - half), baseline), text, font=font, fill=BLACK, anchor="ms")
    for y, text in dec_ticks:  # every tick gets its label (kept inside the frame's height)
        ys = dy + y * scale
        draw.line([(dx - tick, ys), (dx - 1, ys)], fill=BLACK, width=max(1, scale // 2))
        draw.text((text_right, min(max(ys, dy + half_height), dy + h - half_height)), text,
                  font=font, fill=BLACK, anchor="rm")
    if box:
        x1, y1, x2, y2 = box
        draw.rectangle([dx + x1 * scale, dy + y1 * scale, dx + x2 * scale - 1, dy + y2 * scale - 1],
                       outline=BOX_COLOUR, width=scale)
    return out


def export_gif(gif_bytes, ra, dec, pixels, labels, box=None, scale=None):
    '''
    The viewer's GIF -> the exported GIF (bytes), see the module docstring.

    Parameters:
    gif_bytes: a GIF from middleware/video_cache (frames or difference images)
    ra, dec, pixels: the job's field centre and frame size
    labels: text per frame for the strip below it, e.g. "2025-06-07 12:53 UTC"
    box: (x1, y1, x2, y2) corners in frame pixels (top left, bottom right), or None
    scale: None = EXPORT_SCALE up to EXPORT_SCALE_MAX_PIXELS, 1 above
    '''
    if scale is None:
        scale = EXPORT_SCALE if pixels <= EXPORT_SCALE_MAX_PIXELS else 1
    fonts = _fonts(pixels, scale)
    ticks = ruler_ticks(ra, dec, pixels, scale, fonts[0])
    src = Image.open(BytesIO(gif_bytes))
    frames, durations = [], []
    for i, f in enumerate(ImageSequence.Iterator(src)):
        # (GIFs from before the frontend drew the timestamps have a strip below the frame)
        frame = f.convert("RGB").crop((0, 0, pixels, pixels))
        label = labels[i] if i < len(labels) else ""
        frames.append(_decorate(frame, ticks, fonts, pixels, label, box, scale))
        durations.append(f.info.get("duration", 250))
    out = BytesIO()
    frames[0].save(out, format="GIF", save_all=True, append_images=frames[1:],
                   duration=durations, loop=0)
    return out.getvalue()
