"""Admin setup, explicit bridge restart confirmation and silent audio options."""

from __future__ import annotations

import probatio as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .audio import AudioError
from .bridge import addon_info, configure_device
from .const import DEFAULT_GAIN, DEFAULT_PORT, DEFAULT_REPEATS, DOMAIN
from .validation import SetupError, validate_device


class ChimeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    _pending_device: dict | None = None
    _reconfigure = None

    async def async_step_user(self, user_input=None):
        try:
            await addon_info(self.hass)
        except SetupError as error:
            return self.async_show_form(step_id="user", data_schema=vol.Schema({}),
                                        errors={"base": str(error)})
        return await self.async_step_device()

    async def async_step_reconfigure(self, user_input=None):
        self._reconfigure = self._get_reconfigure_entry()
        return await self.async_step_device(user_input)

    async def async_step_device(self, user_input=None):
        errors = {}
        previous = self._reconfigure.data if self._reconfigure else {}
        if user_input is not None:
            try:
                port = user_input["port"]
                if isinstance(port, bool) or not isinstance(port, (int, float)) or not float(port).is_integer():
                    raise SetupError("invalid_port")
                user_input = dict(user_input, port=int(port))
                validate_device(user_input["host"], user_input["port"],
                                user_input["username"], user_input["password"])
                unique_id = f"{user_input['host']}:{user_input['port']}"
                if any(entry.unique_id == unique_id and (
                    self._reconfigure is None or entry.entry_id != self._reconfigure.entry_id
                ) for entry in self._async_current_entries()):
                    return self.async_abort(reason="already_configured")
                if self._reconfigure is None:
                    # Reserve the target before any shared app write, including
                    # against another still-in-progress setup flow.
                    await self.async_set_unique_id(unique_id)
                    self._abort_if_unique_id_configured()
                self._pending_device = dict(user_input)
                return await self.async_step_confirm()
            except SetupError as error:
                errors["base"] = str(error)
        return self.async_show_form(step_id="device", errors=errors, data_schema=vol.Schema({
            vol.Required("host", default=previous.get("host", "")): selector.TextSelector(),
            vol.Required("port", default=previous.get("port", DEFAULT_PORT)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=1, max=65535, mode=selector.NumberSelectorMode.BOX)),
            vol.Required("username", default=previous.get("username", "admin")): selector.TextSelector(),
            vol.Required("password"): selector.TextSelector(selector.TextSelectorConfig(
                type=selector.TextSelectorType.PASSWORD)),
        }))

    async def async_step_confirm(self, user_input=None):
        if self._pending_device is None:
            return self.async_abort(reason="setup_expired")
        errors = {}
        if user_input is not None and not user_input.get("confirm_restart"):
            errors["base"] = "confirmation_required"
        if user_input is not None and user_input.get("confirm_restart"):
            device = self._pending_device
            try:
                name = await configure_device(self.hass, device,
                    self._reconfigure.data["bridge_name"] if self._reconfigure else None)
                # Explicit allowlist: password must never enter ConfigEntry.data.
                data = {key: device[key] for key in ("host", "port", "username")}
                data["bridge_name"] = name
                unique_id = f"{device['host']}:{device['port']}"
                if self._reconfigure:
                    self.hass.config_entries.async_update_entry(
                        self._reconfigure, data=data, unique_id=unique_id)
                    await self.hass.config_entries.async_reload(self._reconfigure.entry_id)
                    return self.async_abort(reason="reconfigure_successful")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=name, data=data,
                    options={"gain_db": DEFAULT_GAIN, "repeats": DEFAULT_REPEATS})
            except SetupError as error:
                errors["base"] = str(error)
            finally:
                device.pop("password", None)
                self._pending_device = None
            if errors:
                return self.async_show_form(step_id="user", data_schema=vol.Schema({}), errors=errors)
        return self.async_show_form(step_id="confirm", errors=errors, data_schema=vol.Schema({
            vol.Required("confirm_restart", default=False): selector.BooleanSelector(),
        }))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return ChimeOptionsFlow()


class ChimeOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        errors = {}
        entry = self.config_entry
        if user_input is not None:
            runtime = getattr(entry, "runtime_data", None)
            if runtime is None:
                errors["base"] = "integration_not_ready"
            else:
                try:
                    repeats = user_input["repeats"]
                    if isinstance(repeats, bool) or not isinstance(repeats, (int, float)) or not float(repeats).is_integer():
                        raise AudioError("invalid_repeats")
                    options = await runtime.async_options(user_input["gain_db"], int(repeats),
                                                           user_input.get("audio_file"))
                    return self.async_create_entry(title="", data=options)
                except AudioError as error:
                    errors["base"] = str(error)
                except (OSError, ValueError):
                    errors["base"] = "invalid_wav"
        return self.async_show_form(step_id="init", errors=errors, data_schema=vol.Schema({
            vol.Optional("audio_file"): selector.FileSelector(selector.FileSelectorConfig(accept=".wav,audio/wav")),
            vol.Required("gain_db", default=entry.options.get("gain_db", DEFAULT_GAIN)):
                selector.NumberSelector(selector.NumberSelectorConfig(min=-30, max=30, step=1,
                    unit_of_measurement="dB", mode=selector.NumberSelectorMode.BOX)),
            vol.Required("repeats", default=entry.options.get("repeats", DEFAULT_REPEATS)):
                selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=10, step=1,
                    mode=selector.NumberSelectorMode.BOX)),
        }))
