"""Vacuum with room segments, so Home Assistant areas (and Matter / Apple Home) can clean single rooms.

Start/pause/stop/return and the state mirror the vacuum entity that already controls the robot
(e.g. tuya-local); cleaning segments sends the room-clean frame (0x14) this integration owns.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.vacuum import Segment, StateVacuumEntity, VacuumActivity, VacuumEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.start import async_at_started

from .const import CONF_DEVICE_ID, CONF_SOURCE_VACUUM, DOMAIN
from .coordinator import LaserMapCoordinator

FEATURES = (VacuumEntityFeature.START | VacuumEntityFeature.PAUSE | VacuumEntityFeature.STOP
            | VacuumEntityFeature.RETURN_HOME | VacuumEntityFeature.STATE | VacuumEntityFeature.CLEAN_AREA)


def source_vacuum(hass: HomeAssistant, entry: ConfigEntry) -> str | None:
    """The configured vacuum to mirror, or the only other vacuum in HA."""
    if src := entry.options.get(CONF_SOURCE_VACUUM):
        return src
    reg = er.async_get(hass)
    others = [s.entity_id for s in hass.states.async_all("vacuum")
              if not ((e := reg.async_get(s.entity_id)) and e.platform == DOMAIN)]
    return others[0] if len(others) == 1 else None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    add([RoomVacuum(entry)])


class RoomVacuum(StateVacuumEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "rooms"
    _attr_supported_features = FEATURES

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._coord: LaserMapCoordinator = entry.runtime_data
        self._source: str | None = None
        device_id = entry.data[CONF_DEVICE_ID]
        self._attr_unique_id = f"{device_id}_vacuum"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, device_id)})

    async def async_added_to_hass(self) -> None:
        @callback
        def link(_hass: HomeAssistant | None = None) -> None:  # after startup: the other vacuum exists by then
            self._source = source_vacuum(self.hass, self._entry)
            if self._source:
                self.async_on_remove(async_track_state_change_event(self.hass, [self._source], self._source_changed))
            self.async_write_ha_state()

        self.async_on_remove(async_at_started(self.hass, link))

    @callback
    def _source_changed(self, event: Event) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        st = self._source and self.hass.states.get(self._source)
        return bool(st) and st.state != "unavailable"

    @property
    def activity(self) -> VacuumActivity | None:
        st = self._source and self.hass.states.get(self._source)
        try:
            return VacuumActivity(st.state) if st else None
        except ValueError:
            return None

    async def _forward(self, service: str) -> None:
        await self.hass.services.async_call("vacuum", service, {ATTR_ENTITY_ID: self._source}, blocking=True)

    async def async_start(self) -> None:
        await self._forward("start")

    async def async_pause(self) -> None:
        await self._forward("pause")

    async def async_stop(self, **kwargs: Any) -> None:
        await self._forward("stop")

    async def async_return_to_base(self, **kwargs: Any) -> None:
        await self._forward("return_to_base")

    async def async_get_segments(self) -> list[Segment]:
        vmap = self._coord.vmap
        return [Segment(id=str(r.id), name=r.name) for r in sorted(vmap.rooms.values(), key=lambda r: r.id)] if vmap else []

    async def async_clean_segments(self, segment_ids: list[str], **kwargs: Any) -> None:
        await self._coord.clean_rooms([int(i) for i in segment_ids])
