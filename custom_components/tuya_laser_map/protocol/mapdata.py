"""Decode Tuya laser maps (map.bin / map.bin.stream, version 2) and paths (cleanPath.bin).

Map header (24 bytes, big endian): version u8, map id u16, type u8, width u16, height u16,
origin x u16, origin y u16, resolution u16 (cm per cell), dock x u16, dock y u16,
decompressed length u32, compressed length u16 — positions in 1/10 cell.
Body: LZ4 block -> width*height cells, then the room table.
Cell: low 3 bits == 7 means floor, room id = cell >> 3; 0xF1 wall; 0xE0 unknown; 0xE7 floor
outside any room.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field

WALL = 0xF1
FLOOR_NO_ROOM = 0xE7


def lz4_block_decompress(src: bytes, size_hint: int = 0) -> bytes:
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        out += src[i:i + lit]
        i += lit
        if i >= n:
            break
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        mlen = token & 0x0F
        if mlen == 15:
            while True:
                b = src[i]
                i += 1
                mlen += b
                if b != 255:
                    break
        mlen += 4
        start = len(out) - offset
        for k in range(mlen):  # byte-wise: overlapping copies are legal
            out.append(out[start + k])
    return bytes(out)


@dataclass
class Room:
    id: int
    name: str


@dataclass
class VacuumMap:
    map_id: int
    width: int
    height: int
    origin: tuple[float, float]  # cells
    resolution_cm: int
    dock: tuple[float, float]  # cells
    cells: bytes
    rooms: dict[int, Room] = field(default_factory=dict)

    def cell(self, x: int, y: int) -> int:
        return self.cells[y * self.width + x]

    # robot coordinates: int16, 1/10 cell, relative to origin, y up
    def to_robot(self, cx: float, cy: float) -> tuple[int, int]:
        return round((cx - self.origin[0]) * 10), round((self.origin[1] - cy) * 10)

    def to_cell(self, rx: float, ry: float) -> tuple[float, float]:
        return self.origin[0] + rx / 10, self.origin[1] - ry / 10


def parse_map(raw: bytes) -> VacuumMap:
    version = raw[0]
    if version not in (1, 2):
        raise ValueError(f"unsupported map version {version}")
    map_id = struct.unpack_from(">H", raw, 1)[0]
    w, h, ox, oy, res, dx, dy = struct.unpack_from(">7H", raw, 4)
    total = struct.unpack_from(">I", raw, 18)[0]
    dec = lz4_block_decompress(raw[24:], total)
    if len(dec) < w * h:
        raise ValueError("truncated map")
    rooms: dict[int, Room] = {}
    tail = dec[w * h:]
    for m in re.finditer(rb"([\x01-\x20])([ -~]{2,})", tail):  # 47-byte records, id u16 at name-26
        if m.group(1)[0] == len(m.group(2)) and m.start() >= 26:
            rid = struct.unpack_from(">H", tail, m.start() - 26)[0]
            rooms[rid] = Room(rid, m.group(2).decode(errors="replace"))
    cells = dec[: w * h]
    for rid in {c >> 3 for c in cells if c & 7 == 7 and c != FLOOR_NO_ROOM}:
        rooms.setdefault(rid, Room(rid, f"Room {rid}"))
    return VacuumMap(map_id, w, h, (ox / 10, oy / 10), res, (dx / 10, dy / 10), cells, rooms)


def parse_path(raw: bytes) -> list[tuple[int, int]]:
    """cleanPath.bin / navPath.bin: 13-byte header (count u32 at 5) + int16 x,y pairs."""
    if len(raw) < 13:
        return []
    count = struct.unpack_from(">I", raw, 5)[0]
    count = min(count, (len(raw) - 13) // 4)
    return [struct.unpack_from(">hh", raw, 13 + 4 * i) for i in range(count)]
