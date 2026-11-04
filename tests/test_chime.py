"""The chime inside a running Home Assistant: setup, availability, playback, silence."""

import asyncio
from datetime import timedelta
from pathlib import Path

import aiohttp
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.hikvision_intercom.audio import Settings, decode_wav, render
from custom_components.hikvision_intercom.chime import ChimeError
from custom_components.hikvision_intercom.const import DOMAIN

from .conftest import STATION_URL, TOKEN

PLAY = "button.living_room_doorbell_chime_play_chime"
STOP = "button.living_room_doorbell_chime_stop_chime"
GAIN = "number.living_room_doorbell_chime_digital_gain"
REPEATS = "number.living_room_doorbell_chime_repetitions"
STATUS = "sensor.living_room_doorbell_chime_request_status"
SOUND = Path(__file__).parents[1] / "custom_components" / "hikvision_intercom" / "assets" / "ding_dong.wav"

ONLINE = {"name": "x", "connected": True, "serial": "S", "call_status": "idle", "broadcast": "idle"}


def mock_bridge(aioclient_mock, *, status=ONLINE, play=None, stop_result=True):
    aioclient_mock.get(STATION_URL, json=status)
    aioclient_mock.post(f"{STATION_URL}/broadcast", **(play or {"status": 202, "json": {"duration": 0.01}}))
    aioclient_mock.delete(f"{STATION_URL}/broadcast", json={"stopped": stop_result})


async def setup(hass, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


def calls(aioclient_mock, method):
    return [call for call in aioclient_mock.mock_calls if call[0] == method]


async def press(hass, entity_id):
    await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)


@pytest.fixture
def default_pcm():
    return render(decode_wav(SOUND.read_bytes()), Settings(0, 4))


async def test_station_entities_exist_and_report_defaults(hass, cloud, supervisor, entry, aioclient_mock):
    mock_bridge(aioclient_mock)
    await setup(hass, entry)
    assert hass.states.get(STATUS).state == "idle"
    assert float(hass.states.get(GAIN).state) == 0 and float(hass.states.get(REPEATS).state) == 4
    assert hass.states.get(PLAY).state != "unavailable"
    # the cloud entities of the same entry live next to them
    for entity_id in ("button.ds_kh6320_wte1_answer_call", "button.ds_kh6320_wte1_hang_up_call",
                      "button.ds_kh6320_wte1_unlock_all_doors", "lock.ds_kh6320_wte1_channel_1_lock",
                      "sensor.ds_kh6320_wte1_call_status", "event.ds_kh6320_wte1_doorbell",
                      "number.ds_kh6320_wte1_ringtone_volume"):
        assert hass.states.get(entity_id) is not None, entity_id


async def test_setup_plays_nothing_and_sends_nothing_but_status(hass, cloud, supervisor, entry, aioclient_mock):
    mock_bridge(aioclient_mock)
    await setup(hass, entry)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert calls(aioclient_mock, "POST") == [] and calls(aioclient_mock, "DELETE") == []


async def test_play_sends_the_rendered_audio_with_the_token(hass, cloud, supervisor, entry, aioclient_mock, default_pcm):
    mock_bridge(aioclient_mock)
    await setup(hass, entry)
    await press(hass, PLAY)
    (_, url, body, headers), = calls(aioclient_mock, "POST")
    assert str(url) == f"{STATION_URL}/broadcast"
    assert body == default_pcm and len(body) == 8 * 8000 * 2  # 2 s x 4 repeats at 8 kHz PCM16
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert hass.states.get(STATUS).state == "idle"
    assert len(calls(aioclient_mock, "DELETE")) == 1  # always released afterwards


async def test_status_is_sending_while_playing_and_stop_ends_it(hass, cloud, supervisor, entry, aioclient_mock):
    mock_bridge(aioclient_mock, play={"status": 202, "json": {"duration": 60}})
    await setup(hass, entry)
    task = hass.async_create_task(press(hass, PLAY))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if hass.states.get(STATUS).state == "sending":
            break
    assert hass.states.get(STATUS).state == "sending"
    await press(hass, STOP)
    await asyncio.wait_for(task, 5)
    await hass.async_block_till_done()
    assert hass.states.get(STATUS).state == "idle"
    assert len(calls(aioclient_mock, "DELETE")) == 1


