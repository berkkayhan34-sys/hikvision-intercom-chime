"""Thin ctypes wrapper over the few Hikvision HCNetSDK calls this bridge needs.

The struct layouts and signatures follow Hikvision's published Device Network
SDK programming manual. ``NET_DVR_Login_V30`` is used because its device-info
struct contains only BYTE/WORD fields, so its layout is identical on every
supported ABI. Error text never includes host, user name or password.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import (
    POINTER, Structure, addressof, byref, c_char_p, c_int, c_ubyte, c_uint, c_ushort,
    c_void_p, create_string_buffer, sizeof,
)
import json
from pathlib import Path

_SERIAL_LEN = 48
_CALL_STATUS_PATH = "/ISAPI/VideoIntercom/callStatus?format=json"
_ISAPI_TIMEOUT_MS = 5000
_RESPONSE_BYTES = 64 * 1024
_STATUS_BYTES = 4 * 1024


class SdkError(RuntimeError):
    """SDK failure carrying only the numeric SDK error code."""

    def __init__(self, action: str, code: int) -> None:
        self.action, self.code = action, code
        super().__init__(f"{action} failed (SDK error {code})")


class DeviceInfoV30(Structure):
    _fields_ = [
        ("serial", c_ubyte * _SERIAL_LEN),
        ("alarm_in", c_ubyte), ("alarm_out", c_ubyte), ("disks", c_ubyte),
        ("dvr_type", c_ubyte), ("channels", c_ubyte), ("start_channel", c_ubyte),
        ("audio_channels", c_ubyte), ("ip_channels", c_ubyte), ("zero_channels", c_ubyte),
        ("main_proto", c_ubyte), ("sub_proto", c_ubyte), ("support", c_ubyte),
        ("support1", c_ubyte), ("support2", c_ubyte), ("device_type", c_ushort),
        ("support3", c_ubyte), ("multi_stream_proto", c_ubyte), ("start_d_channel", c_ubyte),
        ("start_d_talk_channel", c_ubyte), ("high_d_channels", c_ubyte),
        ("support4", c_ubyte), ("language", c_ubyte), ("voice_in_channels", c_ubyte),
        ("start_voice_in_channel", c_ubyte), ("reserved", c_ubyte * 2),
        ("mirror_channels", c_ubyte), ("start_mirror_channel", c_ushort),
        # The SDK header ends the struct with reserved bytes. Keep generous slack so
        # a firmware or SDK revision that writes into them can never overrun us.
        ("reserved_tail", c_ubyte * 32),
    ]


class XmlConfigInput(Structure):
    """NET_DVR_XML_CONFIG_INPUT: an ISAPI request tunnelled over the SDK port."""

    _fields_ = [
        ("size", c_uint), ("request_url", c_void_p), ("request_url_length", c_uint),
        ("body", c_void_p), ("body_length", c_uint), ("receive_timeout", c_uint),
        ("force_encrypt", c_ubyte), ("reserved", c_ubyte * 31),
    ]


class XmlConfigOutput(Structure):
    """NET_DVR_XML_CONFIG_OUTPUT."""

    _fields_ = [
        ("size", c_uint), ("response", c_void_p), ("response_size", c_uint),
        ("returned_size", c_uint), ("status", c_void_p), ("status_size", c_uint),
        ("reserved", c_ubyte * 31),
    ]


# fVoiceDataCallBack(voice handle, buffer, size, audio flag, user data); the flag is 0 for audio the SDK collected locally
# (there is none: audio is only sent from here) and non-zero for audio the device sends back, which for a call is the door.
_VOICE_CALLBACK = ctypes.CFUNCTYPE(None, c_int, c_void_p, c_uint, c_ubyte, c_void_p)
_LOGGER = logging.getLogger(__name__)


class Sdk:
    """One loaded library; every method maps one SDK call and raises SdkError."""

    def __init__(self, lib) -> None:
        self._lib = lib
        self._voice_callbacks: dict[int, object] = {}  # kept alive until the voice channel stops
        lib.NET_DVR_Login_V30.argtypes = [c_char_p, c_ushort, c_char_p, c_char_p,
                                          POINTER(DeviceInfoV30)]
        lib.NET_DVR_Login_V30.restype = c_int
        lib.NET_DVR_Logout_V30.argtypes = [c_int]
        lib.NET_DVR_STDXMLConfig.argtypes = [
            c_int, POINTER(XmlConfigInput), POINTER(XmlConfigOutput)]
        lib.NET_DVR_StartVoiceCom_MR_V30.argtypes = [c_int, c_uint, c_void_p, c_void_p]
        lib.NET_DVR_StartVoiceCom_MR_V30.restype = c_int
        lib.NET_DVR_VoiceComSendData.argtypes = [c_int, c_void_p, c_uint]
        lib.NET_DVR_StopVoiceCom.argtypes = [c_int]

    @classmethod
    def load(cls, library_directory: Path) -> "Sdk":
        # The SDK resolves its component libraries (HCNetSDKCom) next to itself.
        return cls(ctypes.CDLL(str(library_directory / "libhcnetsdk.so")))

    def _fail(self, action: str) -> SdkError:
        return SdkError(action, self._lib.NET_DVR_GetLastError())

    def initialize(self) -> None:
        if not self._lib.NET_DVR_Init():
            raise self._fail("initialize")
        self._lib.NET_DVR_SetConnectTime(3000, 2)
        self._lib.NET_DVR_SetReconnect(10000, 1)

    def cleanup(self) -> None:
        self._lib.NET_DVR_Cleanup()

    def login(self, host: str, port: int, username: str, password: str) -> tuple[int, str]:
        info = DeviceInfoV30()
        user_id = self._lib.NET_DVR_Login_V30(
            host.encode("ascii"), port, username.encode("utf8"), password.encode("utf8"),
            byref(info))
        if user_id < 0:
            raise self._fail("login")
        serial = bytes(info.serial).split(b"\0", 1)[0].decode("ascii", "replace")
        return user_id, serial

    def logout(self, user_id: int) -> None:
        self._lib.NET_DVR_Logout_V30(user_id)

    def _isapi_get(self, user_id: int, path: str) -> str:
        """GET an ISAPI resource through the SDK tunnel (the station has no HTTP port)."""
        request = f"GET {path}".encode("ascii")
        url = create_string_buffer(request)
        response = create_string_buffer(_RESPONSE_BYTES)
        status = create_string_buffer(_STATUS_BYTES)
        call = XmlConfigInput()
        call.size = sizeof(call)
        call.request_url, call.request_url_length = addressof(url), len(request)
        call.receive_timeout = _ISAPI_TIMEOUT_MS
        result = XmlConfigOutput()
        result.size = sizeof(result)
        result.response, result.response_size = addressof(response), _RESPONSE_BYTES
        result.status, result.status_size = addressof(status), _STATUS_BYTES
        if not self._lib.NET_DVR_STDXMLConfig(user_id, byref(call), byref(result)):
            raise self._fail("isapi request")
        return response.value.decode("utf8", "replace")

    def call_state(self, user_id: int) -> str:
        """idle | ringing | in_call | unknown, from the station's call-status resource.

        ``NET_DVR_GetDeviceStatus`` (command 16034) is rejected as unsupported by the
        DS-KH6320 firmware, so the ISAPI resource is the only dependable source.
        """
        try:
            raw = json.loads(self._isapi_get(user_id, _CALL_STATUS_PATH))
            state = str(raw["CallStatus"]["status"]).lower()
        except (ValueError, KeyError, TypeError):
            return "unknown"
        if state == "idle":
            return "idle"
        if "ring" in state:
            return "ringing"
        if "call" in state or "talk" in state:
            return "in_call"
        return "unknown"

    def start_voice(self, user_id: int, on_audio=None) -> int:
        """Open a voice channel; ``on_audio(bytes)`` receives what the device sends back (G.711), on the SDK's thread."""
        callback = None
        if on_audio is not None:
            def receive(handle, buffer, size, flag, user) -> None:
                if flag == 0 or not size or not buffer:
                    return
                try:
                    on_audio(ctypes.string_at(buffer, size))
                except Exception:  # noqa: BLE001 - nothing may propagate into the SDK's thread
                    _LOGGER.debug("device audio handler failed", exc_info=True)

            callback = _VOICE_CALLBACK(receive)
        handle = self._lib.NET_DVR_StartVoiceCom_MR_V30(user_id, 1, callback, None)
        if handle < 0:
            raise self._fail("start voice")
        if callback is not None:
            self._voice_callbacks[handle] = callback
        return handle

    def send_voice(self, handle: int, frame: bytes) -> None:
        buffer = create_string_buffer(frame, len(frame))
        if not self._lib.NET_DVR_VoiceComSendData(handle, buffer, len(frame)):
            raise self._fail("send voice")

    def stop_voice(self, handle: int) -> None:
        self._lib.NET_DVR_StopVoiceCom(handle)
        self._voice_callbacks.pop(handle, None)
