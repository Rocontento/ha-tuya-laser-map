"""Keeps a P2P map session to the robot open, decodes what it streams and renders the map."""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Callable

from homeassistant.core import HomeAssistant, callback

from .const import (
    CONF_DEVICE_ID, CONF_DEVICE_PASSWORD, CONF_HOST, CONF_LOCAL_KEY, CONF_STUN, CONF_THEME, CONF_USER_ID,
    DEFAULT_THEME, ERROR_BACKOFF, RECONNECT_DELAY, SESSION_IDLE_TIMEOUT,
)
from .protocol import commands
from .protocol.lan import b64, send_dps
from .protocol.mapdata import VacuumMap, parse_map, parse_path
from .protocol.p2p import run_session
from .render import calibration_points, render_png, room_outlines

_LOGGER = logging.getLogger(__name__)


class LaserMapCoordinator:
    def __init__(self, hass: HomeAssistant, config: dict, options: dict) -> None:
        self.hass = hass
        self.config = config
        self.theme = options.get(CONF_THEME, DEFAULT_THEME)
        self.vmap: VacuumMap | None = None
        self.path: list[tuple[int, int]] = []
        self.png: bytes | None = None
        self.calibration: list[dict] = []
        self.rooms: dict[int, dict] = {}
        self.selected: set[int] = set()
        self.connected = False
        self._listeners: list[Callable[[], None]] = []
        self._task: asyncio.Task | None = None
        self._render_pending = False

    # --- lifecycle -------------------------------------------------------------------
    def start(self) -> None:
        self._task = self.hass.async_create_background_task(self._loop(), "tuya_laser_map_p2p")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _loop(self) -> None:
        c = self.config
        ices = [{"urls": u} for u in c[CONF_STUN]]
        while True:
            try:
                self._set_connected(True)
                await run_session(c[CONF_HOST], c[CONF_DEVICE_ID], c[CONF_LOCAL_KEY], c[CONF_DEVICE_PASSWORD],
                                  c[CONF_USER_ID], ices, self._on_file, SESSION_IDLE_TIMEOUT)
                await asyncio.sleep(RECONNECT_DELAY)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - keep retrying, robot may be offline
                _LOGGER.debug("P2P session failed: %s", err)
                self._set_connected(False)
                await asyncio.sleep(ERROR_BACKOFF)

    # --- data ------------------------------------------------------------------------
    @callback
    def _on_file(self, name: str, blob: bytes) -> None:
        try:
            if name.startswith("map.bin"):
                self.vmap = parse_map(blob)
            elif name.startswith("cleanPath.bin"):
                self.path = parse_path(blob)
            else:
                return
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not decode %s: %s", name, err)
            return
        self.request_render()

    @callback
    def request_render(self) -> None:
        if self._render_pending or self.vmap is None:
            return
        self._render_pending = True
        self.hass.async_create_task(self._render())

    async def _render(self) -> None:
        await asyncio.sleep(0.5)  # coalesce map + path arriving together
        self._render_pending = False
        vmap, path, sel, theme = self.vmap, list(self.path), set(self.selected), self.theme
        png, cal, rooms = await self.hass.async_add_executor_job(
            lambda: (render_png(vmap, path, sel, theme), calibration_points(vmap), room_outlines(vmap)))
        self.png, self.calibration, self.rooms = png, cal, rooms
        self._notify()

    # --- listeners ---------------------------------------------------------------------
    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    def _notify(self) -> None:
        for cb in list(self._listeners):
            cb()

    def _set_connected(self, value: bool) -> None:
        if self.connected != value:
            self.connected = value
            self._notify()

    # --- commands ----------------------------------------------------------------------
    def resolve_rooms(self, rooms: list) -> list[int]:
        ids = []
        by_name = {r["name"].lower(): rid for rid, r in self.rooms.items()}
        for r in rooms:
            if isinstance(r, int) or (isinstance(r, str) and r.isdigit()):
                ids.append(int(r))
            elif isinstance(r, str) and r.lower() in by_name:
                ids.append(by_name[r.lower()])
            else:
                raise ValueError(f"Unknown room: {r}")
        return ids

    async def send(self, frame: bytes) -> tuple[int, bytes] | None:
        c = self.config
        reply = await send_dps(c[CONF_HOST], c[CONF_DEVICE_ID], c[CONF_LOCAL_KEY], {commands.DP_COMMAND: b64(frame)})
        raw = reply.get(commands.DP_COMMAND)
        return commands.parse(base64.b64decode(raw)) if raw else None

    async def clean_rooms(self, rooms: list, times: int = 1) -> None:
        ids = self.resolve_rooms(rooms)
        self.selected = set(ids)
        self.request_render()
        await self.send(commands.clean_rooms(ids, times))

    async def clean_zones(self, zones: list[list[float]], times: int = 1) -> None:
        polys = []
        for z in zones:
            x1, y1, x2, y2 = (round(v) for v in z[:4])
            polys.append([(x1, y1), (x2, y1), (x2, y2), (x1, y2)])
        await self.send(commands.clean_zones(polys, times))

    async def goto(self, x: float, y: float) -> None:
        await self.send(commands.goto_point(round(x), round(y)))
