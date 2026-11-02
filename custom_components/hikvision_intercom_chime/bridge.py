"""Supervisor configuration boundary; playback remains in the existing SDK app."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import asyncio
from typing import Any

from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import ADDON_SLUG, DOMAIN, MIN_BRIDGE_VERSION
from .validation import SetupError, merge_device


def supervisor_client(hass: HomeAssistant):
    # Lazy import: Container installs must reach the friendly prerequisites form,
    # not fail to import an OS-only dependency before a form can be displayed.
    from homeassistant.components.hassio.handler import get_supervisor_client
    return get_supervisor_client(hass)


def check_prerequisites(hass: HomeAssistant) -> None:
    if "hassio" not in hass.config.components:
        raise SetupError("supervisor_required")
    if not any(entry.state is ConfigEntryState.LOADED
               for entry in hass.config_entries.async_entries("mqtt")):
        raise SetupError("mqtt_required")
    try:
        connected = mqtt.is_connected(hass)
    except KeyError:
        connected = False
    if not connected:
        raise SetupError("mqtt_disconnected")


async def addon_info(hass: HomeAssistant):
    check_prerequisites(hass)
    try:
        async with asyncio.timeout(10):
            info = await supervisor_client(hass).addons.addon_info(ADDON_SLUG)
    except Exception:
        # Supervisor errors may embed configuration. They must not reach logs/UI.
        raise SetupError("bridge_required") from None
    try:
        version = tuple(int(part) for part in info.version.split(".")[:3])
    except (ValueError, AttributeError):
        raise SetupError("bridge_version") from None
    if version < MIN_BRIDGE_VERSION:
        raise SetupError("bridge_version")
    return info


def is_started(info: Any) -> bool:
    return getattr(info.state, "value", info.state) == "started"


def configuration_lock(hass: HomeAssistant) -> asyncio.Lock:
    return hass.data.setdefault(DOMAIN, {}).setdefault("configuration_lock", asyncio.Lock())


async def configure_device(hass: HomeAssistant, device: dict, bridge_name: str | None) -> str:
    """Called only after the user's explicit restart confirmation.

    Password exists only in this stack and Supervisor's app options. Never return
    it, write a backup copy or put it into a Home Assistant integration entry.
    Restore the in-memory old configuration if a config/start operation fails.
    """
    async with configuration_lock(hass):
        info = await addon_info(hass)
        previous = deepcopy(info.options)
        changed, name = merge_device(previous, device, bridge_name)
        client = supervisor_client(hass)
        from aiohasupervisor.models import AddonsOptions
        written = False
        try:
            if changed != previous:
                # A timeout may occur after Supervisor already applied the write.
                written = True
                await client.addons.set_addon_options(ADDON_SLUG, AddonsOptions(config=changed))
                if is_started(info):
                    await client.addons.restart_addon(ADDON_SLUG)
                else:
                    await client.addons.start_addon(ADDON_SLUG)
            elif not is_started(info):
                await client.addons.start_addon(ADDON_SLUG)
        except (Exception, asyncio.CancelledError) as failure:
            if written:
                try:
                    await client.addons.set_addon_options(ADDON_SLUG, AddonsOptions(config=previous))
                    if is_started(info):
                        await client.addons.restart_addon(ADDON_SLUG)
                except Exception:
                    raise SetupError("bridge_rollback_failed") from None
            if isinstance(failure, asyncio.CancelledError):
                raise
            raise SetupError("bridge_config_failed") from None
        return name


@dataclass(frozen=True)
class Bindings:
    audio_path: str
    play: str
    stop: str
    call_state: str | None


def resolve_bindings(entities, devices, bridge_name: str) -> Bindings:
    """Use original MQTT device name and unique-id suffixes, never entity IDs.

    This also tolerates entity-ID renaming and existing bridge name-normalization
    variants, while refusing ambiguous or disabled candidates.
    """
    device_ids = {item.id for item in devices if item.name == bridge_name}
    candidates = [item for item in entities if item.platform == "mqtt"
                  and item.device_id in device_ids and item.disabled_by is None]

    def one(suffix: str, required: bool = True) -> str | None:
        matches = [item.entity_id for item in candidates if item.unique_id.endswith(suffix)]
        if len(matches) > 1 or (required and not matches):
            raise SetupError("bridge_entities_missing")
        return matches[0] if matches else None

    return Bindings(one("_broadcast_audio_path"), one("_broadcast_on"),
                    one("_broadcast_off"), one("-call_state", False))


def bindings(hass: HomeAssistant, bridge_name: str) -> Bindings:
    return resolve_bindings(er.async_get(hass).entities.values(),
                            dr.async_get(hass).devices.values(), bridge_name)
