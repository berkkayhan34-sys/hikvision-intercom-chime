import hashlib
import importlib.util
import urllib.error
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "fetch_sdk", Path(__file__).parents[1] / "fetch_sdk.py"
)
fetch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fetch)


class Response:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def flaky(failures, data=b"payload"):
    calls = []

    def urlopen(url, timeout):
        calls.append(url)
        if len(calls) <= len(failures):
            raise failures[len(calls) - 1]
        return Response(data)

    return urlopen, calls


def test_a_timed_out_read_is_retried_and_then_succeeds(monkeypatch):
    urlopen, calls = flaky(
        [TimeoutError("read timed out"), urllib.error.URLError("reset")]
    )
    monkeypatch.setattr(fetch.urllib.request, "urlopen", urlopen)
    pauses = []
    assert (
        fetch._download("https://example.test/x", "x", pause=pauses.append)
        == b"payload"
    )
    assert len(calls) == 3 and pauses == [2, 4]


def test_giving_up_names_the_file_and_the_error_kind_only(monkeypatch):
    urlopen, calls = flaky([TimeoutError("secret detail")] * 10)
    monkeypatch.setattr(fetch.urllib.request, "urlopen", urlopen)
    with pytest.raises(SystemExit) as info:
        fetch._download(
            "https://example.test/x", "libhcnetsdk.so", pause=lambda s: None
        )
    assert len(calls) == 4
    assert (
        "libhcnetsdk.so" in str(info.value)
        and "TimeoutError" in str(info.value)
        and "secret detail" not in str(info.value)
    )


def test_a_retried_download_is_still_checksum_verified(monkeypatch, tmp_path):
    good = b"genuine"
    manifest = tmp_path / "sdk-files.sha256"
    manifest.write_text(f"{hashlib.sha256(good).hexdigest()}  amd64/lib.so\n")
    monkeypatch.setattr(fetch, "__file__", str(tmp_path / "fetch_sdk.py"))
    monkeypatch.setattr(fetch.time, "sleep", lambda s: None)
    urlopen, _ = flaky([TimeoutError()], data=b"tampered")
    monkeypatch.setattr(fetch.urllib.request, "urlopen", urlopen)
    with pytest.raises(SystemExit) as info:
        fetch.main("amd64", tmp_path / "out")
    assert (
        "checksum mismatch" in str(info.value)
        and not (tmp_path / "out" / "lib.so").exists()
    )
    urlopen, _ = flaky([TimeoutError()], data=good)
    monkeypatch.setattr(fetch.urllib.request, "urlopen", urlopen)
    fetch.main("amd64", tmp_path / "out")
    assert (tmp_path / "out" / "lib.so").read_bytes() == good


def test_only_https_addresses_are_downloaded(monkeypatch):
    opened = []
    monkeypatch.setattr(
        fetch.urllib.request, "urlopen", lambda *a, **k: opened.append(a)
    )
    for url in ("file:///etc/passwd", "http://example.test/x", "ftp://example.test/x"):
        with pytest.raises(SystemExit, match="https"):
            fetch._download(url, "x", pause=lambda s: None)
    assert opened == []
