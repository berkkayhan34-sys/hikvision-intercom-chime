"""Only explicit button presses can start or stop this integration's audio."""

from homeassistant.components.button import ButtonEntity

from .entity import ChimeEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([ChimeButton(entry, "play"), ChimeButton(entry, "stop")])


class ChimeButton(ChimeEntity, ButtonEntity):
    def __init__(self, entry, key):
        super().__init__(entry, key)
        self.key = key
        self._attr_icon = "mdi:bell-ring" if key == "play" else "mdi:stop-circle-outline"

    async def async_press(self):
        if self.key == "play":
            await self.runtime.async_play()
        else:
            await self.runtime.async_stop()
