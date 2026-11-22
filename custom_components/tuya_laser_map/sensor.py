"""Room the robot is in now: Matter Hub maps it onto the Service Area currentArea (Apple Home)."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import area_registry as ar, entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.start import async_at_started

from .const import CONF_DEVICE_ID, DOMAIN
from .coordinator import LaserMapCoordinator
from .vacuum import source_vacuum

# Raw values from the tuya-local profile (status attribute of its vacuum; description of its problem
# sensor). tuya-local needs them in English for its own logic, so they are translated here instead.
STATUS = ["standby", "smart", "zone", "partial", "cleaning", "paused", "seeking_spot", "located_spot", "left_spot",
          "returning", "charging", "charged", "sleep", "selecting_room", "game", "mapping", "collecting_dust", "manual",
          "cleaning_to_continue", "charging_to_continue"]
ERRORS = ["ok", "low_power", "poweroff", "wheel_trap", "cannot_upgrade", "collision_stuck", "dust_station_full", "tile_error",
          "lidar_speed_err", "lidar_cover", "lidar_point_err", "front_wall_dirty", "psd_dirty", "middle_sweep", "side_sweep",
          "fan_speed", "dustbox_out", "dustbox_full", "no_dust_box", "dustbox_fullout", "trapped", "pick_up",
          "no_dust_water_box", "water_box_empty", "forbid_area", "land_check", "findcharge_fail", "battery_err",
          "kit_wheel", "kit_lidar", "kit_water_pump"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    add([CurrentRoomSensor(entry), StatusSensor(entry), ErrorSensor(entry)])


class _MirrorEnum(SensorEntity):
    """Translated enum read from one of the robot's tuya-local entities."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._watch: str | None = None
        device_id = entry.data[CONF_DEVICE_ID]
        self._attr_unique_id = f"{device_id}_{self._attr_translation_key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, device_id)})

    def _find(self, src: str) -> str | None:
        return src

    def _raw(self, st) -> str | None:
        raise NotImplementedError

    async def async_added_to_hass(self) -> None:
        @callback
        def link(_hass: HomeAssistant | None = None) -> None:
            src = source_vacuum(self.hass, self._entry)
            self._watch = src and self._find(src)
            if self._watch:
                self.async_on_remove(async_track_state_change_event(self.hass, [self._watch], self._changed))
            self.async_write_ha_state()

        self.async_on_remove(async_at_started(self.hass, link))

    @callback
    def _changed(self, event: Event) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> str | None:
        st = self._watch and self.hass.states.get(self._watch)
        raw = st and self._raw(st)
        raw = raw and str(raw).replace(" ", "_")
        return raw if raw in self._attr_options else None


class StatusSensor(_MirrorEnum):
    _attr_translation_key = "status"
    _attr_icon = "mdi:robot-vacuum"
    _attr_options = STATUS

    def _raw(self, st):
        return st.attributes.get("status")


class ErrorSensor(_MirrorEnum):
    _attr_translation_key = "error"
    _attr_icon = "mdi:alert-circle-outline"
    _attr_options = ERRORS

    def _find(self, src: str) -> str | None:
        """The problem binary_sensor on the source vacuum's device."""
        reg = er.async_get(self.hass)
        dev = (e := reg.async_get(src)) and e.device_id
        return next((x.entity_id for x in er.async_entries_for_device(reg, dev)
                     if x.domain == "binary_sensor" and x.entity_id.endswith("_problem")), None) if dev else None

    def _raw(self, st):
        return st.attributes.get("description")


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
