import struct

import pytest

from hik_sdk_bridge.g711 import pcm16le_to_mulaw


def decode(byte: int) -> int:
    """Reference ITU-T G.711 mu-law decoder (independent of the encoder)."""
    byte = ~byte & 0xFF
    sample = (((byte & 0x0F) << 3) + 0x84) << ((byte & 0x70) >> 4)
    sample -= 0x84
    return -sample if byte & 0x80 else sample


def encode(value: int) -> int:
    return pcm16le_to_mulaw(struct.pack("<h", value))[0]


@pytest.mark.parametrize("value, expected", [
    (0, 0xFF), (-1, 0x7F), (32767, 0x80), (-32768, 0x00), (1000, 0xCE), (-1000, 0x4E),
])
def test_known_values(value, expected):
    assert encode(value) == expected


def test_round_trip_error_is_within_mulaw_quantization():
    for value in range(-32768, 32768, 7):
        decoded = decode(encode(value))
        step = max(8, abs(min(max(value, -32635), 32635)) // 16)
        assert abs(decoded - min(max(value, -32635), 32635)) <= step


def test_output_is_one_byte_per_sample_and_monotonic_in_magnitude():
    data = struct.pack("<4h", 100, 2000, 20000, -20000)
    assert len(pcm16le_to_mulaw(data)) == 4


def test_rejects_partial_samples():
    with pytest.raises(ValueError):
        pcm16le_to_mulaw(b"\x00\x00\x01")
