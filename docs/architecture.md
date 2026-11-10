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

The initial flow is one path: bridge-app check (an install prompt that re-checks on Submit;
Container installs abort), Hik-Connect login, station, restart confirmation, then the entry
is created together with its station subentry.

Chime entities belong to the config entry, not the subentry, and use the cloud device's
identifiers when the account has exactly one device and one station, so the doorbell is one
device. Attaching them to the subentry would list the shared device under it as well. The
cost: removing a station deletes its entities explicitly (`_async_forget_stations`) instead
of relying on Home Assistant. A separate chime device from an earlier version is removed
once its entities have moved.

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

## Door call audio

`NET_DVR_StartVoiceCom_MR_V30` has a receive callback (we passed `None` for the chime). While the station reports an answered
call, the app opens one voice channel shared by a talker and any listeners (reference counted; it closes with the last user,
when the call ends, or after 10 minutes). Routes: `POST /v1/stations/<n>/call/talk` (request body = mu-law frames as they are
spoken, chunked, paced to real time with a 0.5 s lead), `GET .../call/listen` (chunked response, a bounded queue per listener
that drops the oldest audio), `DELETE .../call`. The chime and call audio exclude each other (`busy`; `no_call` when there is
none). In Home Assistant: `door_call.py` answers a ringing call through the cloud first (only when the station is the
account's single device), `door_stt.py` is the Assist engine (16 kHz PCM -> 8 kHz mu-law), `views.py` the WAV listen stream and the talk WebSocket (16 kHz PCM from a dashboard's microphone into the same `DoorCall.talk`; the call status sensor publishes both paths as `listen_path` and `talk_path`).
Format assumptions to confirm on a real call: send mu-law (known to work for the chime), receive mu-law.

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
