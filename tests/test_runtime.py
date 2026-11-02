import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from support import load
from test_audio import wav
import ha_boundary as boundary

bridge = load("bridge")
runtime_module = load("runtime")
audio = load("audio")
storage = load("storage")


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.hass = boundary.Hass(root)
        self.entry = NS(entry_id="entry123", data={"bridge_name": "Fixture Panel"},
                        options={"gain_db": 0, "repeats": 4})
        self.store = storage.AudioStore(root / ".storage" / "chime", self.entry.entry_id)
        self.runtime = runtime_module.ChimeRuntime(self.hass, self.entry, self.store)
        self.hass.runtime = self.runtime
        self.info = NS(state="started", version="3.0.34", options={"doorbells": []})
        self.patch = patch.object(runtime_module, "addon_info", AsyncMock(return_value=self.info))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    async def test_initialization_validation_numbers_and_idle_stop_are_silent(self):
        await self.runtime.async_validate()
        await self.runtime.async_set_number("gain_db", -5)
        await self.runtime.async_set_number("repeats", 2)
        await self.runtime.async_stop()
        await self.runtime.async_close()
        self.assertEqual(self.hass.calls, [])
        self.assertEqual(self.entry.options["gain_db"], -5)
        self.assertEqual(self.entry.options["repeats"], 2)

    async def test_upload_is_silent_and_context_is_cleaned_in_executor(self):
        source = Path(self.temp.name) / "upload.wav"
        source.write_bytes(wav())
        self.hass.uploads["fixture-upload-id"] = source
        options = await self.runtime.async_options(3, 2, "fixture-upload-id")
        self.assertEqual(self.store.source(options["source_digest"]), wav())
        self.assertFalse(source.exists())
        self.assertEqual(self.hass.calls, [])
        self.assertEqual(self.entry.options, {"gain_db": 0, "repeats": 4})

    async def test_invalid_upload_preserves_settings_and_sends_nothing(self):
        source = Path(self.temp.name) / "upload.wav"
        source.write_bytes(b"malformed")
        self.hass.uploads["fixture-upload-id"] = source
        before = dict(self.entry.options)
        with self.assertRaises(audio.AudioError):
            await self.runtime.async_options(3, 2, "fixture-upload-id")
        self.assertEqual(self.entry.options, before)
        self.assertFalse(source.exists())
        self.assertEqual(self.hass.calls, [])

    async def test_explicit_play_sets_path_then_play_then_stop(self):
        await self.runtime.async_play()
        self.assertEqual([item[2]["entity_id"] for item in self.hass.calls],
                         ["text.renamed_path", "button.renamed_play", "button.renamed_stop"])
        self.assertEqual(self.hass.callbacks, [])
        self.assertEqual(self.runtime.status, "idle")
        import wave
        with wave.open(str(self.store.directory / "rendered.wav"), "rb") as result:
            self.assertEqual(result.getnframes() / result.getframerate(), 8)

    async def test_reported_active_call_blocks_every_command(self):
        for state in ("ringing", "unknown", "unavailable", "call in progress"):
            self.hass.states.values["sensor.renamed_call"] = state
            with self.assertRaises(runtime_module.ChimeError) as error:
                await self.runtime.async_play()
            self.assertEqual(error.exception.code, "call_active")
        self.assertEqual(self.hass.calls, [])

    async def test_missing_call_state_blocks_play(self):
        del self.hass.entities["sensor.renamed_call"]
        with self.assertRaises(runtime_module.ChimeError) as error:
            await self.runtime.async_play()
        self.assertEqual(error.exception.code, "call_state_missing")
        self.assertEqual(self.hass.calls, [])

    async def test_call_starting_during_preparation_blocks_broadcast(self):
        original_call = self.hass.services.async_call
        async def call_with_incoming_call(domain, action, data, *, blocking):
            await original_call(domain, action, data, blocking=blocking)
            if domain == "text":
                self.hass.states.values["sensor.renamed_call"] = "ringing"
        self.hass.services.async_call = call_with_incoming_call
        with self.assertRaises(runtime_module.ChimeError) as error:
            await self.runtime.async_play()
        self.assertEqual(error.exception.code, "call_active")
        self.assertEqual([call[0] for call in self.hass.calls], ["text"])

    async def test_stopped_bridge_blocks_play(self):
        self.info.state = "stopped"
        with self.assertRaises(runtime_module.ChimeError): await self.runtime.async_play()
        self.assertEqual(self.hass.calls, [])

    async def test_unavailable_play_control_blocks_every_command(self):
        self.hass.states.values["button.renamed_play"] = "unavailable"
        with self.assertRaises(runtime_module.ChimeError): await self.runtime.async_play()
        self.assertEqual(self.hass.calls, [])

    async def test_busy_play_and_settings_are_not_queued(self):
        self.hass.hold = True
        task = asyncio.create_task(self.runtime.async_play())
        await self.hass.started.wait()
        with self.assertRaises(runtime_module.ChimeError): await self.runtime.async_play()
        with self.assertRaises(audio.AudioError): await self.runtime.async_options(3, 2)
        await self.runtime.async_stop()
        await task
        self.assertEqual(sum(call[2]["entity_id"] == "button.renamed_play" for call in self.hass.calls), 1)

    async def test_path_without_acknowledgement_never_plays_and_unsubscribes(self):
        self.hass.ack_path = False
        real_wait = asyncio.wait_for
        async def fast_wait(awaitable, timeout): return await real_wait(awaitable, min(timeout, 0.02))
        with patch.object(runtime_module.asyncio, "wait_for", fast_wait):
            with self.assertRaises(runtime_module.ChimeError): await self.runtime.async_play()
        self.assertEqual(len(self.hass.calls), 1)
        self.assertEqual(self.hass.callbacks, [])

    async def test_cancel_sends_one_stop_and_releases_lock(self):
        self.hass.hold = True
        task = asyncio.create_task(self.runtime.async_play())
        await self.hass.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(self.runtime._finished.is_set())
        self.assertFalse(self.runtime._lock.locked())
        self.assertEqual(sum(c[2]["entity_id"] == "button.renamed_stop" for c in self.hass.calls), 1)

    async def test_close_active_request_waits_for_cleanup(self):
        self.hass.hold = True
        task = asyncio.create_task(self.runtime.async_play())
        await self.hass.started.wait()
        await self.runtime.async_close()
        await task
        self.assertEqual(self.hass.calls[-1][2]["entity_id"], "button.renamed_stop")

    async def test_failed_stop_is_not_reported_as_success(self):
        self.hass.stop_fail = True
        with self.assertRaises(runtime_module.ChimeError) as error:
            await self.runtime.async_play()
        self.assertEqual(error.exception.code, "stop_request_failed")
        self.assertEqual(self.runtime.status, "error")
        self.assertTrue(self.runtime._finished.is_set())

    async def test_invalid_number_does_not_commit(self):
        with self.assertRaises(runtime_module.ChimeError): await self.runtime.async_set_number("repeats", 2.5)
        with self.assertRaises(runtime_module.ChimeError): await self.runtime.async_set_number("gain_db", 31)
        with self.assertRaises(runtime_module.ChimeError): await self.runtime.async_set_number("repeats", True)
        self.assertEqual(self.entry.options, {"gain_db": 0, "repeats": 4})


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_prerequisite_gate_is_specific(self):
        hass = boundary.Hass(Path("unused"))
        hass.config.components.clear()
        with self.assertRaisesRegex(bridge.SetupError, "supervisor_required"): bridge.check_prerequisites(hass)
        hass.config.components.add("hassio")
        hass.loaded_mqtt = False
        with self.assertRaisesRegex(bridge.SetupError, "mqtt_required"): bridge.check_prerequisites(hass)
        hass.loaded_mqtt = True
        hass.mqtt_connected = False
        with self.assertRaisesRegex(bridge.SetupError, "mqtt_disconnected"): bridge.check_prerequisites(hass)

    async def test_registry_binding_survives_entity_id_rename(self):
        hass = boundary.Hass(Path("unused"))
        result = bridge.bindings(hass, "Fixture Panel")
        self.assertEqual(result.play, "button.renamed_play")
        self.assertEqual(result.audio_path, "text.renamed_path")

    async def test_disabled_or_ambiguous_entities_never_bind(self):
        hass = boundary.Hass(Path("unused"))
        hass.entities["button.renamed_play"].disabled_by = "user"
        with self.assertRaises(bridge.SetupError): bridge.bindings(hass, "Fixture Panel")
        hass.entities["button.renamed_play"].disabled_by = None
        hass.entities["duplicate"] = deepcopy(hass.entities["button.renamed_play"])
        with self.assertRaises(bridge.SetupError): bridge.bindings(hass, "Fixture Panel")

    async def test_failed_restart_restores_options_and_never_plays(self):
        hass = boundary.Hass(Path("unused"))
        previous = {"doorbells": [], "mqtt": {"fixture": "preserve"}}
        info = NS(state="started", version="3.0.34", options=previous)
        addons = NS(set_addon_options=AsyncMock(), restart_addon=AsyncMock(side_effect=[OSError("secret fixture"), None]))
        with patch.object(bridge, "addon_info", AsyncMock(return_value=info)), patch.object(bridge, "supervisor_client", return_value=NS(addons=addons)):
            with self.assertRaisesRegex(bridge.SetupError, "bridge_config_failed"):
                await bridge.configure_device(hass, {"host": "10.0.0.5", "port": 8000,
                    "username": "admin", "password": "example-only"}, None)
        self.assertEqual(addons.set_addon_options.await_count, 2)
        self.assertEqual(addons.set_addon_options.call_args.args[1].config, previous)
        self.assertEqual(hass.calls, [])

    async def test_rollback_failure_has_a_distinct_non_secret_error(self):
        hass = boundary.Hass(Path("unused"))
        info = NS(state="started", options={"doorbells": []})
        addons = NS(set_addon_options=AsyncMock(side_effect=[None, OSError("private")]), restart_addon=AsyncMock(side_effect=OSError("private")))
        with patch.object(bridge, "addon_info", AsyncMock(return_value=info)), patch.object(bridge, "supervisor_client", return_value=NS(addons=addons)):
            with self.assertRaisesRegex(bridge.SetupError, "bridge_rollback_failed"):
                await bridge.configure_device(hass, {"host": "10.0.0.5", "port": 8000,
                    "username": "admin", "password": "example-only"}, None)

    async def test_timed_out_configuration_write_still_attempts_restore(self):
        hass = boundary.Hass(Path("unused"))
        previous = {"doorbells": []}
        info = NS(state="stopped", options=previous)
        addons = NS(set_addon_options=AsyncMock(side_effect=[TimeoutError(), None]))
        with patch.object(bridge, "addon_info", AsyncMock(return_value=info)), patch.object(bridge, "supervisor_client", return_value=NS(addons=addons)):
            with self.assertRaisesRegex(bridge.SetupError, "bridge_config_failed"):
                await bridge.configure_device(hass, {"host": "10.0.0.5", "port": 8000,
                    "username": "admin", "password": "example-only"}, None)
        self.assertEqual(addons.set_addon_options.await_count, 2)
        self.assertEqual(addons.set_addon_options.call_args.args[1].config, previous)
