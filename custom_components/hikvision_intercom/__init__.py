"""Hikvision Intercom: Hik-Connect cloud controls, native LAN video and a local chime."""

from __future__ import annotations

import logging
from pathlib import Path
import time
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .audio import AudioError
from .bridge_app import remove_station
from .chime import AppLink, ChimeRuntime
from .door_call import DoorCall
from .const import (
    CALL_POLL_INTERVAL,
    CONF_ACCOUNT,
    CONF_BASE_URL,
    CONF_CALL_POLL_INTERVAL,
    CONF_PASSWORD,
    DEFAULT_BASE_URL,
    DOMAIN,
    STATUS_POLL_INTERVAL,
    SUBENTRY_STATION,
    call_signal,
    native_stream_mode,
)
from .hikconnect_api import HikConnectAuthError, HikConnectClient, HikConnectError
from .longlink import HikConnectLongLink
from .push import HikConnectPush
from .storage import AudioStore
from .views import DoorListenView

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [
    Platform.CAMERA,
    Platform.BUTTON,
    Platform.LOCK,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.EVENT,
    Platform.SELECT,
    Platform.NUMBER,
    Platform.SWITCH,
    Platform.STT,
    Platform.CONVERSATION,
]


def _call_poll_interval(entry: ConfigEntry) -> timedelta:
    return timedelta(seconds=entry.options.get(CONF_CALL_POLL_INTERVAL, CALL_POLL_INTERVAL))


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    hass.http.register_view(DoorListenView())
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    client = HikConnectClient(
        entry.data[CONF_ACCOUNT],
        entry.data[CONF_PASSWORD],
        entry.data.get(CONF_BASE_URL, DEFAULT_BASE_URL),
    )

    def _login_and_enumerate():
        client.login()
        devices = client.get_devices()
        cams = []
        for dev in devices:
            if not dev.local_ip:
                continue
            cams.extend(client.get_cameras(dev))
        return devices, cams

    try:
        devices, cameras = await hass.async_add_executor_job(_login_and_enumerate)
    except HikConnectAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except Exception as err:  # noqa: BLE001
        raise ConfigEntryNotReady(f"Hik-Connect setup failed: {err}") from err

    _LOGGER.info(
        "Hik-Connect Intercom: %d device(s), %d camera channel(s)",
        len(devices), len(cameras),
    )

    async def _poll_call_status() -> dict[str, dict]:
        def work():
            polled_at = time.monotonic()
            out: dict[str, dict] = {}
            for dev in devices:
                try:
                    out[dev.serial] = client.get_call_status(dev.serial)
                except HikConnectError as err:
                    raise UpdateFailed(f"call status poll failed for {dev.serial}: {err}") from err
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("call status poll failed for %s: %s", dev.serial, err)
                    out[dev.serial] = {"status": "unknown", "info": {}}
                out[dev.serial]["polled_at"] = polled_at
            return out

        result = await hass.async_add_executor_job(work)
        for serial, polled in result.items():
            current = (coordinator.data or {}).get(serial) or {}
            if max(current.get("local_at", 0.0), current.get("pushed_at", 0.0)) > polled["polled_at"]:
                result[serial] = current
        return result

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=f"{DOMAIN}_call_status",
        update_method=_poll_call_status,
        update_interval=_call_poll_interval(entry),
    )
    await coordinator.async_config_entry_first_refresh()

    native_stream = native_stream_mode(entry.options)
    station_ids = _station_ids(entry)
    station_names = _station_names(entry)

    async def _options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
        coordinator.update_interval = _call_poll_interval(entry)
        if native_stream_mode(entry.options) != native_stream:
            hass.config_entries.async_schedule_reload(entry.entry_id)
        elif _station_ids(entry) != station_ids:
            # Stations were added or removed. Editing a station's sound settings
            # changes only subentry data and is applied live, without a reload.
            await _async_forget_stations(
                hass, entry.entry_id, station_names, station_ids - _station_ids(entry))
            hass.config_entries.async_schedule_reload(entry.entry_id)

    entry.async_on_unload(entry.add_update_listener(_options_updated))

    def _status_work() -> dict[str, dict]:
        try:
            status = client.get_device_status_map()
        except HikConnectError as err:
            raise UpdateFailed(f"status poll failed: {err}") from err
        missing = [d.serial for d in devices if d.serial not in status]
        if missing:
            # The cloud answers an expired session with a well-formed 401 and an
            # empty device list, so silence here would read as "no data" forever.
            raise UpdateFailed(f"cloud returned no data for {', '.join(missing)}")
        for dev in devices:
            if dev.locks:
                try:
                    status[dev.serial]["volumes"] = client.get_audio_volumes(dev.serial)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("volume fetch failed for %s: %s", dev.serial, err)
                try:
                    status[dev.serial]["dnd"] = client.get_dnd(dev.serial)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("dnd fetch failed for %s: %s", dev.serial, err)
                try:
                    status[dev.serial]["last_ring"] = client.get_last_ring(dev.serial)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("ring history fetch failed for %s: %s", dev.serial, err)
                try:
                    status[dev.serial]["work_status"] = client.get_work_status(dev.serial)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("work status fetch failed for %s: %s", dev.serial, err)
                try:
                    status[dev.serial]["operation_time"] = client.get_operation_time(dev.serial)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("operation time fetch failed for %s: %s", dev.serial, err)
        return status

    async def _poll_status() -> dict[str, dict]:
        return await hass.async_add_executor_job(_status_work)

    status_coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=f"{DOMAIN}_status",
        update_method=_poll_status,
        update_interval=timedelta(seconds=STATUS_POLL_INTERVAL),
    )
    await status_coordinator.async_config_entry_first_refresh()

    registry = dr.async_get(hass)
    for device in devices:
        registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, device.serial)},
            manufacturer="Hikvision",
            name=device.name,
            model=device.device_type,
            sw_version=device.version or None,
        )

    # Realtime push: a call event just triggers an immediate authoritative poll.
    async def _on_call_push(*_) -> None:
        await coordinator.async_request_refresh()

    unsubs = [
        async_dispatcher_connect(hass, call_signal(dev.serial), _on_call_push)
        for dev in devices
    ]

    push = HikConnectPush(
        hass, client._base, entry.data[CONF_ACCOUNT], entry.data[CONF_PASSWORD]
    )
    try:
        await push.async_start()
    except Exception as err:  # noqa: BLE001 - never fail setup on push
        _LOGGER.warning("Hik-Connect push listener failed to start: %s", err)
        push = None
    else:
        async def _stop_push(_event) -> None:
            await push.async_stop()

        entry.async_on_unload(
            client.add_login_listener(lambda: hass.add_job(push.async_refresh_session))
        )
        entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _stop_push))

    longlink = None
    if push is None or push.used_fallback_host:
        longlink = await _async_start_longlink(hass, entry, client, coordinator)

    # One cloud device and one station: the chime is part of that device, so the
    # doorbell is a single device. With several of either, each station keeps its own
    # device, because two chimes on one device would have identical entity names.
    shared_serial = devices[0].serial if len(devices) == 1 and len(station_ids) == 1 else None
    chimes = await _async_create_chimes(hass, entry, shared_serial)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "chimes": chimes,
        "door_calls": {subentry_id: DoorCall(hass, entry.entry_id, chime) for subentry_id, chime in chimes.items()},
        "client": client,
        "devices": devices,
        "cameras": cameras,
        "coordinator": coordinator,
        "status_coordinator": status_coordinator,
        "push": push,
        "longlink": longlink,
        "unsubs": unsubs,
        "quality": {},  # (serial_chN -> MAIN|SUB), shared: select writes, camera reads
    }
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_drop_stale_station_devices(hass, entry, chimes)
    return True


