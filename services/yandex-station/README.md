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
| 31115/31116/31114 | quasar switch (uniproxy+HTTP/API/f-state): 31115 and 31114 listen on all interfaces, 31116 on loopback | aisis-switch-prod |
| 31215…/31315… | quasar blue/green prod colors | aisis-quasar-prod-{blue,green} |
| 31415… | stage switch + colors | aisis-switch-stage, aisis-quasar-stage-* |
| 18080 | station kit sync server | aisis-station-kit.service |
| 31116 (LAN) | quasar LAN proxy, only for `ALLOWED_PEERS` (Home Assistant) | aisis-station-lan-proxy.service |
| 10300 | Wyoming faster-whisper (tailnet) | wyoming-whisper.service |

**Who may connect.** Nothing in the quasar server authenticates a speaker: any client that reaches `:31115` (or a colour's own port) can send text turns to the Home Assistant
fast-intent layer, mint glagol tokens and read every speaker's private key, and the API behind the LAN proxy can make the speakers say anything. The quasar layer therefore
admits only this host and `AISIS_ALLOWED_CLIENTS` (the voice front), binds a colour to loopback and checks the station id (`overlays/quasar-admission`, with the firewall rules to
apply today and the steps before the flip); the LAN proxy forwards only for `ALLOWED_PEERS` (`~/.config/aisis/lan-proxy.env`). Ticket [#202](https://github.com/alex-mextner/aisis/issues/202).

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
device in HA (an area set only on the player entity is not read); the registry cache refreshes within a minute. The code is
`src/backend/speaker-room.ts`, `src/routers/uniproxy/spotter-clips.ts` and hunks of `ha-intents.ts`, `input-handler.ts` and
`uniproxy-connection.ts` in [aisis-quasar](https://github.com/alex-mextner/aisis-quasar) (the quasar layer is its own repository,
see [Quasar source](#quasar-source)); the `overlays/speaker-room` source overlay is the audited patch that was folded into it at tag
`aisis-pre-weather-20261117` and only applies to the older, hash-pinned baseline.
A change to the quasar layer ships as before (overlay candidate, [isolated corpus evidence protocol](corpus-runner/README.md),
`bluegreen/deploy.sh deploy --image "$VERIFIED_IMAGE_ID" --corpus-receipt "$CORPUS_RECEIPT"`) and, in the same PR, is committed to
aisis-quasar, tagged and pinned in `quasar-source.lock`;
`overlays/speaker-room/run_tests.sh` runs `tests/*.mts` against the overlay applied to the baseline container's sources,
in scratch containers with no network.

One-time setup on the host: `scripts/install-weather-cli.sh` (clones weather-cli to `~/xp/weather-cli`, links
`~/.local/bin/weather`, writes `~/.config/weather-cli/config.json` from `WEATHER_LAT/LON/TZ` when given, otherwise
weather-cli takes the Home Assistant home zone; idempotent, `--check` reports drift). The adapter finds the CLI
on PATH or in `~/.local/bin` at call time (no restart); `AISIS_WEATHER_COMMAND='["/path/weather"]'` overrides. A missing
CLI shows up as `weather_failed reason=weather_cli_not_found` in the adapter log (a failing run as `reason=exit_N` with
its stderr). Without a place config every answer depends on Home Assistant being up (the home zone is read from it). weather-cli
reads Home Assistant through `aisis-ha-cli` (`WEATHER_HA_URL`, default `AISIS_HACLI_URL`); ha-cli forwards the
query string, which `weather.get_forecasts?return_response` needs.

## Quasar source

The quasar-server source of the running image is the public fork
[alex-mextner/aisis-quasar](https://github.com/alex-mextner/aisis-quasar) (GPL-3.0, upstream
[v3rt3p/quasar-server](https://github.com/v3rt3p/quasar-server)). `quasar-source.lock` pins its repository, tag, 40-hex commit and the
production image (name and immutable id) whose sources that commit reproduces; today `aisis-volume-zero-e859a37b`.
This is the recovery and audit path of https://github.com/alex-mextner/aisis/issues/108: a lost image or host is rebuilt from the
tag, and `verify_quasar_image.py` proves a running image carries exactly those sources.

```bash
export AISIS_QUASAR_SOURCE=$(scripts/prepare-quasar-source.sh)   # clean checkout at the locked commit; refuses a tag that moved or any stray file
python3 scripts/verify_quasar_image.py <image id or tag>          # /src and the root manifests == the checkout, byte for byte (exit 0/1, 2 = error)
docker build -t aisis-quasar:recovery-<date> "$AISIS_QUASAR_SOURCE"   # upstream Dockerfile, from scratch (disaster recovery)
```

Not yet supported: **deploying** an image built that way. The corpus-evidence protocol only accepts a candidate that is exactly one
`COPY src /src` layer over a base image with overlay provenance (`deploy.sh` `build_image overlay`), which a from-scratch build is
not; teaching the protocol a checkout-built candidate is part of https://github.com/alex-mextner/aisis/issues/188. Until then the
overlay path above is the only deploy path and nothing in it changed. `verify_quasar_image.py` only reads the image
(`docker create --pull=never` + `docker cp`, no run, no environment). The Home Assistant token is passed at run time
(`AISIS_HA_TOKEN` or an untracked `/src/ha.config.json`), never read from the repository.

| Public repository | What | Base and license |
|---|---|---|
| [aisis-quasar](https://github.com/alex-mextner/aisis-quasar) | quasar-server with the AISIS patches as commits, one tag per deployed image | v3rt3p/quasar-server `fefcd58`, GPL-3.0 |
| [aisis-station-kit](https://github.com/alex-mextner/aisis-station-kit) | patch on eQuasar (`aisis-station-kit.service`) with a script that applies it to the pinned upstream | krevetkqqq/lite-kit `8a82ff3`, MIT |
| [aisis-androidtv-remote](https://github.com/alex-mextner/aisis-androidtv-remote) | Home Assistant native Android TV Remote integration with IME fixes | Home Assistant Core 2026.9.3 extract, Apache-2.0 |

The `overlays/` directories stay for changes that are not in aisis-quasar yet; the four already deployed (`speaker-room`,
`intent-grammar`, `weather-via-cli`, `volume-zero`) are folded into the fork's tags and no longer apply to the current image (their
hash pins refuse it). The `weather-via-cli` baseline (`entity-index.ts`, sha256 `73474b5c…`) is byte-identical to the tree at tag
`aisis-pre-weather-20261117`; its patched file (`c24efb90…`) is the one at `aisis-bg-weather-via-cli-20261117`. The intent-grammar
patched files (`asr-correct.ts` `1f337435…`, `ha-intents.ts` after speaker-room `f4ab25c3…`, `grammar-hook.ts` `f6f6021e…`) are
the ones at `aisis-pre-weather-20261117`.

### A command is done when the device says so (ha-cli verification)

2026-11-19 21:21 «выключи свет»: the light was off 0.85 s after the command, Home Assistant's own HTTP answer took longer than the
2 s ha-cli then allowed, ha-cli treated the silence as «Home Assistant is down», queued the call (and replayed it behind the
speaker's back) and the quasar layer announced «Умный дом перезагружается»: a reboot nobody observed (HA's log is continuous since
04:33). Rule since: after any state-changing call the outcome is read from the DEVICE STATE, and what is said never names a cause
that was not observed.

- **Where**: `hacli.py` runs every `POST /api/services/<domain>/<service>` against `ha_verify.py` (per domain: light/switch/fan on-off,
  brightness and colour temperature, climate mode and setpoint, media volume/mute/play/pause, cover, lock, number/select;
  `entity_id: all` for lights/switches/fans through one template read). While the call is in flight it polls the target states
  and answers as soon as they are as expected; when Home Assistant's answer is an error or never comes it checks the state anyway.
  A call with nothing to check (tts, scripts, scenes, area/device targets) is answered as Home Assistant answered it.
- **Answers** (all inside `CALL_BUDGET_S` = 2.5 s, the callers abort at 3 s): `200` + `X-Aisis-Verify: ok` (state as expected,
  `X-Aisis-Verified-State`, `X-Aisis-Verify-Ms`); `502` + `failed` (Home Assistant answered, the device did not follow, or is
  `unavailable`; `X-Aisis-Ha-Status` is what Home Assistant said); `504` + `unknown` (no state could be read: silence, never a
  restart); `202` + `X-Aisis-Queue`, `X-Aisis-Eta`, `X-Aisis-Down: refused|reset|dns|unreachable|starting` only when Home Assistant
  was OBSERVED gone (refused/reset/502-504/«starting» on the call AND on a second look) — the call then waits for its return
  and is replayed and checked; `4xx` Home Assistant's own rejection. `X-Aisis-Action` is the journal id of the action.
- **Availability** (`/health`: `status` up|slow|down, `last_error`): one unanswered request is `slow`, `down` needs evidence or 5 s
  of silence; only down windows are measured into `ha-outages.json` (they feed the recovery ETA), a slow answer is not an outage.
- **Adapter** (`ha_actions.py`): the reply is chosen from the verdict: «Яркость 40 процентов.» when verified, «Команду отправил,
  но не вижу, чтобы устройство её выполнило.» when Home Assistant said yes and the device did not move, «Не получилось проверить
  ответ Home Assistant.» when nothing could be read, «Home Assistant недоступен, команду выполню, когда он вернётся.» only on an
  observed outage. The utterance trace keeps per call `verify`, `aid`, `ha`, `down` and per reply `reply_why` (reason code).
- **Logs**: one JSON line per action, availability change and replay: `journalctl --user -u aisis-ha-cli -g ha_action` or
  `~/.local/share/aisis/ha-actions.jsonl` (+ `.1`, `.2`, 1 MB each, lines ≤ 1800 chars, scalar arguments only, no tokens, long texts
  as `(N chars)`). Fields: `service`, `entities`, `expect`, `before`, `call` {status|error, ms; `pending` when the caller was answered
  first, the end follows as an `ha_response` line}, `verify` {result, reason, attempts, ms, after, wrong}, `reply` {status, why}, `ms`.
  The clip post and the session page show them (`проверено: light.x выкл за 0.8 с` / `не подтверждено: …`) by time, and by journal
  id for the adapter's own calls. `ha_fakes.py` is the fake Home Assistant of the tests; `test_ha_replay.py` replays the clip through
  an isolated adapter + ha-cli + fake Home Assistant.
- **Quasar layer**: its own Home Assistant calls (`ha-intents.ts`, in the image) get the verdict from the same headers: `200` is a verified
  success; it still reads `202` as «перезагружается» and any other non-2xx as «Не удалось выполнить команду»; an overlay that says what
  ha-cli observed (`X-Aisis-Verify`, `X-Aisis-Down`) instead is the open follow-up. `deploy.sh unit ha-cli hacli.py` installs `ha_verify.py` and
  `ha_action_log.py` with it.

## Ops runbook

- **Deploy units**: `scripts/install-systemd-units.sh` (repo root) copies every
  unit and drop-in of `systemd/` into `~/.config/systemd/user/`, reloads systemd,
  enables each unit with an `[Install]` section and starts the ones not running;
  it never restarts a running unit (it names the ones to restart). `--check` exits 3
  on drift, `--plan` only prints it. A fresh machine gets it from the `aisis`
  module of the `.files` profile (clones this repository, runs the installer).
- **Deployment worktree**: adapter, clip feed and dashboard run their code from
  `~/xp/aisis/.worktrees/deploy-main` (branch `deploy-main`), selected by the
  `deploy-worktree.conf` drop-ins in `systemd/<unit>.service.d/`; the shared
  primary checkout is not their code path. Create it once with
  `git worktree add .worktrees/deploy-main -b deploy-main origin/main`, update it with
  `flock -w 120 "${AISIS_BG_STATE:-$HOME/.local/share/aisis/bluegreen-state}/maintenance.lock" git -C ~/xp/aisis/.worktrees/deploy-main merge --ff-only origin/main`
  (the lock `deploy.sh` holds while it backs up and installs, so an update never lands in the middle of one), then
  restart the unit you want to move, one at a time, and check its health.
- **ASR benchmark**: `bin/install-asr-bench.sh [--enable]` builds `~/.local/share/aisis/asr-bench-venv` (exact pins in
  `code/requirements-asr-bench.txt`); `--enable` installs the units through `scripts/install-systemd-units.sh --no-start` and
  starts only `aisis-station-asr-bench.timer` (03:40, `Nice=19`, idle I/O, 200 % CPU quota, 6 GB cap, temp files in
  `~/.cache/aisis-asr-bench-tmp`). `code/bench_asr_archive.py` re-transcribes the archived clips with each candidate
  (CPU, `onnx-asr`, plus the incumbent `whisper-turbo`), stores one row per clip and engine in `asr_bench` of the dialogs
  DB and serves the table at the dashboard's `GET /api/asr/bench`. It exits at once when the machine is loaded, so a
  busy night costs nothing. `bench_asr_archive.py reference CLIP_ID "text"` records a human reference.
- **Recogniser acceptance harness** (`code/asr_eval/`, ticket #59): points isolated Wyoming recognisers (own loopback ports, temp audio on
  disk) at three tiers and never replays audio through the live gateway or the live Wyoming port. `tier_a` (synthetic silence, noise,
  hum, music, the station's 313 Hz activation chime, speech followed by silence, plus the gated archive clips as a separate review table),
  `tier_b` (`intents-ru.jsonl` synthesised with Piper, clean and at 5 dB SNR, routed like production and judged by the grammar
  back-test) and `tier_c` (the voice archive, aggregate numbers only) each have `build`, `run --engine NAME --uri tcp://127.0.0.1:PORT` and
  `score`; `latency.py serve|measure` plays clips in real time into an isolated gateway instance (the end of speech is found through the
  gateway's own quiet-frame VAD boost; a clip with no speech has no latency). Every command writes owner-only files. Tests:
  `python3 -m unittest asr_eval.test_asr_eval_tier_a asr_eval.test_asr_eval_tier_b asr_eval.test_asr_eval_tier_c asr_eval.test_asr_eval_batch asr_eval.test_asr_eval_latency` in `code/`.
  Tier B's acceptance bar is relative (owner decision, ticket #59): over the clips production routes well from the exact text, a candidate
  must not be worse than the current recogniser and must be measurably better (more clips only it gets right, two-sided exact sign test
  p < 0.05, per condition: `relative_bar` in `summary.json`); the absolute 95 % / 85 % rates stay in the table as reported metrics.
- **State lives in**: `~/.local/share/aisis/` (venvs `venv/`, `embed-venv/`,
  `dsp-bench-venv/`, `bluegreen-state/`, `corpus/`, `models/`, `prompts/`,
  `releases/`, `voice-archive/`, `dashboard-ui/`, `pm-deps/`, `dialogs.sqlite3`,
  `ha-state-cache.json`, …), `~/.config/aisis/` (runtime.env, whisper-prompt.env,
  fish.key, per-feature json), `~/.cache/aisis/` (quasar-source checkout, fillers),
  `~/.local/state/aisis/` (adapter/pm sqlite, quasar-prod container state).
  Secrets (fish.key, runtime.env, *.env with tokens, sqlite) are never committed.
- **Blue/green deploys**: prebuild one immutable candidate, export its reviewed
  source/dependency closure, execute all original cases with the maintained layer checks through native RunPod,
  and collect the real receipt as described in [the corpus protocol](corpus-runner/README.md).
  From its clean isolated release checkout, run
  `bluegreen/deploy.sh deploy --image "$VERIFIED_IMAGE_ID" --corpus-receipt "$CORPUS_RECEIPT"`.
  The deploy never touches the active color until stage + idle-color checks pass.
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
  content, and the recognizer does not hear it. An activation stream opens with the ~2.3 s the station buffered before its
  spotter fired, sent faster than real time (98 of 103 sessions of the gateway journal 2026-11-10 … 11-17: 2.32 s, sd 0.04 s),
  then live audio; a follow-up turn (no wake word) has no such history. The name ends 0.2-0.4 s before the end of the history
  and the request's first word can start 0.08 s after it, so the gateway (`wake_window.py`) holds the first 0.6 s of a stream,
  reads the history from the arrival times of the frames, finds the pause behind the name in the frame levels of the history
  and hands the Endpoint (VAD, recognizer) only what follows it. The cut audio becomes a clip with `status='activation'`,
  `reason='wake_window'` and no transcript (it was never recognised); the stream's final, if nothing else is said, is the
  empty one with `rejected: wake_word`. The end of the history is only known while the burst holds nothing but the history: a
  lead of more than 2.45 s ahead of the clock (the longest history measured is 2.43 s) has live speech inside it (the station
  opened its socket late, ticket 156: 3.65 s arrived within 0.18 s and the window sat on the request's first word), so such a
  stream is never cut (`skipped=burst`). When the name cannot be told from the request (no pause behind it, speech running
  through the whole window, a quiet history, a
  stream that ends before the decision) nothing is cut and the text-level strip
  below is the fallback (`stt_wake_window skipped=<reason>` in the gateway log). `AISIS_STT_WAKE_WINDOW=0` switches the cut off.
  The text-level strip: the STT gateway cuts the name from the head of a transcript before the final goes to quasar
  (`wake_cut:<name>` in the clip's `reason`; the clip's `transcript` is the cleaned text, the one the dialog log and the
  feed's «ответ» line match on; the ASR bench therefore scores a cut clip's engine output with the name as an error: read
  those WERs with that in mind). A segment that is only the name (Whisper writes «Алиса.», «Малис.», «Лисы.») is an
  activation event: clip `status='activation'`, `reason='wake'`, no final to quasar, the session keeps listening, so the
  request that follows lands in the same session and dialog (tg#30176/30177: the name used to be answered «Не расслышал»
  and the request came in a second session). The device's own spotter spans never reach the gateway (the quasar overlay
  does not forward them), which is why the cut reads the stream instead of a span. An activation nobody followed ends the
  turn silently (the empty final carries `rejected: wake_word`). Python-only: restart `aisis-station-stt`.
- **Whisper VRAM offload** (`code/wyoming_whisper_offload.py`, spec in snap-orca `docs/superpowers/specs/2026-11-18-vram-offload-design.md`):
  `wyoming-whisper.service` runs this wrapper around upstream `wyoming_faster_whisper`. It unloads the model after `WHISPER_IDLE_UNLOAD_S`
  (default 900, `0` = off) without a real request, reloads on the next one, and serves a control socket
  `$XDG_RUNTIME_DIR/vram-offload/wyoming-whisper.sock` (`status` / `offload` / `restore` leases; `python wyoming_whisper_offload.py ctl status`).
  While a lease is held a request gets a Wyoming `Error` `gpu-lent`. `/healthz/asr` answers 202 for `unloaded`, `pinned` and `gpu-lent`
  (and the probe's `context.aisis_probe` is not activity, and a probe never reloads the model), so aisis-failover never restarts a whisper that was unloaded on purpose.
  The gateway reads the socket only when `AISIS_WHISPER_CTL_SOCKET` is set (the `aisis-station-stt.service` unit sets it for the local whisper).
  `status` reports `last_offload_s`, `last_restore_s`, `last_first_answer_s`.
- **Chime guard**: the station's own activation chime leaks into its microphone as a 0.2-0.5 s tone near 313 Hz (ticket #158),
  which the VAD calls speech and recognisers answer with words («Громкость.», a subtitle caption, GigaAM's «да»). `tone_guard.py`
  drops a cut whose audible part (frames above -30 dB of its loudest, so a quiet request after the chime counts) is at most
  0.6 s long (plus the VAD's hangover in the cheap pre-check), whose two strongest 40 Hz-wide spectral peaks hold at least 90 % of the
  energy below 1.9 kHz (spoken words: at most 0.59 on synthetic Piper commands) and in whose every audible window (100 ms) those same two
  peaks hold at least 75 % (stdlib only, ~10 ms): gate reason `tone`, clip
  `status='gate'`. A request run into the chime is longer or has a spread spectrum, so it is not dropped. Like `wake_window.py`
  it is a sibling module of the gateway: the unit runs from the checkout, so both ship together.
  `AISIS_STT_TONE_GUARD` (in `~/.config/aisis/runtime.env`) picks the mode: `shadow` (the default) only logs
  `stt_tone_shadow would_drop ...` per cut and drops nothing; `1` / `enforce` drops; `0` switches it off. Known risk: a one-word
  request that is nearly a pure tone would be dropped, so run `shadow` for a while first: each would-drop line is followed by the
  recogniser's `stt_final text=...` for the same cut, which tells a chime caption from a real word (count: `journalctl --user -u aisis-station-stt | grep -c stt_tone_shadow`).
  Python-only: restart `aisis-station-stt`.
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
  `layer: none` is not a failure: it is the quasar hook's call that the adapter handed back (the grammar did not know the
  phrase, or knew it and the adapter does not run it: the `declined` reason says why), after which the quasar's own fast
  intent or the brain decided. The post and the session page say that in plain Russian and name who answered when the data
  shows it (the adapter's own `turn` record, else the quasar fast intent of the dialog, else the brain turn);
  a phrase with no adapter record at all says why as far as the dialog tells (`trace_view.untraced_reason`).
- **Which quasar log lines are failures**: `dialogstore.is_failure`. A WARN/ERROR line that falls into an open dialog used to
  be that dialog's error. A normal WebSocket close is not (the speaker ends a connection after every exchange and every wake
  word, the deploy smoke checks end theirs): `UniProxy WebSocket closed`, `… (normal): code=…`; `… abnormally: code=…` and every
  other WARN/ERROR still is. The session page shows normal closes as one dim counter line in the quasar log, not as errors.
  The quasar side of the same change is `overlays/uniproxy-close-log` (the close code in the line, INFO for a normal close);
  the parser reads the old and the new text, so it does not wait for that deploy. After a change of the rule the ingester
  re-derives the docker dialogs from `raw_log` once at start (`reclassify_failures`, meta `log_failures_v1`).
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

The sibling modules the supplied file imports must be next to it: for the adapter `announcement_cache.py` and `hypothesis_store.py`,
for `unit stt /path/to/reviewed/stt_wyoming.py` `second_opinion.py`, `spotter_clips.py` and `voiceclips.py`. The command checks
the sources, backs up every installed file it replaces, installs the companions first, and restarts the unit. A failed health
check restores the previous files (a module the previous version did not have is removed). Only the stale bytecode of the
replaced files is invalidated; unrelated modules and files are left alone. A failed restore or restart identifies the retained
backup for recovery.

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

## Recognition alternatives: a second opinion beside Whisper

Whisper answers a transcript and nothing else: no alternatives, no confidence. Case: the owner said «Выключи свет», the speaker's own
spotter heard «включи свет», the quasar layer answered «Свет в студии уже включен.». The streaming gateway (`code/stt_wyoming.py`,
`code/second_opinion.py`) therefore runs a **second recogniser of a different architecture on the same audio**: GigaAM-v3 CTC on the
CPU (no VRAM), a warm worker process from the stt-cli core. CTC posteriors give the posteriors of its own readings and the exact
likelihood of any candidate, so the confusable reading («выключи свет» for «включи свет») is scored. The result is an `NBest`; its
percentages are the share of probability mass among the listed readings, never a probability that the phrase was said, and are
never read aloud.

- **Off by default.** With `AISIS_HYP_ENABLED` unset nothing changes: no worker, no client session, no extra call, the same final.
- **Same audio.** The recogniser gets the very PCM that goes to Whisper (the level-lifted copy), started at the same moment.
- **When the final waits.** Only a phrase of at most 8 words holding a word of a confusable set («включи», «выключи», «громче», …)
  waits for the opinion, at most `AISIS_HYP_WAIT_MS` beyond Whisper. Everything else sends the final at once; its opinion is
  recorded when it arrives. The Wyoming transcript itself is never changed.
- **Evidence.** Before the final leaves, a loopback POST (150 ms total timeout, failures swallowed) hands
  `{station, text, source: "whisper", nbest, ms}` to the adapter's `POST /asr/hypotheses`, and the final waits for the adapter's
  answer, at most that timeout and never longer than `AISIS_HYP_WAIT_MS` (`0`: it does not wait, the POST still goes): quasar asks
  the adapter about this phrase right after the final, so the evidence must be stored first. The outcome is logged
  (`stt_hyp publish status=ok|http:<code>|timeout|error:<class>`); a POST still running at the bound finishes on its own. The
  adapter keeps the evidence 15 s, at most 64 entries (`hypothesis_store.py`), for the quasar hook and the correction dialog;
  `/healthz` of the adapter shows the store. The gateway's websocket carries no station id today (it uses `x-aisis-station` or
  `?station=` when a client sends one), so the evidence is stored for any station. The adapter checks `AISIS_ADAPTER_TOKEN`: give
  the gateway unit the same value. With speaker enforcement on, the words of a foreign voice (the empty `foreign_voice` final) are
  never published, however late the opinion arrives, and that final does not wait for the opinion; it is still kept with the clip.
  **Shadow only in this change:** nothing reads the stored evidence yet (the consumers arrive with the correction-FSM change), and
  with `AISIS_HYP_ENABLED` unset there is no opinion, hence no publish and no wait.
- **Spotter path.** `POST /spotter-clip` runs the same decode on the uploaded audio after answering it and publishes with
  `source: "spotter"`; it cannot prevent the action the speaker already took, it is evidence for diagnostics and the next layer.
- **Kept with the clip.** `clip_hypotheses(clip_id, ts, source, primary_text, nbest, ms)` in `dialogs.sqlite3` (read with
  `voiceclips.hypotheses_of`), one row per archived clip that has an opinion, removed with the clip by the retention pruning.
- **Fail-open.** A missing core or engine, a worker that dies, times out or hangs, or an adapter that is down changes neither the
  transcript nor, beyond the two bounds above (the wait for the opinion, the wait for the adapter), its timing. The worker is restarted at most once per 30 s. A stalled worker cannot pile
  up work: at most 8 decodes and scorings are in flight (a turn beyond that gets no opinion, logged `status=busy`), and one that
  has not finished 20 s after it began is cancelled, which also frees a hung worker start.
- **Health.** `/healthz` and a green `/healthz/asr` of the gateway carry `second_opinion: {enabled, state, model, last_ms}`
  (`disabled`, `idle`, `starting`, `ready`, `down:<error class>`).

| Setting (gateway environment) | Default | Meaning |
|---|---|---|
| `AISIS_HYP_ENABLED` | off | `1`/`true`/`yes`/`on` turns the second opinion on. Flip it only after the install below. |
| `AISIS_HYP_PYTHON` | `<stt-cli home>/engines/onnx-asr/bin/python` when it exists | Interpreter of the onnx-asr engine environment (`onnx-asr==0.12.0`, onnxruntime, numpy; the model in its Hugging Face cache). `<stt-cli home>` is `$STT_HOME`, else `$XDG_DATA_HOME/stt-cli`, else `~/.local/share/stt-cli` (stt-cli's own convention). |
| `AISIS_HYP_MODEL` | `gigaam-v3-ctc` | The onnx-asr model name. |
| `AISIS_HYP_THREADS` | `4` | CPU threads of the worker. |
| `AISIS_HYP_WAIT_MS` | `300` | How long a final that needs the opinion may wait beyond Whisper. `0` never waits. |
| `AISIS_ADAPTER_URL` | `http://127.0.0.1:31117` | The adapter the evidence is posted to. |
| `AISIS_ADAPTER_TOKEN` | unset | Bearer token of the adapter (same as the adapter's own). |

The core is installed into the service venv from a hash-pinned release wheel of stt-cli (it has no dependencies of its own). The pin file
`code/requirements-stt-lib.txt` is added with the stt-cli release whose wheel hash it carries; **until it exists, leave
`AISIS_HYP_ENABLED` unset** (the gateway then runs as before). Then:

```bash
uv pip install --python ~/.local/share/aisis/venv/bin/python --require-hashes --no-deps -r code/requirements-stt-lib.txt
```

The worker does not run in that venv: it runs in an onnx-asr engine environment that the core does not provision (on this host it is
`~/.local/share/stt-cli/engines/onnx-asr`, made by stt-cli's engine tooling). Check the pair before flipping the switch: `~/.local/share/stt-cli/engines/onnx-asr/bin/python -c "import onnx_asr"`
and `~/.local/share/aisis/venv/bin/python -c "import stt_cli.hypotheses"`. Then add `AISIS_HYP_ENABLED=1` to the gateway unit's
environment and restart `aisis-station-stt.service` (the adapter needs this version first so that `/asr/hypotheses` exists; until
then the POST is a logged, swallowed 404).

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

### Claude cache and usage integration

The current adapter checks that its configured command runs `claude_primary.py` and feature-detects `SUPPORTS_CACHE_TELEMETRY` in that pinned immutable router. A co-located browser client keeps its existing text-only payload. With that release installed, the actual owner persona is sent separately on every turn, before dynamic dialog and memory context. Older router releases keep the existing periodic text-injection contract during rollout.

ASR retains its keep-alive session, model, timeout budget, credential order and cooldown behavior, while delegating each native attempt to the router's shared usage recorder. Fact extraction uses the same transport with purpose `facts`. Each purpose has a stable five-minute cache boundary containing only its real instructions; no padding is added to meet token minimums. Haiku 4.5 needs at least 4,096 prefix tokens, so a short ASR catalog can remain uncached.

Safe request IDs and nullable usage counters are copied into brain traces for correlation; the authoritative per-attempt ledger is the router's `usage.sqlite3`. Do not add trace totals to ledger totals, since they refer to the same response. Missing counters are unknown rather than zero.

Deploy the separately reviewed immutable router first, then update its configured command path and this adapter/ASR change through the existing deployment mechanism. Recheck the running files and any parallel Home Assistant releases before activation; the adapter source and its pinned router can have different release histories.

The paired router protocol (issue #173) owns the persona field, folds it into bounded text for the guarded web reserve, and flattens Anthropic’s nested cache-creation counters into cache_creation_5m_input_tokens and cache_creation_1h_input_tokens. This adapter consumes that allowlisted metadata; it does not duplicate the router implementation. ASR passes its exact attempt deadline so a budget expiry is recorded as a timeout rather than caller cancellation.
