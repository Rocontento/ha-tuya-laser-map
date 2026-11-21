"""Tuya Laser Vacuum Map: live local map (P2P) and room/zone cleaning for Tuya laser robots."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

import pathlib

from .const import DOMAIN
from .coordinator import LaserMapCoordinator

PLATFORMS = [Platform.CAMERA]

_ENTRY = vol.Optional("config_entry_id")
CLEAN_ROOMS_SCHEMA = vol.Schema({_ENTRY: cv.string, vol.Required("rooms"): vol.All(cv.ensure_list, [vol.Any(int, cv.string)]),
                                 vol.Optional("times", default=1): vol.All(int, vol.Range(1, 3))})
CLEAN_ZONE_SCHEMA = vol.Schema({_ENTRY: cv.string, vol.Required("zones"): vol.All(cv.ensure_list, [vol.All(cv.ensure_list, [vol.Coerce(float)])]),
                                vol.Optional("times", default=1): vol.All(int, vol.Range(1, 3))})
_POINTS = vol.All(cv.ensure_list, [vol.All(cv.ensure_list, [vol.Coerce(float)], vol.Length(min=2))])
NOGO_MODES = {"no_sweep": 1, "no_mop": 2}
ADD_WALL_SCHEMA = vol.Schema({_ENTRY: cv.string, vol.Required("points"): vol.All(_POINTS, vol.Length(min=2))})
ADD_NOGO_SCHEMA = vol.Schema({_ENTRY: cv.string, vol.Required("zones"): _POINTS,
                              vol.Optional("mode", default="no_sweep"): vol.In(NOGO_MODES)})
REMOVE_LIMIT_SCHEMA = vol.Schema({_ENTRY: cv.string, vol.Required("x"): vol.Coerce(float), vol.Required("y"): vol.Coerce(float)})
CLEAR_LIMITS_SCHEMA = vol.Schema({_ENTRY: cv.string, vol.Required("what"): vol.In(["walls", "no_go_zones", "all"])})
SET_LIMITS_SCHEMA = vol.Schema({_ENTRY: cv.string,
                                vol.Optional("walls"): [vol.All(cv.ensure_list, [vol.Coerce(float)], vol.Length(min=4, max=4))],
                                vol.Optional("no_go_zones"): [vol.Schema({vol.Required("mode"): vol.All(int, vol.Range(0, 3)),
                                                                          vol.Required("points"): vol.All(_POINTS, vol.Length(min=3))})]})
CARD_URL = "/tuya_laser_map/tuya-laser-map-card.js"
GOTO_SCHEMA = vol.Schema({_ENTRY: cv.string, vol.Required("x"): vol.Coerce(float), vol.Required("y"): vol.Coerce(float)})


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Serve the bundled map card and load it on every dashboard (no manual resource needed)."""
    card = pathlib.Path(__file__).parent / "frontend" / "tuya-laser-map-card.js"
    await hass.http.async_register_static_paths([StaticPathConfig(CARD_URL, str(card), False)])
    if "frontend" in hass.config.components:  # absent only in bare test setups
        add_extra_js_url(hass, f"{CARD_URL}?v={card.stat().st_mtime_ns}")
    return True


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

    async def add_virtual_wall(call: ServiceCall) -> None:
        await _coordinator(hass, call).add_walls(call.data["points"])

    async def add_no_go_zone(call: ServiceCall) -> None:
        await _coordinator(hass, call).add_nogo(call.data["zones"], NOGO_MODES[call.data["mode"]])

    async def remove_limit(call: ServiceCall) -> None:
        if not await _coordinator(hass, call).remove_limit_at(call.data["x"], call.data["y"]):
            raise ServiceValidationError("No wall or no-go zone at that point")

    async def clear_limits(call: ServiceCall) -> None:
        what = call.data["what"]
        await _coordinator(hass, call).set_limits(walls=[] if what in ("walls", "all") else None,
                                                  nogo=[] if what in ("no_go_zones", "all") else None)

    async def set_limits(call: ServiceCall) -> None:
        """Replace walls and/or no-go zones as a whole (what the card's editor saves)."""
        nogo = call.data.get("no_go_zones")
        await _coordinator(hass, call).set_limits(
            walls=call.data.get("walls"),
            nogo=None if nogo is None else [{"mode": z["mode"], "points": [(round(x), round(y)) for x, y, *_ in z["points"]]}
                                            for z in nogo])

    async def clean_zone(call: ServiceCall) -> None:
        await _coordinator(hass, call).clean_zones(call.data["zones"], call.data["times"])

    async def goto(call: ServiceCall) -> None:
        await _coordinator(hass, call).goto(call.data["x"], call.data["y"])

    hass.services.async_register(DOMAIN, "clean_rooms", clean_rooms, CLEAN_ROOMS_SCHEMA)
    hass.services.async_register(DOMAIN, "clean_zone", clean_zone, CLEAN_ZONE_SCHEMA)
    hass.services.async_register(DOMAIN, "goto", goto, GOTO_SCHEMA)
    hass.services.async_register(DOMAIN, "add_virtual_wall", add_virtual_wall, ADD_WALL_SCHEMA)
    hass.services.async_register(DOMAIN, "add_no_go_zone", add_no_go_zone, ADD_NOGO_SCHEMA)
    hass.services.async_register(DOMAIN, "remove_limit", remove_limit, REMOVE_LIMIT_SCHEMA)
    hass.services.async_register(DOMAIN, "set_limits", set_limits, SET_LIMITS_SCHEMA)
    hass.services.async_register(DOMAIN, "clear_limits", clear_limits, CLEAR_LIMITS_SCHEMA)
