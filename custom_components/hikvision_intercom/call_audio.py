"""Audio for an answered door call: phone speech to the door, door sound to the phone.

The phone side is Home Assistant's Assist microphone (16 kHz, 16-bit mono PCM); the station speaks 8 kHz G.711 mu-law in
160-byte (20 ms) frames. This module converts both ways and paces the speech to real time.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, AsyncIterator
import math
import struct

SAMPLES_PER_FRAME = 160
FRAME_SECONDS = SAMPLES_PER_FRAME / 8000
LEAD_SECONDS = 0.2  # how far ahead of real time speech may be sent


def _mulaw_byte(sample: int) -> int:
    sign = 0x80 if sample < 0 else 0
    magnitude = min(-sample if sample < 0 else sample, 32635) + 0x84
    exponent = magnitude.bit_length() - 8
    return ~(sign | (exponent << 4) | ((magnitude >> (exponent + 3)) & 0x0F)) & 0xFF


_ENCODE = bytes(_mulaw_byte(v - 0x10000 if v >= 0x8000 else v) for v in range(0x10000))


def _mulaw_sample(code: int) -> int:
    code = ~code & 0xFF
    magnitude = (((code & 0x0F) << 3) + 0x84) << ((code & 0x70) >> 4)
    magnitude -= 0x84
    return -magnitude if code & 0x80 else magnitude


_DECODE = struct.pack("<256h", *(_mulaw_sample(c) for c in range(256)))


def mulaw_encode(samples) -> bytes:
    """Signed 16-bit samples (ints) to mu-law bytes."""
    return bytes(_ENCODE[s & 0xFFFF] for s in samples)


def mulaw_to_pcm16le(data: bytes) -> bytes:
    """mu-law bytes to little-endian signed 16-bit PCM (twice as long)."""
    table = [_DECODE[i:i + 2] for i in range(0, 512, 2)]
    return b"".join(table[b] for b in data)


def wav_header(sample_rate: int = 8000) -> bytes:
    """A WAV header for a stream of unknown length (sizes at their maximum), 16-bit mono PCM."""
    return (b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
            + b"data" + struct.pack("<I", 0xFFFFFFFF))


def _lowpass_taps(count: int, cutoff: float) -> tuple[float, ...]:
    middle = (count - 1) / 2
    taps = []
    for n in range(count):
        x = n - middle
        sinc = 2 * cutoff if x == 0 else math.sin(2 * math.pi * cutoff * x) / (math.pi * x)
        taps.append(sinc * (0.54 - 0.46 * math.cos(2 * math.pi * n / (count - 1))))
    total = sum(taps)
    return tuple(tap / total for tap in taps)


_TAPS = _lowpass_taps(21, 3600 / 16000)  # speech band kept; nothing above the new 4 kHz limit folds back


class Downsampler:
    """16 kHz signed 16-bit little-endian mono, in chunks of any size, to 8 kHz samples."""

    def __init__(self) -> None:
        self._history = [0] * (len(_TAPS) - 1)
        self._pending = b""
        self._seen = 0

    def feed(self, chunk: bytes) -> list[int]:
        data = self._pending + chunk
        usable = len(data) - len(data) % 2
        self._pending = data[usable:]
        incoming = struct.unpack(f"<{usable // 2}h", data[:usable])
        window = self._history + list(incoming)
        out = []
        for index in range((-self._seen) % 2, len(incoming), 2):
            acc = sum(tap * window[index + t] for t, tap in enumerate(_TAPS))
            out.append(max(-32768, min(32767, round(acc))))
        self._seen += len(incoming)
        self._history = window[len(window) - (len(_TAPS) - 1):]
        return out


async def speech_frames(stream: AsyncIterable[bytes]) -> AsyncIterator[bytes]:
    """The Assist microphone stream as paced 160-byte mu-law frames; a last partial frame is padded with silence."""
    loop = asyncio.get_running_loop()
    started, sent = loop.time(), 0
    downsampler, pending = Downsampler(), []

    async def paced(frame_samples):
        nonlocal sent
        wait = started + sent * FRAME_SECONDS - LEAD_SECONDS - loop.time()
        if wait > 0:
            await asyncio.sleep(wait)
        sent += 1
        return mulaw_encode(frame_samples)

    async for chunk in stream:
        pending += downsampler.feed(chunk)
        while len(pending) >= SAMPLES_PER_FRAME:
            frame, pending = pending[:SAMPLES_PER_FRAME], pending[SAMPLES_PER_FRAME:]
            yield await paced(frame)
    if pending:
        yield await paced(pending + [0] * (SAMPLES_PER_FRAME - len(pending)))
