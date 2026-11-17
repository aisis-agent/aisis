# yandex-station — live Yandex Station adapter

The rooted Yandex Station Mini in the living room is **one adapter** of the AISIS
voice surface (more station adapters may follow): it donates microphone, speaker,
compute, LED and hardware controls to AISIS. Everything the speaker hears or says
flows through the stack in this directory.

This directory holds **code only** — Python/shell sources, blue/green deploy
tooling, the compose file and the systemd units. Runtime state (venvs, sqlite
databases, models, corpora, prompts, voice archive, blue/green state) is **NOT in
the repo**; it lives under `~/.local/share/aisis/` (plus `~/.config/aisis/`,
`~/.cache/aisis/`, `~/.local/state/aisis/`).

## Dependency chain

```text
Yandex Station Mini (rooted, LAN 192.168.0.56)
  └─ quasar.yandex.net/.ru + scbh → tls-voice (443, aisis-yandex network compose)
       └─ switch-prod nginx :31115/31116/31114 (project aisis-voice, bluegreen/)
            └─ active quasar color: aisis-quasar-prod-blue :31215/31216/31214
               or            aisis-quasar-prod-green :31315/31316/31314
                 ├─ postgres aisis-yandex-postgres-1 (127.0.0.1:31119)
                 ├─ adapter aisis-station-adapter :31117  (ChatGPT + Bracho TTS + processor)
                 │    ├─ pm aisis-station-pm :31118        (speech prompts via ChatGPT browser CDP)
                 │    ├─ embed aisis-station-embed          (e5-small ONNX memory sidecar)
                 │    └─ ha-cli :8795                      (Home Assistant proxy w/ state cache)
                 └─ stt aisis-station-stt :31121            (streaming gateway → Wyoming whisper
                                                            tcp://100.75.131.84:10300, CUDA)
support services: aisis-station-kit :18080 (eQuasar sync server, ~/lite-kit),
  aisis-station-lan-proxy (192.168.0.56:31116 → 127.0.0.1:31116), aisis-clipfeed
  (Telegram clip feed + ASR truth), aisis-dashboard :8781, aisis-failover (watchdog)
```

## Ports

| Port  | Service | Unit / container |
|-------|---------|------------------|
| 31117 | adapter (processor + TTS/v1) | aisis-station-adapter.service |
| 31118 | pm (ChatGPT browser CDP) | aisis-station-pm.service |
| 31121 | streaming STT gateway | aisis-station-stt.service |
| 8795  | Home Assistant CLI proxy | aisis-ha-cli.service |
| 8781  | dashboard | aisis-dashboard.service |
| 31119 | postgres (loopback) | aisis-yandex-postgres-1 |
| 31115/31116/31114 | quasar switch (uniproxy/API/f-state) | aisis-switch-prod |
| 31215…/31315… | quasar blue/green prod colors | aisis-quasar-prod-{blue,green} |
| 31415… | stage switch + colors | aisis-switch-stage, aisis-quasar-stage-* |
| 18080 | station kit sync server | aisis-station-kit.service |
| 31116 (LAN) | quasar LAN proxy | aisis-station-lan-proxy.service |
| 10300 | Wyoming faster-whisper (tailnet) | wyoming-whisper.service |

## Who answers what

The station is rooted and its Yandex cloud is not reachable, so there is no stock Alice behind the
speaker. A heard phrase is tried in this order; the first layer that recognises it answers:

1. quasar fast intents (container): lights, climate, TV, volume, media keys, «включи <исполнитель>»
   (adapter `/music/play` → Music Assistant → AirPlay on the speaker), outdoor humidity and pressure from HA sensors.
   Weather and the outdoor temperature are NOT answered here: `overlays/weather-via-cli` makes the entity lookup
   return "none", so the turn reaches the adapter (before 2026-11-16 the HA weather entity answered «какая погода»
   with its own template and a humidity percentage, and weather-cli never ran);
