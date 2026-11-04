"""One speech-to-text engine per station: the Assist microphone as the door intercom (see door_stt.py)."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .door_stt import DoorTalkSpeechToText


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        DoorTalkSpeechToText(chime, data["door_calls"][subentry_id])
        for subentry_id, chime in data["chimes"].items()
    )
