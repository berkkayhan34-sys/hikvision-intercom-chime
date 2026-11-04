# Licence and third-party material

This repository is licensed GPL-3.0-or-later (`LICENSE`). The notes below list what it contains that is not original
work, and under which terms.

- `custom_components/hikvision_intercom/` includes code derived from
  **hikconnect_intercom** (<https://github.com/rtammekivi/hikconnect_intercom>), MIT
  licence, Copyright (c) 2026 Silvio Vaira (Bobsilvio). The licence text is in
  [`licenses/hikconnect_intercom-MIT.txt`](licenses/hikconnect_intercom-MIT.txt). The
  affected files are the cloud client, push/long-link listeners, the vendored `lib/`
  package, the camera, lock, event, select, switch and binary-sensor platforms and the
  Hik-Connect parts of the button, number, sensor, setup and config-flow modules.
- The chime (audio rendering, private audio storage, playback control) is the original
  work of this repository's author.
- `hikvision_sdk/` contains **no** Hikvision binaries. Its image build downloads the
  SDK runtime files that the community app
  [pergolafabio/Hikvision-Addons](https://github.com/pergolafabio/Hikvision-Addons)
  already distributes, pinned to one commit and verified against SHA-256 checksums
  (`hikvision_sdk/sdk-files.sha256`). Those files remain Hikvision's property and are
  subject to Hikvision's SDK terms.
- Hikvision, Hik-Connect and EZVIZ are trademarks of their owners. This is an unofficial
  community project.
