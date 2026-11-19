# AISIS Speaker for Home Assistant

This directory maintains the previously installed `aisis_speaker` integration
in the owner's AISIS repository. Its external boundary is the local Quasar HTTP
API. It does not include device credentials, Home Assistant configuration or
runtime state.

## Source and ownership

The source was adopted from an eleven-file installed baseline on 19 November
2026. `baseline-provenance.json` records its exact file hashes and original
modes. All eleven files were inspected; a full Gitleaks scan found no secrets.
The installed package contained no license or author notice, so this import
does not infer an original author or a new license. Existing comments remain.

The old manifest's source repository was unavailable to the authenticated
owner, the complete maintained Git history contained neither integration path,
and HACS reported no managed AISIS integration. The manifest now points to this
maintained source and the owner's issue tracker.

## Observed volume after a command

Absolute volume commands now await the coordinator's public immediate refresh.
That method serializes real device reads with HA's existing refresh lock. Other
commands retain their debounced refresh and the 30-second polling interval.
A failed or stale read cannot become an optimistic requested volume, and a
different level observed after a human action is published as observed.

The installed behavior was reproduced in genuine CI before this change:
[baseline run 44439689803](https://github.com/alex-mextner/aisis/actions/runs/44439689803)
left HA at zero after the native boundary had restored four. The unchanged
volume regression also exposed the cooldown and concurrent-read cases. Separate
config-flow expectations were corrected to the existing `api_token` key.

The imported setup flow's nested branches were flattened only to meet the
existing protected complexity limit; focused CI cases preserve its explicit,
connected, fallback, empty-device and error behavior.

The tests use Home Assistant 2026.9.3 on Python 3.14.6, including the deployed
coordinator/debouncer bytes, actual entity platform, state machine and listeners.
Only the external Quasar boundary is simulated. They cover consecutive volume,
other-command debounce, failed/stale readback, a human's different observed
volume and refresh serialization. Full execution belongs in the maintained
GitHub-hosted CI workflow, never on home hardware.

## Installation and acceptance

After normal review, CI and merge, install only reviewed component files after
verifying the installed baseline and preserving an exact backup. Use the
supported Home Assistant reload/restart path. Do not copy this directory's tests,
provenance file or CI dependencies into Home Assistant.

A separately authorized, non-speaking test must then confirm zero and the
fresh original volume in both native device state and HA within the existing
bound. The earlier native zero/restore succeeded but HA restoration publication
took about ten seconds; that helper attempt remains `restore_unconfirmed`.
The separate announcement guard must remain held until timely HA publication is
actually proved. No optimistic state, broader deadline or polling change is part
of this repair.

Tracking: https://github.com/alex-mextner/aisis/issues/174
