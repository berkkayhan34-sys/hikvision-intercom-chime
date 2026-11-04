import threading
import time

import pytest

from hik_sdk_bridge import station as station_module
from hik_sdk_bridge.config import StationConfig
from hik_sdk_bridge.station import FRAME_BYTES, Station, StationError

from fakes import FakeSdk

PCM = b"\x10\x00" * (FRAME_BYTES * 5 + 30)  # 5.19 frames of mu-law


@pytest.fixture(autouse=True)
def fast_frames(monkeypatch):
    monkeypatch.setattr(station_module, "FRAME_SECONDS", 0.001)


def make(sdk=None):
    sdk = sdk or FakeSdk()
    return Station(StationConfig("hall", "192.168.0.50", 8000, "u", "p"), sdk), sdk


def wait_idle(station, timeout=3):
    end = time.monotonic() + timeout
    while station.status()["broadcast"] == "playing" and time.monotonic() < end:
        time.sleep(0.005)


def test_status_when_connected():
    station, sdk = make()
    assert station.status() == {"name": "hall", "connected": True, "serial": "SERIAL123",
                                "call_status": "idle", "broadcast": "idle",
                                "call_audio": "idle"}


def test_status_when_login_fails_does_not_raise():
    sdk = FakeSdk()
    sdk.login_error = 7
    station, _ = make(sdk)
    status = station.status()
    assert status["connected"] is False and status["serial"] is None
    assert status["call_status"] == "unknown"


def test_session_is_reused_and_dropped_on_status_failure():
    station, sdk = make()
    station.status(), station.status()
    assert sdk.logins == 1
    sdk.call_error = 3
    assert station.status()["connected"] is False
    assert sdk.logouts == [7]
    sdk.call_error = None
    assert station.status()["connected"] is True
    assert sdk.logins == 2


def test_broadcast_streams_whole_padded_frames_then_stops_voice():
    station, sdk = make()
    duration = station.start_broadcast(PCM)
    wait_idle(station)
    assert duration == pytest.approx((FRAME_BYTES * 5 + 30) / 8000, rel=1e-3)
    assert len(sdk.frames) == 6
    assert all(len(frame) == FRAME_BYTES for frame in sdk.frames)
    assert sdk.frames[-1][30:] == bytes([0xFF]) * (FRAME_BYTES - 30)
    assert sdk.voice_stopped == [99]


@pytest.mark.parametrize("state", ["ringing", "in_call", "unknown"])
def test_broadcast_refused_unless_call_idle(state):
    station, sdk = make()
    sdk.call = state
    with pytest.raises(StationError) as error:
        station.start_broadcast(PCM)
    assert error.value.code == "call_active"
    assert sdk.voice_started == 0


def test_broadcast_refused_when_offline():
    sdk = FakeSdk()
    sdk.login_error = 5
    station, _ = make(sdk)
    with pytest.raises(StationError) as error:
        station.start_broadcast(PCM)
    assert error.value.code == "station_offline"


def test_voice_start_failure_drops_session_and_reports_offline():
    station, sdk = make()
    sdk.voice_error = 9
    with pytest.raises(StationError) as error:
        station.start_broadcast(PCM)
    assert error.value.code == "station_offline"
    assert sdk.logouts == [7]


@pytest.mark.parametrize("pcm", [b"", b"\x00" * 100, b"\x00" * (FRAME_BYTES * 2 + 1),
                                  b"\x00" * (station_module.MAX_PCM_BYTES + 2)])
def test_invalid_audio_is_rejected_before_touching_the_device(pcm):
    station, sdk = make()
    with pytest.raises(StationError) as error:
        station.start_broadcast(pcm)
    assert error.value.code == "invalid_audio"
    assert sdk.logins == 0 and sdk.voice_started == 0


def test_second_broadcast_while_playing_is_busy(monkeypatch):
    monkeypatch.setattr(station_module, "FRAME_SECONDS", 0.02)
    station, sdk = make()
    station.start_broadcast(PCM)
    with pytest.raises(StationError) as error:
        station.start_broadcast(PCM)
    assert error.value.code == "busy"
    wait_idle(station)
    assert sdk.voice_started == 1


def test_stop_interrupts_playback_and_releases_voice(monkeypatch):
    monkeypatch.setattr(station_module, "FRAME_SECONDS", 0.05)
    station, sdk = make()
    station.start_broadcast(PCM)
    time.sleep(0.02)
    assert station.stop_broadcast() is True
    assert station.status()["broadcast"] == "idle"
    assert len(sdk.frames) < 6
    assert sdk.voice_stopped == [99]


def test_stop_when_idle_reports_nothing_to_stop():
    station, sdk = make()
    assert station.stop_broadcast() is False
    assert sdk.voice_stopped == []


def test_send_failure_ends_the_broadcast_and_releases_voice():
    station, sdk = make()
    sdk.send_error_after = 2
    station.start_broadcast(PCM)
    wait_idle(station)
    assert len(sdk.frames) == 2
    assert sdk.voice_stopped == [99]


def test_concurrent_starts_allow_exactly_one(monkeypatch):
    monkeypatch.setattr(station_module, "FRAME_SECONDS", 0.02)
    station, sdk = make()
    results = []

    def attempt():
        try:
            station.start_broadcast(PCM)
            results.append("ok")
        except StationError as error:
            results.append(error.code)

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    wait_idle(station)
    assert results.count("ok") == 1 and results.count("busy") == 7
    assert sdk.voice_started == 1


