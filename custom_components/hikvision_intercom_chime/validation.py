"""Pure validation and non-destructive app-option merging."""

from __future__ import annotations

from copy import deepcopy
import ipaddress
import re
import unicodedata

from .const import DEFAULT_PORT


class SetupError(ValueError):
    """Fixed translation key, with no credential-bearing exception message."""


def validate_device(host: str, port: int, username: str, password: str) -> None:
    try:
        address = ipaddress.IPv4Address(host)
    except (ipaddress.AddressValueError, TypeError):
        raise SetupError("invalid_host") from None
    networks = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    if not any(address in ipaddress.IPv4Network(network) for network in networks):
        raise SetupError("invalid_host")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise SetupError("invalid_port")
    for value in (username, password):
        if (not isinstance(value, str) or not value
                or len(value.encode("utf8")) > 64
                or any(ord(character) < 32 for character in value)):
            raise SetupError("invalid_credentials")


def merge_device(options: dict, device: dict, bridge_name: str | None) -> tuple[dict, str]:
    """Update only the matching device; preserve other devices/system/MQTT fields."""
    result = deepcopy(options)
    devices = result.setdefault("doorbells", [])
    host, port = device["host"], device["port"]
    matches = [item for item in devices if (
        item.get("name") == bridge_name if bridge_name else
        item.get("ip") == host and item.get("port", DEFAULT_PORT) == port
    )]
    if len(matches) > 1:
        raise SetupError("ambiguous_bridge_device")
    if matches:
        target = matches[0]
    else:
        if bridge_name:
            raise SetupError("bridge_device_missing")
        # Discard only the exact vendor default, not user-created partial entries.
        placeholder = {"name": "", "ip": "", "username": "admin", "password": "", "scenes": False}
        devices[:] = [item for item in devices if item != placeholder]
        target = {"name": "chime_" + host.replace(".", "_"),
                  "scenes": False, "snapshot": False, "output_relays": 0}
        if any(item.get("name") == target["name"] for item in devices):
            raise SetupError("ambiguous_bridge_device")
        devices.append(target)
    name = target.get("name", "")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9 _-]{1,64}", name):
        raise SetupError("invalid_bridge_name")
    if any(item is not target and bridge_entity_prefix(item.get("name", ""))
           == bridge_entity_prefix(name) for item in devices):
        raise SetupError("ambiguous_bridge_device")
    if any(item is not target and item.get("ip") == host
           and item.get("port", DEFAULT_PORT) == port for item in devices):
        raise SetupError("ambiguous_bridge_device")
    target.update(ip=host, port=port, username=device["username"], password=device["password"])
    return result, name


def bridge_entity_prefix(name: str) -> str:
    # Exact public bridge convention; entity IDs are resolved by registry unique_id.
    return "".join(character for character in unicodedata.normalize("NFD", name.lower())
                   if character.isalnum())
