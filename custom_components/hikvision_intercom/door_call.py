"""Talking and listening during a door call, through the SDK bridge app."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable
from contextlib import asynccontextmanager
import logging

from homeassistant.core import HomeAssistant

from .bridge_client import BridgeError
from .call_audio import speech_frames
from .chime import ChimeRuntime
from .const import DOMAIN
from .hikconnect_api import HikConnectError

_LOGGER = logging.getLogger(__name__)
ANSWER_WAIT_SECONDS = 5.0


class DoorCall:
    """One station's call audio. ``talk`` answers a ringing call first (the person speaking means to take it)."""

    def __init__(self, hass: HomeAssistant, entry_id: str, chime: ChimeRuntime) -> None:
        self._hass, self._entry_id, self._chime = hass, entry_id, chime
        self._lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return self._chime.station_name

    async def _state(self) -> str:
        status = await self._chime.link.call(lambda client: client.status(self.name))
        return status.get("call_status", "unknown")

    async def _answer(self) -> None:
        serial = self._chime.device_serial
        cloud = (self._hass.data.get(DOMAIN, {}).get(self._entry_id) or {}).get("client")
        if serial is None or cloud is None:
            raise BridgeError("no_call")  # which cloud device rings is not known, so nothing is answered blindly
        try:
            await self._hass.async_add_executor_job(cloud.answer_call, serial)
        except HikConnectError:
            raise BridgeError("answer_failed") from None

    async def _in_call(self) -> None:
        state = await self._state()
        if state == "ringing":
            await self._answer()
            loop = asyncio.get_running_loop()
            deadline = loop.time() + ANSWER_WAIT_SECONDS
            while state != "in_call" and loop.time() < deadline:
                await asyncio.sleep(0.5)
                state = await self._state()
        if state != "in_call":
            raise BridgeError("no_call")

    async def talk(self, stream: AsyncIterable[bytes]) -> int:
        """Speech (16 kHz PCM, as Assist delivers it) into the call; returns the 20 ms frames sent."""
        if self._lock.locked():
            raise BridgeError("busy")
        async with self._lock:
            await self._in_call()
            client = await self._chime.link.client()
            try:
                return await client.talk(self.name, speech_frames(stream))
            except BridgeError as error:
                if error.code in ("app_unreachable", "app_unauthorized"):
                    self._chime.link.invalidate()
                raise

    @asynccontextmanager
    async def listen(self):
        """The door's audio (raw mu-law chunks) for as long as the call lasts."""
        client = await self._chime.link.client()
        try:
            async with client.listen(self.name) as chunks:
                yield chunks
        except BridgeError as error:
            if error.code in ("app_unreachable", "app_unauthorized"):
                self._chime.link.invalidate()
            raise
