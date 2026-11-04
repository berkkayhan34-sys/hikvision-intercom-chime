"""Offline metadata, translation and reproducible archive checks."""

import ast
import importlib.util
import json
import struct
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "hikvision_intercom"


def leaf_keys(value, prefix=""):
    if isinstance(value, dict):
        return {key for name, child in value.items()
                for key in leaf_keys(child, prefix + "/" + name)}
    return {prefix}


class PackageTests(unittest.TestCase):
    def test_brand_icons_are_valid_expected_size_pngs(self):
        for filename, size in (("icon.png", 256), ("icon@2x.png", 512)):
            content = (COMPONENT / "brand" / filename).read_bytes()
            self.assertEqual(content[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(struct.unpack(">II", content[16:24]), (size, size))

    def test_single_integration_and_manifest(self):
        self.assertEqual([path.name for path in (ROOT / "custom_components").iterdir()
                          if path.is_dir() and path.name != "__pycache__"], [COMPONENT.name])
        manifest = json.loads((COMPONENT / "manifest.json").read_text())
        self.assertEqual(manifest["domain"], COMPONENT.name)
        self.assertTrue(manifest["config_flow"])
        self.assertEqual(list(manifest)[:2], ["domain", "name"])  # hassfest key order
        self.assertEqual(list(manifest)[2:], sorted(list(manifest)[2:]))
        self.assertEqual(manifest["dependencies"], ["ffmpeg", "file_upload", "http"])
        for key in ("name", "version", "documentation", "issue_tracker", "codeowners"):
            self.assertTrue(manifest[key])

    def test_english_and_turkish_translation_keys_match(self):
        original = json.loads((COMPONENT / "strings.json").read_text(encoding="utf8"))
        for language in ("en", "tr"):
            translated = json.loads((COMPONENT / "translations" / f"{language}.json").read_text(encoding="utf8"))
            self.assertEqual(leaf_keys(original), leaf_keys(translated))

    def test_fixed_errors_have_ui_translations(self):
        strings = json.loads((COMPONENT / "strings.json").read_text(encoding="utf8"))
        flow_errors = strings["config_subentries"]["station"]["error"].keys()
        exceptions = strings["exceptions"].keys()
        flow_visible, runtime_visible = set(), set()
        for path in COMPONENT.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf8"))):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.args and isinstance(node.args[0], ast.Constant)):
                    if node.func.id in {"AudioError", "SetupError"}:
                        flow_visible.add(node.args[0].value)
                    if node.func.id in {"AudioError", "SetupError", "ChimeError", "BridgeError"}:
                        runtime_visible.add(node.args[0].value)
        from custom_components.hikvision_intercom.bridge_client import _HTTP_CODES
        runtime_visible |= set(_HTTP_CODES.values()) | {"busy", "call_active", "app_error"}
        self.assertLessEqual(flow_visible, flow_errors)
        self.assertLessEqual(runtime_visible, exceptions)
        # the flow also shows these directly
        self.assertLessEqual({"integration_not_ready", "confirmation_required", "invalid_wav"}, flow_errors)

    def test_archive_reproducible_and_contains_no_sdk_or_private_artifacts(self):
        spec = importlib.util.spec_from_file_location("build_chime_package", ROOT / "tools" / "build_package.py")
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory) / "first.zip", Path(directory) / "second.zip"
            self.assertEqual(builder.build(first), builder.build(second))
            with ZipFile(first) as archive:
                names = archive.namelist()
                self.assertIn("custom_components/hikvision_intercom/assets/ding_dong.wav", names)
                self.assertIn("custom_components/hikvision_intercom/brand/icon.png", names)
                for name in names:
                    self.assertNotIn("..", Path(name).parts)
                    self.assertFalse(any(part in {".git", "__pycache__", ".storage", "tests"}
                                         for part in Path(name).parts))
                    self.assertNotIn(Path(name).suffix.lower(), {".dll", ".exe", ".so", ".log", ".env"})
