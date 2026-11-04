# Architecture and decisions

```
Home Assistant core                          Hikvision SDK Bridge app (own container)
┌──────────────────────────────┐             ┌────────────────────────────────────┐
│ hikvision_intercom           │   HTTP      │ hik_sdk_bridge                     │
│  cloud client ──► Hik-Connect│  Bearer     │  server  ──► Station ──► ctypes    │
│  camera ──► station (LAN)    │──token────► │            (login, call   Hikvision│
│  chime ──► bridge client     │  :8099      │             state, paced  SDK .so  │
│  Supervisor admin ───────────┼──options──► │             G.711 stream) ────────►│ station :8000
└──────────────────────────────┘             └────────────────────────────────────┘
```

## Decision A - one integration, one small app

Everything that Hik-Connect's cloud or the station's LAN stream can do lives in the
integration. Only what needs Hikvision's native SDK is in the app: the indoor station has no
HTTP/ISAPI port, so its SDK port (8000) is the only local control path, and a read-only probe
of the cloud's ISAPI passthrough found no way to play an arbitrary sound without it
(the station's custom ringtone slots 4-7 play only as the incoming-call ringtone). The SDK is a
glibc binary, so it cannot be loaded into Home Assistant's own container; hence a
Debian-based app image.

The app replaces the community *Hikvision Doorbell* app and its MQTT broker for this use.
It exposes four routes (see `hikvision_sdk/src/hik_sdk_bridge/server.py`): health, station
status, start broadcast, stop broadcast. It has no unlock, reboot or generic ISAPI route.

## Decision B - stations are config subentries

The Hik-Connect account is the config entry. Each local station is a subentry of type
`station` holding only `name`, `host`, `port`, `username` and the sound settings. The
password goes to the app's Supervisor options and nowhere else. Adding or removing a
subentry reloads the entry; changing sound settings does not.

## Decision C - new domain

The merged integration is `hikvision_intercom`. Reusing the old domain
`hikvision_intercom_chime` would have mixed unrelated config entries. Unique IDs of the
chime entities are `<subentry_id>_<key>`, so a re-added station gets new entities; entity IDs
stay the same when the names match (see README).

## Audio path

HA FileSelector upload -> executor: bounded stdlib WAV decoder -> content-addressed private
source -> digital gain with saturation and exact repetition -> raw PCM16 -> HTTP body ->
bridge: PCM16 -> G.711 mu-law (own table encoder; `audioop` is gone in Python 3.13) ->
160-byte frames paced at 20 ms -> `NET_DVR_VoiceComSendData`.

Gain 0 preserves samples bit for bit; positive gain saturates and may distort; hardware
volume is never touched. Only an explicit Play reaches the bridge, and Play is never retried.

## Call state

`NET_DVR_GetDeviceStatus(16034)` is rejected by DS-KH6320 firmware (SDK error 23), so the
bridge reads `GET /ISAPI/VideoIntercom/callStatus?format=json` through the SDK's XML tunnel.
Anything it cannot parse maps to `unknown`, and only `idle` allows playback.

## Concurrency and truthfulness

One lock per chime covers Play and settings validation; a second Play while busy fails at
once and is never queued. The bridge allows one broadcast per station and treats concurrent
starts atomically. Stop only ends playback the bridge itself started. `sending` means a
request was accepted; the SDK offers no audibility acknowledgement.

## Failure behaviour

- Bridge app missing, stopped, unreachable or token rejected: chime entities become
  unavailable and recover on their own (one Supervisor re-resolve per failure). Cloud
  features are unaffected.
- Unusable audio storage or a corrupt stored sound: that station's chime reports an error
  state (or is skipped); the cloud features still load.
- App configuration is applied with rollback: the previous options are restored if the
  write, restart or start fails, and a failed rollback has its own error.

## Verified external contracts

- HA 2026.10.4: `ConfigSubentryFlow`, `async_update_subentry`, entities with
  `config_subentry_id`, `probatio`, FileSelector returns a UUID string,
  `process_uploaded_file` runs in the executor.
- aiohasupervisor 0.6: `addons.list`, `addon_info`, `set_addon_options`, `start/restart_addon`.
- SDK (arm64, DS-KH6320-WTE1 V2.2.108): `NET_DVR_Login_V30`, `NET_DVR_STDXMLConfig`,
  `NET_DVR_StartVoiceCom_MR_V30`, `NET_DVR_VoiceComSendData`, `NET_DVR_StopVoiceCom`
  signatures follow Hikvision's programming manual; login and call-state were exercised
  against the real station, voice streaming was not.
