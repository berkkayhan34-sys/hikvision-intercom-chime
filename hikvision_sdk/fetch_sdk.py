"""Download and verify the pinned Hikvision SDK runtime files at image build time.

Every file is checked against sdk-files.sha256 before it is installed; a mismatch
aborts the build. Usage: fetch_sdk.py <architecture or empty> <destination>
"""

from hashlib import sha256
from pathlib import Path
import platform
import sys
import urllib.request

_MACHINES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "aarch64", "arm64": "aarch64"}

COMMIT = "ce033c5da60ed28740c4e69d6996818cf8d4fdad"
BASE = f"https://raw.githubusercontent.com/pergolafabio/Hikvision-Addons/{COMMIT}/hikvision-doorbell"


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
        with urllib.request.urlopen(f"{BASE}/lib-{architecture}/{relative}", timeout=60) as response:
            data = response.read()
        if sha256(data).hexdigest() != digest:
            raise SystemExit(f"checksum mismatch for {relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    print(f"installed {len(wanted)} verified SDK files for {architecture}")


if __name__ == "__main__":
    main(sys.argv[1], Path(sys.argv[2]))
