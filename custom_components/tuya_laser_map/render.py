"""Render a VacuumMap as a true floor plan: every cell as the robot recorded it (walls and obstacles
dark, floor it can reach on top of the full room shape), the area to clean tinted, the cleaned swath, path, dock and live robot dot.
Pillow + numpy only (both ship with Home Assistant)."""

from __future__ import annotations

import io
from collections import deque

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .protocol.mapdata import FLOOR_NO_ROOM, WALL, VacuumMap

SCALE = 5  # output pixels per map cell
BRUSH_CM = 30  # width of the cleaned swath drawn along the path

THEMES = {
    "light": dict(bg=(255, 255, 255), room=(242, 241, 237), floor=(214, 214, 209), obstacle=(150, 150, 152),
                  plan_wall=(44, 44, 48), window=(10, 132, 255), door=(160, 160, 165), furniture=(200, 198, 192), wall=(58, 58, 62), target=(200, 220, 245),
                  accent=(0, 122, 255), limit=(255, 59, 48)),
    "dark": dict(bg=(18, 18, 20), room=(36, 36, 40), floor=(66, 66, 72), obstacle=(120, 120, 128),
                 plan_wall=(225, 225, 230), window=(10, 132, 255), door=(110, 110, 118), furniture=(70, 70, 76), wall=(205, 205, 210), target=(30, 62, 105),
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


_PLAN_CACHE: dict = {}


def _plan_layers(vmap: VacuumMap, plan: list[dict], t: dict, px, size) -> tuple[np.ndarray, Image.Image]:
    """House interior mask + drawn walls/windows/doors/furniture. Static while the map frame doesn't
    change, so cached (it was most of the render time on a small server)."""
    key = (id(plan), vmap.width, vmap.height, vmap.origin, vmap.resolution_cm, id(t))
    if key in _PLAN_CACHE:
        return _PLAN_CACHE[key]
    block = Image.new("L", size, 0)
    bd = ImageDraw.Draw(block)
    for e in plan:  # interior = what the outside can't reach once walls/windows/doors close the house
        if e["layer"] in ("WALLS", "WINDOWS", "DOORS"):
            q = [px(*p) for p in e["points"]]
            bd.polygon(q, fill=255) if e["closed"] else bd.line(q, fill=255, width=SCALE * 3)
    ImageDraw.floodfill(block, (0, 0), 128)
    lines = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(lines)
    for e in plan:
        if e["layer"] == "FURNITURE":
            q = [px(*p) for p in e["points"]]
            d.line(q + ([q[0]] if e["closed"] else []), fill=t["furniture"], width=1)
    for e in plan:
        q = [px(*p) for p in e["points"]]
        if e["layer"] == "WALLS" and e["closed"]:
            d.polygon(q, fill=t["plan_wall"])
        elif e["layer"] == "WINDOWS":
            d.line(q + ([q[0]] if e["closed"] else []), fill=t["window"], width=2)
        elif e["layer"] == "DOORS":
            d.line(q, fill=t["door"], width=2)
    _PLAN_CACHE.clear()  # ponytail: one robot, one entry
    _PLAN_CACHE[key] = (np.asarray(block) != 128, lines)
    return _PLAN_CACHE[key]


def render_png(vmap: VacuumMap, path: list | None = None, selected: set[int] | None = None,
               theme: str = "light", walls: list[tuple[int, int, int, int]] | None = None,
               zones: list[list[tuple[int, int]]] | None = None,
               targets: list[list[tuple[int, int]]] | None = None, plan: list[dict] | None = None) -> bytes:
    """``targets``: zone polygons (robot coords) being cleaned, tinted like selected rooms.
    ``plan``: floor plan entities in robot coords (floorplan.align); drawn as the real house behind
    what the robot sees."""
    t = THEMES.get(theme, THEMES["light"])
    s = SCALE
    c = np.frombuffer(vmap.cells, np.uint8).reshape(vmap.height, vmap.width)
    img = np.empty(c.shape + (3,), np.uint8)
    img[:] = t["bg"]
    floor = (c & 7) == 7
    size = (vmap.width * s, vmap.height * s)

    def px(rx: float, ry: float) -> tuple[float, float]:
        cx, cy = vmap.to_cell(rx, ry)
        return cx * s, cy * s

    inside = plan_lines = None
    if plan:
        inside, plan_lines = _plan_layers(vmap, plan, t, px, size)
    else:
        _, prints = room_layers(vmap)
        if prints:  # no plan: each room's footprint (furniture holes filled) as background
            img[_arr(_img(np.logical_or.reduce(list(prints.values()))).filter(ImageFilter.MaxFilter(5)))] = t["room"]
    img[floor] = t["floor"]
    if selected:
        img[floor & (c != FLOOR_NO_ROOM) & np.isin(c >> 3, list(selected))] = t["target"]
    img[c == WALL] = t["obstacle" if plan else "wall"]
    big = np.asarray(Image.fromarray(img).resize(size, Image.NEAREST))
    if inside is not None:
        robot = np.asarray(Image.fromarray(((floor | (c == WALL)) * 255).astype(np.uint8)).resize(size, Image.NEAREST)) > 0
        out = np.empty_like(big)
        out[:] = t["bg"]
        out[inside] = t["room"]
        keep = inside & robot  # robot data outside the house (seen through windows) is dropped
        out[keep] = big[keep]
        big = out
    canvas = Image.fromarray(big).convert("RGBA")
    if plan_lines is not None:
        canvas.alpha_composite(plan_lines)

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
    segs = path or []
    if segs and not isinstance(segs[0][0], (list, tuple)):  # a single path (list of points)
        segs = [segs]
    segs = [[px(x, y) for x, y in seg] for seg in segs]
    pts = segs[-1] if segs else []
    swath = max(2, round(BRUSH_CM / max(vmap.resolution_cm, 1) * s))
    overlay(lambda d: [d.line(q, fill=t["accent"] + (55,), width=swath, joint="curve") for q in segs if len(q) > 1])
    overlay(lambda d: [d.line(q, fill=t["accent"] + (230,), width=2, joint="curve") for q in segs if len(q) > 1])
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


def _contour(m: np.ndarray) -> list[tuple[int, int]]:
    """Exact outline of a cell mask along cell edges (right angles only), largest loop, collinear points dropped."""
    nxt: dict[tuple[int, int], list[tuple[int, int]]] = {}
    pad = np.pad(m, 1)
    for (dy, dx), (a, b) in {(-1, 0): ((0, 0), (1, 0)), (0, 1): ((1, 0), (1, 1)),
                             (1, 0): ((1, 1), (0, 1)), (0, -1): ((0, 1), (0, 0))}.items():
        edge = m & ~pad[1 + dy:1 + dy + m.shape[0], 1 + dx:1 + dx + m.shape[1]]
        for y, x in zip(*np.nonzero(edge)):
            nxt.setdefault((int(x) + a[0], int(y) + a[1]), []).append((int(x) + b[0], int(y) + b[1]))
    loops = []
    while nxt:
        start = next(iter(nxt))
        loop, p = [start], start
        while True:
            q = nxt[p].pop()
            if not nxt[p]:
                del nxt[p]
            if q == start:
                break
            loop.append(q)
            p = q
            if p not in nxt:  # malformed (shouldn't happen); give up on this loop
                break
        loops.append(loop)
    area = lambda lp: abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(lp, lp[1:] + lp[:1])))
    lp = max(loops, key=area)
    return [p for i, p in enumerate(lp)  # keep corners only
            if (lp[i - 1][0] - p[0]) * (lp[(i + 1) % len(lp)][1] - p[1]) != (lp[i - 1][1] - p[1]) * (lp[(i + 1) % len(lp)][0] - p[0])]


