"""Tuya laser-sweeper command frames for the raw ``command_trans`` DP (usually DP 15).

Frame: ``aa | 01 | len u32 BE (1 + len(data)) | cmd | data | sum(cmd..data) & 0xFF``.
Layouts follow Tuya's own panel encoder (@ray-js/robot-protocol); v1 variants are the
ones the Conga 9590 answers. Coordinates are int16 BE in 1/10 map pixel relative to the
map origin, y pointing up (see ``MapGeometry``).
"""

from __future__ import annotations

import struct

DP_COMMAND = "15"


def frame(cmd: int, data: bytes = b"") -> bytes:
    body = bytes([cmd]) + data
    return b"\xaa\x01" + len(body).to_bytes(4, "big") + body + bytes([sum(body) & 0xFF])


def parse(raw: bytes) -> tuple[int, bytes] | None:
    if len(raw) < 8 or raw[0] != 0xAA:
        return None
    if raw[1] == 0x01:
        length = int.from_bytes(raw[2:6], "big")
        body = raw[6:6 + length]
    else:
        length = raw[2]
        body = raw[3:3 + length]
    if not body or (sum(body) & 0xFF) != raw[6 + length if raw[1] == 0x01 else 3 + length]:
        return None
    return body[0], body[1:]


def clean_rooms(room_ids: list[int], times: int = 1) -> bytes:
    """0x14: clean the given rooms (ids from the map's room table)."""
    return frame(0x14, bytes([times, len(room_ids), *room_ids]))


def clean_zones(zones: list[list[tuple[int, int]]], times: int = 1) -> bytes:
    """0x28: clean polygons (usually rectangles, 4 points in order)."""
    data = bytearray([times, len(zones)])
    for points in zones:
        data.append(len(points))
        for x, y in points:
            data += struct.pack(">hh", x, y)
    return frame(0x28, bytes(data))


def goto_point(x: int, y: int) -> bytes:
    """0x16: go to / spot clean at a point."""
    return frame(0x16, struct.pack(">hh", x, y))


def set_walls(walls: list[tuple[int, int, int, int]]) -> bytes:
    """0x12: replace all virtual walls (each a segment x1, y1, x2, y2). The robot answers 0x13."""
    return frame(0x12, bytes([len(walls)]) + b"".join(struct.pack(">4h", *w) for w in walls))


def set_nogo(zones: list[dict], protocol: int = 1) -> bytes:
    """0x38: replace all restricted zones ({"mode": 1 no sweep / 2 no mop, "points": [(x, y)...]}).
    The robot answers 0x39. Names are not kept (the app shows none for this robot)."""
    data = bytearray([protocol, len(zones)])
    for z in zones:
        pts = z["points"]
        data.append(z["mode"])
        if protocol == 2:
            data.append(0 if len(pts) == 4 else 1)
        data.append(len(pts))
        for x, y in pts:
            data += struct.pack(">hh", x, y)
        data += bytes(20)  # name length 0 + 19-byte name
    return frame(0x38, bytes(data))


def request(cmd: int) -> bytes:
    """Ask the robot to report state for an odd (report) command, e.g. 0x13 walls, 0x39 no-go zones."""
    return frame(cmd)


def decode_walls(data: bytes) -> list[tuple[int, int, int, int]]:
    """0x13 report: virtual walls as (x1, y1, x2, y2)."""
    return [struct.unpack_from(">4h", data, 1 + 8 * i) for i in range(data[0])] if data else []


def decode_nogo_v2(data: bytes) -> list[dict]:
    """0x39 report (protocol v1/v2): restricted zones with mode and polygon."""
    zones, ver, count, pos = [], data[0], data[1], 2
    for _ in range(count):
        mode = data[pos]
        pos += 2 if ver == 2 else 1  # v2 adds a type byte
        npts = data[pos]
        pts = [struct.unpack_from(">hh", data, pos + 1 + 4 * k) for k in range(npts)]
        pos += 1 + 4 * npts + 20  # name length + 19-byte name
        zones.append({"mode": mode, "points": pts})
    return zones