@callback
def _async_drop_stale_station_devices(
    hass: HomeAssistant, entry: ConfigEntry, chimes: dict[str, ChimeRuntime]
) -> None:
    """Remove the separate chime device an earlier version created, once its
    entities have moved to the shared Doorbell device (never before, or they would go too)."""
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    for subentry_id, chime in chimes.items():
        if not chime.device_serial:
            continue
        stale = device_registry.async_get_device_by_identifier(
            (DOMAIN, f"station_{subentry_id}"), entry.entry_id)
        if stale is not None and not er.async_entries_for_device(
                entity_registry, stale.id, include_disabled_entities=True):
            device_registry.async_remove_device(stale.id)


async def _async_start_longlink(
    hass: HomeAssistant, entry: ConfigEntry, client: HikConnectClient, coordinator
) -> HikConnectLongLink | None:
    @callback
    def _rang(fields: list[str], caller: dict) -> None:
        data = dict(coordinator.data or {})
        serials = [serial for serial in data if serial in fields]
        if not serials:
            hass.async_create_task(coordinator.async_request_refresh())
            return
        now = time.monotonic()
        for serial in serials:
            client.remember_caller_channel(serial, caller)
            data[serial] = {"status": "ringing", "info": caller, "polled_at": now, "pushed_at": now}
        coordinator.async_set_updated_data(data)

    @callback
    def _hung_up(fields: list[str]) -> None:
        data = dict(coordinator.data or {})
        serials = [serial for serial in data if serial in fields]
        for serial in serials:
            data[serial] = {**data[serial], "status": "idle", "local_at": time.monotonic()}
        if serials:
            coordinator.async_set_updated_data(data)

    longlink = HikConnectLongLink(
        hass,
        Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.longlink", private=True),
        entry.data[CONF_ACCOUNT],
        entry.data[CONF_PASSWORD],
        client._base,
        lambda fields, caller: hass.add_job(_rang, fields, caller),
        lambda fields: hass.add_job(_hung_up, fields),
    )
    try:
        await longlink.async_start()
    except Exception as err:  # noqa: BLE001 - never fail setup on push
        _LOGGER.warning("Hik-Connect long-link push failed to start: %s", err)
        return None

    async def _stop_longlink(_event) -> None:
        await longlink.async_stop()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _stop_longlink))
    return longlink


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        data = hass.data[DOMAIN].pop(entry.entry_id, None)
        if data:
            for chime in data["chimes"].values():
                await chime.async_close()
            for unsub in data.get("unsubs", []):
                unsub()
            push = data.get("push")
            if push is not None:
                await push.async_stop()
            longlink = data.get("longlink")
            if longlink is not None:
                await longlink.async_stop()
    return unload_ok


