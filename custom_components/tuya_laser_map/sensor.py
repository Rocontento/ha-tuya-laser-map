"""Room the robot is in now: Matter Hub maps it onto the Service Area currentArea (Apple Home)."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_DEVICE_ID, DOMAIN
from .coordinator import LaserMapCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    add([CurrentRoomSensor(entry)])


class CurrentRoomSensor(SensorEntity):
    """State: the HA area mapped to the robot's room (what Matter Hub matches by name), else the
    robot's room name. Attribute segment_id: the robot room id."""

    _attr_has_entity_name = True
    _attr_translation_key = "current_room"
    _attr_icon = "mdi:map-marker-radius"

    def __init__(self, entry: ConfigEntry) -> None:
        self._coord: LaserMapCoordinator = entry.runtime_data
        device_id = entry.data[CONF_DEVICE_ID]
        self._vacuum_uid = f"{device_id}_vacuum"
        self._attr_unique_id = f"{device_id}_current_room"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, device_id)})

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._coord.add_listener(self._updated))

    def _updated(self) -> None:
        if self._attr_extra_state_attributes != {"segment_id": self._coord.current_room}:
            self.async_write_ha_state()

    def _area_name(self, room: int) -> str | None:
        reg = er.async_get(self.hass)
        vac = reg.async_get_entity_id("vacuum", DOMAIN, self._vacuum_uid)
        entry = vac and reg.async_get(vac)
        mapping = (entry.options.get("vacuum", {}) if entry else {}).get("area_mapping") or {}
        area_id = next((a for a, segs in mapping.items() if str(room) in segs), None)
        area = area_id and ar.async_get(self.hass).async_get_area(area_id)
        return area.name if area else None

    @property
    def native_value(self) -> str | None:
        room = self._coord.current_room
        if room is None:
            return None
        vm = self._coord.vmap
        return self._area_name(room) or (vm.rooms[room].name if vm and room in vm.rooms else str(room))

    @property
    def extra_state_attributes(self) -> dict:
        self._attr_extra_state_attributes = {"segment_id": self._coord.current_room}
        return self._attr_extra_state_attributes
