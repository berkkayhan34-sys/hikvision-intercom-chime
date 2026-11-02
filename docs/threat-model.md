# Security boundaries and failure review

Assets: device admin password, Supervisor app options, local uploaded audio,
other configured intercoms, and occupants' quiet/active intercom sessions.

- Setup/options/reconfigure are HA's authenticated admin configuration flows.
  Normal HA entity permissions govern explicit chime/gain/repeat actions.
- Credentials cross only HA -> its local Supervisor -> installed SDK app -> the
  configured private IPv4 device. They never enter browser diagnostics or output
  artifacts. HA/Supervisor native backups may include credentials; protect backups.
- No extra network listener, endpoint, MQTT broker credential or persistent token
  is created by this integration. Broker installation remains explicit/out of scope.
- Only private RFC1918 IPv4 targets and validated SDK ports are accepted. No URL,
  command, shell interpolation, arbitrary file path or arbitrary SDK command input.
- Audio is limited to 1 MiB, validated PCM16/mono/8k, input <=30 s, output <=60 s.
  This excludes playlists, external media fetches and compression bombs. Standard
  library WAV parsing and bounded sample processing are used in the executor.
- Source file names are hashes; entry identifiers are constrained. Owned storage
  rejects symlink children. HA config directory and its administrator are trusted;
  this is not isolation against an attacker already able to modify HA's filesystem.
- Existing app configuration remains in memory during setup; rollback restores it
  if update/start fails. A failure of rollback has a distinct user-visible error.
- Bridge restart may interrupt other devices managed by that app. The setup flow
  explicitly requires confirmation; there is no restart during ordinary startup.
- No auto-play, unbounded queue, playback retry, native SDK inside Core, global
  speaker-gain change, door unlock, alarm reassignment, firmware or root operation.
- Diagnostics use an allowlist excluding host, username, password, device name,
  serial, local audio path, source bytes and file-upload identifiers.

Residuals: the SDK app requires device admin access and has its own command surface.
This component cannot make that third-party app least-privileged. Call-state
freshness and audible sound cannot be proven by an MQTT publish acknowledgement.
The native app's audio streaming implementation and firmware remain dependencies.
No claim of complete security or real-device acceptance is made from unit tests.
