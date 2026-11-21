"""Build a small synthetic Tuya v2 map and path (no real home data in the repository)."""

import struct

ROOMS = {1: "Living room", 3: "Kitchen", 9: "Bathroom"}
W, H = 40, 30


def _lz4_literals(data: bytes) -> bytes:
    """A valid LZ4 block made of a single literal run."""
    n = len(data)
    if n < 15:
        return bytes([n << 4]) + data
    out, rest = bytearray([0xF0]), n - 15
    while rest >= 255:
        out.append(255)
        rest -= 255
    out.append(rest)
    return bytes(out) + data


def map_bytes() -> bytes:
    cells = bytearray([0xE0] * (W * H))
    for y in range(H):
        for x in range(W):
            if x in (0, W - 1) or y in (0, H - 1):
                cells[y * W + x] = 0xF1
            elif y < 15:
                cells[y * W + x] = (1 << 3) | 7 if x < 25 else (3 << 3) | 7
            else:
                cells[y * W + x] = (9 << 3) | 7 if x < 12 else (1 << 3) | 7
    tail = bytearray([1, len(ROOMS)])
    for rid, name in ROOMS.items():
        tail += struct.pack(">H", rid) + bytes(24) + bytes([len(name)]) + name.encode().ljust(20, b"\0")
    body = bytes(cells) + bytes(tail)
    comp = _lz4_literals(body)
    header = struct.pack(">BHB7HIH", 2, 7, 1, W, H, 200, 150, 5, 300, 50, len(body), len(comp))
    return header + comp


def path_bytes() -> bytes:
    pts = [(-100, 50), (-80, 50), (-60, 40), (0, 0)]
    return bytes([0, 0, 0, 0, 2]) + struct.pack(">I", len(pts)) + bytes(4) + b"".join(struct.pack(">hh", *p) for p in pts)
