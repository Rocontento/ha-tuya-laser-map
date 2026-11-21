"""End-to-end check inside a test Home Assistant: entry setup -> map camera -> clean_rooms frame."""

import asyncio
import base64
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tuya_laser_map.const import DOMAIN

from .synthetic import map_bytes, path_bytes
DATA = {"device_id": "dev1", "host": "192.0.2.10", "local_key": "0123456789abcdef", "device_password": "pw",
        "user_id": "uid", "stun_servers": ["stun:192.0.2.1:3478"]}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


async def test_map_camera_and_room_clean(hass):
    async def fake_session(host, dev, key, pwd, uid, ices, on_file, idle):
        assert ices == [{"urls": "stun:192.0.2.1:3478"}]
        on_file("map.bin.stream", map_bytes())
        on_file("cleanPath.bin.stream", path_bytes())
        await asyncio.sleep(3600)

    sent = {}

    async def fake_send(host, dev, key, dps, timeout=5.0):
        sent.update(dps)
        return {"15": base64.b64encode(bytes.fromhex("aa0100000004150101091f")).decode()}

    entry = MockConfigEntry(domain=DOMAIN, data=DATA, unique_id="dev1", title="Conga")
    entry.add_to_hass(hass)
    with patch("custom_components.tuya_laser_map.coordinator.run_session", fake_session), \
         patch("custom_components.tuya_laser_map.coordinator.send_dps", fake_send):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coord = entry.runtime_data
        for _ in range(100):  # render runs in the executor
            if coord.png:
                break
            await hass.async_block_till_done()
            await asyncio.sleep(0.1)
        state = hass.states.get("camera.conga_map")
        assert state is not None, [s.entity_id for s in hass.states.async_all()]
        assert coord.png[:4] == b"\x89PNG"
        rooms = state.attributes["rooms"]
        assert {r["name"] for r in rooms.values()} >= {"Kitchen", "Living room", "Bathroom"}
        assert len(state.attributes["calibration_points"]) == 3

        await hass.services.async_call(DOMAIN, "clean_rooms", {"rooms": ["Kitchen", 9]}, blocking=True)
        assert base64.b64decode(sent["15"]).hex() == "aa01000000051401020309" + "%02x" % ((0x14 + 1 + 2 + 3 + 9) & 0xFF)
        await hass.config_entries.async_unload(entry.entry_id)
