"""Download and verify the pinned Hikvision SDK runtime files at image build time.

Every file is checked against sdk-files.sha256 before it is installed; a mismatch
aborts the build. Usage: fetch_sdk.py <architecture or empty> <destination>
"""

import platform
import sys
import time
import urllib.request
from hashlib import sha256
from pathlib import Path

_MACHINES = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "aarch64",
    "arm64": "aarch64",
}

COMMIT = "ce033c5da60ed28740c4e69d6996818cf8d4fdad"
BASE = f"https://raw.githubusercontent.com/pergolafabio/Hikvision-Addons/{COMMIT}/hikvision-doorbell"


def _download(url: str, name: str, attempts: int = 4, pause=time.sleep) -> bytes:
    """GET with a few retries: a build on a slow or flaky connection should not fail on one timed-out read.

    Only the transfer is retried; the checksum of what arrives is still verified by the caller.
    """
    if not url.startswith("https://"):
        raise SystemExit(
            f"refusing to download {name} over anything but https"
        )  # urlopen would also open file: and ftp: addresses
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                return response.read()
        except (
            OSError
        ) as error:  # URLError, TimeoutError and socket errors are all OSError
            if attempt == attempts:
                raise SystemExit(
                    f"download of {name} failed after {attempts} attempts: {type(error).__name__}"
                ) from None
            pause(2 * attempt)
    raise AssertionError("unreachable")


def main(architecture: str, destination: Path) -> None:
    architecture = architecture or _MACHINES.get(platform.machine().lower(), "")
    manifest = Path(__file__).with_name("sdk-files.sha256")
    wanted = []
    for line in manifest.read_text().splitlines():
        if line.startswith("#") or not line.strip():
            continue
        digest, name = line.split("  ", 1)
        arch, _, relative = name.partition("/")
        if arch == architecture:
            wanted.append((digest, relative))
    if not wanted:
        raise SystemExit(f"no SDK files listed for architecture {architecture!r}")
    for digest, relative in wanted:
        target = destination / relative
        if destination.resolve() not in target.resolve().parents:
            raise SystemExit("manifest path escapes the destination")
        data = _download(f"{BASE}/lib-{architecture}/{relative}", relative)
        if sha256(data).hexdigest() != digest:
            raise SystemExit(f"checksum mismatch for {relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    print(f"installed {len(wanted)} verified SDK files for {architecture}")


if __name__ == "__main__":
    main(sys.argv[1], Path(sys.argv[2]))