2. adapter `voice_skills.py` (this directory): weather questions of every kind (`weather say --now|--tomorrow|--week|--umbrella|--dress`
   from weather-cli, HA + Open-Meteo) and «включи музыку» with no name (`music_airplay.music_any`: leave what
   plays (also a phone's AirPlay stream) alone, else queue 25 random tracks of the Music Assistant library;
   «продолжи» stays the media key for what was stopped);
3. the ChatGPT brain (pm). It has no tools: its persona block (`user_prompt.CAPABILITIES` plus the
   fragments from `~/.local/share/aisis/prompts/`) tells it what the speaker does and forbids the made-up
   «не могу, скажи штатной командой колонки».

**Which room.** A phrase that names no room («включи свет», «яркость 30», «включи кондиционер») is about the room of
the speaker that heard it. The fast-intent layer gets the station id of the connection (`StationInfo.duid`), finds
the station's `media_player` in Home Assistant (its entity id ends in the station id in lower case:
`media_player.aisis_speaker_<id>`, else `media_player.yandex_station_<id>`) and uses the HA area of that player's device (the
area the registry reports for every entity, `area_name(device_id(entity))`). A room in
the phrase wins («включи свет на кухне» from the Studio speaker lights the kitchen). A station HA does not place in
one of the home's rooms keeps the old default (Гостиная). To move a speaker to another room change the area of its
device in HA (an area set only on the player entity is not read); the registry cache refreshes within a minute. The code is the `overlays/speaker-room` source overlay
(the quasar layer lives in the image, not in this repository): `overlays/speaker-room/prepare_overlay.py --baseline DIR
--output-dir NEW` patches the exact production sources (`docker cp aisis-quasar-prod-<active>:/src/. DIR`, hash-checked)
and `bluegreen/deploy.sh deploy --overlay NEW` ships it; `overlays/speaker-room/run_tests.sh` runs `tests/*.mts` against
the overlay applied to the active image's sources, in scratch containers with no network.

One-time setup on the host: `scripts/install-weather-cli.sh` (clones weather-cli to `~/xp/weather-cli`, links
`~/.local/bin/weather`, writes `~/.config/weather-cli/config.json` from `WEATHER_LAT/LON/TZ` when given, otherwise
weather-cli takes the Home Assistant home zone; idempotent, `--check` reports drift). The adapter finds the CLI
on PATH or in `~/.local/bin` at call time (no restart); `AISIS_WEATHER_COMMAND='["/path/weather"]'` overrides. A missing
CLI shows up as `weather_failed reason=weather_cli_not_found` in the adapter log (a failing run as `reason=exit_N` with
its stderr). Without a place config every answer depends on Home Assistant being up (the home zone is read from it). weather-cli
reads Home Assistant through `aisis-ha-cli` (`WEATHER_HA_URL`, default `AISIS_HACLI_URL`); ha-cli forwards the
query string, which `weather.get_forecasts?return_response` needs.

## Ops runbook

- **Deploy units**: `scripts/install-systemd-units.sh` (repo root) copies every
  unit and drop-in of `systemd/` into `~/.config/systemd/user/`, reloads systemd,
  enables each unit with an `[Install]` section and starts the ones not running;
  it never restarts a running unit (it names the ones to restart). `--check` exits 3
  on drift, `--plan` only prints it. A fresh machine gets it from the `aisis`
  module of the `.files` profile (clones this repository, runs the installer).
- **State lives in**: `~/.local/share/aisis/` (venvs `venv/`, `embed-venv/`,
  `dsp-bench-venv/`, `bluegreen-state/`, `corpus/`, `models/`, `prompts/`,
  `releases/`, `voice-archive/`, `dashboard-ui/`, `pm-deps/`, `dialogs.sqlite3`,
  `ha-state-cache.json`, …), `~/.config/aisis/` (runtime.env, whisper-prompt.env,
  fish.key, per-feature json), `~/.cache/aisis/` (quasar-server source, fillers),
  `~/.local/state/aisis/` (adapter/pm sqlite, quasar-prod container state).
  Secrets (fish.key, runtime.env, *.env with tokens, sqlite) are never committed.
- **Blue/green deploys**: `bluegreen/deploy.sh deploy --overlay SRC_DIR …`;
  the deploy never touches the active color until stage + idle-color checks pass.
  `deploy.sh rollback` flips back. `bgctl.py status` shows the current colors.
