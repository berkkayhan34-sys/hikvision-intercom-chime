"""The door's audio during a call, for a dashboard: a live WAV stream to listen to and a WebSocket to talk into.

Both are authenticated like every Home Assistant API view; neither a browser audio element nor a browser WebSocket can send a
header, so the dashboard asks Home Assistant for a short-lived signed path (``auth/sign_path``) and uses that. Any signed-in user
may use them: the household answers the door, and the dashboard's own access rules decide who is shown the controls. The two paths are published on the call status sensor as ``listen_path`` and ``talk_path``, which is how a dashboard
that knows no brand finds them.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import WSMsgType, web
from homeassistant.components.http import KEY_HASS, KEY_HASS_USER, HomeAssistantView

from .bridge_client import BridgeError
from .call_audio import mulaw_to_pcm16le, wav_header
from .const import DOMAIN, LISTEN_PATH, TALK_PATH

_LOGGER = logging.getLogger(__name__)
_STATUS = {"no_call": 409, "busy": 409, "station_offline": 503, "app_unreachable": 503}
TALK_MESSAGE_LIMIT = (
    64 * 1024
)  # one WebSocket message of speech; real ones are a few KiB
TALK_HEARTBEAT = 20


class DoorListenView(HomeAssistantView):
    url = LISTEN_PATH
    name = "api:hikvision_intercom:listen"
    requires_auth = True

    async def get(
        self, request: web.Request, entry_id: str, subentry_id: str
    ) -> web.StreamResponse:
        if request.get(KEY_HASS_USER) is None:
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
        except (ConnectionResetError, asyncio.CancelledError):
            raise
        except Exception:
            _LOGGER.debug("listen stream ended", exc_info=True)
        return response


class DoorTalkView(HomeAssistantView):
    """Speech into an answered call. The client sends binary messages of 16 kHz, 16-bit, little-endian mono PCM (what
    Assist delivers); the call ends the talk when it ends or the client closes. A problem is sent as one text message
    ``{"error": code}`` before the socket closes. One talker at a time (``busy``)."""

    url = TALK_PATH
    name = "api:hikvision_intercom:talk"
    requires_auth = True

    async def get(
        self, request: web.Request, entry_id: str, subentry_id: str
    ) -> web.StreamResponse:
        if request.get(KEY_HASS_USER) is None:
            return web.json_response({"error": "forbidden"}, status=403)
        data = request.app[KEY_HASS].data.get(DOMAIN, {}).get(entry_id)
        door_call = (data or {}).get("door_calls", {}).get(subentry_id)
        if door_call is None:
            return web.json_response({"error": "not_found"}, status=404)
        socket = web.WebSocketResponse(
            heartbeat=TALK_HEARTBEAT, max_msg_size=TALK_MESSAGE_LIMIT
        )
        await socket.prepare(request)

        async def speech():
            async for message in socket:
                if message.type is WSMsgType.BINARY:
                    yield message.data

        try:
            await door_call.talk(speech())
        except BridgeError as error:
            await socket.send_json({"error": error.code})
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.debug("talk stream ended", exc_info=True)
            await socket.send_json({"error": "failed"})
        finally:
            await socket.close()
        return socket
