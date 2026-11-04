# Hikvision SDK Bridge

A small local service for the **Hikvision Intercom** integration
(<https://github.com/berkkayhan34-sys/hikvision-intercom-chime>). It logs in to a Hikvision
indoor station over the station's SDK port (8000), reports whether the station is idle,
ringing or in a call, and plays audio sent by the integration on the station's speaker.

You do not configure this app by hand. Add a station in the integration
(**Settings -> Devices & services -> Hikvision Intercom -> Add local station**); the
integration writes the station and a generated API token into this app's options and
restarts the app after you confirm.

- Needs no MQTT broker and no Hikvision phone/PC app.
- Station passwords are stored only in this app's options, never in the integration.
- Listens on port 8099 on Home Assistant's internal app network only (no host port is
  published). Every request except `/v1/health` needs the bearer token.
- The Hikvision SDK runtime files are downloaded when the image is built and verified
  against pinned SHA-256 checksums.
- Nothing plays unless the integration sends audio, and playback is refused while the
  station is ringing or in a call.
