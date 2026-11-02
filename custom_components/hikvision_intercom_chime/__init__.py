"""HACS-managed chime controls for an existing local Hikvision SDK app."""

from pathlib import Path

from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .audio import AudioError
from .bridge import addon_info, bindings, is_started
from .const import DOMAIN
from .runtime import ChimeRuntime
from .storage import AudioStore
from .validation import SetupError

PLATFORMS = [Platform.BUTTON, Platform.NUMBER, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """No configuration mutation, app restart or playback on startup/reload."""
    try:
        info = await addon_info(hass)
        if not is_started(info):
            raise SetupError("bridge_stopped")
        bindings(hass, entry.data["bridge_name"])
        root = Path(hass.config.path(".storage", DOMAIN))
        store = await hass.async_add_executor_job(AudioStore, root, entry.entry_id)
        runtime = ChimeRuntime(hass, entry, store)
        await runtime.async_validate()
    except (SetupError, AudioError, OSError) as error:
        # Fixed codes only; filesystem exceptions may contain private local paths.
        code = str(error) if isinstance(error, (SetupError, AudioError)) else "storage_unavailable"
        raise ConfigEntryNotReady(code) from None
    entry.runtime_data = runtime
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_options_updated))
    entry.async_on_unload(mqtt.async_subscribe_connection_status(hass, lambda _connected: runtime.notify()))
    return True


async def async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    # Changing settings updates entity values only. It cannot ring or restart the app.
    entry.runtime_data.notify()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    runtime = getattr(entry, "runtime_data", None)
    if runtime is not None:
        await runtime.async_close()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    # Only integration-owned audio is removed. The SDK app and its credentials
    # remain untouched because other integrations may still depend on them.
    root = Path(hass.config.path(".storage", DOMAIN))
    def remove_audio():
        if (root / entry.entry_id).exists():
            AudioStore(root, entry.entry_id).remove_owned_files()
    await hass.async_add_executor_job(remove_audio)
