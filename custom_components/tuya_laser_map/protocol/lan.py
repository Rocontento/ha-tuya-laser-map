"""Tuya LAN protocol 3.3 framing (TCP 6668): DP writes and the type-32 P2P signaling."""

from __future__ import annotations

import asyncio
import base64
import json
import time
import zlib
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PORT = 6668
PREFIX = (0x000055AA).to_bytes(4, "big")
SUFFIX = (0x0000AA55).to_bytes(4, "big")
CMD_CONTROL = 0x07
CMD_DP_QUERY = 0x0A
CMD_RTC = 0x20  # type 32: P2P signaling ("thing.m.rtc")
_V33_HEADER = b"3.3" + bytes(12)


def _ecb(key: bytes, data: bytes, encrypt: bool) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB())  # noqa: S305 - Tuya 3.3 is AES-ECB
    if encrypt:
        pad = 16 - len(data) % 16
        enc = cipher.encryptor()
        return enc.update(data + bytes([pad]) * pad) + enc.finalize()
    dec = cipher.decryptor()
    out = dec.update(data) + dec.finalize()
    return out[: -out[-1]] if out and 1 <= out[-1] <= 16 else out


def encode_frame(key: bytes, command: int, payload: dict[str, Any] | bytes, sequence: int) -> bytes:
    """One 3.3 frame. DP writes carry the "3.3" header; type-32 signaling must not."""
    body = payload if isinstance(payload, bytes) else json.dumps(payload, separators=(",", ":")).encode()
    body = _ecb(key, body, True)
    if command == CMD_CONTROL:
        body = _V33_HEADER + body
    frame = PREFIX + sequence.to_bytes(4, "big") + command.to_bytes(4, "big") + (len(body) + 8).to_bytes(4, "big") + body
    return frame + (zlib.crc32(frame) & 0xFFFFFFFF).to_bytes(4, "big") + SUFFIX


def decode_frames(key: bytes, buffer: bytes) -> tuple[list[tuple[int, Any]], bytes]:
    """Decode every whole frame in ``buffer``; JSON payloads come back parsed."""
    out: list[tuple[int, Any]] = []
    pos = 0
    while True:
        start = buffer.find(PREFIX, pos)
        if start < 0 or start + 16 > len(buffer):
            break
        length = int.from_bytes(buffer[start + 12:start + 16], "big")
        end = start + 16 + length
        if end > len(buffer):
            break
        command = int.from_bytes(buffer[start + 8:start + 12], "big")
        body = buffer[start + 16:end - 8]
        if body[4:7] == b"3.3":  # 4-byte return code + "3.3" header
            body = body[19:]
        elif body[:3] == b"3.3":
            body = body[15:]
        elif len(body) % 16 == 4:  # return code only
            body = body[4:]
        payload: Any = body
        if body:
            try:
                payload = json.loads(_ecb(key, body, False))
            except Exception:  # noqa: BLE001 - heartbeats/status acks are not JSON
                payload = body
        out.append((command, payload))
        pos = end
    return out, buffer[pos:]


async def send_dps(host: str, device_id: str, local_key: str, dps: dict[str, Any], timeout: float = 5.0) -> dict[str, Any]:
    """Write DPs over a short-lived LAN connection; return any DPs the device reports back."""
    key = local_key.encode()
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, PORT), timeout)
    try:
        payload = {"devId": device_id, "uid": device_id, "t": str(int(time.time())), "dps": dps}
        writer.write(encode_frame(key, CMD_CONTROL, payload, 1))
        await writer.drain()
        reported: dict[str, Any] = {}
        buffer = b""
        deadline = asyncio.get_running_loop().time() + timeout
        while (left := deadline - asyncio.get_running_loop().time()) > 0:
            try:
                chunk = await asyncio.wait_for(reader.read(4096), left)
            except asyncio.TimeoutError:
                break
            if not chunk:
                break
            frames, buffer = decode_frames(key, buffer + chunk)
            for _, data in frames:
                if isinstance(data, dict) and isinstance(data.get("dps"), dict):
                    reported.update(data["dps"])
            if reported:
                break
        return reported
    finally:
        writer.close()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()