async def test_stop_when_idle_sends_nothing(hass, cloud, supervisor, entry, aioclient_mock):
    mock_bridge(aioclient_mock)
    await setup(hass, entry)
    await press(hass, STOP)
    assert calls(aioclient_mock, "DELETE") == []


async def test_second_press_while_playing_is_busy(hass, cloud, supervisor, entry, aioclient_mock):
    mock_bridge(aioclient_mock, play={"status": 202, "json": {"duration": 60}})
    await setup(hass, entry)
    first = hass.async_create_task(press(hass, PLAY))
    await asyncio.sleep(0.05)
    with pytest.raises(HomeAssistantError) as error:
        await press(hass, PLAY)
    assert error.value.translation_key == "busy"
    assert len(calls(aioclient_mock, "POST")) == 1
    await press(hass, STOP)
    await asyncio.wait_for(first, 5)


@pytest.mark.parametrize("response, key", [
    ({"status": 409, "json": {"error": "call_active"}}, "call_active"),
    ({"status": 409, "json": {"error": "busy"}}, "busy"),
    ({"status": 503, "json": {"error": "station_offline"}}, "station_offline"),
    ({"status": 400, "json": {"error": "invalid_audio"}}, "invalid_audio"),
    ({"status": 401, "json": {"error": "unauthorized"}}, "app_unauthorized"),
    ({"status": 500, "json": {}}, "app_error"),
])
async def test_refusals_surface_a_translated_error_and_release_nothing(
        hass, cloud, supervisor, entry, aioclient_mock, response, key):
    mock_bridge(aioclient_mock, play=response)
    await setup(hass, entry)
    with pytest.raises(HomeAssistantError) as error:
        await press(hass, PLAY)
    assert error.value.translation_key == key
    state = hass.states.get(STATUS)
    assert state.state == "error" and state.attributes["last_error"] == key
    assert calls(aioclient_mock, "DELETE") == []  # nothing was started, so nothing to stop


async def test_unreachable_bridge_during_play_is_reported_not_swallowed(hass, cloud, supervisor, entry, aioclient_mock):
    mock_bridge(aioclient_mock, play={"exc": aiohttp.ClientError()})
    await setup(hass, entry)
    with pytest.raises(HomeAssistantError) as error:
        await press(hass, PLAY)
    assert error.value.translation_key == "app_unreachable"


async def test_chime_entities_are_unavailable_when_the_bridge_is_down_and_recover(
        hass, cloud, supervisor, entry, aioclient_mock):
    aioclient_mock.get(STATION_URL, exc=aiohttp.ClientError())
    await setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED  # cloud features are unaffected
    assert hass.states.get(PLAY).state == "unavailable"
    aioclient_mock.clear_requests()
    mock_bridge(aioclient_mock)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
    await hass.async_block_till_done()
    assert hass.states.get(PLAY).state != "unavailable"


async def test_station_not_connected_makes_playback_unavailable(hass, cloud, supervisor, entry, aioclient_mock):
    mock_bridge(aioclient_mock, status={**ONLINE, "connected": False})
    await setup(hass, entry)
    assert hass.states.get(PLAY).state == "unavailable"
    assert hass.states.get(STATUS).state == "idle"  # the request status stays readable


async def test_setting_numbers_is_silent_and_persisted_in_the_subentry(hass, cloud, supervisor, entry, aioclient_mock):
    mock_bridge(aioclient_mock)
    await setup(hass, entry)
    await hass.services.async_call("number", "set_value", {"entity_id": REPEATS, "value": 2}, blocking=True)
    await hass.services.async_call("number", "set_value", {"entity_id": GAIN, "value": -6}, blocking=True)
    assert calls(aioclient_mock, "POST") == []
    data = next(iter(entry.subentries.values())).data
    assert data["repeats"] == 2 and data["gain_db"] == -6
    await hass.config_entries.async_reload(entry.entry_id)  # survives a reload
    await hass.async_block_till_done()
    assert float(hass.states.get(REPEATS).state) == 2


