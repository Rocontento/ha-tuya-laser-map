"""Map camera: the rendered plan, plus calibration points and rooms for xiaomi-vacuum-map-card."""

from __future__ import annotations

from aiohttp import web
from homeassistant.components.camera import Camera
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_DEVICE_ID, DOMAIN
from .coordinator import LaserMapCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    cams = hass.data.setdefault(DOMAIN, {}).setdefault("cameras", {})
    if not hass.data[DOMAIN].get("view"):
        hass.http.register_view(ThemedMapView(cams))
        hass.data[DOMAIN]["view"] = True
    add([MapCamera(entry, cams)])


class ThemedMapView(HomeAssistantView):
    """The map in light or dark, so the card can follow the dashboard theme. Same token as camera_proxy."""

    url = "/api/tuya_laser_map/{entity_id}/{theme}.png"
    name = "api:tuya_laser_map:map"
    requires_auth = False  # the camera's rotating access token is checked instead, like camera_proxy

    def __init__(self, cams: dict) -> None:
        self._cams = cams

    async def get(self, request: web.Request, entity_id: str, theme: str) -> web.Response:
        cam = self._cams.get(entity_id)
        if cam is None or request.query.get("token") not in cam.access_tokens:
            return web.Response(status=401)
        png = cam._coord.pngs.get(theme)
        if png is None:
            return web.Response(status=404)
        return web.Response(body=png, content_type="image/png", headers={"Cache-Control": "no-store"})


class MapCamera(Camera):
    _attr_has_entity_name = True
    _attr_translation_key = "map"
    _attr_content_type = "image/png"
    _attr_frame_interval = 2

    def __init__(self, entry: ConfigEntry, cams: dict) -> None:
        super().__init__()
        self._cams = cams
        self._coord: LaserMapCoordinator = entry.runtime_data
        device_id = entry.data[CONF_DEVICE_ID]
        self._attr_unique_id = f"{device_id}_map"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, device_id)}, name=entry.title,
                                            manufacturer="Tuya", model="Laser robot vacuum (map)")

    async def async_added_to_hass(self) -> None:
        self._cams[self.entity_id] = self
        self.async_on_remove(lambda: self._cams.pop(self.entity_id, None))
        self.async_on_remove(self._coord.add_listener(self._updated))

    def _updated(self) -> None:
        self.async_update_token()
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._coord.png is not None or self._coord.connected

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        return self._coord.png

    def _themed_url(self, theme: str) -> str | None:
        if not self.access_tokens:
            return None
        return f"/api/tuya_laser_map/{self.entity_id}/{theme}.png?token={self.access_tokens[-1]}"

    @property
    def extra_state_attributes(self) -> dict:
        c = self._coord
        return {
            "calibration_points": c.calibration,
            "rooms": {str(rid): r for rid, r in c.rooms.items()},
            "selected_rooms": sorted(c.selected),
            "p2p_connected": c.connected,
            "map_id": c.vmap.map_id if c.vmap else None,
            "resolution_cm": c.vmap.resolution_cm if c.vmap else None,
            "path_points": [len(p) for p in c.path],
            "picture_light": self._themed_url("light"),
            "picture_dark": self._themed_url("dark"),
            "current_room": c.current_room,
            "visited_rooms": c.visited_rooms,
            "updated": c.updated,
            "walls": [list(w) for w in c.walls],
            "no_go_zones": [{"mode": z["mode"],  # 0 all, 1 no sweep, 2 no mop
                             "points": [list(p) for p in z["points"]]} for z in c.nogo],
        }
