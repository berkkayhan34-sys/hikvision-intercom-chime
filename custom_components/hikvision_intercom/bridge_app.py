"""Supervisor boundary: locate the SDK bridge app and manage its station list.

Credentials pass through here exactly once, into the app's own Supervisor
options. They are never returned, logged or stored in a config entry.
"""

from __future__ import annotations

import asyncio
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant

from .const import APP_SLUG_SUFFIX
from .validation import SetupError, merge_station, without_station

_SLUG = re.compile(rf"(local|[0-9a-f]{{8}})_{APP_SLUG_SUFFIX}\Z")


@dataclass(frozen=True)
class AppInfo:
    slug: str
    hostname: str
    running: bool
    options: dict[str, Any]


def _supervisor_client(hass: HomeAssistant):
    # Lazy import: Container installs must reach a friendly error, not an ImportError.
    from homeassistant.components.hassio.handler import get_supervisor_client

    return get_supervisor_client(hass)


def _is_running(state: Any) -> bool:
    return getattr(state, "value", state) == "started"


async def find_app(hass: HomeAssistant) -> AppInfo:
    if "hassio" not in hass.config.components:
        raise SetupError("supervisor_required")
    client = _supervisor_client(hass)
    try:
        async with asyncio.timeout(10):
            installed = await client.addons.list()
            slugs = [item.slug for item in installed if _SLUG.fullmatch(item.slug)]
            if len(slugs) != 1:
                raise SetupError("app_required" if not slugs else "app_ambiguous")
            info = await client.addons.addon_info(slugs[0])
    except SetupError:
        raise
    except Exception:
        # Supervisor errors may embed configuration; they must not reach logs or the UI.
        raise SetupError("app_required") from None
    return AppInfo(
        info.slug, info.hostname, _is_running(info.state), dict(info.options or {})
    )


async def _apply(hass: HomeAssistant, app: AppInfo, options: dict) -> None:
    """Write options and (re)start the app; restore the old options on any failure."""
    from aiohasupervisor.models import AddonsOptions

    client = _supervisor_client(hass)
    previous = deepcopy(app.options)
    written = False
    try:
        if options != previous:
            written = (
                True  # a timeout may land after Supervisor already applied the write
            )
            await client.addons.set_addon_options(
                app.slug, AddonsOptions(config=options)
            )
            if app.running:
                await client.addons.restart_addon(app.slug)
            else:
                await client.addons.start_addon(app.slug)
        elif not app.running:
            await client.addons.start_addon(app.slug)
    except (Exception, asyncio.CancelledError) as failure:
        if written:
            try:
                await client.addons.set_addon_options(
                    app.slug, AddonsOptions(config=previous)
                )
                if app.running:
                    await client.addons.restart_addon(app.slug)
            except Exception:
                raise SetupError("app_rollback_failed") from None
        if isinstance(failure, asyncio.CancelledError):
            raise
        raise SetupError("app_config_failed") from None


def _lock(hass: HomeAssistant) -> asyncio.Lock:
    from .const import DOMAIN

    return hass.data.setdefault(DOMAIN, {}).setdefault("app_lock", asyncio.Lock())


async def configure_station(hass: HomeAssistant, station: dict) -> AppInfo:
    """Add or update a station in the app options. Called only after the user confirmed
    that the app may be restarted. Returns the fresh app info (token included)."""
    async with _lock(hass):
        app = await find_app(hass)
        await _apply(hass, app, merge_station(app.options, station))
        return await find_app(hass)


async def remove_station(hass: HomeAssistant, name: str) -> None:
    """Best effort on subentry removal: never blocks removal if the app is gone."""
    async with _lock(hass):
        try:
            app = await find_app(hass)
        except SetupError:
            return
        options = without_station(app.options, name)
        if options != app.options:
            await _apply(hass, app, options)
