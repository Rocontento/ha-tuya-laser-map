"""Account flow: login -> pick robot -> IP prefilled -> entry with fetched keys."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from custom_components.tuya_laser_map.const import DOMAIN
from custom_components.tuya_laser_map.discovery import _UDP_KEY, decode_broadcast
from custom_components.tuya_laser_map.protocol.lan import _ecb


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def test_decode_broadcast():
    body = _ecb(_UDP_KEY, json.dumps({"gwId": "dev1", "ip": "192.0.2.5"}).encode(), True)
    frame = b"\x00\x00\x55\xaa" + bytes(12) + (len(body) + 8).to_bytes(4, "big") + body + bytes(4) + b"\x00\x00\xaa\x55"
    assert decode_broadcast(frame)["ip"] == "192.0.2.5"


async def test_account_flow(hass):
    client = MagicMock(uid="uid")
    client.get_local_keys = AsyncMock(return_value=[{"device_id": "dev1", "name": "Conga"}, {"device_id": "dev2", "name": "Lamp"}])
    client._mobile_call = AsyncMock(side_effect=[
        {"result": {"localKey": "0123456789abcdef"}},
        {"result": {"password": "pw", "p2pConfig": {"ices": [{"urls": "stun:192.0.2.1:3478"}, {"urls": "turn:x"}]}}},
    ])
    with patch("custom_components.tuya_laser_map.config_flow._login", AsyncMock(return_value=client)), \
         patch("custom_components.tuya_laser_map.config_flow.find_ip", AsyncMock(return_value="192.0.2.10")), \
         patch("custom_components.tuya_laser_map.async_setup_entry", AsyncMock(return_value=True)):
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"next_step_id": "account"})
        assert r["step_id"] == "account"
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"app": "tuya_smart", "email": "a@b.c",
                                                                          "password": "x", "country_code": "34"})
        assert r["step_id"] == "device"
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"device_id": "dev1"})
        schema = {str(k): k.default() for k in r["data_schema"].schema}
        assert schema == {"device_id": "dev1", "name": "Conga", "host": "192.0.2.10"}
        r = await hass.config_entries.flow.async_configure(r["flow_id"], schema)
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert r["title"] == "Conga"
    assert "password" not in r["data"] and r["data"]["stun_servers"] == ["stun:192.0.2.1:3478"]
    assert r["data"]["host"] == "192.0.2.10" and r["data"]["local_key"] == "0123456789abcdef"
