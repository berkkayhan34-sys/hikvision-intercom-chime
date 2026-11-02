"""Flow control tests with explicit framework-boundary doubles, not UI tests."""

from types import SimpleNamespace as NS, ModuleType
import sys
import unittest
from unittest.mock import AsyncMock, patch

import ha_boundary as boundary
from support import load


class FlowBoundary:
    """Capture results; deliberately does not validate HA schemas/selectors."""

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__()

    def async_show_form(self, **kwargs):
        return dict(type="form", **kwargs)

    def async_abort(self, **kwargs):
        return dict(type="abort", **kwargs)

    def async_create_entry(self, **kwargs):
        return dict(type="create_entry", **kwargs)

    def _async_current_entries(self):
        return self.current_entries

    async def async_set_unique_id(self, value):
        self.unique_id = value

    def _abort_if_unique_id_configured(self):
        pass  # Framework uniqueness remains a separate real-HA acceptance gate.


boundary.entries.ConfigFlow = FlowBoundary
boundary.entries.OptionsFlow = FlowBoundary
boundary.core.callback = lambda function: function
schema = ModuleType("probatio")
schema.Schema = lambda value: value
schema.Required = schema.Optional = lambda key, **kwargs: key
sys.modules["probatio"] = schema
selectors = boundary.module("homeassistant.helpers.selector")
for name in ("TextSelector", "TextSelectorConfig", "NumberSelector", "NumberSelectorConfig",
             "BooleanSelector", "FileSelector", "FileSelectorConfig"):
    setattr(selectors, name, lambda *args, **kwargs: (args, kwargs))
selectors.NumberSelectorMode = NS(BOX="box")
selectors.TextSelectorType = NS(PASSWORD="password")
flows = load("config_flow")


class FlowTests(unittest.IsolatedAsyncioTestCase):
    def make_flow(self):
        flow = flows.ChimeConfigFlow()
        flow.hass = NS()
        flow.current_entries = []
        flow._pending_device = None
        flow._reconfigure = None
        return flow

    def device(self):
        return dict(host="10.0.0.8", port=8000.0, username="admin", password="example-only")

    async def test_missing_mqtt_has_actionable_error_and_no_setup_write(self):
        flow = self.make_flow()
        with patch.object(flows, "addon_info", AsyncMock(side_effect=flows.SetupError("mqtt_required"))), \
             patch.object(flows, "configure_device", AsyncMock()) as configure:
            result = await flow.async_step_user()
        self.assertEqual(result["errors"], {"base": "mqtt_required"})
        configure.assert_not_awaited()

    async def test_device_entry_only_reaches_confirmation_without_side_effect(self):
        flow = self.make_flow()
        with patch.object(flows, "configure_device", AsyncMock()) as configure:
            result = await flow.async_step_device(self.device())
        self.assertEqual(result["step_id"], "confirm")
        self.assertIsInstance(flow._pending_device["port"], int)
        self.assertEqual(flow.unique_id, "10.0.0.8:8000")
        configure.assert_not_awaited()

    async def test_duplicate_is_rejected_before_bridge_write(self):
        flow = self.make_flow()
        flow.current_entries = [NS(unique_id="10.0.0.8:8000", entry_id="other")]
        with patch.object(flows, "configure_device", AsyncMock()) as configure:
            result = await flow.async_step_device(self.device())
        self.assertEqual(result["reason"], "already_configured")
        configure.assert_not_awaited()

    async def test_unconfirmed_restart_never_writes(self):
        flow = self.make_flow()
        await flow.async_step_device(self.device())
        with patch.object(flows, "configure_device", AsyncMock()) as configure:
            result = await flow.async_step_confirm({"confirm_restart": False})
        self.assertEqual(result["errors"], {"base": "confirmation_required"})
        configure.assert_not_awaited()

    async def test_confirmed_setup_excludes_password_from_entry_and_clears_pending(self):
        flow = self.make_flow()
        await flow.async_step_device(self.device())
        with patch.object(flows, "configure_device", AsyncMock(return_value="Fixture Panel")):
            result = await flow.async_step_confirm({"confirm_restart": True})
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(set(result["data"]), {"host", "port", "username", "bridge_name"})
        self.assertIsNone(flow._pending_device)
        self.assertNotIn("example-only", repr(result))

    async def test_failed_setup_is_safe_error_and_clears_credentials(self):
        flow = self.make_flow()
        await flow.async_step_device(self.device())
        with patch.object(flows, "configure_device", AsyncMock(side_effect=flows.SetupError("bridge_config_failed"))):
            result = await flow.async_step_confirm({"confirm_restart": True})
        self.assertEqual(result["errors"]["base"], "bridge_config_failed")
        self.assertIsNone(flow._pending_device)
        self.assertNotIn("example-only", repr(result))

    async def test_options_pass_upload_uuid_and_never_request_playback(self):
        flow = flows.ChimeOptionsFlow()
        runtime = NS(async_options=AsyncMock(return_value={"gain_db": -3, "repeats": 2}))
        flow.config_entry = NS(options={}, runtime_data=runtime)
        result = await flow.async_step_init({"audio_file": "fixture-upload-id", "gain_db": -3, "repeats": 2.0})
        self.assertEqual(result["type"], "create_entry")
        runtime.async_options.assert_awaited_once_with(-3, 2, "fixture-upload-id")

    async def test_expired_upload_returns_form_error(self):
        flow = flows.ChimeOptionsFlow()
        flow.config_entry = NS(options={}, runtime_data=NS(async_options=AsyncMock(side_effect=ValueError("private-path"))))
        result = await flow.async_step_init({"audio_file": "expired", "gain_db": 0, "repeats": 1})
        self.assertEqual(result["errors"]["base"], "invalid_wav")
        self.assertNotIn("private-path", repr(result))
