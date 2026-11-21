"""Tuya Laser Vacuum Map: live local map (P2P) and room/zone cleaning for Tuya laser robots."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .coordinator import LaserMapCoordinator

PLATFORMS = [Platform.CAMERA]

_ENTRY = vol.Optional("config_entry_id")
CLEAN_ROOMS_SCHEMA = vol.Schema({_ENTRY: cv.string, vol.Required("rooms"): vol.All(cv.ensure_list, [vol.Any(int, cv.string)]),
                                 vol.Optional("times", default=1): vol.All(int, vol.Range(1, 3))})
CLEAN_ZONE_SCHEMA = vol.Schema({_ENTRY: cv.string, vol.Required("zones"): vol.All(cv.ensure_list, [vol.All(cv.ensure_list, [vol.Coerce(float)])]),
                                vol.Optional("times", default=1): vol.All(int, vol.Range(1, 3))})
GOTO_SCHEMA = vol.Schema({_ENTRY: cv.string, vol.Required("x"): vol.Coerce(float), vol.Required("y"): vol.Coerce(float)})


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = LaserMapCoordinator(hass, dict(entry.data), dict(entry.options))
    entry.runtime_data = coordinator
    coordinator.start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(lambda h, e: h.config_entries.async_reload(e.entry_id)))
    _register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    await entry.runtime_data.stop()
    return ok


def _coordinator(hass: HomeAssistant, call: ServiceCall) -> LaserMapCoordinator:
    entries = [e for e in hass.config_entries.async_loaded_entries(DOMAIN)]
    if entry_id := call.data.get("config_entry_id"):
        entries = [e for e in entries if e.entry_id == entry_id]
    if len(entries) != 1:
        raise ServiceValidationError("Specify config_entry_id: zero or several robots match")
    return entries[0].runtime_data


def _register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, "clean_rooms"):
        return

    async def clean_rooms(call: ServiceCall) -> None:
        try:
            await _coordinator(hass, call).clean_rooms(call.data["rooms"], call.data["times"])
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        except OSError as err:
            raise HomeAssistantError(f"Robot not reachable: {err}") from err

    async def clean_zone(call: ServiceCall) -> None:
        await _coordinator(hass, call).clean_zones(call.data["zones"], call.data["times"])

    async def goto(call: ServiceCall) -> None:
        await _coordinator(hass, call).goto(call.data["x"], call.data["y"])

    hass.services.async_register(DOMAIN, "clean_rooms", clean_rooms, CLEAN_ROOMS_SCHEMA)
    hass.services.async_register(DOMAIN, "clean_zone", clean_zone, CLEAN_ZONE_SCHEMA)
    hass.services.async_register(DOMAIN, "goto", goto, GOTO_SCHEMA)
