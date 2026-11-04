"""Fixtures: the real Home Assistant core, with only the two external boundaries faked
(the Hik-Connect cloud client and Supervisor). The bridge app's HTTP API is mocked at
the aiohttp layer, so the real client, link and runtime code all run."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hikvision_intercom.bridge_app import AppInfo
from custom_components.hikvision_intercom.const import DOMAIN
from custom_components.hikvision_intercom.hikconnect_api import HikDevice

TOKEN = "t" * 40
APP_HOST = "a1b2c3d4-hikvision-sdk"
STATION_NAME = "st_192_168_0_50"
STATION_URL = f"http://{APP_HOST}:8099/v1/stations/{STATION_NAME}"
SERIAL = "DS-KH6320-WTE1TESTSERIAL"
PASSWORD = "very-secret-station-password"


@pytest.fixture(autouse=True)
def _custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def _no_wait_after_playback():
    """The runtime waits for the clip to end plus a margin; keep tests fast."""
    with patch("custom_components.hikvision_intercom.chime.COMPLETION_MARGIN", 0.0):
        yield


class FakeCloud:
    """Stands in for HikConnectClient: no network, just enough data for every platform."""

    _base = "https://api.hik-connect.com"

    def __init__(self, *args, **kwargs) -> None:
        self.login_error: Exception | None = None

    def login(self) -> None:
        if self.login_error:
            raise self.login_error

    def get_devices(self):
        return [HikDevice(SERIAL, "DS-KH6320-WTE1", None, "DS-KH6320-WTE1", {1: 1}, "V2.2.108", True)]

    def get_cameras(self, device):
        return []

    def get_call_status(self, serial):
        return {"status": "idle", "info": {}}

    def get_device_status_map(self):
        return {SERIAL: {"online": True}}

    def get_audio_volumes(self, serial):
        return {"ringtone": 5, "two_way": 5, "microphone": 5}

    def get_dnd(self, serial):
        return False

    def get_last_ring(self, serial):
        return None

    def get_work_status(self, serial):
        return {}

    def get_operation_time(self, serial):
        return {}

    def add_login_listener(self, listener):
        return lambda: None

    def _get(self, path):
        return {}

    def isapi(self, serial, method, path, body=""):
        return {"data": "{}"}


@pytest.fixture
def cloud():
    """Patch the cloud client class and the two realtime push listeners."""
    instance = FakeCloud()
    failing = MagicMock()
    failing.return_value.async_start = AsyncMock(side_effect=RuntimeError("push disabled in tests"))
    with patch("custom_components.hikvision_intercom.HikConnectClient", return_value=instance), \
         patch("custom_components.hikvision_intercom.config_flow.HikConnectClient", return_value=instance), \
         patch("custom_components.hikvision_intercom.HikConnectPush", failing), \
         patch("custom_components.hikvision_intercom.HikConnectLongLink", failing):
        yield instance


@dataclass
class FakeSupervisor:
    """Records what the integration asks the bridge app's Supervisor record to hold."""

    app: AppInfo
    configured: list
    removed: list


@pytest.fixture
def supervisor(hass: HomeAssistant):
    hass.config.components.add("hassio")
    app = AppInfo("a1b2c3d4_hikvision_sdk", APP_HOST, True, {"token": TOKEN, "stations": []})
    state = FakeSupervisor(app, [], [])

    async def configure(hass_, station):
        state.configured.append(dict(station))
        return app

    async def remove(hass_, name):
        state.removed.append(name)

    with patch("custom_components.hikvision_intercom.chime.find_app", AsyncMock(return_value=app)), \
         patch("custom_components.hikvision_intercom.config_flow.find_app", AsyncMock(return_value=app)), \
         patch("custom_components.hikvision_intercom.config_flow.configure_station", configure), \
         patch("custom_components.hikvision_intercom.remove_station", remove):
        yield state


def station_subentry(title: str = "Living Room Doorbell Chime", **data) -> ConfigSubentryData:
    return ConfigSubentryData(
        subentry_type="station", title=title, unique_id="192.168.0.50:8000",
        data={"name": STATION_NAME, "host": "192.168.0.50", "port": 8000,
              "username": "admin", "gain_db": 0, "repeats": 4, **data})


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    config_entry = MockConfigEntry(
        domain=DOMAIN, title="owner@example.test", unique_id="owner@example.test",
        data={"account": "owner@example.test", "password": "cloud-password",
              "base_url": "https://api.hik-connect.com"},
        subentries_data=[station_subentry()])
    config_entry.add_to_hass(hass)
    return config_entry
