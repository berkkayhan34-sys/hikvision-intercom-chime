"""Shared device identity and on-demand state updates."""

from homeassistant.helpers.entity import Entity
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN


class ChimeEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry, key):
        self.runtime = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Hikvision",
            model="Local Intercom Chime",
        )

    @property
    def available(self):
        return self.runtime.mqtt_connected

    async def async_added_to_hass(self):
        self.async_on_remove(self.runtime.listen(self.async_write_ha_state))
