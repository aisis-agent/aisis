# AISIS

AISIS is a universal AI assistant with explicit, permissioned access to a person's digital resources.

The product is not tied to one messenger, one model, or one automation platform. The same assistant can be reached from Alice, Telegram, the web UI, a local voice speaker, and local computer agents while reusing the same identity, conversations, tools, jobs, memory, and policy layer.

## Product shape

- **Surfaces:** Alice, Telegram, web/API, local edge agent, local voice speaker.
- **Existing domain services:** HyperCalendarBot for calendar, ExpenseSyncBot for finance/calculator behavior.
- **New domain service:** Home Assistant conversational agent, without replacing `dext0r/yandex_smart_home`.
- **Personal connectors:** Telegram MTProto, email, Notion, Slack.
- **Model providers:** BYOK cloud providers plus local Codex / Claude Code / OMP executors.
- **Long work:** durable jobs with status, progress, cross-channel delivery, and resumable results.

The authoritative product definition is [docs/specs/000-master-spec.md](docs/specs/000-master-spec.md).

## Documentation map

- `docs/specs/` — product and subsystem specifications.
- `docs/architecture/contracts.md` — typed contracts and discriminated unions.
- `docs/architecture/repo-layout.md` — target (planned) repository layout, boundaries and deployment shape.
- `docs/architecture/voice-stack-layers.md` — the live Yandex Station voice stack: layers, who answers a phrase, and which parts are own, forked or off the shelf.
- `docs/research/` — verified platform constraints and existing-system notes.

## Public repositories

Everything AISIS forks or patches is published under `alex-mextner/aisis-*`, with the upstream history, license and attribution kept.

| Repository | What | Upstream and license | Build |
|---|---|---|---|
| [aisis-quasar](https://github.com/alex-mextner/aisis-quasar) | the quasar-server (Yandex.Station backend emulator) with the AISIS patch series; one tag per deployed image | [v3rt3p/quasar-server](https://github.com/v3rt3p/quasar-server) `fefcd58`, GPL-3.0 | pinned in `services/yandex-station/quasar-source.lock`, `scripts/prepare-quasar-source.sh`, checked by `scripts/verify_quasar_image.py` |
| [aisis-station-kit](https://github.com/alex-mextner/aisis-station-kit) | patch on eQuasar (`aisis-station-kit.service`) and the script that applies it | [krevetkqqq/lite-kit](https://github.com/krevetkqqq/lite-kit) `8a82ff3`, MIT | `apply.sh DEST` in that repository |
| [weather-cli](https://github.com/alex-mextner/weather-cli) | the weather answers of the speaker | own | `scripts/install-weather-cli.sh` |

Other parts are used as published (config only): `wyoming-faster-whisper`, the TLS proxy `v3rt3p/https-proxy`, the streaming recognizer `v3rt3p/quasistreaming-gigam`. The full provenance table (own, upstream + config, forked, vendored) is part of the voice stack document of ticket #185.

To contribute: change the quasar layer in a pull request against `aisis-quasar` (its CI runs the offline intent corpus), everything else against this repository; a quasar change reaches production only through a new tag, `quasar-source.lock` and the corpus-receipt deploy protocol described in [services/yandex-station/README.md](services/yandex-station/README.md#quasar-source).

## Design principles

1. Fast deterministic paths before LLM calls.
2. Domain logic stays in domain services; the assistant orchestrates it.
3. Display text and speech are separate first-class outputs.
4. Long work is a durable job, never a long webhook request.
5. Secrets stay out of prompts; local private sessions stay local whenever possible.
6. Every external write has an explicit policy and audit trail.
7. Model routing is replaceable and provider-neutral.
