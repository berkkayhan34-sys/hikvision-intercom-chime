"""The Supervisor boundary: finding the app and changing its station list safely."""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from custom_components.hikvision_intercom import bridge_app
from custom_components.hikvision_intercom.validation import SetupError

STATION = {
    "name": "st_192_168_0_60",
    "host": "192.168.0.60",
    "port": 8000,
    "username": "admin",
    "password": "pw",
}
TOKEN = "k" * 40


class FakeAddons:
    def __init__(self, options, *, installed=("a1b2c3d4_hikvision_sdk",), running=True):
        self.options, self.installed, self.running = options, list(installed), running
        self.written, self.actions = [], []
        self.fail_on: dict[str, Exception] = {}
        self.fail_restore = False
        self.restart_fails_once = False

    async def list(self):
        return [SimpleNamespace(slug=slug) for slug in self.installed]

    async def addon_info(self, slug):
        if "info" in self.fail_on:
            raise self.fail_on["info"]
        return SimpleNamespace(
            slug=slug,
            hostname=slug.replace("_", "-"),
            options=self.options,
            state="started" if self.running else "stopped",
        )

    async def set_addon_options(self, slug, options):
        if "set" in self.fail_on:
            raise self.fail_on["set"]
        if self.fail_restore and self.written:
            raise RuntimeError("cannot restore")
        self.written.append(options.config)
        self.options = options.config
        self.actions.append("set")

    async def restart_addon(self, slug):
        self.actions.append("restart")
        if "restart" in self.fail_on:
            failure = self.fail_on["restart"]
            if self.restart_fails_once:
                del self.fail_on["restart"]
            raise failure

    async def start_addon(self, slug):
        self.actions.append("start")
        if "start" in self.fail_on:
            raise self.fail_on["start"]


@pytest.fixture
def addons(hass):
    hass.config.components.add("hassio")
    fake = FakeAddons({"token": TOKEN, "stations": []})
    with patch.object(
        bridge_app, "_supervisor_client", return_value=SimpleNamespace(addons=fake)
    ):
        yield fake


async def test_find_app_returns_hostname_options_and_state(hass, addons):
    app = await bridge_app.find_app(hass)
    assert (app.slug, app.hostname, app.running) == (
        "a1b2c3d4_hikvision_sdk",
        "a1b2c3d4-hikvision-sdk",
        True,
    )
    assert app.options["token"] == TOKEN


async def test_local_install_slug_is_recognised(hass, addons):
    addons.installed = ["local_hikvision_sdk"]
    assert (await bridge_app.find_app(hass)).slug == "local_hikvision_sdk"


@pytest.mark.parametrize(
    "installed, code",
    [
        ([], "app_required"),
        (
            ["core_mosquitto", "aff2db71_hikvision_doorbell"],
            "app_required",
        ),  # the old SDK app is not this one
        (["xyz_hikvision_sdk"], "app_required"),
        (["a1b2c3d4_hikvision_sdk", "e5f6a7b8_hikvision_sdk"], "app_ambiguous"),
    ],
)
async def test_find_app_requires_exactly_one_matching_app(
    hass, addons, installed, code
):
    addons.installed = installed
    with pytest.raises(SetupError) as error:
        await bridge_app.find_app(hass)
    assert str(error.value) == code


async def test_find_app_without_supervisor(hass):
    with pytest.raises(SetupError) as error:
        await bridge_app.find_app(hass)
    assert str(error.value) == "supervisor_required"


async def test_supervisor_errors_never_leak_their_text(hass, addons):
    addons.fail_on["info"] = RuntimeError("options: password=hunter2")
    with pytest.raises(SetupError) as error:
        await bridge_app.find_app(hass)
    assert str(error.value) == "app_required"


async def test_configure_writes_then_restarts_a_running_app(hass, addons):
    await bridge_app.configure_station(hass, STATION)
    assert addons.actions == ["set", "restart"]
    assert addons.options["stations"] == [STATION] and addons.options["token"] == TOKEN


async def test_configure_starts_a_stopped_app(hass, addons):
    addons.running = False
    await bridge_app.configure_station(hass, STATION)
    assert addons.actions == ["set", "start"]


async def test_configure_does_nothing_when_options_already_match(hass, addons):
    addons.options = {"token": TOKEN, "stations": [STATION]}
    await bridge_app.configure_station(hass, STATION)
    assert addons.actions == []


async def test_configure_generates_a_token_when_missing(hass, addons):
    addons.options = {"token": "", "stations": []}
    await bridge_app.configure_station(hass, STATION)
    assert len(addons.options["token"]) >= 32


async def test_failed_restart_restores_the_previous_options(hass, addons):
    previous = {"token": TOKEN, "stations": []}
    addons.fail_on["restart"] = RuntimeError("boom")
    addons.restart_fails_once = True
    with pytest.raises(SetupError) as error:
        await bridge_app.configure_station(hass, STATION)
    assert str(error.value) == "app_config_failed"
    assert addons.options == previous
    assert addons.actions == ["set", "restart", "set", "restart"]


async def test_restart_failing_again_during_restore_is_reported_as_rollback_failure(
    hass, addons
):
    addons.fail_on["restart"] = RuntimeError("boom")
    with pytest.raises(SetupError) as error:
        await bridge_app.configure_station(hass, STATION)
    assert str(error.value) == "app_rollback_failed"


async def test_failed_write_does_not_restart(hass, addons):
    addons.fail_on["set"] = RuntimeError("boom")
    with pytest.raises(SetupError):
        await bridge_app.configure_station(hass, STATION)
    assert "restart" not in addons.actions


async def test_failed_restore_is_reported_distinctly(hass, addons):
    addons.fail_on["restart"] = RuntimeError("boom")
    addons.fail_restore = True
    with pytest.raises(SetupError) as error:
        await bridge_app.configure_station(hass, STATION)
    assert str(error.value) == "app_rollback_failed"


async def test_cancellation_restores_options_and_is_propagated(hass, addons):
    addons.fail_on["restart"] = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await bridge_app.configure_station(hass, STATION)
    assert addons.options == {"token": TOKEN, "stations": []}


async def test_remove_station_drops_only_that_station(hass, addons):
    other = dict(STATION, name="other", host="192.168.0.9")
    addons.options = {"token": TOKEN, "stations": [STATION, other]}
    await bridge_app.remove_station(hass, STATION["name"])
    assert addons.options["stations"] == [other] and addons.actions == [
        "set",
        "restart",
    ]


async def test_remove_unknown_station_is_a_no_op(hass, addons):
    await bridge_app.remove_station(hass, "nope")
    assert addons.actions == []


async def test_remove_station_when_the_app_is_gone_does_not_raise(hass, addons):
    addons.installed = []
    await bridge_app.remove_station(hass, STATION["name"])
