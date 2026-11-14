"""
GIF / video side of the backend.

Input contract (what main.get_timelapse() returns):
    result["images"]      list of JPG paths, oldest first, all the same size
    result["timestamps"]  matching UTC ISO timestamps, e.g. "2025-11-04T12:00:00.000"
    result["difference_images"]  same, but frame minus median (movers pop out)
    result["gaia"][i]["xy_per_frame"]  star positions in JPG pixels, per frame

Minimal working version below so the CLI in main.py can show a preview -
replace / extend freely (timestamp labels, Gaia circles, speed, mp4, ...).
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image


def make_gif(images: list[str], timestamps: list[str], out_path: str | Path,
             duration_ms: int = 600) -> str:
    if len(images) != len(timestamps):
        raise ValueError("images and timestamps must have the same length")
    if not images:
        raise ValueError("no images")
    frames = [Image.open(p).convert("L") for p in images]
    out_path = Path(out_path)
    frames[0].save(out_path, save_all=True, append_images=frames[1:],
                   duration=duration_ms, loop=0)
    return str(out_path)
