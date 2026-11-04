"""The door's audio during a call as a live WAV stream, for the dashboard's Listen button.

Authenticated like every Home Assistant API view; a browser audio element cannot send a header, so the dashboard asks
Home Assistant for a short-lived signed path (``auth/sign_path``) and plays that. Administrators only.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web
from homeassistant.components.http import KEY_HASS, KEY_HASS_USER, HomeAssistantView

from .bridge_client import BridgeError
from .call_audio import mulaw_to_pcm16le, wav_header
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
_STATUS = {"no_call": 409, "busy": 409, "station_offline": 503, "app_unreachable": 503}


class DoorListenView(HomeAssistantView):
    url = "/api/hikvision_intercom/listen/{entry_id}/{subentry_id}"
    name = "api:hikvision_intercom:listen"
    requires_auth = True

    async def get(
        self, request: web.Request, entry_id: str, subentry_id: str
    ) -> web.StreamResponse:
        user = request.get(KEY_HASS_USER)
        if user is None or not user.is_admin:
            return web.json_response({"error": "forbidden"}, status=403)
        data = request.app[KEY_HASS].data.get(DOMAIN, {}).get(entry_id)
        door_call = (data or {}).get("door_calls", {}).get(subentry_id)
        if door_call is None:
            return web.json_response({"error": "not_found"}, status=404)
        response = web.StreamResponse(
            headers={"Content-Type": "audio/wav", "Cache-Control": "no-store"}
        )
        try:
            async with door_call.listen() as chunks:
                await response.prepare(request)
                await response.write(wav_header())
                async for chunk in chunks:
                    await response.write(mulaw_to_pcm16le(chunk))
        except BridgeError as error:
            if response.prepared:
                return response  # the call ended mid-stream: the stream simply ends
            return web.json_response(
                {"error": error.code}, status=_STATUS.get(error.code, 502)
            )
        except ConnectionResetError, asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.debug("listen stream ended", exc_info=True)
        return response
