"""Private, integration-owned audio storage with immutable source files."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile

from .audio import AudioError, decode_wav
from .const import MAX_FILE_BYTES, MAX_SOURCES

_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")


class AudioStore:
    """All methods perform disk IO and must run in Home Assistant's executor."""

    def __init__(self, root: Path, entry_id: str) -> None:
        if not _IDENTIFIER.fullmatch(entry_id):
            raise AudioError("invalid_storage_id")
        # Home Assistant's config root is trusted; integration-owned children may
        # not be symlinks. Never resolve an untrusted child before this check.
        if root.is_symlink():
            raise AudioError("unsafe_storage")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory = root / entry_id
        if self.directory.is_symlink():
            raise AudioError("unsafe_storage")
        self.directory.mkdir(mode=0o700, exist_ok=True)

    @staticmethod
    def read_file(path: Path) -> bytes:
        if path.is_symlink():
            raise AudioError("unsafe_storage")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                raise AudioError("invalid_file_size")
            data = stream.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise AudioError("invalid_file_size")
        return data

    def _atomic_write(self, destination: Path, data: bytes) -> None:
        if self.directory.is_symlink() or destination.is_symlink():
            raise AudioError("unsafe_storage")
        descriptor, name = tempfile.mkstemp(prefix=".chime-", dir=self.directory)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    def add_source(self, data: bytes) -> str:
        decode_wav(data)
        digest = hashlib.sha256(data).hexdigest()
        destination = self.directory / (digest + ".wav")
        if destination.exists():
            if self.read_file(destination) != data:
                raise AudioError("unsafe_storage")
            return digest
        if len(list(self.directory.glob("[a-f0-9]" * 64 + ".wav"))) >= MAX_SOURCES:
            raise AudioError("source_limit")
        self._atomic_write(destination, data)
        return digest

    def source(self, digest: str) -> bytes:
        if not _DIGEST.fullmatch(digest):
            raise AudioError("invalid_storage_id")
        return self.read_file(self.directory / (digest + ".wav"))

    def remove_owned_files(self) -> None:
        if self.directory.is_symlink():
            raise AudioError("unsafe_storage")
        for path in self.directory.iterdir():
            if path.suffix == ".wav" and _DIGEST.fullmatch(path.stem):
                if path.is_file() and not path.is_symlink():
                    path.unlink()
        # Unknown files are preserved; never recursively delete an arbitrary tree.
        if not any(self.directory.iterdir()):
            self.directory.rmdir()