def room_regions(vmap: VacuumMap, plan: list[dict] | None = None) -> dict[int, np.ndarray]:
    """Cells of each room. With a floor plan: the real room, i.e. the house interior between the plan's
    walls, shared out to the nearest room floor the robot recorded. Without: the robot footprint."""
    _, prints = room_layers(vmap)
    if not plan:
        return {r: fp for r, fp in prints.items() if fp.any()}
    h, w = vmap.height, vmap.width
    cell = lambda p: (vmap.to_cell(*p)[0] + 1, vmap.to_cell(*p)[1] + 1)  # 1-cell border so (0, 0) is outside
    walls = Image.new("L", (w + 2, h + 2), 0)
    d = ImageDraw.Draw(walls)
    for e in plan:
        if e["layer"] == "WALLS" and e["closed"]:
            d.polygon([cell(p) for p in e["points"]], fill=255, outline=255)
        elif e["layer"] == "WINDOWS":
            d.line([cell(p) for p in e["points"]], fill=255, width=2)
    closed = walls.copy()
    for e in plan:  # doors close the house for the inside/outside test only
        if e["layer"] == "DOORS":
            ImageDraw.Draw(closed).line([cell(p) for p in e["points"]], fill=255, width=2)
    ImageDraw.floodfill(closed, (0, 0), 128)
    free = ((np.asarray(closed) != 128) & (np.asarray(walls) == 0))[1:-1, 1:-1]
    c = np.frombuffer(vmap.cells, np.uint8).reshape(h, w)
    floor = ((c & 7) == 7) & (c != FLOOR_NO_ROOM)
    lab = np.where(free & floor, c >> 3, 0).astype(np.int32)
    q = deque(zip(*np.nonzero(lab)))
    while q:  # multi-source BFS inside the house: every free cell joins the nearest room
        y, x = q.popleft()
        for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
            if 0 <= ny < h and 0 <= nx < w and free[ny, nx] and not lab[ny, nx]:
                lab[ny, nx] = lab[y, x]
                q.append((ny, nx))
    return {int(r): lab == r for r in np.unique(lab) if r}


def room_outlines(vmap: VacuumMap, plan: list[dict] | None = None) -> dict[int, dict]:
    """Per room: label position and exact outline in robot coordinates, for the card."""
    out = {}
    for r, m in room_regions(vmap, plan).items():
        ys, xs = np.nonzero(m)
        i = int(np.argmin((xs - xs.mean()) ** 2 + (ys - ys.mean()) ** 2))  # label on a cell of the room
        out[r] = {"name": vmap.rooms[r].name if r in vmap.rooms else f"Room {r}",
                  "label": vmap.to_robot(xs[i] + 0.5, ys[i] + 0.5),
                  "outline": [vmap.to_robot(x, y) for x, y in _contour(m)]}
    return out
