import ctypes
import json
from ctypes import memmove, sizeof

import pytest

from hik_sdk_bridge.sdk import (
    DeviceInfoV30,
    Sdk,
    SdkError,
    XmlConfigInput,
    XmlConfigOutput,
)


class Lib:
    """Records calls; every NET_DVR_* attribute is a plain callable."""

    def __init__(self):
        self.calls = []
        self.login_result = 5
        self.last_error = 0
        self.call_status_value = "idle"
        self.status_ok = 1
        self.raw_response = None
        self.voice_handle = 3
        self.send_ok = 1
        self.serial = b"ABC123"

    def __getattr__(self, name):
        if not name.startswith("NET_DVR_"):
            raise AttributeError(name)
        return _Function(self, name)


class _Function:
    def __init__(self, lib, name):
        self.lib, self.name = lib, name
        self.argtypes = self.restype = None

    def __call__(self, *args):
        lib = self.lib
        lib.calls.append((self.name, args))
        if self.name == "NET_DVR_Login_V30":
            info = args[4]._obj
            for index, value in enumerate(lib.serial):
                info.serial[index] = value
            return lib.login_result
        if self.name == "NET_DVR_GetLastError":
            return lib.last_error
        if self.name == "NET_DVR_Init":
            return 1
        if self.name == "NET_DVR_STDXMLConfig":
            request = ctypes.string_at(
                args[1]._obj.request_url, args[1]._obj.request_url_length
            )
            lib.requests = [*getattr(lib, "requests", []), request]
            body = lib.raw_response
            if body is None:
                body = json.dumps(
                    {"CallStatus": {"status": lib.call_status_value}}
                ).encode()
            memmove(args[2]._obj.response, body, len(body))
            return lib.status_ok
        if self.name == "NET_DVR_StartVoiceCom_MR_V30":
            return lib.voice_handle
        if self.name == "NET_DVR_VoiceComSendData":
            return lib.send_ok
        return 1


def test_struct_layouts_match_the_sdk_headers():
    # Official fields end at byte 78; the struct must reserve at least the SDK's tail.
    assert sizeof(DeviceInfoV30) >= 80
    assert XmlConfigInput.request_url.offset == 8 and XmlConfigInput.body.offset == 24
    assert sizeof(XmlConfigInput) == 72 and sizeof(XmlConfigOutput) == 72
    assert (
        DeviceInfoV30.device_type.offset == 48 + 14
    )  # after serial and 14 BYTE fields


def test_login_returns_user_id_and_serial_and_sends_credentials_as_bytes():
    lib = Lib()
    user_id, serial = Sdk(lib).login("192.168.0.9", 8000, "admin", "pw")
    assert (user_id, serial) == (5, "ABC123")
    _name, args = next(call for call in lib.calls if call[0] == "NET_DVR_Login_V30")
    assert args[:4] == (b"192.168.0.9", 8000, b"admin", b"pw")


def test_failed_login_carries_only_the_error_code():
    lib = Lib()
    lib.login_result, lib.last_error = -1, 1
    with pytest.raises(SdkError) as error:
        Sdk(lib).login("192.168.0.9", 8000, "admin", "very-secret")
    assert error.value.code == 1
    assert "very-secret" not in str(error.value) and "192.168" not in str(error.value)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("idle", "idle"),
        ("IDLE", "idle"),
        ("ring", "ringing"),
        ("ringing", "ringing"),
        ("onCall", "in_call"),
        ("talking", "in_call"),
        ("weird", "unknown"),
        ("", "unknown"),
    ],
)
def test_call_state_mapping(raw, expected):
    lib = Lib()
    lib.call_status_value = raw
    assert Sdk(lib).call_state(5) == expected
    assert lib.requests == [b"GET /ISAPI/VideoIntercom/callStatus?format=json"]


@pytest.mark.parametrize("body", [b"not json", b"{}", b'{"CallStatus": 3}', b""])
def test_unparsable_call_state_is_unknown_never_idle(body):
    lib = Lib()
    lib.raw_response = body
    assert Sdk(lib).call_state(5) == "unknown"


def test_call_state_failure_raises():
    lib = Lib()
    lib.status_ok, lib.last_error = 0, 23
    with pytest.raises(SdkError) as error:
        Sdk(lib).call_state(5)
    assert error.value.code == 23


def test_voice_session_calls_and_failures():
    lib = Lib()
    sdk = Sdk(lib)
    assert sdk.start_voice(5) == 3
    sdk.send_voice(3, b"\xff" * 160)
    sdk.stop_voice(3)
    sent = next(args for name, args in lib.calls if name == "NET_DVR_VoiceComSendData")
    assert sent[0] == 3 and sent[2] == 160
    lib.send_ok, lib.last_error = 0, 12
    with pytest.raises(SdkError):
        sdk.send_voice(3, b"\xff" * 160)
    lib.voice_handle = -1
    with pytest.raises(SdkError):
        sdk.start_voice(5)


def test_initialize_sets_timeouts():
    lib = Lib()
    Sdk(lib).initialize()
    names = [name for name, _ in lib.calls]
    assert names[:3] == [
        "NET_DVR_Init",
        "NET_DVR_SetConnectTime",
        "NET_DVR_SetReconnect",
    ]


def test_device_audio_reaches_the_handler_and_local_audio_does_not():
    lib = Lib()
    sdk = Sdk(lib)
    received = []
    assert sdk.start_voice(5, received.append) == 3
    callback = lib.calls[-1][1][2]
    assert callback is not None
    data = ctypes.create_string_buffer(b"\x01\x02\x03", 3)
    callback(3, ctypes.addressof(data), 3, 1, None)  # from the device
    callback(3, ctypes.addressof(data), 3, 0, None)  # collected locally: ignored
    callback(3, None, 3, 1, None)  # no buffer: ignored
    callback(3, ctypes.addressof(data), 0, 1, None)  # no data: ignored
    assert received == [b"\x01\x02\x03"]


def test_a_failing_handler_never_raises_into_the_sdk_thread():
    lib = Lib()
    sdk = Sdk(lib)

    def broken(data):
        raise RuntimeError("handler bug")

    sdk.start_voice(5, broken)
    data = ctypes.create_string_buffer(b"\x01", 1)
    lib.calls[-1][1][2](3, ctypes.addressof(data), 1, 1, None)  # must not raise


def test_the_callback_is_kept_alive_until_the_channel_stops():
    lib = Lib()
    sdk = Sdk(lib)
    sdk.start_voice(5, lambda data: None)
    assert 3 in sdk._voice_callbacks
    sdk.stop_voice(3)
    assert 3 not in sdk._voice_callbacks


def test_without_a_handler_no_callback_is_passed():
    lib = Lib()
    Sdk(lib).start_voice(5)
    assert lib.calls[-1][1][2] is None
