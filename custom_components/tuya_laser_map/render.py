"""Render a VacuumMap as a true floor plan: every cell as the robot recorded it (walls and obstacles
dark, floor it can reach on top of the full room shape), the area to clean tinted, the cleaned swath, path, dock and live robot dot.
Pillow + numpy only (both ship with Home Assistant)."""

from __future__ import annotations

import io

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .protocol.mapdata import FLOOR_NO_ROOM, WALL, VacuumMap

SCALE = 5  # output pixels per map cell
BRUSH_CM = 30  # width of the cleaned swath drawn along the path

THEMES = {
    "light": dict(bg=(255, 255, 255), room=(236, 236, 232), floor=(196, 196, 192), wall=(58, 58, 62), target=(200, 220, 245),
                  accent=(0, 122, 255), limit=(255, 59, 48)),
    "dark": dict(bg=(18, 18, 20), room=(40, 40, 44), floor=(72, 72, 78), wall=(205, 205, 210), target=(30, 62, 105),
                 accent=(10, 132, 255), limit=(255, 69, 58)),
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


def room_layers(vmap: VacuumMap) -> tuple[np.ndarray, dict[int, np.ndarray]]:
    if (cached := vmap.__dict__.get("_layers")) is not None:
        return cached
    cells = np.frombuffer(vmap.cells, np.uint8).reshape(vmap.height, vmap.width)
    floor = ((cells & 7) == 7) & (cells != FLOOR_NO_ROOM)
    rid = np.where(floor, cells >> 3, 0)
    vmap.__dict__["_layers"] = (rid, {int(r): footprint(rid == r) for r in np.unique(rid) if r})
    return vmap.__dict__["_layers"]


def render_png(vmap: VacuumMap, path: list[tuple[int, int]] | None = None, selected: set[int] | None = None,
               theme: str = "light", walls: list[tuple[int, int, int, int]] | None = None,
               zones: list[list[tuple[int, int]]] | None = None,
               targets: list[list[tuple[int, int]]] | None = None) -> bytes:
    """``targets``: zone polygons (robot coords) being cleaned, tinted like selected rooms."""
    t = THEMES.get(theme, THEMES["light"])
    s = SCALE
    c = np.frombuffer(vmap.cells, np.uint8).reshape(vmap.height, vmap.width)
    img = np.empty(c.shape + (3,), np.uint8)
    img[:] = t["bg"]
    floor = (c & 7) == 7
    _, prints = room_layers(vmap)
    if prints:  # background: each room as it really is (furniture holes filled), robot's view on top
        img[_arr(_img(np.logical_or.reduce(list(prints.values()))).filter(ImageFilter.MaxFilter(5)))] = t["room"]
    img[floor] = t["floor"]
    if selected:
        img[floor & (c != FLOOR_NO_ROOM) & np.isin(c >> 3, list(selected))] = t["target"]
    img[c == WALL] = t["wall"]
    canvas = Image.fromarray(img).resize((vmap.width * s, vmap.height * s), Image.NEAREST).convert("RGBA")

    def px(rx: float, ry: float) -> tuple[float, float]:
        cx, cy = vmap.to_cell(rx, ry)
        return cx * s, cy * s

    def overlay(draw) -> None:
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        draw(ImageDraw.Draw(layer))
        canvas.alpha_composite(layer)

    if targets:  # tint only the floor inside the zone, not walls/obstacles
        mask = Image.new("L", canvas.size, 0)
        for poly in targets:
            ImageDraw.Draw(mask).polygon([px(*p) for p in poly], fill=255)
        fl = Image.fromarray((floor & (c != WALL)).astype(np.uint8) * 255).resize(canvas.size, Image.NEAREST)
        tint = Image.new("RGBA", canvas.size, t["target"] + (255,))
        tint.putalpha(Image.fromarray(np.minimum(np.asarray(mask), np.asarray(fl))))
        canvas.alpha_composite(tint)
    pts = [px(x, y) for x, y in path or []]
    if len(pts) > 1:
        swath = max(2, round(BRUSH_CM / max(vmap.resolution_cm, 1) * s))
        overlay(lambda d: d.line(pts, fill=t["accent"] + (55,), width=swath, joint="curve"))
        overlay(lambda d: d.line(pts, fill=t["accent"] + (230,), width=2, joint="curve"))
    if zones:
        overlay(lambda d: [d.polygon([px(*p) for p in poly], fill=t["limit"] + (40,), outline=t["limit"] + (255,),
                                     width=3) for poly in zones])
    d = ImageDraw.Draw(canvas)
    for x1, y1, x2, y2 in walls or []:
        d.line([px(x1, y1), px(x2, y2)], fill=t["limit"] + (255,), width=4)
    # room names are drawn by the map card (localised), not baked into the image
    x, y = vmap.dock[0] * s, vmap.dock[1] * s
    r = s * 2.4
    d.ellipse([x - r, y - r, x + r, y + r], fill=t["bg"] + (255,), outline=t["accent"] + (255,), width=2)
    d.polygon([(x - r * .45, y), (x, y - r * .45), (x + r * .45, y)], fill=t["accent"] + (255,))
    d.rectangle([x - r * .3, y, x + r * .3, y + r * .38], fill=t["accent"] + (255,))
    if pts and ((pts[-1][0] - x) ** 2 + (pts[-1][1] - y) ** 2) ** 0.5 > s * 6:  # robot = last path point; hidden when docked
        x, y = pts[-1]
        d.ellipse([x - s * 1.9, y - s * 1.9, x + s * 1.9, y + s * 1.9], fill=(255, 255, 255, 255))
        d.ellipse([x - s * 1.3, y - s * 1.3, x + s * 1.3, y + s * 1.3], fill=t["accent"] + (255,))
    out = io.BytesIO()
    canvas.convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue()


def calibration_points(vmap: VacuumMap) -> list[dict]:
    """xiaomi-vacuum-map-card calibration: robot coordinates -> image pixels (3 points)."""
    pts = []
    for cx, cy in ((0, 0), (vmap.width, 0), (0, vmap.height)):
        rx, ry = vmap.to_robot(cx, cy)
        pts.append({"vacuum": {"x": rx, "y": ry}, "map": {"x": cx * SCALE, "y": cy * SCALE}})
    return pts


def _outline(fp: np.ndarray, step: int = 3) -> list[tuple[int, int]]:
    """Row-scan polygon (left edge down, right edge up) every ``step`` cells.
    ponytail: exact for y-monotone rooms; L/U shapes get their concave side bridged."""
    rows = [y for y in range(0, fp.shape[0], step) if fp[y].any()]
    last = int(np.nonzero(fp.any(1))[0].max())
    if rows[-1] != last:
        rows.append(last)
    left = [(int(np.nonzero(fp[y])[0].min()), y) for y in rows]
    right = [(int(np.nonzero(fp[y])[0].max()) + 1, y) for y in rows]
    left[-1] = (left[-1][0], left[-1][1] + 1)
    right[-1] = (right[-1][0], right[-1][1] + 1)
    return left + right[::-1]


def room_outlines(vmap: VacuumMap) -> dict[int, dict]:
    """Per room: label position and outline (row-scan polygon) in robot coordinates, for the card."""
    _, prints = room_layers(vmap)
    out = {}
    for r, fp in prints.items():
        ys, xs = np.nonzero(fp)
        if not len(xs):
            continue
        poly = [vmap.to_robot(x, y) for x, y in _outline(fp)]
        out[r] = {"name": vmap.rooms[r].name if r in vmap.rooms else f"Room {r}",
                  "label": vmap.to_robot(xs.mean(), ys.mean()), "outline": poly}
    return out
