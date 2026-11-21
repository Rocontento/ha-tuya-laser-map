"""Keeps a P2P map session to the robot open, decodes what it streams and renders the map."""

from __future__ import annotations

import asyncio
import time
import base64
import logging
from collections.abc import Callable

from homeassistant.util import dt as dt_util
from homeassistant.core import HomeAssistant, callback

from .const import (
    CONF_DEVICE_ID, CONF_DEVICE_PASSWORD, CONF_HOST, CONF_LOCAL_KEY, CONF_STUN, CONF_THEME, CONF_USER_ID,
    DEFAULT_THEME, ERROR_BACKOFF, RECONNECT_DELAY, SESSION_IDLE_TIMEOUT,
)
from .protocol import commands
from .protocol.lan import b64, send_dps
from .protocol.mapdata import FLOOR_NO_ROOM, VacuumMap, parse_map, parse_path
from .protocol.p2p import run_session
from .render import calibration_points, render_png, room_outlines

_LOGGER = logging.getLogger(__name__)


class LaserMapCoordinator:
    def __init__(self, hass: HomeAssistant, config: dict, options: dict) -> None:
        self.hass = hass
        self.config = config
        self.theme = options.get(CONF_THEME, DEFAULT_THEME)
        self.vmap: VacuumMap | None = None
        self.path: list[list[tuple[int, int]]] = []  # path segments of the current cleaning job
        self.png: bytes | None = None
        self.updated: dict[str, str] = {}  # file kind -> last time the robot sent a new one (diagnostics)
        self.calibration: list[dict] = []
        self.rooms: dict[int, dict] = {}
        self.selected: set[int] = set()
        self.targets: list[list[tuple[int, int]]] = []  # zones being cleaned
        self.plan: list[dict] | None = None  # LiDAR floor plan, robot coords (floorplan.py)
        self.walls: list[tuple[int, int, int, int]] = []
        self.nogo: list[dict] = []
        self._nogo_protocol = 1
        self.connected = False
        self._listeners: list[Callable[[], None]] = []
        self._task: asyncio.Task | None = None
        self._render_pending = False
        self._rendering = self._render_again = False
        self._outline_key: tuple | None = None
        self._outline_at = 0.0
        self._last_blob: dict[str, bytes] = {}

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
        try:
            await self.refresh_limits()
        except Exception as err:  # noqa: BLE001 - limits are optional; the map still works
            _LOGGER.debug("Could not read walls/no-go zones: %s", err)
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
        kind = name.split(".")[0]
        if self._last_blob.get(kind) == blob:  # the robot re-sends an unchanged map periodically
            return
        self._last_blob[kind] = blob
        self.updated[kind] = dt_util.utcnow().isoformat(timespec="seconds")
        _LOGGER.debug("received %s (%d B)", name, len(blob))
        try:
            if name.startswith("map.bin"):
                self.vmap = parse_map(blob)
            elif name.startswith("cleanPath.bin"):
                self._add_path(parse_path(blob))
            else:
                return
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not decode %s: %s", name, err)
            return
        self.request_render()

    def _room_at(self, pt: tuple[int, int]) -> int | None:
        """Room under a robot point, or the nearest room cell within 30 cm if it is on a border."""
        vm = self.vmap
        cx, cy = (int(v) for v in vm.to_cell(*pt))
        for r in range(0, 7):
            for y in range(cy - r, cy + r + 1):
                for x in range(cx - r, cx + r + 1):
                    if 0 <= x < vm.width and 0 <= y < vm.height:
                        c = vm.cell(x, y)
                        if c & 7 == 7 and c != FLOOR_NO_ROOM and (c >> 3) in vm.rooms:
                            return c >> 3
        return None

    @property
    def current_room(self) -> int | None:
        """Room under the robot (last path point)."""
        if self.vmap is None or not self.path or not self.path[-1]:
            return None
        return self._room_at(self.path[-1][-1])

    @property
    def visited_rooms(self) -> list[int]:
        """Rooms the current job's path went through, in order. ponytail: samples every 10th point."""
        if self.vmap is None:
            return []
        seen: dict[int, None] = {}
        for seg in self.path:
            for pt in seg[::10] + seg[-1:]:
                if (rid := self._room_at(pt)) is not None:
                    seen[rid] = None
        return list(seen)

    def _add_path(self, pts: list[tuple[int, int]]) -> None:
        """The robot restarts its path file on pause/resume: keep earlier segments of the same job.
        A path that grows the last segment replaces it; one starting at the dock is a new job.
        ponytail: dock radius 50 cm heuristic, no vacuum-state coupling."""
        if not pts:
            return
        last = self.path[-1] if self.path else []
        n = min(len(last), len(pts), 3)
        if last and pts[:n] == last[:n]:
            self.path[-1] = pts
            return
        if self.vmap:
            dx, dy = self.vmap.to_robot(*self.vmap.dock)
            near_dock = ((pts[0][0] - dx) ** 2 + (pts[0][1] - dy) ** 2) ** 0.5 < 1000 / self.vmap.resolution_cm * 0.5
        else:
            near_dock = True
        self.path = [pts] if near_dock or not last else self.path + [pts]

    @callback
    def request_render(self) -> None:
        if self._render_pending or self.vmap is None:
            return
        self._render_pending = True
        self.hass.async_create_task(self._render())

    async def _render(self) -> None:
        """One render at a time; data arriving meanwhile is drawn right after (no pile-up)."""
        await asyncio.sleep(0.3)  # coalesce map + path arriving together
        self._render_pending = False
        if self._rendering:
            self._render_again = True
            return
        self._rendering = True
        try:
            while True:
                self._render_again = False
                vmap, path, sel, theme = self.vmap, [list(p) for p in self.path], set(self.selected), self.theme
                targets, plan = list(self.targets), self.plan  # walls/no-go zones: drawn by the card
                now = time.monotonic()
                key = (vmap.map_id, id(plan))
                outlines = key != self._outline_key or now - self._outline_at > 60
                png, cal, rooms = await self.hass.async_add_executor_job(
                    lambda: (render_png(vmap, path, sel, theme, targets=targets, plan=plan),
                             calibration_points(vmap), room_outlines(vmap, plan) if outlines else None))
                if outlines:
                    self.rooms, self._outline_key, self._outline_at = rooms, key, now
                self.png, self.calibration = png, cal
                _LOGGER.debug("rendered in %.2fs%s", time.monotonic() - now, " (+outlines)" if outlines else "")
                self._notify()
                if not self._render_again:
                    break
        finally:
            self._rendering = False

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
        reply = await send_dps(c[CONF_HOST], c[CONF_DEVICE_ID], c[CONF_LOCAL_KEY], {commands.DP_COMMAND: b64(frame)},
                               want=commands.DP_COMMAND)
        raw = reply.get(commands.DP_COMMAND)
        parsed = commands.parse(base64.b64decode(raw)) if raw else None
        _LOGGER.debug("sent %s -> reply %s", frame.hex(), parsed and (hex(parsed[0]), parsed[1].hex()))
        if parsed and self._apply_limits_report(*parsed):
            self.request_render()
        return parsed

    def _apply_limits_report(self, cmd: int, data: bytes) -> bool:
        if cmd == 0x13:
            self.walls = [tuple(w) for w in commands.decode_walls(data)]
        elif cmd == 0x39 and data:
            self._nogo_protocol = data[0]
            self.nogo = commands.decode_nogo_v2(data)
        else:
            return False
        self._notify()
        return True

    # --- walls and no-go zones (each write replaces the robot's whole list) ----------------
    async def refresh_limits(self) -> None:
        await self.send(commands.request(0x13))
        await self.send(commands.request(0x39))

    async def set_limits(self, walls: list | None = None, nogo: list | None = None) -> None:
        if walls is not None:
            await self.send(commands.set_walls([tuple(round(v) for v in w[:4]) for w in walls]))
        if nogo is not None:
            await self.send(commands.set_nogo(nogo, self._nogo_protocol))
        await self.refresh_limits()  # read back what the robot actually stored

    async def add_walls(self, points: list[list[float]]) -> None:
        """A drawn path [[x, y], ...] becomes one wall per segment."""
        segs = [(*points[i][:2], *points[i + 1][:2]) for i in range(len(points) - 1)]
        await self.set_limits(walls=self.walls + segs)

    async def add_nogo(self, rects: list[list[float]], mode: int = 1) -> None:
        new = [{"mode": mode, "points": [(round(x1), round(y1)), (round(x2), round(y1)), (round(x2), round(y2)),
                                         (round(x1), round(y2))]} for x1, y1, x2, y2 in (r[:4] for r in rects)]
        await self.set_limits(nogo=self.nogo + new)

    async def clean_rooms(self, rooms: list, times: int = 1) -> None:
        ids = self.resolve_rooms(rooms)
        self.selected, self.targets = set(ids), []
        self.request_render()
        await self.send(commands.clean_rooms(ids, times))

    async def clean_zones(self, zones: list[list[float]], times: int = 1) -> None:
        polys = []
        for z in zones:
            x1, y1, x2, y2 = (round(v) for v in z[:4])
            polys.append([(x1, y1), (x2, y1), (x2, y2), (x1, y2)])
        self.selected, self.targets = set(), polys
        self.request_render()
        reply = await self.send(commands.clean_zones_v2(polys, times))
        if not reply or reply[0] != 0x3B:  # older firmware: fall back to 0x28
            await self.send(commands.clean_zones(polys, times))

    async def goto(self, x: float, y: float) -> None:
        await self.send(commands.goto_point(round(x), round(y)))

    async def remove_limit_at(self, x: float, y: float, radius: float = 60) -> bool:
        """Remove the no-go zone containing (x, y), or else the wall passing within ``radius``
        (robot units: 1/10 cell, i.e. 60 = 30 cm at 5 cm cells)."""
        for i, z in enumerate(self.nogo):
            xs, ys = [p[0] for p in z["points"]], [p[1] for p in z["points"]]
            if min(xs) <= x <= max(xs) and min(ys) <= y <= max(ys):
                await self.set_limits(nogo=self.nogo[:i] + self.nogo[i + 1:])
                return True
        for i, (x1, y1, x2, y2) in enumerate(self.walls):
            dx, dy = x2 - x1, y2 - y1
            t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / ((dx * dx + dy * dy) or 1)))
            if ((x - x1 - t * dx) ** 2 + (y - y1 - t * dy) ** 2) ** 0.5 <= radius:
                await self.set_limits(walls=self.walls[:i] + self.walls[i + 1:])
                return True
        return False
