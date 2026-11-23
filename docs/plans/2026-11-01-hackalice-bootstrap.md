# HackAlice firmware preparation

Goal: add a deterministic, non-destructive preparation step for the documented
Mini 2 image, separate from device enrollment and the existing voice backend.

The setup-server automation was not executed. This change is limited to local
artifact validation and private staging; it has no device, DNS, TLS, ADB, SSH,
router, listener, model, or account side effects.

- [x] Write tests for the pinned model/version, real archive magic, digest/size
      mismatch, symlinks, existing destinations, and incomplete preparation.
- [x] Implement a Python-standard-library verifier and private staging command.
      The command never downloads or applies an image and writes readiness last.
- [x] Run tests and validate the actual downloaded official image.
- [x] Stage the verified image on Ubuntu after transfer.
- [ ] Document source evidence, data-loss boundary, and the distinction between
      a prepared artifact and a working physical speaker. Review the diff.

A factory reset wipes device data and is not authorized by this preparation.
The documented newer-firmware route is not a diagnosis of an untested build.

Verification: 150 backend tests, 24 network tests and 25 contract tests passed.
The actual pinned image passed a real private staging probe. An informational
review identified a directory-swap race; a failing regression reproduced it,
and descriptor-relative I/O fixed it. Required review job
`20261101T003356-d240d1cd` refused with exit 10 because the mandatory Sol seat
is quota-blocked. Normal commit was refused by review-before-commit. No hook
was bypassed, no commit/push/release occurred, and the code is not installed.
