"""One indoor station: SDK session, call-state gate and paced audio broadcast."""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Iterable
from contextlib import contextmanager, suppress

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
MAX_CALL_SECONDS = 600  # one call's audio session; longer than any door call
TALK_LEAD_SECONDS = 0.5  # how far ahead of real time a talker may send
LISTEN_QUEUE_FRAMES = (
    100  # about two seconds of door audio per listener; older audio is dropped
)


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
        self._call_audio: int | None = (
            None  # SDK voice handle while a call's audio is open
        )
        self._call_users = 0
        self._listeners: set[queue.Queue] = set()

    # -- session ---------------------------------------------------------
    def _ensure_session(self) -> bool:
        with self._lock:
            if self._user_id is not None:
                return True
            try:
                self._user_id, self._serial = self._sdk.login(
                    self._config.host,
                    self._config.port,
                    self._config.username,
                    self._config.password,
                )
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
        self.stop_call_audio()
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
                    _LOGGER.warning(
                        "Station %s call status failed: %s", self.name, error
                    )
                    self._drop_session()
                    connected = False
            if self._call_audio is not None and call_state != "in_call":
                self._close_call_audio()  # the call is over; free the voice channel
            return {
                "name": self.name,
                "connected": connected,
                "serial": self._serial if connected else None,
                "call_status": call_state,
                "broadcast": "playing" if self._playing() else "idle",
                "call_audio": "active" if self._call_audio is not None else "idle",
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
                _LOGGER.warning(
                    "Station %s broadcast start failed: %s", self.name, error
                )
                self._drop_session()
                raise StationError("station_offline") from None
            self._stop.clear()
            self._broadcast = threading.Thread(
                target=self._stream,
                args=(handle, mulaw),
                daemon=True,
                name=f"broadcast-{self.name}",
            )
            self._broadcast.start()
        return len(mulaw) / SAMPLE_RATE

    def _stream(self, handle: int, mulaw: bytes) -> None:
        try:
            deadline = time.monotonic()
            for offset in range(0, len(mulaw), FRAME_BYTES):
                if self._stop.is_set():
                    break
                frame = mulaw[offset : offset + FRAME_BYTES]
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

    # -- call audio: talk to the door and listen to it during an answered call -----------------------
    def _open_call_audio(self) -> None:
        """Open the voice channel of the current call; the lock is held."""
        if self._call_audio is not None:
            return
        if self._playing():
            raise StationError("busy")
        if not self._ensure_session():
            raise StationError("station_offline")
        try:
            if self._sdk.call_state(self._user_id) != "in_call":
                raise StationError("no_call")
            self._call_audio = self._sdk.start_voice(self._user_id, self._on_door_audio)
        except SdkError as error:
            _LOGGER.warning("Station %s call audio start failed: %s", self.name, error)
            self._drop_session()
            raise StationError("station_offline") from None

    def _close_call_audio(self) -> None:
        """Stop the voice channel and release every listener; the lock is held."""
        handle, self._call_audio = self._call_audio, None
        self._call_users = 0
        for listener in list(self._listeners):
            self._offer(listener, None)
        if handle is not None:
            with suppress(SdkError):
                self._sdk.stop_voice(handle)
            _LOGGER.info("Station %s call audio finished", self.name)

    @contextmanager
    def _call_session(self):
        with self._lock:
            self._open_call_audio()
            self._call_users += 1
        try:
            yield
        finally:
            with self._lock:
                self._call_users -= 1
                if self._call_users <= 0:
                    self._close_call_audio()

    @staticmethod
    def _offer(listener: queue.Queue, item) -> None:
        try:
            listener.put_nowait(item)
        except queue.Full:
            try:
                listener.get_nowait()
                listener.put_nowait(item)
            except queue.Empty, queue.Full:
                pass

    def _on_door_audio(self, data: bytes) -> None:
        """Called on the SDK's thread for every chunk of audio the door sends."""
        for listener in list(self._listeners):
            self._offer(listener, data)

    def talk(self, chunks: Iterable[bytes]) -> int:
        """Send 8 kHz mu-law audio into the answered call; returns the frames sent.

        Opens the channel before the first chunk is read, so a request made without a call fails at once. Sending is
        paced to real time (with a short lead), so a client cannot flood the station.
        """
        with self._call_session():
            handle = self._call_audio
            pending, frames, started = bytearray(), 0, time.monotonic()

            def send(frame: bytes) -> None:
                nonlocal frames
                wait = (
                    started
                    + frames * FRAME_SECONDS
                    - TALK_LEAD_SECONDS
                    - time.monotonic()
                )
                if wait > 0:
                    time.sleep(wait)
                if self._call_audio != handle:
                    raise StationError(
                        "no_call"
                    )  # the call ended while we were talking
                try:
                    self._sdk.send_voice(handle, frame)
                except SdkError as error:
                    _LOGGER.warning(
                        "Station %s call audio interrupted: %s", self.name, error
                    )
                    with self._lock:
                        self._close_call_audio()
                    raise StationError("station_offline") from None
                frames += 1

            for chunk in chunks:
                pending += chunk
                while len(pending) >= FRAME_BYTES:
                    send(bytes(pending[:FRAME_BYTES]))
                    del pending[:FRAME_BYTES]
                if time.monotonic() - started > MAX_CALL_SECONDS:
                    break
            if pending:
                send(bytes(pending) + bytes([SILENCE]) * (FRAME_BYTES - len(pending)))
            return frames

    def listen(self) -> CallListener:
        """Start receiving the door's audio; raises like ``talk`` when there is no call."""
        listener = queue.Queue(maxsize=LISTEN_QUEUE_FRAMES)
        session = self._call_session()
        session.__enter__()
        with self._lock:
            self._listeners.add(listener)
        return CallListener(self, listener, session)

    def stop_call_audio(self) -> bool:
        """End our own call-audio channel (talk and listen); returns whether one was open."""
        with self._lock:
            was_open = self._call_audio is not None
            self._close_call_audio()
        return was_open


class CallListener:
    """Iterate to receive door audio; always ``close()`` (the server does, on any exit)."""

    def __init__(self, station: Station, listener: queue.Queue, session) -> None:
        self._station, self._listener, self._session = station, listener, session
        self._closed = False

    def __iter__(self):
        started = time.monotonic()
        while not self._closed and time.monotonic() - started < MAX_CALL_SECONDS:
            try:
                item = self._listener.get(timeout=1)
            except queue.Empty:
                if self._station._call_audio is None:
                    return
                continue
            if item is None:
                return
            yield item

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._station._lock:
            self._station._listeners.discard(self._listener)
        self._session.__exit__(None, None, None)
