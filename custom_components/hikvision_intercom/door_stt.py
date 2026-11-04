"""The Assist microphone as a door intercom: whatever is said is played into the answered call.

Assist records natively in the Home Assistant app (so this works on an http address too) and hands every speech-to-text
engine the live audio until the speaker pauses. This engine sends that audio into the door call as it arrives and
answers with a fixed text for the conversation step to acknowledge.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterable

from homeassistant.components.stt import (
    AudioBitRates,
    AudioChannels,
    AudioCodecs,
    AudioFormats,
    AudioSampleRates,
    SpeechMetadata,
    SpeechResult,
    SpeechResultState,
    SpeechToTextEntity,
)
from homeassistant.generated.languages import LANGUAGES

from .bridge_client import BridgeError
from .door_call import DoorCall
from .entity import chime_device_info

_LOGGER = logging.getLogger(__name__)
SENT_TEXT = "Sent to the door"


class DoorTalkSpeechToText(SpeechToTextEntity):
    _attr_has_entity_name = True
    _attr_name = "Call microphone"

    def __init__(self, chime, door_call: DoorCall) -> None:
        self._door_call = door_call
        self._attr_unique_id = f"{chime.subentry_id}_door_stt"
        self._attr_device_info = chime_device_info(chime)

    @property
    def supported_languages(self) -> list[str]:
        return list(LANGUAGES)

    @property
    def supported_formats(self) -> list[AudioFormats]:
        return [AudioFormats.WAV]

    @property
    def supported_codecs(self) -> list[AudioCodecs]:
        return [AudioCodecs.PCM]

    @property
    def supported_bit_rates(self) -> list[AudioBitRates]:
        return [AudioBitRates.BITRATE_16]

    @property
    def supported_sample_rates(self) -> list[AudioSampleRates]:
        return [AudioSampleRates.SAMPLERATE_16000]

    @property
    def supported_channels(self) -> list[AudioChannels]:
        return [AudioChannels.CHANNEL_MONO]

    async def async_process_audio_stream(
        self, metadata: SpeechMetadata, stream: AsyncIterable[bytes]
    ) -> SpeechResult:
        try:
            await self._door_call.talk(stream)
        except BridgeError as error:
            _LOGGER.warning(
                "Could not talk into the door call: %s", error.code
            )  # a fixed code, nothing else
            return SpeechResult(None, SpeechResultState.ERROR)
        return SpeechResult(SENT_TEXT, SpeechResultState.SUCCESS)
