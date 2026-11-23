# Mini 2 firmware preparation

This is an **artifact preparation tool**, not a device installer or a running
setup server. It never opens a listener, contacts a speaker, changes DNS,
enables device administration, resets a device, or flashes an image.

## Why the earlier DNS-only pilot is insufficient

[HackAlice's model guide](https://hackalice.me/start/) distinguishes initial
access from a self-hosted voice backend. Its [Mini 2 setup evidence](https://hackalice.me/start/root/)
records certificate rejection for version `0.372.1.13.4794674157.20261021.302`
and a successful factory-image path using
`0.369.1.46.4778262943.20261013.299`. A lack of completed proxy HTTP requests
alone does not establish this failure, and this report does not diagnose every
other October 21 build. Record the actual device version before deciding.

A factory reset **erases `/data` and requires account and Wi-Fi enrollment
again**. Preparation is not consent to reset the device. Do not factory-reset a
speaker before its setup environment is ready and the owner has accepted that
specific data loss. Do not power it off during a firmware installation.

Primary references, inspected November 1, 2026:

- HackAlice source: `Marker284/yandex-station-extended` at
  `db3539ea915c2388ba5075be2294ea45bdadb603`, `docs/start/root.md`.
- [lite-kit](https://github.com/krevetkqqq/lite-kit) at
  `8a82ff32d75ea27b2604685671a6647ca625bf10`. Its setup scripts are **not** run by
  AISIS preparation. `COLUMN_IPS` is request classification, not a security
  access list; its sync handler runs before that classification. Do not expose
  that server to unrelated clients or run its broad start script alongside
  existing AISIS port listeners without a reviewed cutover and restoration.
- [Self-hosted backend overview](https://hackalice.me/recipes/bekend-self-hosted/).
  Initial device access and the voice pipeline are separate requirements.

## Pinned artifact and commands

Only platform `yandexmini_2` and version
`0.369.1.46.4778262943.20261013.299` are accepted by the command. The artifact
was downloaded from the official HTTPS URL pinned in `firmware_artifacts.py`.
Its observed SHA-256, size and metadata CRC-32 are pinned together:

```text
size:   67796480
crc32:  1524631406
sha256: 192aa9842c77257c4955c9a02cea837f91217d25206d2368815700280ab3c8c3
```

The `.zip` filename contains a **CPIO/SWUpdate** image beginning with `070702`.
Do not treat it as a normal ZIP archive. Digest matching establishes identity
with the inspected artifact; it is not independent cryptographic verification
of the vendor's firmware signature or proof of device compatibility.

From this checkout, with a previously downloaded image:

```sh
python3 backend/yandex/firmware_artifacts.py verify \
  --platform yandexmini_2 --image /absolute/private/path/image.zip

python3 backend/yandex/firmware_artifacts.py prepare \
  --platform yandexmini_2 --image /absolute/private/path/image.zip \
  --output /absolute/private/path/new-staging-directory
```

The tool targets Linux/Ubuntu. Symlinked directory layouts are intentionally
refused rather than resolved implicitly. The parent directory must already exist. The output must not exist. Symlink
paths and parent traversal are refused. Preparation creates a private directory
(mode `0700`), verifies the copied bytes and publishes `manifest.json` last;
files are mode `0600`. A failed directory without a manifest is **not ready** and
is retained for diagnosis. Use a new output path after a failed run; do not
automatically remove an earlier directory. Existing directories are never overwritten. The
result explicitly says `prepared_not_deployed` and `device_changes: false`.
Firmware, account/device inventory and external certificates stay outside Git.

## Remaining deployment checks

Before any device operation, verify current IP/MAC/model and actual firmware,
record a recoverable setup, and obtain the owner's explicit decision about a
reset. Keep initial setup isolated from other household devices. The existing
AISIS DNS relay and TLS compatibility/voice listeners cannot simply share their
ports with another setup server. Preserve original router settings and the
independent rollback; do not interrupt an ongoing firmware transfer with a
blind compatibility timeout. A short DHCP-DNS window without confirmed client
renewal is not evidence the speaker used that DNS server.

Once initial device access is independently established, follow the reviewed
[SSH and update-protection procedure](https://hackalice.me/start/ssh/) before
returning normal DNS. Verify that administration is limited to the owner and
that the original network configuration is restored.

Then prove the existing AISIS chain separately: microphone → STT → guarded
`pm` ChatGPT web turn → **the exact returned answer** synthesized as Брачо →
physical speaker playback. The old uncertain request must not be replayed.
ChatGPT Voice projects described elsewhere in the wiki are not a drop-in
replacement for the required `pm` web conversation plus the configured Fish
Брачо voice. A prepared image, unit tests, HTTP 200, or a decoded audio file is
not physical speaker acceptance.
