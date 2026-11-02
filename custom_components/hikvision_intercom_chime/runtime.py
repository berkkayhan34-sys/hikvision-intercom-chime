"""Explicit, serialized chime actions. Initialization and settings are silent."""

from __future__ import annotations

import asyncio
import math
from pathlib import Path
from collections.abc import Callable

from homeassistant.components import mqtt
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event

from .audio import AudioError, Settings, decode_wav, render
from .bridge import addon_info, bindings, is_started
from .const import DOMAIN
from .storage import AudioStore
from .validation import SetupError


class ChimeError(HomeAssistantError):
    """Translation-key-only runtime failure; no input values or secrets."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(translation_domain=DOMAIN, translation_key=code)


class ChimeRuntime:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, store: AudioStore) -> None:
        self.hass, self.entry, self.store = hass, entry, store
        self.status = "idle"
        self.last_error: str | None = None
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._finished = asyncio.Event()
        self._finished.set()
        self._listeners: set[Callable[[], None]] = set()

    def listen(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._listeners.add(callback)
        return lambda: self._listeners.discard(callback)

    def notify(self) -> None:
        for callback in tuple(self._listeners):
            callback()

    @property
    def settings(self) -> Settings:
        return Settings(self.entry.options.get("gain_db", 0),
                        self.entry.options.get("repeats", 4))

    def _source(self, options: dict | None = None) -> bytes:
        options = self.entry.options if options is None else options
        digest = options.get("source_digest")
        if digest:
            return self.store.source(digest)
        return AudioStore.read_file(Path(__file__).parent / "assets" / "ding_dong.wav")

    async def async_validate(self) -> None:
        source = await self.hass.async_add_executor_job(self._source)
        await self.hass.async_add_executor_job(render, decode_wav(source), self.settings)

    async def async_options(self, gain: float, repeats: int, upload_id: str | None = None) -> dict:
        """Validate before committing settings. Never change the bridge or play."""
        if self._lock.locked():
            raise AudioError("busy")
        async with self._lock:
            settings = Settings(gain, repeats)
            options = dict(self.entry.options)
            if upload_id:
                def import_upload():
                    with process_uploaded_file(self.hass, upload_id) as path:
                        source = AudioStore.read_file(path)
                        render(decode_wav(source), settings)
                        return self.store.add_source(source)
                options["source_digest"] = await self.hass.async_add_executor_job(import_upload)
            else:
                source = await self.hass.async_add_executor_job(self._source)
                await self.hass.async_add_executor_job(render, decode_wav(source), settings)
            options.update(gain_db=settings.gain_db, repeats=settings.repeats)
            return options

    async def async_set_number(self, key: str, value: float) -> None:
        if key not in ("gain_db", "repeats"):
            raise ChimeError("invalid_gain")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ChimeError("invalid_repeats" if key == "repeats" else "invalid_gain")
        if key == "repeats" and not float(value).is_integer():
            raise ChimeError("invalid_repeats")
        try:
            options = await self.async_options(
                value if key == "gain_db" else self.settings.gain_db,
                int(value) if key == "repeats" else self.settings.repeats,
            )
        except AudioError as error:
            raise ChimeError(str(error)) from None
        except OSError:
            raise ChimeError("storage_unavailable") from None
        self.hass.config_entries.async_update_entry(self.entry, options=options)
        self.notify()

    async def _wait_for_path(self, entity_id: str, expected: str) -> None:
        event = asyncio.Event()
        def changed(_event):
            if self.hass.states.is_state(entity_id, expected):
                event.set()
        unsubscribe = async_track_state_change_event(self.hass, entity_id, changed)
        try:
            changed(None)
            await asyncio.wait_for(event.wait(), 5)
        finally:
            unsubscribe()

    async def async_play(self) -> None:
        if self._lock.locked():
            raise ChimeError("busy")
        async with self._lock:
            self._finished.clear()
            self._stop.clear()
            opened = False
            target = None
            failed = False
            try:
                settings = self.settings
                info = await addon_info(self.hass)
                if not is_started(info):
                    raise ChimeError("bridge_stopped")
                target = bindings(self.hass, self.entry.data["bridge_name"])
                for entity_id in (target.audio_path, target.play, target.stop):
                    state = self.hass.states.get(entity_id)
                    if state is None or state.state == "unavailable":
                        raise ChimeError("bridge_unavailable")
                if target.call_state:
                    if not self.hass.states.is_state(target.call_state, "idle"):
                        raise ChimeError("call_active")
                else:
                    raise ChimeError("call_state_missing")
                source = await self.hass.async_add_executor_job(self._source)
                audio = decode_wav(source)
                rendered = await self.hass.async_add_executor_job(render, audio, settings)
                path = await self.hass.async_add_executor_job(self.store.write_rendered, rendered)
                relative = path.relative_to(Path(self.hass.config.config_dir))
                addon_path = "/config/" + relative.as_posix()
                async with asyncio.timeout(5):
                    await self.hass.services.async_call("text", "set_value",
                        {"entity_id": target.audio_path, "value": addon_path}, blocking=True)
                await self._wait_for_path(target.audio_path, addon_path)
                if self._stop.is_set():
                    return
                # A call may have started while audio/path preparation was awaited.
                if not self.hass.states.is_state(target.call_state, "idle"):
                    raise ChimeError("call_active")
                # Set ownership before publishing: a timeout may still have delivered
                # the command. Finally sends a bounded stop, without retrying Play.
                opened = True
                self.status, self.last_error = "sending", None
                self.notify()
                async with asyncio.timeout(5):
                    await self.hass.services.async_call("button", "press",
                        {"entity_id": target.play}, blocking=True)
                try:
                    await asyncio.wait_for(self._stop.wait(), audio.duration * settings.repeats + 1)
                except TimeoutError:
                    pass
            except (SetupError, AudioError, ChimeError) as error:
                failed = True
                self.last_error = error.code if isinstance(error, ChimeError) else str(error)
                raise ChimeError(self.last_error) from None
            except asyncio.CancelledError:
                raise
            except Exception:
                failed = True
                self.last_error = "playback_request_failed"
                raise ChimeError(self.last_error) from None
            finally:
                try:
                    if opened and target:
                        async with asyncio.timeout(5):
                            await self.hass.services.async_call("button", "press",
                                {"entity_id": target.stop}, blocking=True)
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

    @property
    def mqtt_connected(self) -> bool:
        try:
            return mqtt.is_connected(self.hass)
        except KeyError:
            return False
