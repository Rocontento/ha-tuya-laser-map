"""Floor plan from a LiDAR room scan (DXF, e.g. exported by OpenPlan3D / any RoomPlan app).

The plan is aligned to the robot map automatically: its walls are rasterised and matched against the
wall cells the robot's LiDAR recorded (FFT cross-correlation over a few rotations). The result is kept
in robot coordinates so it survives map updates, and stored privately in Home Assistant's .storage.
"""

from __future__ import annotations

import math

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .protocol.mapdata import WALL, VacuumMap

LAYERS = ("WALLS", "DOORS", "WINDOWS", "FURNITURE")


def parse_dxf(text: str) -> list[dict]:
    """LWPOLYLINE / LINE entities of the plan layers: [{"layer", "points": [(x, y) mm], "closed"}]."""
    lines = [s.strip() for s in text.splitlines()]
    pairs = list(zip(lines[0::2], lines[1::2]))
    out, i, in_entities = [], 0, False
    while i < len(pairs):
        code, val = pairs[i]
        if code == "2" and val == "ENTITIES":
            in_entities = True
        if in_entities and code == "0" and val in ("LWPOLYLINE", "LINE"):
            ent, x = {"layer": "", "points": [], "closed": False}, 0.0
            i += 1
            while i < len(pairs) and pairs[i][0] != "0":
                c, v = pairs[i]
                if c == "8":
                    ent["layer"] = v.upper()
                elif c == "70" and val == "LWPOLYLINE":
                    ent["closed"] = bool(int(v) & 1)
                elif c in ("10", "11"):
                    x = float(v)
                elif c in ("20", "21"):
                    ent["points"].append((x, float(v)))
                i += 1
            if ent["layer"] in LAYERS and len(ent["points"]) > 1:
                out.append(ent)
            continue
        i += 1
    return out


def _raster(walls: list[list[tuple[float, float]]], theta: float, size: tuple[int, int]) -> np.ndarray:
    """Walls (already centred, in cells, y up) rotated by theta, drawn centred on a size canvas (y down)."""
    im = Image.new("L", size, 0)
    d = ImageDraw.Draw(im)
    ct, st = math.cos(theta), math.sin(theta)
    for poly in walls:
        d.polygon([(ct * x - st * y + size[0] / 2, size[1] / 2 - (st * x + ct * y)) for x, y in poly], fill=255)
    return np.asarray(im) > 0


def align(vmap: VacuumMap, ents: list[dict]) -> list[dict]:
    """Fit the plan onto the robot map; return the entities in robot coordinates.
    ponytail: rotations 0/90/180/270 +-6 deg in 0.5 deg steps, no mirroring (scan apps export y-up)."""
    walls = [e["points"] for e in ents if e["layer"] == "WALLS" and e["closed"]]
    if not walls:
        raise ValueError("no_walls")
    cell_mm = vmap.resolution_cm * 10.0
    allp = np.array([p for w in walls for p in w])
    cx, cy = allp.mean(axis=0)
    centred = [[((x - cx) / cell_mm, (y - cy) / cell_mm) for x, y in w] for w in walls]
    h, w = vmap.height, vmap.width
    size = (w * 2, h * 2)
    cells = np.frombuffer(vmap.cells, np.uint8).reshape(h, w)
    target = np.zeros((h * 2, w * 2))
    target[:h, :w] = np.asarray(Image.fromarray((cells == WALL).astype(np.uint8) * 255).filter(ImageFilter.MaxFilter(3))) > 0
    ft = np.fft.rfft2(target)
    best = (-1.0, 0.0, 0, 0)
    for base in (0, 90, 180, 270):
        for da in np.arange(-6, 6.01, 0.5):
            th = math.radians(base + da)
            corr = np.fft.irfft2(ft * np.conj(np.fft.rfft2(_raster(centred, th, size))), s=target.shape)
            iy, ix = np.unravel_index(int(np.argmax(corr)), corr.shape)
            if corr[iy, ix] > best[0]:
                best = (float(corr[iy, ix]), th, int(iy), int(ix))
    _, th, dy, dx = best
    dy -= size[1] if dy > size[1] // 2 else 0
    dx -= size[0] if dx > size[0] // 2 else 0
    ct, st = math.cos(th), math.sin(th)

    def to_robot(x: float, y: float) -> list[int]:
        x, y = (x - cx) / cell_mm, (y - cy) / cell_mm
        col = ct * x - st * y + size[0] / 2 + dx
        row = size[1] / 2 - (st * x + ct * y) + dy
        return list(vmap.to_robot(col, row))

    return [{"layer": e["layer"], "closed": e["closed"], "points": [to_robot(x, y) for x, y in e["points"]]}
            for e in ents]