async def test_gain_and_repeat_changes_affect_the_next_play(hass, cloud, supervisor, entry, aioclient_mock):
    mock_bridge(aioclient_mock)
    await setup(hass, entry)
    await hass.services.async_call("number", "set_value", {"entity_id": REPEATS, "value": 1}, blocking=True)
    await press(hass, PLAY)
    (_, _, body, _), = calls(aioclient_mock, "POST")
    assert len(body) == 2 * 8000 * 2


@pytest.mark.parametrize("key, value", [("gain_db", float("nan")), ("gain_db", True),
                                        ("repeats", 2.5), ("repeats", float("inf")), ("other", 1)])
async def test_invalid_number_values_are_rejected_before_saving(hass, cloud, supervisor, entry, aioclient_mock, key, value):
    mock_bridge(aioclient_mock)
    await setup(hass, entry)
    runtime = hass.data[DOMAIN][entry.entry_id]["chimes"][next(iter(entry.subentries))]
    before = dict(next(iter(entry.subentries.values())).data)
    with pytest.raises(ChimeError):
        await runtime.async_set_number(key, value)
    assert dict(next(iter(entry.subentries.values())).data) == before


async def test_unload_during_playback_releases_cleanly(hass, cloud, supervisor, entry, aioclient_mock):
    mock_bridge(aioclient_mock, play={"status": 202, "json": {"duration": 60}})
    await setup(hass, entry)
    task = hass.async_create_task(press(hass, PLAY))
    await asyncio.sleep(0.05)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await asyncio.wait_for(task, 5)
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert len(calls(aioclient_mock, "DELETE")) == 1


async def test_token_rotation_is_recovered_by_resolving_the_app_again(hass, cloud, supervisor, entry, aioclient_mock):
    """A 401 or unreachable route triggers exactly one re-resolve through Supervisor."""
    mock_bridge(aioclient_mock)
    await setup(hass, entry)
    from custom_components.hikvision_intercom import chime
    chime_runtime = hass.data[DOMAIN][entry.entry_id]["chimes"][next(iter(entry.subentries))]
    attempts = []

    async def operation(client):
        attempts.append(client)
        if len(attempts) == 1:
            raise chime.BridgeError("app_unauthorized")
        return "ok"

    assert await chime_runtime.link.call(operation) == "ok"
    assert len(attempts) == 2 and attempts[0] is not attempts[1]


async def test_a_second_failure_after_re_resolving_is_raised(hass, cloud, supervisor, entry, aioclient_mock):
    mock_bridge(aioclient_mock)
    await setup(hass, entry)
    from custom_components.hikvision_intercom import chime
    link = hass.data[DOMAIN][entry.entry_id]["chimes"][next(iter(entry.subentries))].link

    async def always_unauthorized(client):
        raise chime.BridgeError("app_unauthorized")

    with pytest.raises(chime.BridgeError) as error:
        await link.call(always_unauthorized)
    assert error.value.code == "app_unauthorized"


async def test_stop_pressed_while_preparing_prevents_any_broadcast(hass, cloud, supervisor, entry, aioclient_mock):
    mock_bridge(aioclient_mock)
    await setup(hass, entry)
    runtime = hass.data[DOMAIN][entry.entry_id]["chimes"][next(iter(entry.subentries))]
    original = runtime._source

    def stop_during_render(*args):
        runtime._stop.set()  # Stop arrives while the executor job is running
        return original(*args)

    runtime._source = stop_during_render
    await press(hass, PLAY)
    assert calls(aioclient_mock, "POST") == [] and calls(aioclient_mock, "DELETE") == []
    assert hass.states.get(STATUS).state == "idle"
