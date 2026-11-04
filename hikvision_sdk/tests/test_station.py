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
                                "call_status": "idle", "broadcast": "idle"}


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
