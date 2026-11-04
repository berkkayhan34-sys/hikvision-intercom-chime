"""HTTP client for the SDK bridge app's local API (see hikvision_sdk/.../server.py)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, AsyncIterator
from contextlib import asynccontextmanager

import aiohttp

from .const import APP_PORT


class BridgeError(Exception):
    """Fixed code (translation key); never contains URLs, tokens or response bodies."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


_HTTP_CODES = {401: "app_unauthorized", 404: "station_unknown", 503: "station_offline",
               400: "invalid_audio", 413: "invalid_audio"}


class BridgeClient:
    def __init__(self, session: aiohttp.ClientSession, hostname: str, token: str) -> None:
        self._session = session
        self._base = f"http://{hostname}:{APP_PORT}/v1/stations"
        self._headers = {"Authorization": f"Bearer {token}"}

    async def _request(self, method: str, name: str, suffix: str = "", data: bytes | None = None):
        headers = dict(self._headers)
        if data is not None:
            headers["Content-Type"] = "application/octet-stream"
        try:
            async with asyncio.timeout(15):
                async with self._session.request(
                        method, f"{self._base}/{name}{suffix}", headers=headers, data=data) as response:
                    payload = await response.json(content_type=None)
                    status = response.status
        except (aiohttp.ClientError, TimeoutError, ValueError):
            raise BridgeError("app_unreachable") from None
        if status < 300:
            return payload
        if status == 409:
            error = payload.get("error") if isinstance(payload, dict) else None
            raise BridgeError(error if error in ("busy", "call_active", "no_call") else "app_error")
        raise BridgeError(_HTTP_CODES.get(status, "app_error"))

    async def status(self, name: str) -> dict:
        return await self._request("GET", name)

    async def broadcast(self, name: str, pcm: bytes) -> float:
        return float((await self._request("POST", name, "/broadcast", pcm))["duration"])

    async def stop(self, name: str) -> bool:
        return bool((await self._request("DELETE", name, "/broadcast"))["stopped"])

    async def talk(self, name: str, frames: AsyncIterable[bytes]) -> int:
        """Stream mu-law frames into the answered call while they are produced; returns the frames the app sent."""
        try:
            async with self._session.post(
                    f"{self._base}/{name}/call/talk", headers=self._headers, data=frames,
                    timeout=aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=30)) as response:
                payload = await response.json(content_type=None)
                status = response.status
        except (aiohttp.ClientError, TimeoutError, ValueError):
            raise BridgeError("app_unreachable") from None
        if status < 300:
            return int(payload["frames"])
        if status == 409:
            error = payload.get("error") if isinstance(payload, dict) else None
            raise BridgeError(error if error in ("busy", "no_call") else "app_error")
        raise BridgeError(_HTTP_CODES.get(status, "app_error"))

    @asynccontextmanager
    async def listen(self, name: str) -> AsyncIterator[AsyncIterator[bytes]]:
        """The door's audio as raw mu-law chunks until the call ends or the block is left."""
        try:
            response = await self._session.get(
                f"{self._base}/{name}/call/listen", headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=30))
        except (aiohttp.ClientError, TimeoutError):
            raise BridgeError("app_unreachable") from None
        try:
            if response.status == 409:
                payload = await response.json(content_type=None)
                raise BridgeError("no_call" if payload.get("error") == "no_call" else "app_error")
            if response.status >= 300:
                raise BridgeError(_HTTP_CODES.get(response.status, "app_error"))
            yield response.content.iter_any()
        finally:
            response.release()

    async def stop_call(self, name: str) -> bool:
        return bool((await self._request("DELETE", name, "/call"))["stopped"])
