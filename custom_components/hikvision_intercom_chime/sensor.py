"""Request state, never an unverified 'audible playback succeeded' claim."""

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from homeassistant.const import EntityCategory

from .entity import ChimeEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([ChimeStatus(entry)])


class ChimeStatus(ChimeEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["idle", "sending", "error"]
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:information-outline"

    def __init__(self, entry):
        super().__init__(entry, "status")

    @property
    def native_value(self):
        return self.runtime.status

    @property
    def extra_state_attributes(self):
        return {"last_error": self.runtime.last_error}
