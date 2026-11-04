"""Doorbell chime: explicit, serialized playback on one indoor station.

Nothing here plays sound on startup, reload or settings changes; only
``async_play`` (a button press or service call) sends audio.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
import logging
import math
from pathlib import Path

from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .audio import AudioError, Settings, decode_wav, render
from .bridge_app import find_app
from .bridge_client import BridgeClient, BridgeError
from .const import CHIME_POLL_INTERVAL, DEFAULT_GAIN, DEFAULT_REPEATS, DOMAIN
from .storage import AudioStore
from .validation import SetupError

_LOGGER = logging.getLogger(__name__)
_DEFAULT_SOUND = Path(__file__).parent / "assets" / "ding_dong.wav"
# Extra time after the clip's duration before the runtime stops it, to let the tail finish.
COMPLETION_MARGIN = 1.0


class ChimeError(HomeAssistantError):
    """Translation-key-only runtime failure; no input values or secrets."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(translation_domain=DOMAIN, translation_key=code)


class AppLink:
    """Finds the bridge app and caches a client; re-resolves when it moved or rotated."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._client: BridgeClient | None = None

    async def _resolve(self) -> BridgeClient:
        try:
            app = await find_app(self._hass)
        except SetupError as error:
            raise BridgeError(str(error)) from None
        if not app.running:
            raise BridgeError("app_stopped")
        token = app.options.get("token")
        if not isinstance(token, str) or not token:
            raise BridgeError("app_unconfigured")
        self._client = BridgeClient(async_get_clientsession(self._hass), app.hostname, token)
        return self._client

    async def call(self, operation: Callable):
        """Run ``operation(client)``; on a stale route or token, resolve once and retry."""
        client = self._client or await self._resolve()
        try:
            return await operation(client)
        except BridgeError as error:
            if error.code not in ("app_unreachable", "app_unauthorized"):
                raise
        self._client = None
        return await operation(await self._resolve())


class ChimeRuntime:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, subentry: ConfigSubentry,
                 link: AppLink, store: AudioStore, device_serial: str | None = None) -> None:
        self.hass, self.entry, self.link, self.store = hass, entry, link, store
        # Serial of the cloud device this chime shares, or None for a device of its own.
        self.device_serial = device_serial
        self.subentry_id = subentry.subentry_id
        self.station_name: str = subentry.data["name"]
        self.status = "idle"
        self.last_error: str | None = None
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._finished = asyncio.Event()
        self._finished.set()
        self._listeners: set[Callable[[], None]] = set()
        self.coordinator = DataUpdateCoordinator(
            hass, _LOGGER, config_entry=entry, name=f"{DOMAIN}_{self.subentry_id}",
            update_method=self._async_poll,
            update_interval=timedelta(seconds=CHIME_POLL_INTERVAL))

    # -- state -------------------------------------------------------------
    @property
    def subentry(self) -> ConfigSubentry:
        # Home Assistant replaces the subentry object on every update; never cache it.
        return self.entry.subentries[self.subentry_id]

    @property
    def settings(self) -> Settings:
        data = self.subentry.data
        return Settings(data.get("gain_db", DEFAULT_GAIN), data.get("repeats", DEFAULT_REPEATS))

    def listen(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._listeners.add(callback)
        return lambda: self._listeners.discard(callback)

    def notify(self) -> None:
        for callback in tuple(self._listeners):
            callback()

    async def _async_poll(self) -> dict:
        try:
            return await self.link.call(lambda client: client.status(self.station_name))
        except BridgeError as error:
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key=error.code) from None

    # -- audio source and settings -------------------------------------------
    def _source(self, data=None) -> bytes:
        digest = (self.subentry.data if data is None else data).get("source_digest")
        if digest:
            return self.store.source(digest)
        return AudioStore.read_file(_DEFAULT_SOUND)

    async def async_validate(self) -> None:
        source = await self.hass.async_add_executor_job(self._source)
        await self.hass.async_add_executor_job(render, decode_wav(source), self.settings)

    async def async_settings_data(self, gain: float, repeats: int,
                                  upload_id: str | None = None) -> dict:
        """Validate, then return new subentry data. Never contacts the app or plays."""
        if self._lock.locked():
            raise AudioError("busy")
        async with self._lock:
            settings = Settings(gain, repeats)
            data = dict(self.subentry.data)
            if upload_id:
                def import_upload():
                    with process_uploaded_file(self.hass, upload_id) as path:
                        source = AudioStore.read_file(path)
                        render(decode_wav(source), settings)
                        return self.store.add_source(source)
                data["source_digest"] = await self.hass.async_add_executor_job(import_upload)
            else:
                source = await self.hass.async_add_executor_job(self._source, data)
                await self.hass.async_add_executor_job(render, decode_wav(source), settings)
            data.update(gain_db=settings.gain_db, repeats=settings.repeats)
            return data

    async def async_set_number(self, key: str, value: float) -> None:
        if key not in ("gain_db", "repeats"):
            raise ChimeError("invalid_gain")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ChimeError("invalid_repeats" if key == "repeats" else "invalid_gain")
        if key == "repeats" and not float(value).is_integer():
            raise ChimeError("invalid_repeats")
        try:
            data = await self.async_settings_data(
                value if key == "gain_db" else self.settings.gain_db,
                int(value) if key == "repeats" else self.settings.repeats)
        except AudioError as error:
            raise ChimeError(str(error)) from None
        except OSError:
            raise ChimeError("storage_unavailable") from None
        self.hass.config_entries.async_update_subentry(self.entry, self.subentry, data=data)
        self.notify()

    # -- playback ------------------------------------------------------------
    async def async_play(self) -> None:
        if self._lock.locked():
            raise ChimeError("busy")
        async with self._lock:
            self._finished.clear()
            self._stop.clear()
            started = False
            failed = False
            try:
                settings = self.settings
                source = await self.hass.async_add_executor_job(self._source)
                pcm = await self.hass.async_add_executor_job(render, decode_wav(source), settings)
                if self._stop.is_set():
                    return  # Stop was pressed while the sound was being prepared
                duration = await self.link.call(
                    lambda client: client.broadcast(self.station_name, pcm))
                started = True
                self.status, self.last_error = "sending", None
                self.notify()
                try:
                    await asyncio.wait_for(self._stop.wait(), duration + COMPLETION_MARGIN)
                except TimeoutError:
                    pass
            except (AudioError, BridgeError) as error:
                failed = True
                self.last_error = error.code if isinstance(error, BridgeError) else str(error)
                raise ChimeError(self.last_error) from None
            except asyncio.CancelledError:
                raise
            except Exception:
                failed = True
                self.last_error = "playback_request_failed"
                raise ChimeError(self.last_error) from None
            finally:
                if started:
                    try:
                        # Stops only this bridge's own broadcast; a no-op once playback ended.
                        await asyncio.wait_for(self.link.call(
                            lambda client: client.stop(self.station_name)), 10)
                    except Exception:
                        self.last_error = "stop_request_failed"
                        failed = True
                self.status = "error" if failed else "idle"
                self._finished.set()
                self.notify()
            if self.last_error == "stop_request_failed":
                raise ChimeError("stop_request_failed")

    async def async_stop(self) -> None:
        # An idle Stop cannot terminate somebody else's intercom session.
        if not self._finished.is_set():
            self._stop.set()

    async def async_close(self) -> None:
        await self.async_stop()
        await asyncio.wait_for(self._finished.wait(), 10)
        self._listeners.clear()
