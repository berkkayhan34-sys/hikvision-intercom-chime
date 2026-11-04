"""Validated app options. Credentials exist only in the Supervisor options file."""

from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import json
from pathlib import Path
import re

OPTIONS_PATH = Path("/data/options.json")
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_PRIVATE = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")


class ConfigError(ValueError):
    """Message names the offending field only, never a value."""


@dataclass(frozen=True)
class StationConfig:
    name: str
    host: str
    port: int
    username: str = field(repr=False)
    password: str = field(repr=False)


@dataclass(frozen=True)
class Config:
    token: str = field(repr=False)
    stations: tuple[StationConfig, ...]


def _credential(value: object, field_name: str) -> str:
    if (not isinstance(value, str) or not value
            or len(value.encode("utf8")) > 64
            or any(ord(character) < 32 for character in value)):
        raise ConfigError(f"invalid {field_name}")
    return value


def parse_station(raw: dict) -> StationConfig:
    name = raw.get("name")
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ConfigError("invalid station name")
    try:
        address = ipaddress.IPv4Address(raw.get("host"))
    except (ipaddress.AddressValueError, TypeError):
        raise ConfigError("invalid station host") from None
    if not any(address in ipaddress.IPv4Network(net) for net in _PRIVATE):
        raise ConfigError("station host must be a private LAN address")
    port = raw.get("port", 8000)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigError("invalid station port")
    return StationConfig(name, str(address), port,
                         _credential(raw.get("username"), "station username"),
                         _credential(raw.get("password"), "station password"))


def parse(raw: dict) -> Config:
    token = raw.get("token")
    if not isinstance(token, str) or len(token) < 32 or len(token) > 128:
        raise ConfigError("token must be 32-128 characters")
    stations = tuple(parse_station(item) for item in raw.get("stations") or [])
    if len({station.name for station in stations}) != len(stations):
        raise ConfigError("duplicate station name")
    return Config(token, stations)


def load(path: Path = OPTIONS_PATH) -> Config:
    try:
        raw = json.loads(path.read_text("utf8"))
    except (OSError, ValueError):
        raise ConfigError("options file unreadable") from None
    if not isinstance(raw, dict):
        raise ConfigError("options file malformed")
    return parse(raw)
