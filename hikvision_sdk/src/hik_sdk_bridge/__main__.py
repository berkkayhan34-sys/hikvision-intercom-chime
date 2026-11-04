"""Entry point: load options, start the SDK, serve until SIGTERM."""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from pathlib import Path

from . import config as app_config
from .sdk import Sdk, SdkError
from .server import BridgeServer
from .station import Station

PORT = 8099
LIBRARY_DIRECTORY = Path(os.environ.get("HIK_SDK_LIB_DIR", "/app/sdk"))


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("hik_sdk_bridge")
    try:
        options = app_config.load()
        sdk = Sdk.load(LIBRARY_DIRECTORY)
        sdk.initialize()
    except (app_config.ConfigError, SdkError, OSError) as error:
        log.error("Cannot start: %s", error)
        return 1

    stations = {item.name: Station(item, sdk) for item in options.stations}
    for station in stations.values():
        station.status()  # first login attempt; later attempts happen on demand
    server = BridgeServer(("0.0.0.0", PORT), options.token, stations)

    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Serving %d station(s) on port %d", len(stations), PORT)
    stopping.wait()

    server.shutdown()
    for station in stations.values():
        station.close()
    sdk.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
