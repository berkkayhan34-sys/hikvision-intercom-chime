import hashlib
from pathlib import Path
import tempfile
import unittest

from support import load
from test_audio import wav

storage = load("storage")
AudioError = load("audio").AudioError


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "audio"
        self.store = storage.AudioStore(self.root, "entry_123")

    def test_content_addressed_deduplication(self):
        data = wav()
        digest = self.store.add_source(data)
        self.assertEqual(digest, hashlib.sha256(data).hexdigest())
        self.assertEqual(self.store.add_source(data), digest)
        self.assertEqual(self.store.source(digest), data)
        self.assertEqual(len(list(self.store.directory.iterdir())), 1)

    def test_invalid_upload_does_not_change_old_source(self):
        old = self.store.add_source(wav())
        with self.assertRaises(AudioError): self.store.add_source(b"not audio")
        self.assertEqual(self.store.source(old), wav())

    def test_path_traversal_rejected(self):
        for name in ("../outside", "..", "a/b", "C:\\secret"):
            with self.assertRaises(AudioError): storage.AudioStore(self.root, name)
            with self.assertRaises(AudioError): self.store.source(name)

    def test_failed_atomic_replace_preserves_old_file(self):
        from unittest.mock import patch
        self.store.write_rendered(b"old")
        with patch.object(storage.os, "replace", side_effect=OSError("failure")):
            with self.assertRaises(OSError): self.store.write_rendered(b"new")
        self.assertEqual((self.store.directory / "rendered.wav").read_bytes(), b"old")
        self.assertFalse(list(self.store.directory.glob(".chime-*")))

    def test_removal_is_owned_files_only(self):
        self.store.add_source(wav())
        self.store.write_rendered(b"generated")
        unrelated = self.store.directory / "user-note.txt"
        unrelated.write_text("preserve", encoding="utf8")
        self.store.remove_owned_files()
        self.assertEqual(unrelated.read_text(), "preserve")
        self.assertEqual(list(self.store.directory.iterdir()), [unrelated])

    def test_source_quota_is_bounded(self):
        for i in range(16): self.store.add_source(wav(bytes([i, 0]) * 4))
        with self.assertRaisesRegex(AudioError, "source_limit"):
            self.store.add_source(wav(b"\xff\0" * 4))

    def test_symlink_source_is_never_read(self):
        outside = Path(self.temp.name) / "outside.wav"
        outside.write_bytes(wav())
        link = self.store.directory / ("a" * 64 + ".wav")
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("Host does not permit creating test symlinks")
        with self.assertRaises(AudioError): self.store.source("a" * 64)

    def test_corrupt_existing_source_is_not_overwritten(self):
        data = wav()
        digest = hashlib.sha256(data).hexdigest()
        target = self.store.directory / (digest + ".wav")
        target.write_bytes(b"foreign data")
        with self.assertRaises(AudioError): self.store.add_source(data)
        self.assertEqual(target.read_bytes(), b"foreign data")
