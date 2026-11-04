"""HTTP client for the SDK bridge app's local API (see hikvision_sdk/.../server.py)."""

from __future__ import annotations

import asyncio

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
            raise BridgeError(error if error in ("busy", "call_active") else "app_error")
        raise BridgeError(_HTTP_CODES.get(status, "app_error"))

    async def status(self, name: str) -> dict:
        return await self._request("GET", name)

    async def broadcast(self, name: str, pcm: bytes) -> float:
        return float((await self._request("POST", name, "/broadcast", pcm))["duration"])

    async def stop(self, name: str) -> bool:
        return bool((await self._request("DELETE", name, "/broadcast"))["stopped"])
