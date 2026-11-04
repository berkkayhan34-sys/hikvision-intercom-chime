"""Bounded PCM WAV processing. No network, device access or external executables."""

from __future__ import annotations

import io
import math
import struct
import wave
from dataclasses import dataclass

from .const import (
    MAX_FILE_BYTES,
    MAX_SOURCE_SECONDS,
    MAX_TOTAL_SECONDS,
    PEAK,
    SAMPLE_RATE,
)


class AudioError(ValueError):
    """Safe, fixed error code suitable for UI translation; never input content."""


@dataclass(frozen=True)
class Settings:
    gain_db: float = 0
    repeats: int = 4

    def __post_init__(self) -> None:
        if (
            isinstance(self.gain_db, bool)
            or not isinstance(self.gain_db, (int, float))
            or not math.isfinite(self.gain_db)
            or not -30 <= self.gain_db <= 30
        ):
            raise AudioError("invalid_gain")
        if (
            isinstance(self.repeats, bool)
            or not isinstance(self.repeats, int)
            or not 1 <= self.repeats <= 10
        ):
            raise AudioError("invalid_repeats")


@dataclass(frozen=True)
class Audio:
    pcm: bytes

    @property
    def duration(self) -> float:
        return len(self.pcm) / (2 * SAMPLE_RATE)


def decode_wav(data: bytes) -> Audio:
    """Accept only real, complete mono/8k/PCM16 WAV; reject missing frames."""
    if not isinstance(data, bytes) or not 44 <= len(data) <= MAX_FILE_BYTES:
        raise AudioError("invalid_file_size")
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise AudioError("invalid_wav")
    try:
        with wave.open(io.BytesIO(data), "rb") as source:
            if (
                source.getnchannels() != 1
                or source.getsampwidth() != 2
                or source.getframerate() != SAMPLE_RATE
                or source.getcomptype() != "NONE"
            ):
                raise AudioError("unsupported_wav")
            frames = source.getnframes()
            if not 0 < frames <= MAX_SOURCE_SECONDS * SAMPLE_RATE:
                raise AudioError("invalid_duration")
            pcm = source.readframes(frames)
            if len(pcm) != frames * 2:
                raise AudioError("truncated_wav")
    except (wave.Error, EOFError, struct.error):
        raise AudioError("invalid_wav") from None
    return Audio(pcm)


def render(audio: Audio, settings: Settings) -> bytes:
    """Apply bounded digital gain with saturation, then exact PCM repetition.

    Returns raw little-endian 8 kHz mono PCM16, the format the SDK bridge accepts.

    Positive gain may distort; it cannot raise the device's hardware volume.
    Gain zero preserves the original PCM bit for bit, including full-scale inputs.
    """
    if audio.duration * settings.repeats > MAX_TOTAL_SECONDS:
        raise AudioError("total_duration_exceeded")
    pcm = audio.pcm
    if settings.gain_db:
        scale = 10 ** (settings.gain_db / 20)
        samples = (
            max(-PEAK, min(PEAK, round(value * scale)))
            for (value,) in struct.iter_unpack("<h", pcm)
        )
        pcm = b"".join(struct.pack("<h", value) for value in samples)
    return pcm * settings.repeats
