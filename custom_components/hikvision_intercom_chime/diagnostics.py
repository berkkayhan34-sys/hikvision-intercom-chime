"""Explicit allowlist: never include app options, credentials or private audio."""


async def async_get_config_entry_diagnostics(hass, entry):
    runtime = getattr(entry, "runtime_data", None)
    return {
        "integration_version": "0.1.1",
        "configured": True,
        "runtime_loaded": runtime is not None,
        "mqtt_connected": runtime.mqtt_connected if runtime else False,
        "status": runtime.status if runtime else "not_loaded",
        "gain_db": entry.options.get("gain_db", 0),
        "repeats": entry.options.get("repeats", 4),
        "custom_audio": bool(entry.options.get("source_digest")),
    }
