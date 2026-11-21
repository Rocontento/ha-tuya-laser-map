"""Config flow: sign in with the Tuya Smart app account once (keys fetched, password not stored),
or enter the device keys manually."""

from __future__ import annotations

from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.helpers.selector import (
    BooleanSelector, FileSelector, FileSelectorConfig, SelectOptionDict, SelectSelector, SelectSelectorConfig, TextSelector, TextSelectorConfig, TextSelectorType,
)

from .const import (
    CONF_DEVICE_ID, CONF_DEVICE_PASSWORD, CONF_FLOOR_PLAN, CONF_HOST, CONF_LOCAL_KEY, CONF_REMOVE_PLAN, CONF_STUN,
    CONF_THEME, CONF_USER_ID, DEFAULT_THEME, DOMAIN,
)
from .discovery import find_ip

APPS = {"tuya_smart": "Tuya Smart", "smart_life": "Smart Life"}
PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
# ponytail: small table; any other country is typed by hand in the form
CALLING_CODES = {"ES": "34", "US": "1", "CA": "1", "GB": "44", "FR": "33", "DE": "49", "IT": "39", "PT": "351",
                 "NL": "31", "BE": "32", "CH": "41", "AT": "43", "IE": "353", "MX": "52", "AR": "54", "CO": "57",
                 "CL": "56", "PE": "51", "BR": "55", "AU": "61", "PL": "48", "SE": "46"}


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


async def _login(session: aiohttp.ClientSession, app: str, email: str, password: str, country: str):
    from tuya_mobile import TuyaMobileApp, TuyaPasswordClient  # noqa: PLC0415 - heavy import, flow only

    client = TuyaPasswordClient.for_application(TuyaMobileApp(app), session, username=email)
    await client.login_with_password(password, country_code=country)
    return client


async def _fetch_keys(client, device_id: str) -> dict[str, Any]:
    dev = await client._mobile_call("thing.m.device.get", "4.1", {"devId": device_id})  # noqa: SLF001
    local_key = _find(dev, "localKey")
    raw = await client._mobile_call("m.ipc.v4.rtc.config.get", "1.0", {"devId": device_id})  # noqa: SLF001
    while isinstance(raw, dict) and isinstance(raw.get("result"), dict) and "p2pConfig" not in raw:
        raw = raw["result"]
    stun = [i["urls"] for i in raw.get("p2pConfig", {}).get("ices", []) if str(i.get("urls", "")).startswith("stun")]
    if not local_key or not raw.get("password") or not stun:
        raise ValueError("no_p2p")
    return {CONF_LOCAL_KEY: local_key, CONF_DEVICE_PASSWORD: raw["password"], CONF_USER_ID: client.uid, CONF_STUN: stun}


class LaserMapConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=["account", "manual"])

    _client: Any = None
    _devices: dict[str, str] | None = None

    async def async_step_account(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                self._client = await _login(async_get_clientsession(self.hass), user_input["app"], user_input["email"],
                                            user_input["password"], user_input["country_code"])
            except Exception:  # noqa: BLE001 - tuya_mobile raises typed errors; map them all to auth
                errors["base"] = "auth"
            else:
                try:
                    found = await self._client.get_local_keys([])
                    self._devices = {str(d["device_id"]): d.get("name") or str(d["device_id"]) for d in found}
                except Exception:  # noqa: BLE001 - list is a convenience; fall back to typing the id
                    self._devices = {}
                return await self.async_step_device()
        country = CALLING_CODES.get(self.hass.config.country or "", "34")
        schema = vol.Schema({
            vol.Required("app", default="tuya_smart"): vol.In(APPS),
            vol.Required("email"): str,
            vol.Required("password"): PASSWORD,
            vol.Required("country_code", default=country): str,
        })
        return self.async_show_form(step_id="account", data_schema=schema, errors=errors)

    async def async_step_device(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None and CONF_HOST in user_input:
            device_id = user_input[CONF_DEVICE_ID].strip()
            await self.async_set_unique_id(device_id)
            self._abort_if_unique_id_configured()
            try:
                keys = await _fetch_keys(self._client, device_id)
            except Exception:  # noqa: BLE001 - wrong device or no P2P support
                errors["base"] = "no_p2p"
            else:
                return self.async_create_entry(title=user_input["name"],
                                               data={CONF_DEVICE_ID: device_id, CONF_HOST: user_input[CONF_HOST], **keys})
        if user_input is None and len(self._devices or {}) > 1:
            return self.async_show_form(step_id="device", data_schema=vol.Schema({
                vol.Required(CONF_DEVICE_ID): SelectSelector(SelectSelectorConfig(
                    options=[SelectOptionDict(value=k, label=v) for k, v in self._devices.items()])),
            }))
        device_id = (user_input or {}).get(CONF_DEVICE_ID) or (next(iter(self._devices)) if self._devices else "")
        host = (user_input or {}).get(CONF_HOST) or (await find_ip(self.hass, device_id) if device_id else None) or ""
        return self.async_show_form(step_id="device", errors=errors, data_schema=vol.Schema({
            vol.Required(CONF_DEVICE_ID, default=device_id): str,
            vol.Required("name", default=(self._devices or {}).get(device_id, "Robot vacuum")): str,
            vol.Required(CONF_HOST, default=host): str,
        }))

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
        errors: dict[str, str] = {}
        if user_input is not None:
            from . import plan_store  # noqa: PLC0415 (avoid import cycle at module load)
            from .floorplan import align, parse_dxf  # noqa: PLC0415

            store = plan_store(self.hass, self.config_entry.entry_id)
            if user_input.get(CONF_REMOVE_PLAN):
                await store.async_remove()
            elif file_id := user_input.get(CONF_FLOOR_PLAN):
                vmap = getattr(self.config_entry.runtime_data, "vmap", None)
                if vmap is None:
                    errors["base"] = "map_not_ready"
                else:
                    def _load() -> list[dict]:
                        with process_uploaded_file(self.hass, file_id) as path:
                            return align(vmap, parse_dxf(path.read_text(encoding="utf-8", errors="replace")))
                    try:
                        await store.async_save({"entities": await self.hass.async_add_executor_job(_load)})
                    except ValueError:
                        errors["base"] = "bad_plan"
            if not errors:
                return self.async_create_entry(data={CONF_THEME: user_input[CONF_THEME]})
        theme = self.config_entry.options.get(CONF_THEME, DEFAULT_THEME)
        return self.async_show_form(step_id="init", errors=errors, data_schema=vol.Schema({
            vol.Required(CONF_THEME, default=theme): vol.In({"light": "Light", "dark": "Dark"}),
            vol.Optional(CONF_FLOOR_PLAN): FileSelector(FileSelectorConfig(accept=".dxf")),
            vol.Optional(CONF_REMOVE_PLAN, default=False): BooleanSelector(),
        }))
