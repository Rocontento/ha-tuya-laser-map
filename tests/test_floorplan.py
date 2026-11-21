"""Floor plan: a synthetic DXF room is aligned onto a synthetic robot map (no real home data)."""

import numpy as np

from custom_components.tuya_laser_map.floorplan import align, parse_dxf
from custom_components.tuya_laser_map.protocol.mapdata import WALL, VacuumMap
from custom_components.tuya_laser_map.render import render_png


def _dxf(polys: list[list[tuple[float, float]]]) -> str:
    out = ["0", "SECTION", "2", "ENTITIES"]
    for p in polys:
        out += ["0", "LWPOLYLINE", "8", "WALLS", "90", str(len(p)), "70", "1"]
        for x, y in p:
            out += ["10", str(x), "20", str(y)]
    return "\n".join(out + ["0", "ENDSEC", "0", "EOF"])


def test_plan_aligns_to_robot_walls():
    w, h = 120, 100
    cells = np.full((h, w), 0xE7, np.uint8)
    cells[20:80, 30] = cells[20:80, 90] = WALL       # an L-ish room: 3 m x 3 m walls at 5 cm cells
    cells[20, 30:91] = cells[80, 30:91] = WALL
    cells[50, 30:60] = WALL                           # asymmetric inner wall so the fit is unique
    vm = VacuumMap(1, w, h, (60.0, 50.0), 5, (60.0, 50.0), cells.tobytes())
    t = 60  # wall thickness in mm
    # same room drawn in mm, shifted by an arbitrary offset: the fit must undo it
    off = (12_345.0, -6_789.0)
    rect = lambda x1, y1, x2, y2: [(x1 + off[0], y1 + off[1]), (x2 + off[0], y1 + off[1]),
                                   (x2 + off[0], y2 + off[1]), (x1 + off[0], y2 + off[1])]
    walls = [rect(0, 0, t, 3000), rect(3000, 0, 3000 + t, 3000), rect(0, 0, 3000 + t, t),
             rect(0, 3000, 3000 + t, 3000 + t), rect(0, 1500, 1500, 1500 + t)]
    ents = parse_dxf(_dxf(walls))
    assert len(ents) == 5 and ents[0]["layer"] == "WALLS" and ents[0]["closed"]
    plan = align(vm, ents)
    # the long left wall must land on map column 30 (robot x = (30 - 60) * 10 = -300)
    xs = [p[0] for p in plan[0]["points"]]
    assert abs(min(xs) - (-300)) <= 15, xs
    assert render_png(vm, plan=plan)[:4] == b"\x89PNG"
