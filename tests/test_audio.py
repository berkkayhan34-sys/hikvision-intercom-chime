import io
import math
import struct
import unittest
import wave

from support import COMPONENT, load

audio = load("audio")


def wav(pcm=None, *, channels=1, width=2, rate=8000):
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setparams((channels, width, rate, 0, "NONE", "not compressed"))
        target.writeframes(pcm if pcm is not None else struct.pack("<hhhh", 0, 1200, -2400, 32767))
    return output.getvalue()


class AudioTests(unittest.TestCase):
    def test_default_is_two_seconds(self):
        data = (COMPONENT / "assets" / "ding_dong.wav").read_bytes()
        self.assertEqual(audio.decode_wav(data).duration, 2)

    def test_four_repeats_preserve_every_sample_at_zero_gain(self):
        source = audio.decode_wav(wav())
        result = audio.render(source, audio.Settings(0, 4))
        with wave.open(io.BytesIO(result), "rb") as stream:
            self.assertEqual(stream.readframes(stream.getnframes()), source.pcm * 4)

    def test_default_four_repeats_are_eight_seconds(self):
        source = audio.decode_wav((COMPONENT / "assets" / "ding_dong.wav").read_bytes())
        with wave.open(io.BytesIO(audio.render(source, audio.Settings())), "rb") as stream:
            self.assertEqual(stream.getnframes() / stream.getframerate(), 8)

    def test_gain_is_decibels(self):
        source = audio.decode_wav(wav(struct.pack("<hh", 1000, -1000)))
        with wave.open(io.BytesIO(audio.render(source, audio.Settings(6, 1))), "rb") as stream:
            samples = struct.unpack("<hh", stream.readframes(2))
        self.assertEqual(samples, (1995, -1995))

    def test_positive_gain_saturates_not_integer_wraparound(self):
        source = audio.decode_wav(wav(struct.pack("<hh", 30000, -30000)))
        with wave.open(io.BytesIO(audio.render(source, audio.Settings(30, 1))), "rb") as stream:
            self.assertEqual(struct.unpack("<hh", stream.readframes(2)), (32760, -32760))

    def test_negative_gain(self):
        source = audio.decode_wav(wav(struct.pack("<h", 10000)))
        with wave.open(io.BytesIO(audio.render(source, audio.Settings(-20, 1))), "rb") as stream:
            self.assertEqual(struct.unpack("<h", stream.readframes(1))[0], 1000)

    def test_rejects_malformed_unsupported_and_oversized_inputs(self):
        cases = [b"", b"not a wav" * 10, b"x" * 1048577,
                 wav(channels=2), wav(width=1), wav(rate=44100), wav(pcm=b"")]
        for data in cases:
            with self.subTest(length=len(data)):
                with self.assertRaises(audio.AudioError):
                    audio.decode_wav(data)

    def test_rejects_truncated_payload(self):
        with self.assertRaises(audio.AudioError):
            audio.decode_wav(wav(b"\0\0" * 8000)[:-100])

    def test_maximum_duration_is_enforced_from_frames(self):
        with self.assertRaisesRegex(audio.AudioError, "invalid_duration"):
            audio.decode_wav(wav(b"\0\0" * (30 * 8000 + 1)))

    def test_total_duration_is_bounded(self):
        source = audio.decode_wav(wav(b"\0\0" * (21 * 8000)))
        with self.assertRaisesRegex(audio.AudioError, "total_duration_exceeded"):
            audio.render(source, audio.Settings(0, 3))

    def test_settings_refuse_bool_nan_and_non_integral_repeats(self):
        for gain in (True, math.nan, math.inf, -31, 31, "3"):
            with self.subTest(gain=gain):
                with self.assertRaises(audio.AudioError): audio.Settings(gain, 4)
        for repeats in (True, 0, 11, 1.2, "2"):
            with self.subTest(repeats=repeats):
                with self.assertRaises(audio.AudioError): audio.Settings(0, repeats)
