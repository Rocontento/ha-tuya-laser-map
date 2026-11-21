"""Config flow: sign in with the Tuya Smart app account once (keys fetched, password not stored),
or enter the device keys manually."""

from __future__ import annotations

from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .const import (
    CONF_DEVICE_ID, CONF_DEVICE_PASSWORD, CONF_HOST, CONF_LOCAL_KEY, CONF_STUN, CONF_THEME, CONF_USER_ID,
    DEFAULT_THEME, DOMAIN,
)

APPS = {"tuya_smart": "Tuya Smart", "smart_life": "Smart Life"}
PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


def _find(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if value.get(key):
            return value[key]
        value = list(value.values())
    if isinstance(value, list):
        for v in value:
            if (found := _find(v, key)) is not None:
                return found
    return None


async def _fetch_from_account(session: aiohttp.ClientSession, app: str, email: str, password: str,
                              country: str, device_id: str) -> dict[str, Any]:
    from tuya_mobile import TuyaMobileApp, TuyaPasswordClient  # noqa: PLC0415 - heavy import, flow only

    client = TuyaPasswordClient.for_application(TuyaMobileApp(app), session, username=email)
    await client.login_with_password(password, country_code=country)
    dev = await client._mobile_call("thing.m.device.get", "4.1", {"devId": device_id})  # noqa: SLF001
    local_key = _find(dev, "localKey")
    raw = await client._mobile_call("m.ipc.v4.rtc.config.get", "1.0", {"devId": device_id})  # noqa: SLF001
    while isinstance(raw, dict) and isinstance(raw.get("result"), dict) and "p2pConfig" not in raw:
        raw = raw["result"]
    stun = [i["urls"] for i in raw.get("p2pConfig", {}).get("ices", []) if str(i.get("urls", "")).startswith("stun")]
    if not raw.get("password") or not stun:
        raise ValueError("no_p2p")
    if not local_key:
        raise ValueError("no_p2p")
    return {CONF_LOCAL_KEY: local_key, CONF_DEVICE_PASSWORD: raw["password"], CONF_USER_ID: client.uid, CONF_STUN: stun}


class LaserMapConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=["account", "manual"])

    async def async_step_account(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_DEVICE_ID])
            self._abort_if_unique_id_configured()
            try:
                keys = await _fetch_from_account(async_get_clientsession(self.hass), user_input["app"], user_input["email"],
                                                 user_input["password"], user_input["country_code"], user_input[CONF_DEVICE_ID])
            except ValueError:
                errors["base"] = "no_p2p"
            except Exception:  # noqa: BLE001 - tuya_mobile raises typed errors; map them all to auth
                errors["base"] = "auth"
            else:
                data = {CONF_DEVICE_ID: user_input[CONF_DEVICE_ID], CONF_HOST: user_input[CONF_HOST], **keys}
                return self.async_create_entry(title=user_input["name"], data=data)
        schema = vol.Schema({
            vol.Required("name", default="Robot vacuum"): str,
            vol.Required(CONF_HOST): str,
            vol.Required(CONF_DEVICE_ID): str,
            vol.Required("app", default="tuya_smart"): vol.In(APPS),
            vol.Required("email"): str,
            vol.Required("password"): PASSWORD,
            vol.Required("country_code", default="34"): str,
        })
        return self.async_show_form(step_id="account", data_schema=schema, errors=errors)

    async def async_step_manual(self, user_input: dict | None = None) -> ConfigFlowResult:
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_DEVICE_ID])
            self._abort_if_unique_id_configured()
            data = {**user_input, CONF_STUN: [s.strip() for s in user_input[CONF_STUN].split(",") if s.strip()]}
            name = data.pop("name")
            return self.async_create_entry(title=name, data=data)
        schema = vol.Schema({
            vol.Required("name", default="Robot vacuum"): str,
            vol.Required(CONF_HOST): str,
            vol.Required(CONF_DEVICE_ID): str,
            vol.Required(CONF_LOCAL_KEY): PASSWORD,
            vol.Required(CONF_DEVICE_PASSWORD): PASSWORD,
            vol.Required(CONF_USER_ID): str,
            vol.Required(CONF_STUN): str,
        })
        return self.async_show_form(step_id="manual", data_schema=schema)

    @staticmethod
    def async_get_options_flow(entry) -> OptionsFlow:
        return LaserMapOptionsFlow()


class LaserMapOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        theme = self.config_entry.options.get(CONF_THEME, DEFAULT_THEME)
        return self.async_show_form(step_id="init", data_schema=vol.Schema(
            {vol.Required(CONF_THEME, default=theme): vol.In({"light": "Light", "dark": "Dark"})}))