def _station_ids(entry: ConfigEntry) -> set[str]:
    return set(_station_names(entry))


def _station_names(entry: ConfigEntry) -> dict[str, str]:
    """subentry id -> the station name used inside the bridge app."""
    return {
        subentry.subentry_id: subentry.data["name"]
        for subentry in entry.subentries.values()
        if subentry.subentry_type == SUBENTRY_STATION
    }


def _audio_root(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(".storage", DOMAIN))


async def _async_create_chimes(
    hass: HomeAssistant, entry: ConfigEntry, shared_serial: str | None
) -> dict[str, ChimeRuntime]:
    """One runtime per station. A missing or stopped bridge app, or an unusable audio
    file, never fails setup: the cloud features keep working and the station's chime
    entities report unavailable or an error state."""
    link = AppLink(hass)
    chimes: dict[str, ChimeRuntime] = {}
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_STATION:
            continue
        try:
            store = await hass.async_add_executor_job(
                AudioStore, _audio_root(hass), subentry.subentry_id)
        except (AudioError, OSError):
            _LOGGER.error("Audio storage for station %s is unusable; chime disabled",
                          subentry.title)
            continue
        chime = ChimeRuntime(hass, entry, subentry, link, store, shared_serial)
        try:
            await chime.async_validate()
        except (AudioError, OSError) as error:
            chime.status = "error"
            chime.last_error = str(error) if isinstance(error, AudioError) else "storage_unavailable"
        await chime.coordinator.async_refresh()
        chimes[subentry.subentry_id] = chime
    return chimes


async def _async_forget_stations(
    hass: HomeAssistant, entry_id: str, names: dict[str, str], removed: set[str]
) -> None:
    """Delete audio uploaded for removed stations (only files this integration wrote)
    and drop their credentials from the bridge app, best effort."""
    def work() -> None:
        for subentry_id in removed:
            AudioStore(_audio_root(hass), subentry_id).remove_owned_files()

    await hass.async_add_executor_job(work)
    # Chime entities belong to the entry (not the subentry), so Home Assistant will not
    # drop them with the station; unique ids are "<subentry_id>_<key>".
    entity_registry = er.async_get(hass)
    for registered in er.async_entries_for_config_entry(entity_registry, entry_id):
        if registered.unique_id.split("_", 1)[0] in removed:
            entity_registry.async_remove(registered.entity_id)
    for subentry_id in removed:
        await remove_station(hass, names[subentry_id])


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await _async_forget_stations(hass, entry.entry_id, _station_names(entry), _station_ids(entry))
