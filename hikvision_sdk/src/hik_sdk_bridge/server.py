"""Authenticated JSON/HTTP API for the Home Assistant integration.

Contract (all JSON except the audio bodies: the broadcast body is raw PCM16LE 8 kHz mono, the call audio is raw 8 kHz
G.711 mu-law, 160-byte frames):
  GET    /v1/health                      -> 200 {"status": "ok", "version": ...}   (no auth)
  GET    /v1/stations/<name>             -> 200 station status | 404
  POST   /v1/stations/<name>/broadcast   -> 202 {"duration": s} | 400 | 404 | 409 | 413 | 503
  DELETE /v1/stations/<name>/broadcast   -> 200 {"stopped": bool} | 404
  POST   /v1/stations/<name>/call/talk   -> 200 {"frames": n} | 404 | 409 (no_call, busy) | 413 | 503; the request body
                                            is the audio, sent while it is spoken (chunked), and ends the talk
  GET    /v1/stations/<name>/call/listen -> 200 chunked mu-law audio of the door until the call ends | 404 | 409 | 503
  DELETE /v1/stations/<name>/call        -> 200 {"stopped": bool} | 404
Errors are {"error": <code>}. Everything except /v1/health needs
``Authorization: Bearer <token>``.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import logging
import re
import sys

from . import __version__
from .station import MAX_PCM_BYTES, Station, StationError

_LOGGER = logging.getLogger(__name__)
_STATUS = {"invalid_audio": 400, "busy": 409, "call_active": 409, "no_call": 409, "station_offline": 503}
_STATION_PATH = re.compile(
    r"/v1/stations/([a-z0-9][a-z0-9_-]{0,63})(/broadcast|/call/talk|/call/listen|/call)?\Z")
MAX_TALK_BYTES = 8000 * 600  # ten minutes of 8 kHz mu-law
MAX_CHUNK_BYTES = 64 * 1024


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, token: str, stations: dict[str, Station]) -> None:
        super().__init__(address, Handler)
        self.token = token.encode("utf8")
        self.stations = stations

    def handle_error(self, request, client_address) -> None:
        # A client that drops a connection (a listener closing its page) is routine, not an error worth a traceback.
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            _LOGGER.debug("client connection ended: %s", sys.exc_info()[1].__class__.__name__)
            return
        super().handle_error(request, client_address)


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
        """Return (station, suffix) or None after sending the error."""
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return None
        match = _STATION_PATH.fullmatch(self.path)
        station = self.server.stations.get(match.group(1)) if match else None
        if station is None:
            self._send(404, {"error": "not_found"})
            return None
        return station, match.group(2) or ""

    def do_GET(self) -> None:
        if self.path == "/v1/health":
            self._send(200, {"status": "ok", "version": __version__})
            return
        routed = self._route()
        if routed is None:
            return
        station, suffix = routed
        if not suffix:
            self._send(200, station.status())
        elif suffix == "/call/listen":
            self._listen(station)
        else:
            self._send(404, {"error": "not_found"})

    def _listen(self, station: Station) -> None:
        try:
            listener = station.listen()
        except StationError as error:
            self._send(_STATUS[error.code], {"error": error.code})
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            for data in listener:
                self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
        except OSError:
            pass  # the listener went away
        finally:
            listener.close()
            self.close_connection = True

    def _body_chunks(self):
        """The request body as it arrives (chunked, or one piece with a Content-Length); ValueError when malformed."""
        total = 0
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            while True:
                size = int(self.rfile.readline(64).split(b";", 1)[0].strip(), 16)
                if size == 0:
                    while self.rfile.readline(1024).strip():
                        pass  # trailers
                    return
                total += size
                if size > MAX_CHUNK_BYTES or total > MAX_TALK_BYTES:
                    raise OverflowError
                data = self.rfile.read(size)
                if len(data) != size or self.rfile.readline(4) != b"\r\n":
                    raise ValueError("truncated chunk")
                yield data
        else:
            length = int(self.headers.get("Content-Length", ""))
            if not 0 <= length <= MAX_TALK_BYTES:
                raise OverflowError
            while total < length:
                data = self.rfile.read(min(MAX_CHUNK_BYTES, length - total))
                if not data:
                    raise ValueError("truncated body")
                total += len(data)
                yield data

    def _talk(self, station: Station) -> None:
        try:
            frames = station.talk(self._body_chunks())
        except StationError as error:
            self._send(_STATUS[error.code], {"error": error.code})
        except OverflowError:
            self._send(413, {"error": "too_large"})
        except (ValueError, OSError):
            self._send(400, {"error": "bad_request"})
        else:
            self._send(200, {"frames": frames})

    def do_POST(self) -> None:
        routed = self._route()
        if routed is None:
            return
        station, suffix = routed
        if suffix == "/call/talk":
            self._talk(station)
            return
        if suffix != "/broadcast":
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
        if routed is None:
            return
        station, suffix = routed
        if suffix == "/broadcast":
            self._send(200, {"stopped": station.stop_broadcast()})
        elif suffix == "/call":
            self._send(200, {"stopped": station.stop_call_audio()})
        else:
            self._send(404, {"error": "not_found"})
