"""Talking and listening during a door call: conversions, the bridge client, the call runtime, the Assist engine and the
listen endpoint, against a fake bridge app that speaks the real HTTP contract."""

import asyncio
import math
import struct
from unittest.mock import patch

import pytest
from aiohttp import web
from homeassistant.components import assist_pipeline, stt
from homeassistant.components.assist_pipeline.pipeline import (
    KEY_ASSIST_PIPELINE,
    PipelineEventType,
    PipelineStage,
)
from homeassistant.core import Context
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_socket import enable_socket, socket_allow_hosts

from custom_components.hikvision_intercom import call_audio
from custom_components.hikvision_intercom.bridge_app import AppInfo
from custom_components.hikvision_intercom.bridge_client import BridgeClient, BridgeError
from custom_components.hikvision_intercom.const import DOMAIN
from custom_components.hikvision_intercom.door_stt import SENT_TEXT
from custom_components.hikvision_intercom.hikconnect_api import HikConnectError

from .conftest import STATION_NAME, TOKEN
from .test_chime import setup

METADATA = stt.SpeechMetadata(
    language="en",
    format=stt.AudioFormats.WAV,
    codec=stt.AudioCodecs.PCM,
    bit_rate=stt.AudioBitRates.BITRATE_16,
    sample_rate=stt.AudioSampleRates.SAMPLERATE_16000,
    channel=stt.AudioChannels.CHANNEL_MONO,
)


@pytest.fixture(autouse=True)
def _loopback():
    enable_socket()
    socket_allow_hosts(["127.0.0.1"], allow_unix_socket=True)


def tone(seconds, freq=800, amplitude=9000):
    count = int(16000 * seconds)
    return struct.pack(
        f"<{count}h",
        *(
            round(amplitude * math.sin(2 * math.pi * freq * n / 16000))
            for n in range(count)
        ),
    )


async def mic(data, size=640):
    for start in range(0, len(data), size):
        yield data[start : start + size]
        await asyncio.sleep(0)


