"""Build a reproducible local evaluation ZIP, never publish or contact HA."""

from hashlib import sha256
import json
from pathlib import Path
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "hikvision_intercom_chime"


def package_files():
    paths = [ROOT / "README.md", ROOT / "hacs.json"]
    paths.extend((ROOT / "docs").glob("*.md"))
    paths.extend(path for path in (ROOT / "custom_components" / DOMAIN).rglob("*")
                 if path.is_file() and "__pycache__" not in path.parts)
    for path in sorted(paths):
        if path.is_symlink() or path.suffix not in {".py", ".json", ".wav", ".md", ".png"}:
            raise ValueError("Unexpected package file")
        yield path


def build(destination: Path):
    files = list(package_files())
    destination.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(destination, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            info = ZipInfo(path.relative_to(ROOT).as_posix(), date_time=(2026, 11, 3, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    with ZipFile(destination) as archive:
        if archive.testzip() is not None:
            raise ValueError("ZIP integrity failure")
    return sha256(destination.read_bytes()).hexdigest(), len(files)


if __name__ == "__main__":
    version = json.loads((ROOT / "custom_components" / DOMAIN / "manifest.json").read_text())["version"]
    target = ROOT / "dist" / f"hikvision-intercom-chime-{version}-local.zip"
    digest, count = build(target)
    target.with_suffix(".zip.sha256").write_text(f"{digest}  {target.name}\n", encoding="ascii")
    print(f"Files: {count}; SHA256: {digest}\n{target}")
