import http.client
import json
import threading

import pytest

from hik_sdk_bridge import station as station_module
from hik_sdk_bridge.config import StationConfig
from hik_sdk_bridge.server import BridgeServer
from hik_sdk_bridge.station import FRAME_BYTES, Station

from fakes import FakeSdk

TOKEN = "k" * 40
PCM = b"\x10\x00" * (FRAME_BYTES * 3)


@pytest.fixture
def api(monkeypatch, socket_enabled):
    monkeypatch.setattr(station_module, "FRAME_SECONDS", 0.001)
    sdk = FakeSdk()
    stations = {"hall": Station(StationConfig("hall", "192.168.0.50", 8000, "u", "p"), sdk)}
    server = BridgeServer(("127.0.0.1", 0), TOKEN, stations)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    port = server.server_address[1]

    def call(method, path, body=None, token=TOKEN, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        send = dict(headers or {})
        if token:
            send["Authorization"] = f"Bearer {token}"
        connection.request(method, path, body=body, headers=send)
        response = connection.getresponse()
        data = json.loads(response.read() or b"{}")
        connection.close()
        return response.status, data

    call.port = port
    yield call, sdk
    server.shutdown()
    serving.join()
    server.server_close()


def test_health_needs_no_token_and_leaks_nothing_else(api):
    call, _ = api
    status, data = call("GET", "/v1/health", token=None)
    assert status == 200 and set(data) == {"status", "version"}


@pytest.mark.parametrize("token", [None, "", "wrong", TOKEN + "x", TOKEN[:-1]])
def test_wrong_or_missing_token_is_401_everywhere(api, token):
    call, sdk = api
    for method, path in [("GET", "/v1/stations/hall"), ("POST", "/v1/stations/hall/broadcast"),
                         ("DELETE", "/v1/stations/hall/broadcast"), ("GET", "/v1/stations/nope")]:
        assert call(method, path, body=b"" if method == "POST" else None, token=token)[0] == 401
    assert sdk.logins == 0 and sdk.voice_started == 0


def test_status_endpoint(api):
    call, _ = api
    status, data = call("GET", "/v1/stations/hall")
    assert status == 200 and data["connected"] and data["call_status"] == "idle"


@pytest.mark.parametrize("method, path", [
    ("GET", "/v1/stations/missing"), ("GET", "/v1/stations/hall/broadcast"),
    ("POST", "/v1/stations/hall"), ("DELETE", "/v1/stations/hall"),
    ("GET", "/v1/stations/../hall"), ("GET", "/v1/other"), ("GET", "/v1/stations/Hall"),
])
def test_unknown_routes_are_404(api, method, path):
    call, _ = api
    assert call(method, path, body=b"" if method == "POST" else None)[0] == 404


def test_broadcast_accepts_pcm_and_reports_duration(api):
    call, sdk = api
    status, data = call("POST", "/v1/stations/hall/broadcast", PCM,
                        headers={"Content-Type": "application/octet-stream"})
    assert status == 202 and data["duration"] == pytest.approx(0.06)


def test_broadcast_conflict_and_offline_codes(api):
    call, sdk = api
    sdk.call = "ringing"
    assert call("POST", "/v1/stations/hall/broadcast", PCM) == (409, {"error": "call_active"})
    sdk.call, sdk.login_error = "idle", 4
    # Session is already up from the previous status check; force a fresh failure.
    sdk.call_error = 4
    call("GET", "/v1/stations/hall")
    assert call("POST", "/v1/stations/hall/broadcast", PCM) == (503, {"error": "station_offline"})


def test_invalid_body_is_400(api):
    call, sdk = api
    assert call("POST", "/v1/stations/hall/broadcast", b"\x00\x00") == (400, {"error": "invalid_audio"})
    assert sdk.voice_started == 0


def test_oversized_declared_length_is_413_without_reading_the_body(api):
    call, sdk = api
    port = call.port
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.putrequest("POST", "/v1/stations/hall/broadcast")
    connection.putheader("Authorization", f"Bearer {TOKEN}")
    connection.putheader("Content-Length", str(station_module.MAX_PCM_BYTES + 2))
    connection.endheaders()  # the body is never sent
    response = connection.getresponse()
    assert response.status == 413
    connection.close()
    assert sdk.voice_started == 0


def test_missing_content_length_is_411(api):
    call, _ = api
    connection = http.client.HTTPConnection("127.0.0.1", call.port, timeout=5)
    connection.putrequest("POST", "/v1/stations/hall/broadcast")
    connection.putheader("Authorization", f"Bearer {TOKEN}")
    connection.endheaders()
    assert connection.getresponse().status == 411
    connection.close()


def test_delete_is_idempotent(api):
    call, _ = api
    assert call("DELETE", "/v1/stations/hall/broadcast") == (200, {"stopped": False})
