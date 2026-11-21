"""Tuya sweeper P2P map channel over the LAN (variant "tuya", AES/KCP 3).

Flow (reverse engineered on a Conga 9590 / Tuya product q3yo7zqlfxkr0cv0):
1. TCP 6668, protocol 3.3, command 0x20 (no 3.3 header): send an SDP offer with our AES
   media key, then a host ICE candidate. The robot answers and sends its candidate.
2. ICE: the robot is the controlling agent. Answer its binding requests (integrity keyed by
   our ice-pwd); our own checks must not carry ICE-CONTROLLING (STUN 420 otherwise).
3. KCP over that UDP pair: every segment (PUSH and ACK) is ``segment | HMAC-SHA1(media_key,
   segment)``; PUSH data is ``IV | AES-128-CBC``. Conversation 0 = control, 5 = streams.
4. Conversation 0 after the robot nominates the pair: auth (type 1, md5(password||localKey)),
   album query ("ipc_sweeper_robot"), download request for the .stream files, start (5, 1).
5. Conversation 5 carries chunks: 80-byte header (name @20, payload len @68, total @72) + data.
The ICE token list must hold Tuya's STUN servers (from the RTC config) or the robot never
gathers its host candidate; no TURN credentials are needed on the LAN.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import secrets
import socket
import struct
import time
import zlib
from collections.abc import Callable

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .lan import CMD_DP_QUERY, CMD_RTC, PORT, decode_frames, encode_frame

_LOGGER = logging.getLogger(__name__)

ALBUM = "ipc_sweeper_robot"
STREAM_FILES = ("map.bin.stream", "cleanPath.bin.stream", "navPath.bin.stream")
_ALNUM = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"

# --- STUN -------------------------------------------------------------------------------
_COOKIE = (0x2112A442).to_bytes(4, "big")
BIND_REQUEST, BIND_SUCCESS = 0x0001, 0x0101


def _attr(t: int, v: bytes) -> bytes:
    return struct.pack(">HH", t, len(v)) + v + b"\0" * ((4 - len(v) % 4) % 4)


def _stun(msg_type: int, tid: bytes, attrs: list[bytes], key: bytes | None) -> bytes:
    body = b"".join(attrs)
    if key is not None:
        hdr = struct.pack(">HH", msg_type, len(body) + 24) + _COOKIE + tid
        body += _attr(0x0008, hmac.new(key, hdr + body, hashlib.sha1).digest())
    hdr = struct.pack(">HH", msg_type, len(body) + 8) + _COOKIE + tid
    body += _attr(0x8028, struct.pack(">I", (zlib.crc32(hdr + body) & 0xFFFFFFFF) ^ 0x5354554E))
    return struct.pack(">HH", msg_type, len(body)) + _COOKIE + tid + body


def _is_stun(d: bytes) -> bool:
    return len(d) >= 20 and d[4:8] == _COOKIE


def _xor_addr(ip: str, port: int) -> bytes:
    b = bytearray(8)
    b[1] = 1
    struct.pack_into(">H", b, 2, port ^ 0x2112)
    for i, o in enumerate(ip.split(".")):
        b[4 + i] = int(o) ^ _COOKIE[i]
    return bytes(b)


# --- KCP + records ----------------------------------------------------------------------
_PUSH, _ACK = 0x51, 0x52


def _record(key: bytes, plain: bytes) -> bytes:
    iv = os.urandom(16)
    pad = 16 - len(plain) % 16
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return iv + enc.update(plain + bytes([pad]) * pad) + enc.finalize()


def _unrecord(key: bytes, rec: bytes) -> bytes:
    dec = Cipher(algorithms.AES(key), modes.CBC(rec[:16])).decryptor()
    out = dec.update(rec[16:]) + dec.finalize()
    return out[: -out[-1]] if out and 1 <= out[-1] <= 16 else out


def _tag(key: bytes, seg: bytes) -> bytes:
    return seg + hmac.new(key, seg, hashlib.sha1).digest()


def _segments(dgram: bytes):
    pos = 0
    while pos + 24 <= len(dgram):
        conv, cmd, _frg, _wnd, ts, sn, una, ln = struct.unpack_from("<IBBHIIII", dgram, pos)
        yield conv, cmd, ts, sn, una, dgram[pos + 24:pos + 24 + ln]
        pos += 24 + ln + 20  # every segment is tagged


# --- control conversation ---------------------------------------------------------------
_MAGIC = (0x12345678).to_bytes(4, "little")


def _ctl(ptype: int, sub: int, payload: bytes) -> bytes:
    return _MAGIC + struct.pack("<IIII", ptype, 0, sub, len(payload)) + payload


def _control_burst(device_password: str, local_key: str) -> list[bytes]:
    cred = hashlib.md5(f"{device_password}||{local_key}".encode(), usedforsecurity=False).hexdigest()
    auth = bytearray(96)
    auth[0:5] = b"admin"
    auth[32:32 + len(cred)] = cred.encode()
    query = b"\xff\xff\xff\xff" + ALBUM.encode().ljust(112, b"\0")
    names = b"".join(n.encode().ljust(48, b"\0") for n in STREAM_FILES)
    download = struct.pack("<II", 5, 0) + ALBUM.encode().ljust(52, b"\0") + struct.pack("<I", len(STREAM_FILES)) + names
    start = struct.pack("<II", 5, 1) + bytes(64)
    return [
        _MAGIC + struct.pack("<I", 1) + bytes(auth),
        _ctl(2, 0x000C0064, query),
        _ctl(0x00010003, 0x000D0064, download),
        _ctl(4, 0x000D0064, start),
    ]


def split_chunks(stream: bytes) -> list[tuple[str, int, int, bytes]]:
    """Split conversation-5 bytes into (name, index, total, payload) chunks."""
    out, pos = [], 0
    while pos + 80 <= len(stream):
        name = stream[pos + 20:pos + 68].split(b"\0")[0].decode(errors="replace")
        plen, total = struct.unpack_from("<II", stream, pos + 68)
        idx = struct.unpack_from("<I", stream, pos + 12)[0]
        if pos + 80 + plen > len(stream):
            break
        out.append((name, idx, total, stream[pos + 80:pos + 80 + plen]))
        pos += 80 + plen
    return out


class StreamAssembler:
    """Rebuild complete files from chunks; emits (name, data) when a file is complete."""

    def __init__(self) -> None:
        self._buf = b""
        self._parts: dict[str, dict[int, bytes]] = {}

    def feed(self, data: bytes) -> list[tuple[str, bytes]]:
        self._buf += data
        done, pos = [], 0
        while pos + 80 <= len(self._buf):
            name = self._buf[pos + 20:pos + 68].split(b"\0")[0].decode(errors="replace")
            plen, total = struct.unpack_from("<II", self._buf, pos + 68)
            idx = struct.unpack_from("<I", self._buf, pos + 12)[0]
            if pos + 80 + plen > len(self._buf):
                break
            parts = self._parts.setdefault(name, {})
            if idx == 0:
                parts.clear()
            parts[idx] = self._buf[pos + 80:pos + 80 + plen]
            pos += 80 + plen
            blob = b"".join(parts[i] for i in sorted(parts))
            if len(blob) >= total:
                done.append((name, blob[:total]))
                parts.clear()
        self._buf = self._buf[pos:]
        return done


async def run_session(
    host: str,
    device_id: str,
    local_key: str,
    device_password: str,
    user_id: str,
    ice_servers: list[dict],
    on_file: Callable[[str, bytes], None],
    idle_timeout: float = 120.0,
) -> None:
    """One P2P session: returns when the robot goes quiet for ``idle_timeout`` or drops."""
    key = local_key.encode()
    media_key = os.urandom(16)
    ufrag, pwd = "".join(secrets.choice(_ALNUM) for _ in range(4)), "".join(secrets.choice(_ALNUM) for _ in range(24))
    now = int(time.time())
    session_id = f"{device_id}{now}{''.join(secrets.choice(_ALNUM) for _ in range(8))}"
    trace_id = f"ipc_p2p_ios_{device_id}_{now}000"
    loop = asyncio.get_running_loop()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((host, PORT))
        local_ip = probe.getsockname()[0]
    queue: asyncio.Queue = asyncio.Queue()

    class _Proto(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            queue.put_nowait((data, addr))

    transport, _ = await loop.create_datagram_endpoint(_Proto, local_addr=(local_ip, 0))
    udp_port = transport.get_extra_info("sockname")[1]
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, PORT), 10)
    st: dict = {"r_ufrag": None, "r_pwd": None, "addr": None, "up": False, "unacked": {}}
    rx_next = {0: 0, 5: 0}
    rx_seen: dict[int, set] = {0: set(), 5: set()}
    conv5: dict[int, bytes] = {}
    assembler = StreamAssembler()

    def send(d: bytes) -> None:
        if st["addr"]:
            transport.sendto(d, st["addr"])

    def bind_request() -> bytes:
        attrs = [_attr(0x0006, f"{st['r_ufrag']}:{ufrag}".encode()), _attr(0x0024, b"\x6e\xff\xff\xff")]
        return _stun(BIND_REQUEST, b"AYUT" + os.urandom(8), attrs, st["r_pwd"])

    def start_control() -> None:
        for sn, pkt in enumerate(_control_burst(device_password, local_key)):
            hdr = struct.pack("<IBBHIIII", 0, _PUSH, 0, 512, int(loop.time() * 1000) & 0xFFFFFFFF, sn, 0, 0)
            rec = _record(media_key, pkt)
            seg = hdr[:20] + struct.pack("<I", len(rec)) + rec
            st["unacked"][sn] = _tag(media_key, seg)
            send(st["unacked"][sn])

        def resend(n: int = 0) -> None:
            if st["unacked"] and n < 10:
                for d in st["unacked"].values():
                    send(d)
                loop.call_later(0.6, resend, n + 1)

        loop.call_later(0.6, resend)

    async def signaling() -> None:
        sdp = "\r\n".join((
            "v=0", f"o=- {now} 1 IN IP4 127.0.0.1", "s=-", "t=0 0", "a=group:BUNDLE imm0",
            f"a=msid-semantic: WMS {session_id}", "m=application 9 tuya 6001", "c=IN IP4 0.0.0.0",
            "a=rtcp:9 IN IP4 0.0.0.0", f"a=ice-ufrag:{ufrag}", f"a=ice-pwd:{pwd}", "a=ice-options:trickle",
            f"a=aes-key:{media_key.hex()}", "a=mid:imm0", "a=rtpmap:6001 AES/KCP 3", f"a=ssrc:0 cname:{user_id}", ""))
        hdr = {"from": user_id, "to": device_id, "sessionid": session_id, "moto_id": "", "path": "lan", "trace_id": trace_id}
        writer.write(encode_frame(key, CMD_DP_QUERY, {"gwId": device_id, "devId": device_id}, 1))
        writer.write(encode_frame(key, CMD_RTC, {
            "msg": {"sdp": sdp, "preconnect": True, "token": ice_servers, "tcp_token": {}, "log": {}},
            "header": {**hdr, "type": "offer", "is_pre": 0, "p2p_skill": 35, "security_level": 3}}, 2))
        await writer.drain()
        await asyncio.sleep(0.4)
        cand = f"a=candidate:{secrets.randbelow(2_000_000_000)} 1 UDP 2130706431 {local_ip} {udp_port} typ host\r\n"
        writer.write(encode_frame(key, CMD_RTC, {"msg": {"candidate": cand}, "header": {**hdr, "type": "candidate", "sub_dev_id": ""}}, 3))
        await writer.drain()
        buf = b""
        while chunk := await reader.read(65536):
            frames, buf = decode_frames(key, buf + chunk)
            for cmd, payload in frames:
                if cmd != CMD_RTC or not isinstance(payload, dict):
                    continue
                h, m = payload.get("header", {}), payload.get("msg", {})
                if h.get("type") == "answer":
                    s = m.get("sdp", "")
                    st["r_ufrag"] = (re.search(r"ice-ufrag:(\S+)", s) or [None, None])[1]
                    p = re.search(r"ice-pwd:(\S+)", s)
                    st["r_pwd"] = p.group(1).encode() if p else None
                elif h.get("type") == "disconnect":
                    raise ConnectionError(f"robot closed the session: {m}")

    async def media() -> None:
        while True:
            dgram, src = await asyncio.wait_for(queue.get(), idle_timeout)
            if st["addr"] is None and src[0] == host:
                st["addr"] = src
            if _is_stun(dgram):
                if int.from_bytes(dgram[:2], "big") == BIND_REQUEST and st["r_pwd"]:
                    send(_stun(BIND_SUCCESS, dgram[8:20], [_attr(0x0020, _xor_addr(*src))], pwd.encode()))
                    if not st["up"] and b"\x00\x25\x00\x00" in dgram[20:]:  # robot nominated (USE-CANDIDATE)
                        st["up"] = True
                        send(bind_request())
                        loop.call_later(0.3, start_control)
                continue
            for conv, cmd, ts, sn, una, data in _segments(dgram):
                if conv == 0:
                    for s in [s for s in st["unacked"] if s < una or (cmd == _ACK and s == sn)]:
                        st["unacked"].pop(s, None)
                if cmd != _PUSH or conv not in rx_next:
                    continue
                rx_seen[conv].add(sn)
                while rx_next[conv] in rx_seen[conv]:
                    rx_next[conv] += 1
                ack = struct.pack("<IBBHIIII", conv, _ACK, 0, 512, ts, sn, rx_next[conv], 0)
                send(_tag(media_key, ack))
                if conv == 5 and sn >= st.setdefault("deliver", 0) and sn not in conv5:
                    conv5[sn] = _unrecord(media_key, data)
                    while st["deliver"] in conv5:  # deliver in order
                        for name, blob in assembler.feed(conv5.pop(st["deliver"])):
                            on_file(name, blob)
                        st["deliver"] += 1

    sig = asyncio.ensure_future(signaling())
    med = asyncio.ensure_future(media())
    try:
        done, _ = await asyncio.wait({sig, med}, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            if t.exception() and not isinstance(t.exception(), asyncio.TimeoutError):
                raise t.exception()
    finally:
        for t in (sig, med):
            t.cancel()
        try:
            hdr = {"from": user_id, "to": device_id, "sessionid": session_id, "moto_id": "", "path": "lan", "trace_id": trace_id}
            writer.write(encode_frame(key, CMD_RTC, {"header": {**hdr, "type": "disconnect"}, "msg": {"close_reason": 4, "close_reason_local": 0}}, 9))
            await writer.drain()
        except Exception:  # noqa: BLE001
            pass
        transport.close()
        writer.close()
