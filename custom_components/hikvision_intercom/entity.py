"""Shared device identity and availability for the chime entities of one station.

The chime entities join the Hik-Connect doorbell's own device when the account has
exactly one device, so the doorbell is one device in Home Assistant. Entities belong to
the config entry, not the station subentry, so the merged device is not also listed
under the subentry; removing a station removes its entities explicitly.
"""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .chime import ChimeRuntime
from .const import DOMAIN


def chime_device_info(chime: ChimeRuntime) -> DeviceInfo:
    """The device a station's entities belong to: the cloud doorbell device when shared, else a device of its own."""
    if chime.device_serial:
        # Identifiers only: the cloud side owns the name, model and firmware.
        return DeviceInfo(identifiers={(DOMAIN, chime.device_serial)})
    return DeviceInfo(
        identifiers={(DOMAIN, f"station_{chime.subentry_id}")},
        name=chime.subentry.title,
        manufacturer="Hikvision",
        model="Indoor station chime",
    )


class ChimeEntity(CoordinatorEntity):
    _attr_has_entity_name = True

    def __init__(self, chime: ChimeRuntime, key: str) -> None:
        super().__init__(chime.coordinator)
        self.runtime = chime
        self._attr_unique_id = f"{chime.subentry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = chime_device_info(chime)

    @property
    def available(self) -> bool:
        # Playback needs a reachable bridge app and a logged-in station.
        return super().available and bool(
            (self.coordinator.data or {}).get("connected")
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.runtime.listen(self.async_write_ha_state))
