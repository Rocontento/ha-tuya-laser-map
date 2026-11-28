"""The bundled tuya-local profile is put back when a tuya-local update wipes it."""

from types import SimpleNamespace

from custom_components.tuya_laser_map import PROFILE, _install_tuya_local_profile


def test_profile_reinstalled(tmp_path):
    hass = SimpleNamespace(config=SimpleNamespace(path=lambda *p: str(tmp_path.joinpath(*p))))
    assert not _install_tuya_local_profile(hass)          # no tuya-local: nothing to do
    devices = tmp_path / "custom_components" / "tuya_local" / "devices"
    devices.mkdir(parents=True)
    assert _install_tuya_local_profile(hass)              # wiped by an update: written back
    assert b"Conga 9590" in (devices / PROFILE).read_bytes()
    assert not _install_tuya_local_profile(hass)          # already there: untouched
