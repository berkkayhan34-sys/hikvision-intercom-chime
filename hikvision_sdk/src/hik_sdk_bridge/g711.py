"""G.711 mu-law encoding of 16-bit PCM.

The stdlib ``audioop`` module was removed in Python 3.13, and the station
expects 8 kHz mono mu-law frames, so the encoder lives here. A 64 Ki-entry
lookup table keeps encoding allocation-light and constant-time per sample.
"""

from __future__ import annotations

import struct

_BIAS = 0x84
_CLIP = 32635


def _encode_sample(sample: int) -> int:
    sign = 0x80 if sample < 0 else 0
    magnitude = min(-sample if sample < 0 else sample, _CLIP) + _BIAS
    exponent = magnitude.bit_length() - 8
    mantissa = (magnitude >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


# Indexed by the unsigned reinterpretation of each signed 16-bit sample.
_TABLE = bytes(
    _encode_sample(value - 0x10000 if value >= 0x8000 else value)
    for value in range(0x10000)
)


def pcm16le_to_mulaw(pcm: bytes) -> bytes:
    """Convert little-endian signed 16-bit PCM to mu-law, one byte per sample."""
    if len(pcm) % 2:
        raise ValueError("PCM16 data must contain whole samples")
    return bytes(_TABLE[value] for value in struct.unpack(f"<{len(pcm) // 2}H", pcm))
