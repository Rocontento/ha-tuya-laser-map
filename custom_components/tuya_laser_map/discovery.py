"""Find a Tuya device's LAN IP: from an existing tuya_local/localtuya entry, else its UDP broadcast."""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import suppress

from homeassistant.core import HomeAssistant

from .protocol.lan import _ecb

_UDP_KEY = hashlib.md5(b"yGAdlopoPVldABfn").digest()  # noqa: S324 - fixed key from the Tuya protocol


def decode_broadcast(data: bytes) -> dict | None:
    """Decode a 0x55AA discovery frame (6666 plain, 6667 AES with the public UDP key)."""
    if len(data) < 28 or data[:4] != b"\x00\x00\x55\xaa":
        return None
    body = data[20:-8]
    for candidate in (body, body[4:]):
        with suppress(Exception):
            return json.loads(candidate)
        with suppress(Exception):
            return json.loads(_ecb(_UDP_KEY, candidate, False))
    return None


class _Listener(asyncio.DatagramProtocol):
    def __init__(self, device_id: str, found: asyncio.Future) -> None:
        self.device_id, self.found = device_id, found

    def datagram_received(self, data: bytes, addr) -> None:
        info = decode_broadcast(data)
        if info and info.get("gwId") == self.device_id and not self.found.done():
            self.found.set_result(info.get("ip") or addr[0])


async def find_ip(hass: HomeAssistant, device_id: str, timeout: float = 6.0) -> str | None:
    for domain in ("tuya_local", "localtuya"):
        for entry in hass.config_entries.async_entries(domain):
            if entry.data.get("device_id") == device_id and entry.data.get("host"):
                return entry.data["host"]
            for dev in (entry.data.get("devices") or {}).values():
                if isinstance(dev, dict) and dev.get("device_id") == device_id and dev.get("host"):
                    return dev["host"]
    loop = asyncio.get_running_loop()
    found: asyncio.Future = loop.create_future()
    transports = []
    for port in (6666, 6667):
        with suppress(OSError):
            t, _ = await loop.create_datagram_endpoint(lambda: _Listener(device_id, found),
                                                       local_addr=("0.0.0.0", port), reuse_port=True)  # noqa: S104
            transports.append(t)
    try:
        return await asyncio.wait_for(found, timeout) if transports else None
    except TimeoutError:
        return None
    finally:
        for t in transports:
            t.close()
