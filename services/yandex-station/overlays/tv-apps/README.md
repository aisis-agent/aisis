# Living-room launch commands for AISIS

This overlay adds explicit Netflix and YouTube commands to the existing fast intent layer. The target is always the living-room TV/Chromecast pair, including requests spoken to the Studio AISIS speaker.

Accepted commands use «открой», «включи» or «запусти» with Netflix/Нетфликс or YouTube/Ютуб/Ютюб/Ютьюб, optionally «на телевизоре»/«на ТВ»/«на телеке»/«на телике» and «пожалуйста». Complete-phrase matching rejects questions, negations, music requests, off/pause/volume commands and compound requests.

The layer invokes exactly one `script.turn_on` call for `script.living_room_netflix` or `script.living_room_youtube`. Those HA scripts own power synchronization and launch. AISIS observes `media_player.televizor` afterward. An active state plus the expected `app_id` confirms success; old activity retained while off, stale HA cache, unavailable data or another app do not. The single request and subsequent observation share a four-second reply budget; the existing three-second HTTP request timeout is preserved. Every observation races the remaining budget and clears its timer, so late success or an unresolved read stays pending. An accepted launch that is still pending gets an explicit unconfirmed reply. An uncertain request is never retried.

## Projector fireplace

The complete commands «открой», «включи» or «запусти камин», optionally «на проекторе», invoke only `script.living_room_fireplace`. Other targets, heating requests, questions, negation, songs and caller-provided URLs do not match. The HA script owns projector/Kodi power, video selection and repeat management. AISIS does not launch the TV.

Every fireplace command generates a new UUID and sends it as `script.turn_on` variables `{request_id}`. The authenticated host controller carries that identifier through startup, any bounded YouTube/local fallback, and playback observation. AISIS requires the same request ID on `sensor.living_room_fireplace`, a valid `launch_started_at` no earlier than this voice command (allowing two seconds of host clock skew), and a nonempty source ID with source kind `youtube` or `local`. A newly observed previous video can never confirm a different request.

Playback is confirmed only when the sensor is playing, owned=true, file_matches=true, progressing=true and repeat=one, with no error and an `observed_at` at or after the launch timestamp and no more than two seconds in the future. The native Kodi entity `media_player.home` must also be playing and its title must equal the controller's actual nonempty title. The title is selected dynamically; no fixed filename or title list remains. Kodi HA `media_content_id` can be a library unique ID, so the host controller verifies actual file identity and time advancement separately. Signed playback URLs do not enter this voice contract.

A same-request `starting` receipt produces «Запускаю камин на проекторе». A same-request `error` receipt reports failure only for the controller's definite failure codes: `selection_unreadable`, `no_distinct_source`, `sources_unavailable`, `invalid_target` or `stopped`. Unknown or malformed codes, `cancelled`, `unknown`, absent, stale, mismatched or nonprogressing evidence produce an unconfirmed response. Controller readiness is classified before requesting native Kodi state, so a slow or failed native read cannot hide a starting or definite failure receipt. Missing or rejected readiness after a valid starting receipt preserves that accepted state; an explicit later receipt can update it. Native evidence is required only for playback confirmation. An uncertain HTTP response is observed without a second launch request; it is not reported as a definitive failure. The existing four-second reply budget remains absolute, and a later host completion does not turn the earlier pending response into a success claim.

## Source provenance

The baseline was read from the actual active `aisis-quasar-prod-blue` image, not the separately modified source cache.

- Image: `sha256:b05533500fe7e99c90cb47137ac3a5af99aaa71034fc3ccb01dbaa69e72661f9`
- `/src/backend/ha-intents.ts`: `d27745ec7670384fe960b409f3ccdfed4f1043773a31a8c9b38671041c85cebc`
- Patched matcher for this request contract: `e0cd5dd5e35410d7db7ed968a3777b997aef7f0d3e054c1395383e66ef4543b1`

`prepare_overlay.py --baseline FILE --output-dir NEW_DIRECTORY` checks that baseline, applies the exact small patch with no fuzz, checks the result and copies the new helper. It emits a source manifest and private automatic execution log. It does not deploy or launch devices.

The existing blue/green stage, corpus, dialog, idle-color readiness and switch checks remain required. Full corpus and adversarial execution belongs on the managed RunPod runner; the image and all executed source/dependency bytes must match the stage/idle candidate before switching.

## Targeted tests

The native `Fireplace voice overlay` Actions workflow uses the deployed Node25.9.0 runtime and TypeScript5.9.3 to compile the helper and run its pure parser, request, lifecycle and deadline regressions. The original CI checks remain unchanged. The captured production matcher is an external, SHA-verified input; it is not duplicated into the repository or exempted from lint limits.

On the managed RunPod runner, use the captured production matcher and TypeScript5.9.3 for the actual-matcher contract harness:

`AISIS_ACTIVE_MATCHER_SOURCE=/path/to/captured-or-patched-ha-intents.ts node --experimental-vm-modules --test tests/*.mjs`

The actual-matcher tests evaluate the captured production matcher with external service modules isolated; they prove the new dispatch location, exact nonblocking service payload, and error/cache boundaries. Pure tests exercise the complete phrase grammar and bounded one-shot action. They complement the original corpus/dialog gates and do not prove physical app launch.
