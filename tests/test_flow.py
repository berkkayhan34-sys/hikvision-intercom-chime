"""Config flows: the cloud login and the local-station subentry (add, reconfigure)."""

from contextlib import contextmanager
from pathlib import Path
import struct
import wave

import pytest
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.core import HomeAssistant
from unittest.mock import patch

from custom_components.hikvision_intercom.const import DOMAIN
from custom_components.hikvision_intercom.hikconnect_api import HikConnectAuthError
from custom_components.hikvision_intercom.validation import SetupError

from .conftest import PASSWORD
from .test_chime import ONLINE, mock_bridge

UPLOAD_ID = "0c9f56a6-7f4b-4b6e-9a53-3a1d2c5a6b10"
CONNECTION = {"title": "Hall chime", "host": "192.168.0.60", "port": 8000,
              "username": "admin", "password": PASSWORD}


# --- cloud account -----------------------------------------------------------------
async def test_cloud_login_creates_the_entry(hass: HomeAssistant, cloud):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"account": "Owner@Example.test", "password": "pw",
                            "server": "https://api.hik-connect.com", "base_url": ""})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["account"] == "Owner@Example.test"
    assert result["result"].unique_id == "owner@example.test"


@pytest.mark.parametrize("error, key", [(HikConnectAuthError("bad"), "invalid_auth"),
                                        (OSError("down"), "cannot_connect")])
async def test_cloud_login_errors_keep_the_form(hass: HomeAssistant, cloud, error, key):
    cloud.login_error = error
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"account": "a@b.test", "password": "pw",
                            "server": "https://api.hik-connect.com", "base_url": ""})
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"base": key}


# --- add a station -------------------------------------------------------------------
async def start_subentry(hass, entry):
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, "station"), context={"source": SOURCE_USER})


async def loaded(hass, cloud, supervisor, entry, aioclient_mock):
    mock_bridge(aioclient_mock)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_add_station_stores_no_password_and_configures_the_app(
        hass, cloud, supervisor, entry, aioclient_mock):
    await loaded(hass, cloud, supervisor, entry, aioclient_mock)
    result = await start_subentry(hass, entry)
    assert result["step_id"] == "connection"
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], CONNECTION)
    assert result["step_id"] == "confirm"
    assert supervisor.configured == []  # nothing touches the app before confirmation
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"confirm_restart": True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert supervisor.configured == [{**CONNECTION, "name": "st_192_168_0_60"}]
    created = [s for s in entry.subentries.values() if s.title == "Hall chime"][0]
    assert dict(created.data) == {"name": "st_192_168_0_60", "host": "192.168.0.60", "port": 8000,
                                  "username": "admin", "gain_db": 0, "repeats": 4}
    assert PASSWORD not in repr(entry.as_dict()) and created.unique_id == "192.168.0.60:8000"
    await hass.async_block_till_done()  # let the reload for the new station finish


async def test_new_station_gets_its_own_entities_after_reload(hass, cloud, supervisor, entry, aioclient_mock):
    await loaded(hass, cloud, supervisor, entry, aioclient_mock)
    aioclient_mock.get("http://a1b2c3d4-hikvision-sdk:8099/v1/stations/st_192_168_0_60", json=ONLINE)
    result = await start_subentry(hass, entry)
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], CONNECTION)
    await hass.config_entries.subentries.async_configure(result["flow_id"], {"confirm_restart": True})
    await hass.async_block_till_done()
    assert hass.states.get("button.hall_chime_play_chime") is not None
    assert hass.states.get("button.living_room_doorbell_chime_play_chime") is not None


async def test_add_station_requires_the_app(hass, cloud, supervisor, entry, aioclient_mock):
    await loaded(hass, cloud, supervisor, entry, aioclient_mock)
    with patch("custom_components.hikvision_intercom.config_flow.find_app",
               side_effect=SetupError("app_required")):
        result = await start_subentry(hass, entry)
    assert result["step_id"] == "user" and result["errors"] == {"base": "app_required"}


@pytest.mark.parametrize("change, key", [
    ({"host": "8.8.8.8"}, "invalid_host"), ({"host": "example.test"}, "invalid_host"),
    ({"host": "192.168.0.999"}, "invalid_host"), ({"port": 8000.5}, "invalid_port"),
    ({"username": ""}, "invalid_credentials"), ({"password": "x" * 65}, "invalid_credentials"),
    ({"password": "a\nb"}, "invalid_credentials")])
async def test_connection_validation_errors(hass, cloud, supervisor, entry, aioclient_mock, change, key):
    await loaded(hass, cloud, supervisor, entry, aioclient_mock)
    result = await start_subentry(hass, entry)
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {**CONNECTION, **change})
    assert result["step_id"] == "connection" and result["errors"] == {"base": key}
    assert supervisor.configured == []


@pytest.mark.parametrize("port", [0, 70000])
async def test_ports_outside_the_form_range_never_reach_the_app(hass, cloud, supervisor, entry, aioclient_mock, port):
    await loaded(hass, cloud, supervisor, entry, aioclient_mock)
    result = await start_subentry(hass, entry)
    with pytest.raises(InvalidData):
        await hass.config_entries.subentries.async_configure(result["flow_id"], {**CONNECTION, "port": port})
    assert supervisor.configured == []