- **Failover**: `aisis-failover.service` runs `bluegreen/failover.py --env prod`,
  probing adapter/stt/pm/ha-cli/kit/lan-proxy/embed units and the postgres,
  tls-voice, dns-relay containers. A `maintenance` file in
  `~/.local/share/aisis/bluegreen-state/` suspends it during deploys.
- **Recognizer health**: a port or Describe answer proves nothing about Wyoming (2026-11-17: temp WAV writes hit the `/tmp`
  tmpfs user quota, every transcription returned an invalid header, Describe stayed green). The STT gateway's
  `/healthz/asr` transcribes half a second of silence for real with the live language and profile. 503 + error class means the
  recognizer answered wrongly (hung up, empty or malformed header, error event, no transcript); no verdict: 504 (timeout,
  refused or unreachable address: slow, contended, starting or gone, which a restart does not fix and which systemd
  already handles), 202 (live utterances hold a slot), 501 (no usable recognizer address). `aisis-failover` probes it in
  the background about once a minute (`asr` component) and restarts `wyoming-whisper.service` after two 503s, at most once per 10 minutes (a restart cannot fix a full
  disk, then the incident alert is what counts); a probe
  without a verdict (also a dead or older gateway) neither counts nor clears a failure (the `stt` component watches the
  gateway). Known gap: a recognizer that merely hangs is not restarted by this probe.
  The recognizer's temp audio lives in `~/.cache/aisis-asr-tmp` (`wyoming-whisper.service.d/20-disk-tmpdir.conf`:
  `CacheDirectory=` + `TMPDIR`, swept at every start), not on the quota-limited `/tmp`.
