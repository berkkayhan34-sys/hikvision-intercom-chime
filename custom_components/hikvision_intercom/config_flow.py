"""Config flow: one setup path (bridge app, Hik-Connect account, local station) and a
station subentry per indoor station for later additions and the chime sound."""

from __future__ import annotations

from typing import Any

import probatio as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryData,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    FileSelector,
    FileSelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .audio import AudioError
from .bridge_app import configure_station, find_app
from .const import (
    APP_REPOSITORY_URL,
    CALL_POLL_INTERVAL,
    CONF_ACCOUNT,
    CONF_BASE_URL,
    CONF_CALL_POLL_INTERVAL,
    CONF_NATIVE_STREAM,
    CONF_PASSWORD,
    CONF_SERVER,
    DEFAULT_BASE_URL,
    DEFAULT_GAIN,
    DEFAULT_REPEATS,
    DEFAULT_SDK_PORT,
    DEFAULT_STATION_TITLE,
    DOMAIN,
    MAX_CALL_POLL_INTERVAL,
    MIN_CALL_POLL_INTERVAL,
    NATIVE_STREAM_CODECS,
    SERVER_CUSTOM,
    SERVERS,
    SUBENTRY_STATION,
    native_stream_mode,
)
from .hikconnect_api import HikConnectAuthError, HikConnectClient
from .validation import SetupError, station_name, validate_station


def _resolve_base_url(user_input: dict[str, Any]) -> str | None:
    """Map the selected server (or custom override) to a base URL."""
    server = user_input.get(CONF_SERVER, DEFAULT_BASE_URL)
    if server != SERVER_CUSTOM:
        return server
    custom = (user_input.get(CONF_BASE_URL) or "").strip().rstrip("/")
    if not custom:
        return None
    if not custom.startswith(("http://", "https://")):
        custom = f"https://{custom}"
    return custom


def _server_defaults(base_url: str) -> tuple[str, str]:
    """(server-select default, custom-url default) for a stored base URL."""
    if base_url in SERVERS:
        return base_url, ""
    return SERVER_CUSTOM, base_url or ""


def _schema(
    account: str, server: str, base_url: str, password_optional: bool
) -> vol.Schema:
    options = [
        SelectOptionDict(value=url, label=label) for url, label in SERVERS.items()
    ]
    options.append(SelectOptionDict(value=SERVER_CUSTOM, label="Custom…"))
    password = (
        vol.Optional(CONF_PASSWORD)
        if password_optional
        else vol.Required(CONF_PASSWORD)
    )
    return vol.Schema(
        {
            vol.Required(CONF_ACCOUNT, default=account): str,
            password: str,
            vol.Required(CONF_SERVER, default=server): SelectSelector(
                SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
            ),
            vol.Optional(CONF_BASE_URL, default=base_url): str,
        }
    )


def _port(value) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not float(value).is_integer()
    ):
        raise SetupError("invalid_port")
    return int(value)


def _station_fields(title_default: str | None) -> dict:
    fields: dict = {}
    if title_default is not None:
        fields[vol.Required("title", default=title_default)] = TextSelector()
    fields.update(
        {
            vol.Required("host"): TextSelector(),
            vol.Required("port", default=DEFAULT_SDK_PORT): NumberSelector(
                NumberSelectorConfig(min=1, max=65535, mode=NumberSelectorMode.BOX)
            ),
            vol.Required("username", default="admin"): TextSelector(),
            vol.Required("password"): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
        }
    )
    return fields


def _checked_station(user_input: dict[str, Any]) -> dict[str, Any]:
    """Validate the station form and add the app-side name. Raises SetupError."""
    station = dict(user_input, port=_port(user_input["port"]))
    validate_station(
        station["host"], station["port"], station["username"], station["password"]
    )
    station["name"] = station_name(station["host"])
    return station


def _station_unique_id(station: dict[str, Any]) -> str:
    return f"{station['host']}:{station['port']}"


def _station_data(station: dict[str, Any]) -> dict[str, Any]:
    # The password lives only in the app's options, never in the entry.
    data = {key: station[key] for key in ("name", "host", "port", "username")}
    data.update(gain_db=DEFAULT_GAIN, repeats=DEFAULT_REPEATS)
    return data


class HikConnectLocalConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Hik-Connect account login."""

    VERSION = 1

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry: ConfigEntry):
        return {SUBENTRY_STATION: StationSubentryFlow}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return HikConnectOptionsFlow()

    _account: dict[str, Any] | None = None
    _station: dict[str, Any] | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The SDK bridge app comes first: without it the chime cannot work."""
        try:
            await find_app(self.hass)
        except SetupError as error:
            if str(error) == "supervisor_required":
                return self.async_abort(reason="supervisor_required")
            return await self.async_step_app(error=str(error))
        return await self.async_step_account()

    async def async_step_app(
        self, user_input: dict[str, Any] | None = None, error: str | None = None
    ) -> ConfigFlowResult:
        """Ask for the app to be installed; Submit checks again."""
        errors: dict[str, str] = {}
        if error is not None:
            errors["base"] = error
        elif user_input is not None:
            try:
                await find_app(self.hass)
            except SetupError as failure:
                if str(failure) == "supervisor_required":
                    return self.async_abort(reason="supervisor_required")
                errors["base"] = str(failure)
            else:
                return await self.async_step_account()
        return self.async_show_form(
            step_id="app",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={
                "repository_url": APP_REPOSITORY_URL,
                "add_repository_link": (
                    "https://my.home-assistant.io/redirect/supervisor_add_addon_repository/"
                    f"?repository_url={APP_REPOSITORY_URL}"
                ),
            },
        )

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            base_url = _resolve_base_url(user_input)
            if base_url is None:
                errors["base"] = "custom_url_required"
            else:
                client = HikConnectClient(
                    user_input[CONF_ACCOUNT], user_input[CONF_PASSWORD], base_url
                )
                try:
                    await self.hass.async_add_executor_job(client.login)
                except HikConnectAuthError:
                    errors["base"] = "invalid_auth"
                except Exception:
                    errors["base"] = "cannot_connect"
                else:
                    await self.async_set_unique_id(user_input[CONF_ACCOUNT].lower())
                    self._abort_if_unique_id_configured()
                    self._account = {
                        CONF_ACCOUNT: user_input[CONF_ACCOUNT],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_BASE_URL: base_url,
                    }
                    return await self.async_step_station()
        return self.async_show_form(
            step_id="account",
            data_schema=_schema("", DEFAULT_BASE_URL, "", password_optional=False),
            errors=errors,
        )

    async def async_step_station(
        self, user_input: dict[str, Any] | None = None, error: str | None = None
    ) -> ConfigFlowResult:
        """The local indoor station that plays the chime."""
        if self._account is None:
            return self.async_abort(reason="setup_expired")
        errors: dict[str, str] = {}
        if error is not None:
            errors["base"] = error
        elif user_input is not None:
            try:
                self._station = _checked_station(user_input)
            except SetupError as failure:
                errors["base"] = str(failure)
            else:
                return await self.async_step_confirm()
        return self.async_show_form(
            step_id="station",
            data_schema=vol.Schema(_station_fields(None)),
            errors=errors,
        )

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        station, account = self._station, self._account
        if station is None or account is None:
            return self.async_abort(reason="setup_expired")
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get("confirm_restart"):
                errors["base"] = "confirmation_required"
            else:
                try:
                    await configure_station(self.hass, station)
                except SetupError as failure:
                    # Nothing was created; the password is dropped and asked for again.
                    station.pop("password", None)
                    self._station = None
                    return await self.async_step_station(error=str(failure))
                data = _station_data(station)
                unique_id = _station_unique_id(station)
                station.pop("password", None)
                self._station = None
                return self.async_create_entry(
                    title=account[CONF_ACCOUNT],
                    data=account,
                    subentries=[
                        ConfigSubentryData(
                            subentry_type=SUBENTRY_STATION,
                            title=DEFAULT_STATION_TITLE,
                            unique_id=unique_id,
                            data=data,
                        )
                    ],
                )
        return self.async_show_form(
            step_id="confirm",
            errors=errors,
            data_schema=vol.Schema(
                {vol.Required("confirm_restart", default=False): BooleanSelector()}
            ),
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change server/account/password without removing the integration."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            base_url = _resolve_base_url(user_input)
            if base_url is None:
                errors["base"] = "custom_url_required"
            else:
                # blank password keeps the existing one
                password = user_input.get(CONF_PASSWORD) or entry.data[CONF_PASSWORD]
                client = HikConnectClient(user_input[CONF_ACCOUNT], password, base_url)
                try:
                    await self.hass.async_add_executor_job(client.login)
                except HikConnectAuthError:
                    errors["base"] = "invalid_auth"
                except Exception:
                    errors["base"] = "cannot_connect"
                else:
                    await self.async_set_unique_id(user_input[CONF_ACCOUNT].lower())
                    self._abort_if_unique_id_mismatch(reason="account_mismatch")
                    return self.async_update_reload_and_abort(
                        entry,
                        data={
                            CONF_ACCOUNT: user_input[CONF_ACCOUNT],
                            CONF_PASSWORD: password,
                            CONF_BASE_URL: base_url,
                        },
                    )
        server, base_url = _server_defaults(
            entry.data.get(CONF_BASE_URL, DEFAULT_BASE_URL)
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_schema(
                entry.data.get(CONF_ACCOUNT, ""),
                server,
                base_url,
                password_optional=True,
            ),
            errors=errors,
        )


class HikConnectOptionsFlow(OptionsFlow):
    """Tune polling and video for an existing entry."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_CALL_POLL_INTERVAL: int(user_input[CONF_CALL_POLL_INTERVAL]),
                    CONF_NATIVE_STREAM: user_input[CONF_NATIVE_STREAM],
                }
            )
        current = self.config_entry.options.get(
            CONF_CALL_POLL_INTERVAL, CALL_POLL_INTERVAL
        )
        native = native_stream_mode(self.config_entry.options)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_CALL_POLL_INTERVAL, default=current
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_CALL_POLL_INTERVAL,
                            max=MAX_CALL_POLL_INTERVAL,
                            step=1,
                            unit_of_measurement="s",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required(CONF_NATIVE_STREAM, default=native): SelectSelector(
                        SelectSelectorConfig(
                            options=list(NATIVE_STREAM_CODECS),
                            mode=SelectSelectorMode.LIST,
                            translation_key=CONF_NATIVE_STREAM,
                        )
                    ),
                }
            ),
        )


class StationSubentryFlow(ConfigSubentryFlow):
    """Add a local indoor station for the doorbell chime, or change its sound."""

    _pending: dict | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        try:
            await find_app(self.hass)
        except SetupError as error:
            return self.async_show_form(
                step_id="user", data_schema=vol.Schema({}), errors={"base": str(error)}
            )
        return await self.async_step_connection()

    async def async_step_connection(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                station = _checked_station(user_input)
                unique_id = _station_unique_id(station)
                if any(
                    sub.unique_id == unique_id
                    for sub in self._get_entry().subentries.values()
                ):
                    return self.async_abort(reason="already_configured")
                self._pending = station
                return await self.async_step_confirm()
            except SetupError as error:
                errors["base"] = str(error)
        return self.async_show_form(
            step_id="connection",
            errors=errors,
            data_schema=vol.Schema(_station_fields(DEFAULT_STATION_TITLE)),
        )

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        station = self._pending
        if station is None:
            return self.async_abort(reason="setup_expired")
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get("confirm_restart"):
                errors["base"] = "confirmation_required"
            else:
                try:
                    await configure_station(self.hass, station)
                except SetupError as error:
                    errors["base"] = str(error)
                else:
                    return self.async_create_entry(
                        title=station["title"],
                        data=_station_data(station),
                        unique_id=_station_unique_id(station),
                    )
                finally:
                    station.pop("password", None)
                    self._pending = None
                return self.async_show_form(
                    step_id="user", data_schema=vol.Schema({}), errors=errors
                )
        return self.async_show_form(
            step_id="confirm",
            errors=errors,
            data_schema=vol.Schema(
                {vol.Required("confirm_restart", default=False): BooleanSelector()}
            ),
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        entry, subentry = self._get_entry(), self._get_reconfigure_subentry()
        errors: dict[str, str] = {}
        if user_input is not None:
            runtime = (
                self.hass.data.get(DOMAIN, {})
                .get(entry.entry_id, {})
                .get("chimes", {})
                .get(subentry.subentry_id)
            )
            if runtime is None:
                errors["base"] = "integration_not_ready"
            else:
                try:
                    repeats = user_input["repeats"]
                    if (
                        isinstance(repeats, bool)
                        or not isinstance(repeats, (int, float))
                        or not float(repeats).is_integer()
                    ):
                        raise AudioError("invalid_repeats")
                    data = await runtime.async_settings_data(
                        user_input["gain_db"],
                        int(repeats),
                        user_input.get("audio_file"),
                    )
                    return self.async_update_and_abort(entry, subentry, data=data)
                except AudioError as error:
                    errors["base"] = str(error)
                except OSError, ValueError:
                    errors["base"] = "invalid_wav"
        return self.async_show_form(
            step_id="reconfigure",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Optional("audio_file"): FileSelector(
                        FileSelectorConfig(accept=".wav,audio/wav")
                    ),
                    vol.Required(
                        "gain_db", default=subentry.data.get("gain_db", DEFAULT_GAIN)
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=-30,
                            max=30,
                            step=1,
                            unit_of_measurement="dB",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required(
                        "repeats", default=subentry.data.get("repeats", DEFAULT_REPEATS)
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=1, max=10, step=1, mode=NumberSelectorMode.BOX
                        )
                    ),
                }
            ),
        )