async def test_duplicate_address_aborts_before_any_change(hass, cloud, supervisor, entry, aioclient_mock):
    await loaded(hass, cloud, supervisor, entry, aioclient_mock)
    result = await start_subentry(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**CONNECTION, "host": "192.168.0.50"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"
    assert supervisor.configured == []


async def test_unticked_confirmation_changes_nothing(hass, cloud, supervisor, entry, aioclient_mock):
    await loaded(hass, cloud, supervisor, entry, aioclient_mock)
    result = await start_subentry(hass, entry)
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], CONNECTION)
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"confirm_restart": False})
    assert result["step_id"] == "confirm" and result["errors"] == {"base": "confirmation_required"}
    assert supervisor.configured == [] and len(entry.subentries) == 1


async def test_app_failure_creates_nothing_and_drops_the_password(hass, cloud, supervisor, entry, aioclient_mock):
    await loaded(hass, cloud, supervisor, entry, aioclient_mock)

    async def fail(hass_, station):
        raise SetupError("app_config_failed")

    result = await start_subentry(hass, entry)
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], CONNECTION)
    with patch("custom_components.hikvision_intercom.config_flow.configure_station", fail):
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {"confirm_restart": True})
    assert result["step_id"] == "user" and result["errors"] == {"base": "app_config_failed"}
    assert len(entry.subentries) == 1
    flow = hass.config_entries.subentries._progress[result["flow_id"]]
    assert flow._pending is None


# --- reconfigure the sound ---------------------------------------------------------------
async def reconfigure(hass, entry):
    subentry_id = next(iter(entry.subentries))
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, "station"), context={"source": "reconfigure", "subentry_id": subentry_id})


async def test_reconfigure_saves_gain_and_repeats_silently(hass, cloud, supervisor, entry, aioclient_mock):
    await loaded(hass, cloud, supervisor, entry, aioclient_mock)
    result = await reconfigure(hass, entry)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"gain_db": 6, "repeats": 3})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    data = next(iter(entry.subentries.values())).data
    assert data["gain_db"] == 6 and data["repeats"] == 3
    assert [c for c in aioclient_mock.mock_calls if c[0] == "POST"] == []
    assert entry.state is ConfigEntryState.LOADED


async def test_reconfigure_rejects_fractional_repeats(hass, cloud, supervisor, entry, aioclient_mock):
    await loaded(hass, cloud, supervisor, entry, aioclient_mock)
    result = await reconfigure(hass, entry)
    before = dict(next(iter(entry.subentries.values())).data)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"gain_db": 0, "repeats": 2.5})
    assert result["errors"] == {"base": "invalid_repeats"}
    assert dict(next(iter(entry.subentries.values())).data) == before


@pytest.mark.parametrize("values", [{"gain_db": 99, "repeats": 4}, {"gain_db": 0, "repeats": 11}])
async def test_reconfigure_form_refuses_out_of_range_values(hass, cloud, supervisor, entry, aioclient_mock, values):
    await loaded(hass, cloud, supervisor, entry, aioclient_mock)
    result = await reconfigure(hass, entry)
    before = dict(next(iter(entry.subentries.values())).data)
    with pytest.raises(InvalidData):
        await hass.config_entries.subentries.async_configure(result["flow_id"], values)
    assert dict(next(iter(entry.subentries.values())).data) == before


def make_wav(path: Path, rate=8000, seconds=1):
    with wave.open(str(path), "wb") as target:
        target.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        target.writeframes(struct.pack("<h", 800) * int(rate * seconds))
    return path


@contextmanager
def uploaded(path):
    yield path


async def test_reconfigure_with_an_uploaded_wav_stores_it_and_uses_it(
        hass, cloud, supervisor, entry, aioclient_mock, tmp_path):
    await loaded(hass, cloud, supervisor, entry, aioclient_mock)
    wav = make_wav(tmp_path / "bell.wav", seconds=1)
    result = await reconfigure(hass, entry)
    with patch("custom_components.hikvision_intercom.chime.process_uploaded_file", lambda h, i: uploaded(wav)):
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {"audio_file": UPLOAD_ID, "gain_db": 0, "repeats": 2})
    assert result["reason"] == "reconfigure_successful"
    digest = next(iter(entry.subentries.values())).data["source_digest"]
    assert len(digest) == 64
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.living_room_doorbell_chime_play_chime"}, blocking=True)
    (_, _, body, _), = [c for c in aioclient_mock.mock_calls if c[0] == "POST"]
    assert len(body) == 1 * 8000 * 2 * 2  # the uploaded 1 s clip, twice


@pytest.mark.parametrize("rate, error", [(16000, "unsupported_wav")])
async def test_reconfigure_rejects_unsupported_wav(hass, cloud, supervisor, entry, aioclient_mock, tmp_path, rate, error):
    await loaded(hass, cloud, supervisor, entry, aioclient_mock)
    wav = make_wav(tmp_path / "bad.wav", rate=rate)
    result = await reconfigure(hass, entry)
    with patch("custom_components.hikvision_intercom.chime.process_uploaded_file", lambda h, i: uploaded(wav)):
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {"audio_file": UPLOAD_ID, "gain_db": 0, "repeats": 2})
    assert result["errors"] == {"base": error}
    assert "source_digest" not in next(iter(entry.subentries.values())).data
