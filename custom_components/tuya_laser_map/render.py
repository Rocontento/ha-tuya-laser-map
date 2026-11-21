"""Render a VacuumMap as a clean "layered plan": room footprint (light), cleanable floor (grey),
accent dock and dashed path. Pillow + numpy only (both ship with Home Assistant)."""

from __future__ import annotations

import io

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .protocol.mapdata import FLOOR_NO_ROOM, VacuumMap

SCALE = 3  # output pixels per map cell
_SS = 2  # supersampling factor for smooth edges

THEMES = {
    "light": dict(bg=(255, 255, 255, 255), footprint=(236, 236, 233), floor=(178, 178, 175),
                  selected=(124, 154, 58), accent=(130, 160, 60), text=(40, 40, 40)),
    "dark": dict(bg=(18, 18, 20, 255), footprint=(44, 44, 48), floor=(92, 92, 98),
                 selected=(124, 154, 58), accent=(150, 185, 70), text=(225, 225, 230)),
}


def _img(mask: np.ndarray) -> Image.Image:
    return Image.fromarray(np.where(mask, 255, 0).astype(np.uint8))


def _arr(im: Image.Image) -> np.ndarray:
    return np.asarray(im) > 127


def _close(m: np.ndarray, k: int) -> np.ndarray:
    return _arr(_img(m).filter(ImageFilter.MaxFilter(k)).filter(ImageFilter.MinFilter(k)))


def _open(m: np.ndarray, k: int) -> np.ndarray:
    return _arr(_img(m).filter(ImageFilter.MinFilter(k)).filter(ImageFilter.MaxFilter(k)))


def _fill_holes(m: np.ndarray) -> np.ndarray:
    im = Image.new("L", (m.shape[1] + 2, m.shape[0] + 2), 0)
    im.paste(_img(m), (1, 1))
    ImageDraw.floodfill(im, (0, 0), 128)
    a = np.asarray(im)[1:-1, 1:-1]
    return a != 128


def footprint(m: np.ndarray) -> np.ndarray:
    """The room as an architect would draw it: furniture holes filled, notches closed."""
    return _fill_holes(_open(_close(_fill_holes(m), 9), 5))


def _smooth_layer(mask: np.ndarray, size: tuple[int, int], blur: float, shrink: int = 1) -> Image.Image:
    im = _img(mask).resize(size, Image.NEAREST).filter(ImageFilter.GaussianBlur(blur))
    im = im.point(lambda v: 255 if v > 128 else 0)
    return im.filter(ImageFilter.MinFilter(shrink)) if shrink > 1 else im


def room_layers(vmap: VacuumMap) -> tuple[np.ndarray, dict[int, np.ndarray]]:
    cells = np.frombuffer(vmap.cells, np.uint8).reshape(vmap.height, vmap.width)
    floor = ((cells & 7) == 7) & (cells != FLOOR_NO_ROOM)
    rid = np.where(floor, cells >> 3, 0)
    return rid, {int(r): footprint(rid == r) for r in np.unique(rid) if r}


def render_png(vmap: VacuumMap, path: list[tuple[int, int]] | None = None, selected: set[int] | None = None,
               theme: str = "light", room_names: dict[int, str] | None = None) -> bytes:
    t = THEMES.get(theme, THEMES["light"])
    selected = selected or set()
    s = SCALE * _SS
    size = (vmap.width * s, vmap.height * s)
    rid, prints = room_layers(vmap)
    canvas = Image.new("RGBA", size, t["bg"])

    def paint(mask_im: Image.Image, color) -> None:
        layer = Image.new("RGBA", size, color + (255,))
        layer.putalpha(mask_im)
        canvas.alpha_composite(layer)

    for r, fp in prints.items():
        paint(_smooth_layer(fp, size, s * 1.2, 2 * s + 1), t["footprint"])
    for r, fp in prints.items():
        m = _close(_open(rid == r, 3), 5) & fp
        paint(_smooth_layer(m, size, s * 1.3, 3), t["selected"] if r in selected else t["floor"])
    d = ImageDraw.Draw(canvas)
    if path:
        pts = [((vmap.origin[0] + x / 10) * s, (vmap.origin[1] - y / 10) * s) for x, y in path]
        acc = 0.0
        for a, b in zip(pts, pts[1:]):
            if (acc // (s * 4.5)) % 2 == 0:
                d.line([a, b], fill=t["bg"][:3] + (255,), width=s + 3)
                d.line([a, b], fill=t["accent"] + (255,), width=max(2, s - 1))
            acc += ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5
    font = ImageFont.load_default(size=int(s * 2.4))
    names = room_names or {}
    for r, fp in prints.items():
        ys, xs = np.nonzero(fp)
        if not len(xs):
            continue
        label = names.get(r) or (vmap.rooms[r].name if r in vmap.rooms else f"Room {r}")
        d.text((xs.mean() * s, ys.mean() * s), label, font=font, anchor="mm",
               fill=(255, 255, 255, 255) if r in selected else t["text"] + (255,))
    x, y = vmap.dock[0] * s, vmap.dock[1] * s
    d.ellipse([x - s * 3.2, y - s * 3.2, x + s * 3.2, y + s * 3.2], fill=t["bg"][:3] + (255,), outline=t["accent"] + (255,), width=max(2, s // 2))
    d.polygon([(x - s * 1.3, y), (x, y - s * 1.3), (x + s * 1.3, y)], fill=t["accent"] + (255,))
    d.rectangle([x - s * 0.9, y, x + s * 0.9, y + s * 1.1], fill=t["accent"] + (255,))
    out = io.BytesIO()
    canvas.resize((vmap.width * SCALE, vmap.height * SCALE), Image.LANCZOS).save(out, "PNG", optimize=True)
    return out.getvalue()


def calibration_points(vmap: VacuumMap) -> list[dict]:
    """xiaomi-vacuum-map-card calibration: robot coordinates -> image pixels (3 points)."""
    pts = []
    for cx, cy in ((0, 0), (vmap.width, 0), (0, vmap.height)):
        rx, ry = vmap.to_robot(cx, cy)
        pts.append({"vacuum": {"x": rx, "y": ry}, "map": {"x": cx * SCALE, "y": cy * SCALE}})
    return pts


def room_outlines(vmap: VacuumMap) -> dict[int, dict]:
    """Per room: label position and outline (bounding polygon) in robot coordinates, for the card."""
    _, prints = room_layers(vmap)
    out = {}
    for r, fp in prints.items():
        ys, xs = np.nonzero(fp)
        if not len(xs):
            continue
        x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
        poly = [vmap.to_robot(x, y) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
        out[r] = {"name": vmap.rooms[r].name if r in vmap.rooms else f"Room {r}",
                  "label": vmap.to_robot(xs.mean(), ys.mean()), "outline": poly}
    return out
