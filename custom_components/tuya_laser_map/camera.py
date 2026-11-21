"""Map camera: the rendered plan, plus calibration points and rooms for xiaomi-vacuum-map-card."""

from __future__ import annotations

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_DEVICE_ID, DOMAIN
from .coordinator import LaserMapCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    add([MapCamera(entry)])


class MapCamera(Camera):
    _attr_has_entity_name = True
    _attr_translation_key = "map"
    _attr_content_type = "image/png"
    _attr_frame_interval = 2

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__()
        self._coord: LaserMapCoordinator = entry.runtime_data
        device_id = entry.data[CONF_DEVICE_ID]
        self._attr_unique_id = f"{device_id}_map"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, device_id)}, name=entry.title,
                                            manufacturer="Tuya", model="Laser robot vacuum (map)")

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._coord.add_listener(self._updated))

    def _updated(self) -> None:
        self.async_update_token()
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._coord.png is not None or self._coord.connected

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        return self._coord.png

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
            "updated": c.updated,
            "walls": [list(w) for w in c.walls],
            "no_go_zones": [{"mode": z["mode"],  # 0 all, 1 no sweep, 2 no mop
                             "points": [list(p) for p in z["points"]]} for z in c.nogo],
        }