def test_close_stops_playback_and_logs_out(monkeypatch):
    monkeypatch.setattr(station_module, "FRAME_SECONDS", 0.05)
    station, sdk = make()
    station.start_broadcast(PCM)
    station.close()
    assert sdk.voice_stopped == [99] and sdk.logouts == [7]


# --- call audio: talk to the door, listen to it ----------------------------------------------------------------
FRAME = bytes(range(160))


def in_call(sdk):
    sdk.call = "in_call"
    return sdk


def test_talking_without_a_call_fails_before_reading_any_audio():
    station, sdk = make()
    consumed = []

    def chunks():
        consumed.append(1)
        yield FRAME

    with pytest.raises(StationError) as info:
        station.talk(chunks())
    assert info.value.code == "no_call" and consumed == [] and sdk.voice_started == 0


@pytest.mark.parametrize("state", ["ringing", "idle", "unknown"])
def test_audio_is_only_opened_for_an_answered_call(state):
    station, sdk = make()
    sdk.call = state
    with pytest.raises(StationError) as info:
        station.talk([FRAME])
    assert info.value.code == "no_call"


def test_talk_sends_whole_frames_pads_the_last_and_closes_the_channel():
    station, sdk = make(in_call(FakeSdk()))
    frames = station.talk([FRAME[:100], FRAME[100:] + FRAME, FRAME[:30]])
    assert frames == 3
    assert sdk.frames == [FRAME, FRAME, FRAME[:30] + bytes([0xFF]) * 130]
    assert sdk.voice_stopped == [99] and station.status()["call_audio"] == "idle"


def test_talk_is_paced_to_real_time_not_dumped(monkeypatch):
    monkeypatch.setattr(station_module, "FRAME_SECONDS", 0.02)
    monkeypatch.setattr(station_module, "TALK_LEAD_SECONDS", 0.1)
    station, _ = make(in_call(FakeSdk()))
    started = time.monotonic()
    station.talk([FRAME * 25])  # 0.5 s of audio at once
    assert time.monotonic() - started > 0.3


def test_a_station_that_stops_taking_audio_ends_the_talk_and_frees_the_channel():
    sdk = in_call(FakeSdk())
    sdk.send_error_after = 2
    station, _ = make(sdk)
    with pytest.raises(StationError) as info:
        station.talk([FRAME * 5])
    assert info.value.code == "station_offline" and sdk.voice_stopped == [99]


def test_the_call_ending_during_a_talk_stops_it():
    sdk = in_call(FakeSdk())
    station, _ = make(sdk)

    def chunks():
        yield FRAME
        sdk.call = "idle"
        station.status()  # the integration's poll notices the call is over
        yield FRAME * 3

    with pytest.raises(StationError) as info:
        station.talk(chunks())
    assert info.value.code == "no_call"


def test_listening_receives_the_door_audio_in_order_and_releases_on_close():
    station, sdk = make(in_call(FakeSdk()))
    listener = station.listen()
    assert station.status()["call_audio"] == "active"
    sdk.deliver(b"a" * 160)
    sdk.deliver(b"b" * 160)
    received = iter(listener)
    assert [next(received), next(received)] == [b"a" * 160, b"b" * 160]
    listener.close()
    assert sdk.voice_stopped == [99] and station.status()["call_audio"] == "idle"
    listener.close()  # safe to repeat
    assert sdk.voice_stopped == [99]


def test_listening_ends_when_the_call_ends():
    sdk = in_call(FakeSdk())
    station, _ = make(sdk)
    listener = station.listen()
    sdk.call = "idle"
    station.status()
    assert list(listener) == []
    listener.close()


def test_listening_without_a_call_is_refused():
    station, _ = make()
    with pytest.raises(StationError) as info:
        station.listen()
    assert info.value.code == "no_call"


def test_talking_and_listening_share_one_channel_that_closes_with_the_last_user():
    station, sdk = make(in_call(FakeSdk()))
    listener = station.listen()
    station.talk([FRAME])
    assert sdk.voice_started == 1 and sdk.voice_stopped == []  # the listener keeps it open
    listener.close()
    assert sdk.voice_stopped == [99]


def test_a_slow_listener_loses_old_audio_instead_of_growing_without_limit(monkeypatch):
    monkeypatch.setattr(station_module, "LISTEN_QUEUE_FRAMES", 3)
    station, sdk = make(in_call(FakeSdk()))
    listener = station.listen()
    for n in range(10):
        sdk.deliver(bytes([n]) * 160)
    got = [next(iter(listener)) for _ in range(3)]
    assert [chunk[0] for chunk in got] == [7, 8, 9]
    listener.close()


def test_the_voice_channel_is_not_opened_while_a_broadcast_plays():
    station, sdk = make()
    station.start_broadcast(PCM * 20)
    sdk.call = "in_call"
    with pytest.raises(StationError) as info:
        station.talk([FRAME])
    assert info.value.code == "busy"
    station.stop_broadcast()


def test_stop_call_audio_ends_our_channel_only():
    station, sdk = make(in_call(FakeSdk()))
    listener = station.listen()
    assert station.stop_call_audio() is True and sdk.voice_stopped == [99]
    assert station.stop_call_audio() is False
    listener.close()
