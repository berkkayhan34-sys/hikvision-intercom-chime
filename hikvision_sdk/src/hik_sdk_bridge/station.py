"""One indoor station: SDK session, call-state gate and paced audio broadcast."""

from __future__ import annotations

import logging
import threading
import time

from .config import StationConfig
from .g711 import pcm16le_to_mulaw
from .sdk import Sdk, SdkError

_LOGGER = logging.getLogger(__name__)

SAMPLE_RATE = 8000
FRAME_BYTES = 160  # 20 ms of 8 kHz mu-law
FRAME_SECONDS = FRAME_BYTES / SAMPLE_RATE
SILENCE = 0xFF  # mu-law zero
MIN_PCM_BYTES = 2 * FRAME_BYTES
MAX_PCM_BYTES = 1_048_576  # ~65 s at 8 kHz PCM16; the integration sends at most 60 s


class StationError(Exception):
    """Fixed machine-readable code; the server maps it to an HTTP status."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class Station:
    def __init__(self, config: StationConfig, sdk: Sdk) -> None:
        self.name = config.name
        self._config = config
        self._sdk = sdk
        self._lock = threading.RLock()
        self._user_id: int | None = None
        self._serial: str | None = None
        self._broadcast: threading.Thread | None = None
        self._stop = threading.Event()

    # -- session ---------------------------------------------------------
    def _ensure_session(self) -> bool:
        with self._lock:
            if self._user_id is not None:
                return True
            try:
                self._user_id, self._serial = self._sdk.login(
                    self._config.host, self._config.port,
                    self._config.username, self._config.password)
            except SdkError as error:
                _LOGGER.warning("Station %s login failed: %s", self.name, error)
                return False
            _LOGGER.info("Station %s connected", self.name)
            return True

    def _drop_session(self) -> None:
        with self._lock:
            if self._user_id is not None:
                self._sdk.logout(self._user_id)
                self._user_id = None

    def close(self) -> None:
        self.stop_broadcast()
        self._drop_session()

    # -- state -----------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            connected = self._ensure_session()
            call_state = "unknown"
            if connected:
                try:
                    call_state = self._sdk.call_state(self._user_id)
                except SdkError as error:
                    _LOGGER.warning("Station %s call status failed: %s", self.name, error)
                    self._drop_session()
                    connected = False
            return {
                "name": self.name,
                "connected": connected,
                "serial": self._serial if connected else None,
                "call_status": call_state,
                "broadcast": "playing" if self._playing() else "idle",
            }

    def _playing(self) -> bool:
        return self._broadcast is not None and self._broadcast.is_alive()

    # -- broadcast -------------------------------------------------------
    def start_broadcast(self, pcm: bytes) -> float:
        """Begin streaming 8 kHz mono PCM16; returns the duration in seconds."""
        if not MIN_PCM_BYTES <= len(pcm) <= MAX_PCM_BYTES or len(pcm) % 2:
            raise StationError("invalid_audio")
        mulaw = pcm16le_to_mulaw(pcm)
        with self._lock:
            if self._playing():
                raise StationError("busy")
            if not self._ensure_session():
                raise StationError("station_offline")
            try:
                if self._sdk.call_state(self._user_id) != "idle":
                    raise StationError("call_active")
                handle = self._sdk.start_voice(self._user_id)
            except SdkError as error:
                _LOGGER.warning("Station %s broadcast start failed: %s", self.name, error)
                self._drop_session()
                raise StationError("station_offline") from None
            self._stop.clear()
            self._broadcast = threading.Thread(
                target=self._stream, args=(handle, mulaw), daemon=True,
                name=f"broadcast-{self.name}")
            self._broadcast.start()
        return len(mulaw) / SAMPLE_RATE

    def _stream(self, handle: int, mulaw: bytes) -> None:
        try:
            deadline = time.monotonic()
            for offset in range(0, len(mulaw), FRAME_BYTES):
                if self._stop.is_set():
                    break
                frame = mulaw[offset:offset + FRAME_BYTES]
                frame += bytes([SILENCE]) * (FRAME_BYTES - len(frame))
                self._sdk.send_voice(handle, frame)
                deadline += FRAME_SECONDS
                if self._stop.wait(max(0.0, deadline - time.monotonic())):
                    break
        except SdkError as error:
            _LOGGER.warning("Station %s broadcast interrupted: %s", self.name, error)
        finally:
            self._sdk.stop_voice(handle)
            _LOGGER.info("Station %s broadcast finished", self.name)

    def stop_broadcast(self) -> bool:
        """Stop our own broadcast only; returns whether one was running."""
        with self._lock:
            thread = self._broadcast
        if thread is None or not thread.is_alive():
            return False
        self._stop.set()
        thread.join(timeout=5)
        return True