# --- conversions ------------------------------------------------------------------------------------------------------
def test_mulaw_round_trip_stays_within_the_quantiser_step():
    for sample in range(-32000, 32000, 11):
        decoded = struct.unpack(
            "<h", call_audio.mulaw_to_pcm16le(call_audio.mulaw_encode([sample]))
        )[0]
        assert abs(decoded - sample) <= max(40, abs(sample) // 16), (sample, decoded)


def test_known_mulaw_codes():
    assert (
        call_audio.mulaw_encode([0]) == b"\xff"
        and call_audio.mulaw_encode([-1]) == b"\x7f"
    )
    assert (
        call_audio.mulaw_encode([32767]) == b"\x80"
        and call_audio.mulaw_encode([-32768]) == b"\x00"
    )


def test_the_wav_header_describes_8khz_16bit_mono_of_unknown_length():
    header = call_audio.wav_header()
    assert header[:4] == b"RIFF" and header[8:16] == b"WAVEfmt " and len(header) == 44
    channels, rate, _, _, bits = struct.unpack("<HIIHH", header[22:36])
    assert (channels, rate, bits) == (1, 8000, 16) and header[
        40:44
    ] == b"\xff\xff\xff\xff"


@pytest.mark.parametrize("sizes", [(640,), (1, 3, 7, 11, 2000), (2,) * 50])
def test_chunk_boundaries_do_not_change_the_downsampled_speech(sizes):
    data = tone(0.4)
    whole = call_audio.Downsampler().feed(data)
    parts, down, position = [], call_audio.Downsampler(), 0
    for size in sizes * 400:
        if position >= len(data):
            break
        parts += down.feed(data[position : position + size])
        position += size
    assert parts + down.feed(data[position:]) == whole


async def test_speech_becomes_paced_160_byte_frames_with_a_padded_last_one(monkeypatch):
    monkeypatch.setattr(call_audio, "FRAME_SECONDS", 0.0005)
    frames = [
        frame async for frame in call_audio.speech_frames(mic(tone(0.5) + tone(0.011)))
    ]
    assert len(frames) == 25 + 1 and all(len(frame) == 160 for frame in frames)
    assert frames[-1][-5:] == b"\xff" * 5


async def test_a_burst_is_paced_not_dumped(monkeypatch):
    monkeypatch.setattr(call_audio, "FRAME_SECONDS", 0.02)
    monkeypatch.setattr(call_audio, "LEAD_SECONDS", 0.1)
    started = asyncio.get_running_loop().time()
    [frame async for frame in call_audio.speech_frames(mic(tone(0.5), size=16000))]
    assert asyncio.get_running_loop().time() - started > 0.3


# --- a fake bridge app that speaks the real HTTP contract -------------------------------------------------------------
class FakeBridge:
    def __init__(self):
        self.call = "in_call"
        self.talked = []
        self.listeners = []
        self.talk_requests = 0

    async def start(self):
        app = web.Application()
        app.router.add_get("/v1/stations/{name}", self.status)
        app.router.add_post("/v1/stations/{name}/call/talk", self.talk)
        app.router.add_get("/v1/stations/{name}/call/listen", self.listen)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]

    async def stop(self):
        await self.runner.cleanup()

    def _denied(self, request):
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return web.json_response({"error": "unauthorized"}, status=401)
        return None

    async def status(self, request):
        return self._denied(request) or web.json_response(
            {
                "name": STATION_NAME,
                "connected": True,
                "serial": "S",
                "call_status": self.call,
                "broadcast": "idle",
            }
        )

    async def talk(self, request):
        self.talk_requests += 1
        if denied := self._denied(request):
            return denied
        if self.call != "in_call":
            return web.json_response({"error": "no_call"}, status=409)
        total = 0
        async for chunk in request.content.iter_any():
            self.talked.append(chunk)
            total += len(chunk)
        return web.json_response({"frames": total // 160})

    async def listen(self, request):
        if denied := self._denied(request):
            return denied
        if self.call != "in_call":
            return web.json_response({"error": "no_call"}, status=409)
        response = web.StreamResponse()
        await response.prepare(request)
        queue = asyncio.Queue()
        self.listeners.append(queue)
        while (chunk := await queue.get()) is not None:
            await response.write(chunk)
        return response


@pytest.fixture
async def bridge():
    fake = FakeBridge()
    await fake.start()
    with patch(
        "custom_components.hikvision_intercom.bridge_client.APP_PORT", fake.port
    ):
        yield fake
    await fake.stop()


@pytest.fixture
def live(hass, bridge, supervisor, cloud):
    """The integration pointed at the fake bridge instead of the aiohttp mock."""
    app = AppInfo(
        "a1b2c3d4_hikvision_sdk", "127.0.0.1", True, {"token": TOKEN, "stations": []}
    )
    with patch("custom_components.hikvision_intercom.chime.find_app", return_value=app):
        yield bridge


# --- the bridge client ----------------------------------------------------------------------------------------------------
async def test_talk_streams_the_frames_and_returns_the_count(hass, bridge):
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    client = BridgeClient(async_get_clientsession(hass), "127.0.0.1", TOKEN)

    async def frames():
        for n in range(5):
            yield bytes([n]) * 160

    assert await client.talk(STATION_NAME, frames()) == 5
    assert b"".join(bridge.talked) == b"".join(bytes([n]) * 160 for n in range(5))


async def test_talk_without_a_call_is_a_no_call_error(hass, bridge):
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    bridge.call = "idle"
    client = BridgeClient(async_get_clientsession(hass), "127.0.0.1", TOKEN)

    async def frames():
        yield b"\xff" * 160

    with pytest.raises(BridgeError) as info:
        await client.talk(STATION_NAME, frames())
    assert info.value.code == "no_call"


async def test_a_wrong_token_is_reported_without_echoing_it(hass, bridge):
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    client = BridgeClient(
        async_get_clientsession(hass), "127.0.0.1", "wrong-token-value"
    )

    async def frames():
        yield b"\xff" * 160

    with pytest.raises(BridgeError) as info:
        await client.talk(STATION_NAME, frames())
    assert info.value.code == "app_unauthorized" and "wrong-token-value" not in repr(
        info.value
    )


async def test_listen_yields_door_audio_and_ends_when_the_call_does(hass, bridge):
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    client = BridgeClient(async_get_clientsession(hass), "127.0.0.1", TOKEN)
    received = []
    async with client.listen(STATION_NAME) as chunks:
        await asyncio.sleep(0.05)
        bridge.listeners[0].put_nowait(b"a" * 160)
        bridge.listeners[0].put_nowait(None)
        async for chunk in chunks:
            received.append(chunk)
    assert b"".join(received) == b"a" * 160


async def test_listen_without_a_call_is_a_no_call_error(hass, bridge):
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    bridge.call = "idle"
    client = BridgeClient(async_get_clientsession(hass), "127.0.0.1", TOKEN)
    with pytest.raises(BridgeError) as info:
        async with client.listen(STATION_NAME):
            pass
    assert info.value.code == "no_call"


# --- the call runtime and the Assist engine -----------------------------------------------------------------------------------
async def door_engine(hass, entry):
    await setup(hass, entry)
    registry = er.async_get(hass)
    entity_id = next(
        e.entity_id
        for e in registry.entities.values()
        if e.platform == DOMAIN and e.domain == "stt"
    )
    return stt.async_get_speech_to_text_entity(hass, entity_id)


async def test_speaking_into_an_answered_call_sends_the_speech_to_the_door(
    hass, live, entry, monkeypatch
):
    monkeypatch.setattr(call_audio, "FRAME_SECONDS", 0.0005)
    engine = await door_engine(hass, entry)
    result = await engine.async_process_audio_stream(METADATA, mic(tone(0.5)))
    assert result.result is stt.SpeechResultState.SUCCESS and result.text == SENT_TEXT
    assert len(b"".join(live.talked)) == 25 * 160


async def test_without_a_call_nothing_is_sent_and_the_result_is_an_error(
    hass, live, entry, caplog
):
    live.call = "idle"
    engine = await door_engine(hass, entry)
    result = await engine.async_process_audio_stream(METADATA, mic(tone(0.2)))
    assert result.result is stt.SpeechResultState.ERROR and result.text is None
    assert live.talk_requests == 0 and "no_call" in caplog.text


async def test_a_ringing_call_is_answered_first_then_spoken_into(
    hass, live, entry, cloud, monkeypatch
):
    monkeypatch.setattr(call_audio, "FRAME_SECONDS", 0.0005)
    monkeypatch.setattr(
        "custom_components.hikvision_intercom.door_call.asyncio.sleep", asyncio.sleep
    )
    live.call = "ringing"
    engine = await door_engine(hass, entry)

    async def answered_soon():
        await asyncio.sleep(0.2)
        live.call = "in_call"

    answering = asyncio.create_task(answered_soon())
    result = await engine.async_process_audio_stream(METADATA, mic(tone(0.2)))
    await answering
    assert result.result is stt.SpeechResultState.SUCCESS
    assert cloud.answered == ["DS-KH6320-WTE1TESTSERIAL"] and live.talk_requests == 1


async def test_a_call_that_is_never_answered_is_not_spoken_into(
    hass, live, entry, cloud, monkeypatch
):
    monkeypatch.setattr(
        "custom_components.hikvision_intercom.door_call.ANSWER_WAIT_SECONDS", 0.3
    )
    live.call = "ringing"
    engine = await door_engine(hass, entry)
    result = await engine.async_process_audio_stream(METADATA, mic(tone(0.2)))
    assert result.result is stt.SpeechResultState.ERROR and live.talk_requests == 0


async def test_a_refused_answer_is_an_error_and_sends_nothing(
    hass, live, entry, cloud, caplog
):
    live.call = "ringing"
    cloud.answer_error = HikConnectError("rejected (secret detail)")
    engine = await door_engine(hass, entry)
    result = await engine.async_process_audio_stream(METADATA, mic(tone(0.2)))
    assert result.result is stt.SpeechResultState.ERROR and live.talk_requests == 0
    assert "answer_failed" in caplog.text and "secret detail" not in caplog.text


async def test_only_one_person_talks_at_a_time(hass, live, entry):
    engine = await door_engine(hass, entry)
    release = asyncio.Event()

    async def held():
        yield b"\x00\x00" * 320
        await release.wait()

    first = asyncio.create_task(engine.async_process_audio_stream(METADATA, held()))
    await asyncio.sleep(0.15)
    second = await engine.async_process_audio_stream(METADATA, mic(tone(0.1)))
    assert second.result is stt.SpeechResultState.ERROR
    release.set()
    assert (await first).result is stt.SpeechResultState.SUCCESS


# --- the listen endpoint ------------------------------------------------------------------------------------------------------
async def listen_url(hass, entry):
    subentry_id = next(iter(entry.subentries))
    return f"/api/hikvision_intercom/listen/{entry.entry_id}/{subentry_id}"


async def test_the_listen_endpoint_streams_wav_to_an_administrator(
    hass, live, entry, hass_client
):
    await setup(hass, entry)
    client = await hass_client()
    response = await client.get(await listen_url(hass, entry))
    assert response.status == 200 and response.headers["Content-Type"] == "audio/wav"
    header = await response.content.readexactly(44)
    assert header[:4] == b"RIFF"
    live.listeners[0].put_nowait(bytes([0xFF]) * 160)  # mu-law silence
    audio = await response.content.readexactly(320)
    assert audio == b"\x00\x00" * 160
    live.listeners[0].put_nowait(None)
    assert await response.content.read() == b""


async def test_the_listen_endpoint_is_open_to_every_signed_in_user(
    hass, live, entry, hass_client, hass_read_only_access_token
):
    await setup(hass, entry)
    client = await hass_client(hass_read_only_access_token)
    response = await client.get(await listen_url(hass, entry))
    assert response.status == 200 and response.headers["Content-Type"] == "audio/wav"
    live.listeners[0].put_nowait(None)


async def test_the_listen_endpoint_needs_a_login(
    hass, live, entry, hass_client_no_auth
):
    await setup(hass, entry)
    client = await hass_client_no_auth()
    assert (await client.get(await listen_url(hass, entry))).status == 401


async def test_listening_without_a_call_answers_409(hass, live, entry, hass_client):
    live.call = "idle"
    await setup(hass, entry)
    client = await hass_client()
    response = await client.get(await listen_url(hass, entry))
    assert response.status == 409 and (await response.json()) == {"error": "no_call"}


async def test_an_unknown_station_is_404(hass, live, entry, hass_client):
    await setup(hass, entry)
    client = await hass_client()
    assert (
        await client.get(f"/api/hikvision_intercom/listen/{entry.entry_id}/nope")
    ).status == 404


# --- the talk endpoint (a dashboard's microphone) ------------------------------------------------------------------------------
@pytest.fixture
def quick_heartbeat():
    """A short heartbeat, so the socket's timer is not left pending when the test ends."""
    with patch("custom_components.hikvision_intercom.views.TALK_HEARTBEAT", 0.05):
        yield


async def talk_url(hass, entry):
    subentry_id = next(iter(entry.subentries))
    return f"/api/hikvision_intercom/talk/{entry.entry_id}/{subentry_id}"


async def test_the_talk_endpoint_sends_a_dashboards_speech_into_the_call(
    hass, live, entry, hass_client
):
    await setup(hass, entry)
    client = await hass_client()
    socket = await client.ws_connect(await talk_url(hass, entry))
    for start in range(0, 6400, 640):
        await socket.send_bytes(tone(0.2)[start : start + 640])
    await socket.close()
    for _ in range(40):
        if live.talked:
            break
        await asyncio.sleep(0.05)
    assert b"".join(live.talked)  # mu-law frames reached the station
    assert len(b"".join(live.talked)) % 160 == 0


async def test_the_talk_endpoint_says_why_when_there_is_no_call(
    hass, live, entry, hass_client, quick_heartbeat
):
    live.call = "idle"
    await setup(hass, entry)
    client = await hass_client()
    socket = await client.ws_connect(await talk_url(hass, entry))
    assert (await socket.receive_json()) == {"error": "no_call"}
    assert live.talk_requests == 0
    assert (await socket.receive()).type.name in {"CLOSE", "CLOSED"}
    await socket.close()
    await asyncio.sleep(0.15)


async def test_the_talk_endpoint_is_open_to_every_signed_in_user(
    hass, live, entry, hass_client, hass_read_only_access_token
):
    await setup(hass, entry)
    client = await hass_client(hass_read_only_access_token)
    socket = await client.ws_connect(await talk_url(hass, entry))
    await socket.send_bytes(tone(0.2))
    await socket.close()
    for _ in range(40):
        if live.talked:
            break
        await asyncio.sleep(0.05)
    assert live.talked


async def test_the_talk_endpoint_needs_a_login(hass, live, entry, hass_client_no_auth):
    await setup(hass, entry)
    client = await hass_client_no_auth()
    assert (await client.get(await talk_url(hass, entry))).status == 401


async def test_an_unknown_station_cannot_be_talked_to(hass, live, entry, hass_client):
    await setup(hass, entry)
    client = await hass_client()
    response = await client.get(f"/api/hikvision_intercom/talk/{entry.entry_id}/nope")
    assert response.status == 404


async def test_a_second_talker_is_told_the_line_is_busy(
    hass, live, entry, hass_client, quick_heartbeat
):
    await setup(hass, entry)
    client = await hass_client()
    first = await client.ws_connect(await talk_url(hass, entry))
    await first.send_bytes(tone(0.05))
    for _ in range(
        100
    ):  # the first talker holds the line once the station has its request
        if live.talk_requests:
            break
        await asyncio.sleep(0.05)
    assert live.talk_requests == 1
    second = await client.ws_connect(await talk_url(hass, entry))
    assert (await second.receive_json()) == {"error": "busy"}
    await second.close()
    await first.close()
    await asyncio.sleep(0.15)


async def test_a_message_larger_than_the_limit_ends_the_talk(
    hass, live, entry, hass_client
):
    await setup(hass, entry)
    client = await hass_client()
    socket = await client.ws_connect(await talk_url(hass, entry))
    await socket.send_bytes(b"\x00" * (64 * 1024 + 1))
    message = await socket.receive()
    assert message.type.name in {"CLOSE", "CLOSING", "CLOSED"} or message.data in (
        1009,
        None,
    )
    await socket.close()


async def test_the_call_status_sensor_publishes_where_to_listen_and_talk(
    hass, live, entry
):
    await setup(hass, entry)
    state = hass.states.get("sensor.ds_kh6320_wte1_call_status")
    subentry_id = next(iter(entry.subentries))
    ids = f"{entry.entry_id}/{subentry_id}"
    assert state.attributes["listen_path"] == f"/api/hikvision_intercom/listen/{ids}"
    assert state.attributes["talk_path"] == f"/api/hikvision_intercom/talk/{ids}"


# --- through Home Assistant's own Assist pipeline --------------------------------------------------------------------------------
async def test_the_door_intercom_assistant_relays_speech_end_to_end(
    hass, live, entry, monkeypatch
):
    monkeypatch.setattr(call_audio, "FRAME_SECONDS", 0.0005)
    await setup(hass, entry)
    assert await async_setup_component(hass, "assist_pipeline", {})
    registry = er.async_get(hass)
    stt_id = next(
        e.entity_id
        for e in registry.entities.values()
        if e.platform == DOMAIN and e.domain == "stt"
    )
    conv_id = next(
        e.entity_id
        for e in registry.entities.values()
        if e.platform == DOMAIN and e.domain == "conversation"
    )
    pipeline = await hass.data[KEY_ASSIST_PIPELINE].pipeline_store.async_create_item(
        {
            "name": "Door intercom",
            "language": "en",
            "conversation_engine": conv_id,
            "conversation_language": "en",
            "stt_engine": stt_id,
            "stt_language": "en",
            "tts_engine": None,
            "tts_language": None,
            "tts_voice": None,
            "wake_word_entity": None,
            "wake_word_id": None,
        }
    )
    events = []
    await assist_pipeline.async_pipeline_from_audio_stream(
        hass,
        context=Context(),
        event_callback=events.append,
        stt_metadata=METADATA,
        stt_stream=mic(tone(0.5)),
        pipeline_id=pipeline.id,
        end_stage=PipelineStage.INTENT,
    )
    errors = [e.data for e in events if e.type == PipelineEventType.ERROR]
    assert not errors, errors
    stt_end = next(e for e in events if e.type == PipelineEventType.STT_END)
    assert stt_end.data["stt_output"]["text"] == SENT_TEXT
    intent_end = next(e for e in events if e.type == PipelineEventType.INTENT_END)
    assert (
        intent_end.data["intent_output"]["response"]["speech"]["plain"]["speech"]
        == "Sent"
    )
    assert len(b"".join(live.talked)) >= 24 * 160
