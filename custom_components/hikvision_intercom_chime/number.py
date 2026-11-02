"""Persisted gain and repetition controls; setting either is silent."""

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory

from .entity import ChimeEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([ChimeNumber(entry, "gain_db"), ChimeNumber(entry, "repeats")])


class ChimeNumber(ChimeEntity, NumberEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_step = 1

    def __init__(self, entry, key):
        super().__init__(entry, key)
        self.key = key
        if key == "gain_db":
            self._attr_native_min_value, self._attr_native_max_value = -30, 30
            self._attr_native_unit_of_measurement = "dB"
            self._attr_icon = "mdi:volume-high"
        else:
            self._attr_native_min_value, self._attr_native_max_value = 1, 10
            self._attr_icon = "mdi:repeat"

    @property
    def native_value(self):
        settings = self.runtime.settings
        return settings.gain_db if self.key == "gain_db" else settings.repeats

    async def async_set_native_value(self, value):
        await self.runtime.async_set_number(self.key, value)
