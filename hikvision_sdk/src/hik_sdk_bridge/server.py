"""Authenticated JSON/HTTP API for the Home Assistant integration.

Contract (all JSON except the broadcast body, which is raw PCM16LE 8 kHz mono):
  GET    /v1/health                      -> 200 {"status": "ok", "version": ...}   (no auth)
  GET    /v1/stations/<name>             -> 200 station status | 404
  POST   /v1/stations/<name>/broadcast   -> 202 {"duration": s} | 400 | 404 | 409 | 413 | 503
  DELETE /v1/stations/<name>/broadcast   -> 200 {"stopped": bool} | 404
Errors are {"error": <code>}. Everything except /v1/health needs
``Authorization: Bearer <token>``.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import logging
import re

from . import __version__
from .station import MAX_PCM_BYTES, Station, StationError

_LOGGER = logging.getLogger(__name__)
_STATUS = {"invalid_audio": 400, "busy": 409, "call_active": 409, "station_offline": 503}
_STATION_PATH = re.compile(r"/v1/stations/([a-z0-9][a-z0-9_-]{0,63})(/broadcast)?\Z")


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, token: str, stations: dict[str, Station]) -> None:
        super().__init__(address, Handler)
        self.token = token.encode("utf8")
        self.stations = stations


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 15  # a stalled client must not hold a worker thread

    server: BridgeServer

    def log_message(self, format, *args) -> None:  # noqa: A002 - stdlib signature
        _LOGGER.debug("%s %s", self.command, self.path.split("?", 1)[0])

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf8")
        if status >= 300:
            # A rejected request may leave its body unread; never reuse the socket.
            self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        scheme, _, presented = header.partition(" ")
        return scheme == "Bearer" and hmac.compare_digest(
            presented.encode("utf8"), self.server.token)

    def _route(self):
        """Return (station, is_broadcast) or None after sending the error."""
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return None
        match = _STATION_PATH.fullmatch(self.path)
        station = self.server.stations.get(match.group(1)) if match else None
        if station is None:
            self._send(404, {"error": "not_found"})
            return None
        return station, bool(match.group(2))

    def do_GET(self) -> None:
        if self.path == "/v1/health":
            self._send(200, {"status": "ok", "version": __version__})
            return
        routed = self._route()
        if routed and not routed[1]:
            self._send(200, routed[0].status())
        elif routed:
            self._send(404, {"error": "not_found"})

    def do_POST(self) -> None:
        routed = self._route()
        if routed is None:
            return
        station, is_broadcast = routed
        if not is_broadcast:
            self._send(404, {"error": "not_found"})
            return
        if self.headers.get("Transfer-Encoding"):
            self._send(411, {"error": "length_required"})
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._send(411, {"error": "length_required"})
            return
        if not 0 <= length <= MAX_PCM_BYTES:
            self._send(413, {"error": "too_large"})
            return
        body = self.rfile.read(length)
        try:
            duration = station.start_broadcast(body)
        except StationError as error:
            self._send(_STATUS[error.code], {"error": error.code})
            return
        self._send(202, {"duration": duration})

    def do_DELETE(self) -> None:
        routed = self._route()
        if routed and routed[1]:
            self._send(200, {"stopped": routed[0].stop_broadcast()})
        elif routed:
            self._send(404, {"error": "not_found"})
