"""Pure validation and non-destructive merging of the SDK bridge app's options."""

from __future__ import annotations

import ipaddress
import re
import secrets
from copy import deepcopy

from .const import DEFAULT_SDK_PORT


class SetupError(ValueError):
    """Fixed translation key, with no credential-bearing exception message."""


_STATION_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_PRIVATE_NETWORKS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")


def validate_station(host: str, port: int, username: str, password: str) -> None:
    try:
        address = ipaddress.IPv4Address(host)
    except (ipaddress.AddressValueError, TypeError):
        raise SetupError("invalid_host") from None
    if not any(
        address in ipaddress.IPv4Network(network) for network in _PRIVATE_NETWORKS
    ):
        raise SetupError("invalid_host")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise SetupError("invalid_port")
    for value in (username, password):
        if (
            not isinstance(value, str)
            or not value
            or len(value.encode("utf8")) > 64
            or any(ord(character) < 32 for character in value)
        ):
            raise SetupError("invalid_credentials")


def station_name(host: str) -> str:
    """Stable app-side identifier derived from the address, e.g. 192.168.0.50 -> st_192_168_0_50."""
    name = "st_" + host.replace(".", "_")
    if not _STATION_NAME.fullmatch(name):
        raise SetupError("invalid_host")
    return name


def merge_station(options: dict, station: dict) -> dict:
    """Return new app options with ``station`` added or updated by name.

    Other stations are preserved untouched, a missing or malformed API token is
    generated (never overwritten when valid), and an address that already
    belongs to a different station name is refused.
    """
    result = deepcopy(options)
    token = result.get("token")
    if not isinstance(token, str) or not 32 <= len(token) <= 128:
        result["token"] = secrets.token_urlsafe(32)
    stations = result.setdefault("stations", [])
    name = station["name"]
    for item in stations:
        if item.get("name") != name and (
            item.get("host") == station["host"]
            and item.get("port", DEFAULT_SDK_PORT) == station["port"]
        ):
            raise SetupError("duplicate_station")
    entry = {
        key: station[key] for key in ("name", "host", "port", "username", "password")
    }
    for index, item in enumerate(stations):
        if item.get("name") == name:
            stations[index] = entry
            break
    else:
        stations.append(entry)
    return result


def without_station(options: dict, name: str) -> dict:
    result = deepcopy(options)
    result["stations"] = [
        item for item in result.get("stations", []) if item.get("name") != name
    ]
    return result