- **Activation word**: the name that woke the station (`wake_word.py`, names = the grammar's `lexicon.wake`) is never request
  content. The STT gateway cuts it from the head of a transcript before the final goes to quasar (`wake_cut:<name>` in the
  clip's `reason`; the clip's `transcript` is the cleaned text, the one the dialog log and the feed's «ответ» line match on;
  the ASR bench therefore scores a cut clip's engine output with the name as an error: read those WERs with that in mind). A segment that is only the name (Whisper writes «Алиса.», «Малис.», «Лисы.») is an
  activation event: clip `status='activation'`, `reason='wake'`, no final to quasar, the session keeps listening, so the
  request that follows lands in the same session and dialog (tg#30176/30177: the name used to be answered «Не расслышал»
  and the request came in a second session). The gateway has no wake-word timestamps (quasar does not forward the
  device's spotter spans), so the name is found in the transcript, not cut from the audio; the recognizer still hears it.
  An activation nobody followed ends the turn silently (the empty final carries `rejected: wake_word`). Python-only: restart `aisis-station-stt`.
- **Whisper prompt**: `aisis-whisper-prompt.timer` regenerates the recognizer's
  initial prompt every 6 h (code/refresh-whisper-prompt.sh -> `voice_vocab.py prompt`).
  It is home vocabulary only (fixed example commands, HA room and device names, library
  artists; never agent, work or chat text), capped at 150 tokens, and built without
  one-word imperatives: Whisper answers room noise with whatever the prompt reads like,
  and «Громкость. Пауза. Дальше.» in the prompt came back as 15 of 59 utterances
  (2026-11-09..15). When HA is unreachable the previous prompt stays (exit 3 from
  `voice_vocab.py prompt`, refresh keeps the env file).
- **Clip feed (recognition messages in Telegram)**: `aisis-clipfeed.service` posts
  every heard utterance through the `tg` CLI into the forum topic `tg_topic` of
  `~/.config/aisis/clip_feed.json` (a pin to a `tg_session` that has ended makes
  tg fall back to General without an error, so the topic id is the durable
  address). Owner replies (`=`, `шум`, real text) reach the agent session bound
  to that topic, which records them with `clip-truth` (`bin/clip-truth`;
  `ln -sf ~/xp/aisis/services/yandex-station/bin/clip-truth ~/.local/bin/clip-truth`).
  `clip-truth` re-pins the feed to its own session and topic; `clipfeed.py test`
  posts one synthetic clip to prove the path.
  Commands the speaker recognises on the device itself (its commands spotter: «включи свет», «громче», «пауза») arrive
  as text only, so the STT gateway never hears them. Quasar forwards the spotter-log recording the station uploads
  next to each of them to the gateway (`POST /spotter-clip`, `spotter_clips.py`), which archives it as a clip with
  `trigger='spotter'` (the transcript is the speaker's own recognition, not Whisper's) linked to the text dialog nearest in
  time (one of the same station first); the feed posts it like any other utterance, captioned «распознано самой колонкой».
- **How an utterance was processed (diagnostics, session links)**: the adapter writes one record per call it answers into
  `utterance_traces` of `dialogs.sqlite3` (`utterance_trace.py`; the quasar hook's `/intent/decide` call is `via: hook`,
  the speaker's turn `via: turn`): layer (grammar / skill / calc / jev / brain / error / none), the grammar's decision with
  its parameters (intent, tier, priority band, confidence, slots, executor and arguments), why a decision was not run
  by the adapter, the clarification or reprompt it asked (and the phrase it was joined to), calls (Home Assistant
  service + entity + data + status, weather-cli, music, timers), Jev's verdict, the brain's provider/slot/model/attempts,
  per-stage latency and errors. It is stored by the adapter itself (own SQLite connection in a thread, fail-soft;
  `AISIS_UTTERANCE_TRACE=off` stops it). A record belongs to the dialog that was running at its time
  (`dialogstore._attach_traces`; the adapter does not know the quasar dialog id). Each clip post carries the compact lines
  (ranked: the answer and its parameters stay, stage times go first when the 1024-character caption is full) and
  **always** a link to the session page, `<dashboard>/session/<dialog id>`, or `/session/clip/<clip id>` for a clip with
  no dialog. The dashboard address is `AISIS_DASHBOARD_URL`, else this machine's own Tailscale MagicDNS name from
  `tailscale status --self` (`session_links.py`); when neither is known the post says so instead of printing a dead link.
  The dashboard serves two plain HTML pages (`session_pages.py`, no SPA build needed): `/session/...` (activation, each
  request with audio and ASR facts, the traces, the quasar log) and `/grammar` (every intent of the bundle with its YAML
  block and a GitHub link to its line in `ru.yaml`); a session links the intent that fired to
  `/grammar#intent-<id>`. The wake word alone is an activation (`voice_clips.status='activation'`): it is never a post of
  its own, the next request's post and the session page show it. `clipfeed.py test --clip <id>` posts an archived clip
  the way the feed would (labelled TEST, not entered in the ledger).
- Containers keep their historical names (`aisis-yandex-postgres-1`,
  `aisis-yandex-network-{tls-voice,dns-relay}-1`, compose project `aisis-yandex`
  for postgres) — do not recreate them just to rename.

### Install snippet

```bash
~/xp/aisis/scripts/install-systemd-units.sh            # install + enable + start what is not running
~/xp/aisis/scripts/install-systemd-units.sh --check    # drift report, exit 3 on drift

# one-time rename of the pre-consolidation units (only on a machine that still has the old names):
#   aisis-yandex-adapter→aisis-station-adapter, -stt→aisis-station-stt,
#   -pm→aisis-station-pm, -embed→aisis-station-embed,
#   aisis-quasar-lan-proxy→aisis-station-lan-proxy, aisis-kit→aisis-station-kit
for old in aisis-yandex-adapter aisis-yandex-stt aisis-yandex-pm aisis-yandex-embed aisis-quasar-lan-proxy aisis-kit; do
  systemctl --user disable --now "$old.service"
done
```

## Cached universal-remote announcements

The adapter accepts the five prepared Bracho labels through its existing,
authenticated `POST /v1/audio/speech` endpoint. A control marker has the exact form
`[ha-remote:<64 lowercase hexadecimal characters>]`. A missing, corrupt or
malformed marker returns HTTP 503; it never falls back to Fish or Alice synthesis.
Leading Unicode whitespace, format characters (including a BOM), and case variants
of the reserved prefix are also rejected rather than spoken. The accepted marker
format remains exact. Ordinary speech retains its selected provider and fallback.

Prepare the cache once after the Home Assistant `remote_announcements` component
has generated its five clips:

```bash
python3 services/yandex-station/code/announcement_cache.py --source http://HA_HOST:8123
```

This command downloads and validates the fixed labels only; it does not play them.
The Home Assistant component deliberately exposes these generic labels and their
manifest without authentication. Its endpoints are
`/api/remote_announcements/manifest` and
`/api/remote_announcements/audio/<key>.ogg`; they contain no settings or credentials.
Use an HTTP(S) base URL without user information, a query or a fragment. FFmpeg 7 or newer
with Opus support must be available on the importer's and adapter's PATH, or via
their process environment's `FFMPEG` setting. Validation uses the raw PCM muxer's
single-stream restriction introduced in [FFmpeg 7](https://ffmpeg.org/doxygen/7.0/pcmenc_8c_source.html).

The default cache is `~/.cache/aisis/remote-announcements/`. `--folder` selects a
separate import location for preparation or tests; the running adapter uses the
default location. Each clip must use voice `5cd1ffa5dbb4419cb5766435139c6d92`, be a
single Ogg Opus audio stream, match its checksum and metadata, and decode to at
most two seconds. The importer prints the path of its private, incremental log
at both startup and completion, including failures. No audio belongs in Git.

Publication prepares and validates a private generation, syncs its data and
metadata, then atomically replaces `current`. A failed pre-publication operation
preserves the previous cache. If the final directory sync fails after replacement,
the now-visible generation is retained and the import reports the failure; inspect
readiness before retrying. Published generations are retained deliberately so an
already queued old marker stays playable. Imports are an explicit preparation
operation, not a per-announcement job or an automatic recurring cleanup.

`GET /v1/remote-announcements/status`, using the existing adapter bearer credential,
returns only capability metadata. `installed: true` means that this adapter has
the cache extension; `ready: true` additionally means that all five current clips
were validated. It returns the voice ID and fixed clip keys, never credentials or
configuration. During an exclusive cache publication, speech returns HTTP 503 and
status returns `ready: false` promptly instead of waiting in a request worker.
Import preparation still waits for the normal publication lock when reusing clips.
A readiness response does not prove actual speaker latency or music ducking; those
require a coordinated live check.

### Installing the adapter and its companion

After review and CI, deploy during a confirmed idle service window with the
supported command:

```bash
services/yandex-station/bluegreen/deploy.sh unit adapter /path/to/reviewed/adapter.py
```

`announcement_cache.py` must be next to the supplied adapter. The command checks
both sources, backs up both installed files, installs the companion first, and
restarts the adapter. A failed health check restores the previous pair. Only the
companion's stale bytecode is invalidated; unrelated modules and files are left
alone. A failed restore or restart identifies the retained backup for recovery.

Deployment and rollback share a process-owned `maintenance.lock`. The associated
marker is refreshed only while its owner token and inode still belong to that
operation. An unknown, manually created or legacy marker blocks all maintenance
commands, including rollback: its age alone is not evidence that its owner has
finished. Resolve that ownership through the normal session/lease coordination
before removing or replacing the marker. A recognized abandoned v1 marker can be
reclaimed only after acquiring the process lock. There is no implicit force or
foreign-owner override.

## Grammar route: answers without a model

A declarative grammar (`code/intent_grammar/`, README there) decides what the speaker says without calling a model: canned wake,
greeting, thanks and refusal lines, «Не расслышал. Повтори, пожалуйста.» for garbled short phrases, the clock and date, timers and
alarms, negation («не включай свет» does nothing), relative volume, AC mode and setpoint in words, light colour. The adapter runs it
before calc, the skills and the brain (`intent_route.py`); the quasar overlay `overlays/intent-grammar/` asks it first through
`/intent/decide`. Measurements, switches (`AISIS_INTENT_GRAMMAR`, `GRAMMAR_HOOK`), risks and the owner's steps:
`docs/research/intent-grammar-rollout-2026-11.md`. Back-test the labelled corpora with
`cd code && python3 -m intent_grammar backtest --route combined --gate`.

## Jev: an advisory classifier for the turns that go to the brain

Off by default. Jev (TypeSafe's decision model through the Vercel AI Gateway, `code/jev.py`) is asked only about a turn the
grammar, the quasar layer, arithmetic and the skills did **not** answer, i.e. one that is about to wait ~2.4 s for the brain. One
call (~0.25 s warm) answers four typed questions: what the phrase is (`command` nobody implements / `talk` / `garbled` /
`not_addressed`), the brain tier, the reasoning effort, and the route in the wording FastModelBench measured (deterministic / fast /
reprompt / escalate). Tier, effort and route are advice that is only logged: the brain has no such knob yet. Where it sits and what it
measured: `docs/research/jev-usage-2026-11.md`, `docs/research/jev-route-bench-2026-11.md`.

| Setting (adapter environment) | Default | Meaning |
|---|---|---|
| `AISIS_JEV` | `off` | `off`: no Jev. `shadow`: decide **concurrently** with the brain and log; never changes or delays a reply. `on`: act per class (below); with both switches off it is `shadow`. |
| `VERCEL_JEV_KEY` | unset | Gateway key. Without it Jev is not used whatever `AISIS_JEV` says. Never logged. |
| `AISIS_JEV_REFUSE` | `off` | `on`: a `command` at or above the threshold gets «Этого я пока не умею.» instead of waiting for the brain. |
| `AISIS_JEV_REPROMPT` | `off` | `on`: a `garbled` at or above the threshold gets «Не расслышал. Повтори, пожалуйста.» (microphone stays open; shares the grammar's one-reprompt-a-minute cooldown, so it needs `AISIS_INTENT_GRAMMAR` not `off`). |
| `AISIS_JEV_THRESHOLD` | `0.95` | The confidence an answer needs to act; 0.5 to 1.0, anything else falls back to 0.95 and is logged by name. The tier and effort hints degrade to «balanced» / «medium» below it too. |
| `AISIS_JEV_URL`, `AISIS_JEV_MODEL`, `AISIS_JEV_TIMEOUT` | gateway, `typesafe-ai/jev`, 0.4 s | Endpoint, model, per-call budget (can only be lowered). |

**Emergencies are never Jev's.** A phrase that mentions an emergency stem (скорая, полиция, пожар, помощь, «мне плохо», сердце, дышать,
тонет, отравился, сознание, врач, 112 …; `jev.is_emergency`) is not sent to Jev in `on` mode and never gets Jev's refusal or reprompt;
it goes to the brain, as without Jev. The grammar's `refuse.emergency` (priority 1000) answers first whenever the grammar is on; the guard
is the second, independent line for when it is off or misses. A test keeps the guard's list a superset of the grammar's pinned emergency set.

Journal lines (logger `aisis.yandex`; `turn` is a 10-hex hash of the phrase, the text itself is never logged; `ms` and `cost` (USD) are
Jev's latency and what the gateway reports; `outcome` is `ok`, `below_threshold` or the failure; `decision` is what the answers read as
even when below the threshold):

```
jev_shadow utterance=talk tier=fast effort=none conf=0.97 tier_conf=0.81 effort_conf=0.90 route=escalate route_conf=0.88 decision=jev_talk outcome=ok ms=251 cost=0.0000405 turn=3f9a1c07be guard=none
```

Rollback: `AISIS_JEV=off` (or drop the key), restart `aisis-station-adapter`; `/healthz` `jev` shows the mode, `acting`, the switches, the
threshold and the call counters.

### Nightly loop

`code/intent_grammar` also runs a nightly loop (`systemd/aisis-intent-nightly.timer`, 03:30, plus `aisis-intent-morning.timer`, 08:30): it appends newly labelled real rows to the private corpus, measures the
production route against the gate (held-out ratchet, emergency safety set, 90 % goal reported), and briefs a detached omp session. With a green gate the session lands grammar changes through the normal merge path
(`review`, PR with `Refs #107`, `gh ship <PR> --task '#107'`); with a red gate it runs `review brainstorm` and lands nothing; a blocked gate is never bypassed. Hatch requests are put in front of the owner in the
morning through the harness. `python3 -m intent_grammar nightly run --dry-run` changes nothing outside the day's private working directory. Jev labelling is **opt-in** (`AISIS_NIGHTLY_ARGS=--jev` and `VERCEL_JEV_KEY`
in `~/.config/aisis/intent-nightly.env`): it sends private utterances to Vercel, so the unit does not enable it. Details: `code/intent_grammar/README.md`, «Nightly loop».
