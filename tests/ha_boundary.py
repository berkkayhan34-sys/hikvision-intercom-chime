"""Minimal explicit mocks of HA/Supervisor interfaces for isolated unit tests.

No fake broker, device-success reply or media encoder substitutes the behavior
under test: actual chime/runtime/merge/storage code runs. These mocks cannot
establish framework compatibility or rendered UI acceptance.
"""

import asyncio
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
import sys
import threading
import types
from types import SimpleNamespace as NS


def module(name):
    value = types.ModuleType(name)
    sys.modules[name] = value
    return value


class HAError(Exception):
    def __init__(self, *args, translation_domain=None, translation_key=None):
        super().__init__(translation_key or (args[0] if args else "error"))


class EntryState(Enum):
    LOADED = "loaded"


module("homeassistant")
components = module("homeassistant.components")
mqtt = module("homeassistant.components.mqtt")
mqtt.is_connected = lambda hass: hass.mqtt_connected
components.mqtt = mqtt
file_upload = module("homeassistant.components.file_upload")


@contextmanager
def uploaded_file(hass, identifier):
    if threading.current_thread() is threading.main_thread():
        raise AssertionError("Uploaded-file context must be inside executor")
    path = hass.uploads[identifier]
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


file_upload.process_uploaded_file = uploaded_file
entries = module("homeassistant.config_entries")
entries.ConfigEntryState, entries.ConfigEntry = EntryState, object
core = module("homeassistant.core")
core.HomeAssistant = object
exceptions = module("homeassistant.exceptions")
exceptions.HomeAssistantError = HAError
module("homeassistant.helpers")
er = module("homeassistant.helpers.entity_registry")
dr = module("homeassistant.helpers.device_registry")
er.async_get = lambda hass: NS(entities=hass.entities)
dr.async_get = lambda hass: NS(devices=hass.devices)
events = module("homeassistant.helpers.event")


def subscribe(hass, entity_id, callback):
    hass.callbacks.append(callback)
    return lambda: hass.callbacks.remove(callback)


events.async_track_state_change_event = subscribe
module("aiohasupervisor")
models = module("aiohasupervisor.models")
models.AddonsOptions = lambda *, config: NS(config=config)


class States:
    def __init__(self):
        self.values = {
            "text.renamed_path": "unknown", "button.renamed_play": "unknown",
            "button.renamed_stop": "unknown", "sensor.renamed_call": "idle",
        }

    def get(self, name):
        return NS(state=self.values[name]) if name in self.values else None

    def is_state(self, name, expected):
        return self.values.get(name) == expected


class Hass:
    def __init__(self, root):
        self.config = NS(components={"hassio"}, config_dir=str(root))
        self.data, self.uploads = {}, {}
        self.mqtt_connected = True
        self.loaded_mqtt = True
        self.states = States()
        self.calls, self.callbacks = [], []
        self.ack_path, self.hold, self.stop_fail = True, False, False
        self.started = asyncio.Event()
        self.runtime = None
        self.services = NS(async_call=self.call)
        self.config_entries = NS(async_entries=lambda domain: [NS(state=EntryState.LOADED)] if self.loaded_mqtt else [],
                                 async_update_entry=lambda entry, **kwargs: setattr(entry, "options", kwargs["options"]))
        self.devices = {"device": NS(id="device", name="Fixture Panel")}
        self.entities = {}
        for entity_id, suffix in [
            ("text.renamed_path", "_broadcast_audio_path"),
            ("button.renamed_play", "_broadcast_on"),
            ("button.renamed_stop", "_broadcast_off"),
            ("sensor.renamed_call", "-call_state"),
        ]:
            self.entities[entity_id] = NS(entity_id=entity_id, unique_id="fixture" + suffix,
                platform="mqtt", device_id="device", disabled_by=None)

    async def async_add_executor_job(self, function, *args):
        return await asyncio.to_thread(function, *args)

    async def call(self, domain, action, data, *, blocking):
        self.calls.append((domain, action, dict(data)))
        if domain == "text" and self.ack_path:
            self.states.values[data["entity_id"]] = data["value"]
            for callback in tuple(self.callbacks): callback(None)
        elif data["entity_id"] == "button.renamed_play":
            self.started.set()
            if not self.hold: self.runtime._stop.set()
        elif data["entity_id"] == "button.renamed_stop" and self.stop_fail:
            raise OSError("private diagnostics must not escape")
