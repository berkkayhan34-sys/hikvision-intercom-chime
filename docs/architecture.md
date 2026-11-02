# Architecture and decisions

Large/substantial integration; authoring risk R2 (auth, persistence, async and
external integration). No production deployment or playback is authorized here.

## Accepted decision A

HACS installs Python controls/config flows, not the proprietary native SDK.
Home Assistant OS/Supervisor and an existing configured MQTT connection are
required. `pergolafabio/Hikvision-Addons` stable `Hikvision Doorbell` >= 3.0.34
owns SDK authentication and voice forwarding. No Windows component is involved.
Missing prerequisites are errors with installation instructions, not auto-installs.

Setup merges only the selected device into Supervisor app options and starts or
restarts the app after explicit confirmation. Passwords are not kept in the
integration entry. Reconfigure repeats that deliberate, bounded operation.
Normal startup only reads existing bridge state and binds its MQTT entities.

## Audio path

HA's authenticated FileSelector -> uploaded-file context in executor -> bounded
standard-library WAV decoder -> content-addressed private source -> digital gain
and exact repetition -> atomic private rendered WAV under HA config -> bridge's
Broadcast Audio Path text entity -> registry-resolved Broadcast On button.

Only explicit Play reaches Broadcast On. Gain zero preserves samples; positive
gain uses saturation, may distort, and cannot increase physical speaker gain.
The integration never changes hardware volume, zone type, firmware or locks.
Supported input is deliberately PCM16/mono/8000 Hz WAV; no guessed MP3 codec,
untrusted remote media URL, shell process or new conversion dependency is used.

Settings are authoritative in ConfigEntry.options. Uploaded sources are immutable;
an interrupted upload cannot corrupt the old source. At most 16 distinct sources
per entry are retained. Rendered output is reproducible and replaced atomically.
Removal deletes only integration-owned audio; SDK app credentials remain because
other installed integrations can need them. Rollback and cleanup are in README.

## Concurrency and truthfulness

One runtime lock covers Play and settings validation. Repeated Play while busy
fails immediately; it never queues a later unexpected chime. The exact audio path
must be acknowledged by MQTT before Broadcast On. Play is never retried.
An integration-owned request gets one Stop at completion/cancellation; idle Stop
and idle unload do not send device commands. The app also closes voice forwarding
when its file ends. Backend/API failure is never labelled audible success.

The status `sending` means a request has been made, not confirmed sound. MQTT and
the app do not provide speaker audibility acknowledgement. Call-state protection
is only as accurate as the bridge's reported state. Physical acceptance remains
separate. There is no playback on setup, restart, upload or number adjustment.

## Verified external contracts

- HA 2026.10.4 selector: FileSelector returns a file UUID string, not a dict.
- HA 2026.10.4 uses `probatio`; no third-party runtime requirement is added.
- `file_upload.process_uploaded_file` must be entered/exited inside executor.
- HA Supervisor's existing aiohasupervisor client configures app options.
- Bridge public MQTT registry unique IDs end in `_broadcast_audio_path`,
  `_broadcast_on`, `_broadcast_off`; call-state ends in `-call_state`.
- Device original name scopes lookup; no hardcoded household entity ID or serial.

References: HA developer docs and `home-assistant/core` 2026.10.4;
`pergolafabio/Hikvision-Addons` tag `doorbell-v3.0.34`. No vendor implementation
or native library has been copied into this repository.
