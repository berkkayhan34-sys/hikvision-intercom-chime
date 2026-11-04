"""Shared device identity and availability for the chime entities of one station."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .chime import ChimeRuntime
from .const import DOMAIN


class ChimeEntity(CoordinatorEntity):
    _attr_has_entity_name = True

    def __init__(self, chime: ChimeRuntime, key: str) -> None:
        super().__init__(chime.coordinator)
        self.runtime = chime
        self._attr_unique_id = f"{chime.subentry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"station_{chime.subentry_id}")},
            name=chime.subentry.title,
            manufacturer="Hikvision",
            model="Indoor station chime",
        )

    @property
    def available(self) -> bool:
        # Playback needs a reachable bridge app and a logged-in station.
        return super().available and bool((self.coordinator.data or {}).get("connected"))

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.runtime.listen(self.async_write_ha_state))
