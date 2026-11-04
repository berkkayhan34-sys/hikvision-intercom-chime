"""A scriptable stand-in for the loaded SDK, used above the ctypes boundary."""

from hik_sdk_bridge.sdk import SdkError


class FakeSdk:
    def __init__(self):
        self.login_error = None
        self.call = "idle"
        self.call_error = None
        self.voice_error = None
        self.send_error_after = None
        self.logins = 0
        self.logouts = []
        self.frames = []
        self.voice_started = 0
        self.voice_stopped = []

    def login(self, host, port, username, password):
        if self.login_error:
            raise SdkError("login", self.login_error)
        self.logins += 1
        return 7, "SERIAL123"

    def logout(self, user_id):
        self.logouts.append(user_id)

    def call_state(self, user_id):
        if self.call_error:
            raise SdkError("call status", self.call_error)
        return self.call

    def start_voice(self, user_id):
        if self.voice_error:
            raise SdkError("start voice", self.voice_error)
        self.voice_started += 1
        return 99

    def send_voice(self, handle, frame):
        if self.send_error_after is not None and len(self.frames) >= self.send_error_after:
            raise SdkError("send voice", 12)
        self.frames.append(bytes(frame))

    def stop_voice(self, handle):
        self.voice_stopped.append(handle)
