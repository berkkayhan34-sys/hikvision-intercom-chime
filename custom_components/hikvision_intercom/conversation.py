"""The answer to a message spoken into the door call: a short acknowledgement and nothing else."""

from __future__ import annotations

from homeassistant.components.conversation import (
    ConversationEntity,
    ConversationInput,
    ConversationResult,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

REPLY = "Sent"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([CallAcknowledge(entry)])


class CallAcknowledge(ConversationEntity):
    """Used as the conversation agent of the door intercom assistant, so no other assistant acts on the relayed words."""

    _attr_has_entity_name = True
    _attr_name = "Call acknowledge"

    def __init__(self, entry: ConfigEntry) -> None:
        self._attr_unique_id = f"{entry.entry_id}_call_acknowledge"

    @property
    def supported_languages(self) -> str:
        return "*"

    async def async_process(self, user_input: ConversationInput) -> ConversationResult:
        response = intent.IntentResponse(language=user_input.language)
        response.async_set_speech(REPLY)
        return ConversationResult(
            response=response, conversation_id=user_input.conversation_id
        )
