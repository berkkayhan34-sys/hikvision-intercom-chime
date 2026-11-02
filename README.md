# Hikvision Intercom Chime

<p align="center"><img src="custom_components/hikvision_intercom_chime/brand/icon@2x.png" width="180" alt="Hikvision Intercom Chime" /></p>

Home Assistant controls for sending a custom chime to a Hikvision indoor panel.
Windows, SADP and iVMS are **not** required during operation.

**HACS custom repository.** This project is not submitted to the default HACS
catalogue. [Türkçe kurulum rehberi](docs/KURULUM_TR.md).

## Requirements

- Home Assistant OS with Supervisor, Core 2026.10.0 or later.
- A configured and connected MQTT integration/broker.
- Installed **Hikvision Doorbell** stable app >=3.0.34 from
  <https://github.com/pergolafabio/Hikvision-Addons>.
- Your indoor station's private IPv4, SDK port (normally 8000), and device admin
  username/password. A Hik-Connect account password is a different credential.

Setup checks these requirements and reports missing ones. It does not silently
install a broker/app, weaken networking, or download native SDKs into Core.
Container/Core-only installations are not supported by this Supervisor edition.
Other panels/firmwares may not support voice forwarding; software installation
does not guarantee hardware compatibility. The original prototype was heard on
DS-KH6320-WTE1, V2.2.108. Real component setup, entity creation and silent WAV
upload/save were verified on HA 2026.10.4. Physical playback of this new component
has not been acceptance-tested; prototype playback is separate evidence.

## Install and setup

HACS -> three-dot menu -> Custom repositories -> add
`https://github.com/berkkayhan34-sys/hikvision-intercom-chime` -> category
**Integration** -> Download **Hikvision Intercom Chime**. Restart HA to discover the new
component. Then Settings -> Devices & services -> Add integration ->
**Hikvision Intercom Chime**.

For local/manual evaluation copy only `custom_components/hikvision_intercom_chime`
to HA's `custom_components` directory. Do not install on a live household merely
to run automated tests. Setup asks for device credentials locally and describes
the SDK app restart before applying changes. That restart affects the app's other
devices too. No setup step sends audio. Protect your HA/Supervisor backups.

## Controls

- **Play chime**: explicitly send the selected file at selected gain/repeats.
- **Stop chime**: stop only this integration's active request.
- **Gain**: -30..+30 dB digital gain. Positive gain may distort; it cannot boost
  the physical amplifier beyond the device's own limit. Start low for new files.
- **Repetitions**: 1..10, with a 60-second combined duration limit.
- **Request status**: idle/sending/error. Sending is not proof of audible sound.

Default: the accepted two-second ding-dong, four repetitions, gain 0 dB. Its source
is already loud; no additional gain is necessary to reproduce the accepted sample.
Entity IDs are assigned by HA: use the entity selector, not an example guessed ID.
Automations can invoke **button.press** on the integration's Play button.
An active reported call blocks Play; reliability depends on bridge call-state data.

## Add your own file

Integration Configure/options -> choose **WAV audio file**, gain and repetitions.
Upload is optional when changing settings. Input must be **8000 Hz, mono, PCM
signed 16-bit WAV**, <=1 MiB and <=30 seconds. MP3, stereo, float WAV and other
sample rates are explicitly rejected, not silently misplayed. Convert externally
to that format before uploading. A file can be rejected if duration * repetitions
exceeds 60 seconds. Up to 16 distinct source files per entry are retained.

Uploads, options and number changes are silent and apply on the next explicit
Play. User audio stays under HA's private `.storage/hikvision_intercom_chime/`;
it is not put under `/local` or shared publicly. No arbitrary paths/URLs accepted.

## Recovery / remove

Use Reconfigure to change device credentials. They are stored only in the SDK
app's Supervisor options; the integration entry contains no password. A failed
app configuration attempt restores previous options, or reports rollback failure.
Take a native HA backup before deploying a new component or modifying shared apps.

Removing this configuration entry removes only its own audio files. It does not
uninstall MQTT/the SDK app or remove app credentials, because other integrations
may use them. Remove an obsolete device from the SDK app's configuration manually
when it is no longer used. Unrelated devices, intercom settings and dashboards
are not modified. No playback acceptance test runs automatically.

## Development

`python -m unittest discover -s tests -v`

`python -m compileall -q custom_components tests`

Run the tests without any live device or broker. The tests use explicit framework
boundary doubles and do not prove physical playback. No proprietary
SDK binaries, private configuration, passwords or third-party implementation are
included. The chime asset was generated specifically for this project.

The turquoise intercom icon is original AI-generated artwork approved for this
project. This is an unofficial community project, not a Hikvision product.
No open-source redistribution license has been selected by the owner yet.
