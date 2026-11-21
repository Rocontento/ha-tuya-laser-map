"""Charging: lets Matter Hub report the battery charge state (Apple Home) for the room vacuum."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.start import async_at_started

from .const import CONF_DEVICE_ID, DOMAIN
from .vacuum import source_vacuum

CHARGING = {"charging", "charging_to_continue"}  # tuya laser status values while on the dock charging


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    add([ChargingSensor(entry)])


class ChargingSensor(BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "charging"
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._source: str | None = None
        device_id = entry.data[CONF_DEVICE_ID]
        self._attr_unique_id = f"{device_id}_charging"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, device_id)})

    async def async_added_to_hass(self) -> None:
        @callback
        def link(_hass: HomeAssistant | None = None) -> None:
            self._source = source_vacuum(self.hass, self._entry)
            if self._source:
                self.async_on_remove(async_track_state_change_event(self.hass, [self._source], self._changed))
            self.async_write_ha_state()

        self.async_on_remove(async_at_started(self.hass, link))

    @callback
    def _changed(self, event: Event) -> None:
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool | None:
        st = self._source and self.hass.states.get(self._source)
        return None if not st else st.attributes.get("status") in CHARGING
