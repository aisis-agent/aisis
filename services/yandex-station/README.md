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

## Wording: who does what

The Yandex Station is **microphones, a loudspeaker and a wake-word spotter**, nothing else (a commands spotter on the device also
recognises a few phrases and uploads their recording). It knows nothing and decides nothing: every text the system shows or says
(the clip feed, session pages, the dashboard, the brain's prompts, docs) says WHAT was done and WHICH aisis layer did it, in the
passive voice or by the layer's name, never «колонка сама …» / «ход колонки». The layers: the gate and the STT gateway (recognition),
the adapter (the grammar, its handlers `voice_skills.py` for weather and «включи музыку», arithmetic, Jev, the brain), the quasar
fast-intent layer (our server, reached through the hook), Home Assistant actions. aisis uses **no Yandex Alice skills**, so the word
«навык» names nothing in a label: the trace layer `skill` reads «встроенный обработчик», `via: turn` (the adapter's call over the
processor socket) reads «текстовый запрос к адаптеру». `test_neutral_wording.py` pins the label tables.

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
                 │    └─ Home Assistant (mDNS-discovered, signed in via /ha) ← adapter, dashboard, quasar layer
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
   fragments from `~/.local/share/aisis/prompts/`) tells it that these commands are handled before it, forbids the made-up
   «не могу, скажи штатной командой» and names no executor of a command.

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
reads Home Assistant directly: `WEATHER_HA_URL` and a fresh access token (`WEATHER_HA_TOKEN`) are resolved from the connection store
each time the CLI is started.

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
(`docker create --pull=never` + `docker cp`, no run, no environment). The quasar layer reads the Home Assistant connection store
(`/run/aisis-ha/connection.json`, see «Home Assistant connection»), never a token from the repository.

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

### A command is done when the device says so (confirmed service calls)

2026-11-19 21:21 «выключи свет»: the light was off 0.85 s after the command, Home Assistant's own HTTP answer took longer than the
2 s the old proxy allowed, the proxy treated the silence as «Home Assistant is down», queued the call (and replayed it behind the
speaker's back) and the quasar layer announced «Умный дом перезагружается»: a reboot nobody observed (HA's log is continuous since
04:33). Rule since: after any state-changing call the outcome is read from the DEVICE STATE, and what is said never names a cause
that was not observed.

- **Where**: `ha_call.py` (the adapter, in process) sends the call over Home Assistant's WebSocket (`call_service`) and, on the same
  connection, subscribes to the state triggers of the target entities (`subscribe_trigger`). It answers as soon as Home Assistant's
  own events show the state `ha_verify.py` expects (per domain: light/switch/fan on-off, brightness and colour temperature, climate
  mode and setpoint, media volume/mute/play/pause, cover, lock, number/select; `entity_id: all` for lights/switches/fans through one
  template read after Home Assistant's answer) — not when its HTTP answer finally arrives. A call with nothing to check (tts, scripts,
  scenes, area/device targets) is answered as Home Assistant answered it. No polling, no cache, no queue.
- **Verdicts** (all inside `CALL_BUDGET_S` = 2.5 s; the quasar layer's fetch aborts at 3 s, the adapter's session at 3 s): `ok` (state as
  expected), `failed` (Home Assistant answered, the device did not follow within the budget, or is `unavailable`), `unknown` (no state
  could be read, Home Assistant did not answer in time, the connection was lost in flight, or a climate unit has not shown the command YET
  — 2026-11-15 and 2026-11-20: an air conditioner takes seconds), `skipped` (nothing in the state tells, or Home Assistant rejected the
  call). When Home Assistant cannot be reached at all (refused, reset, DNS, unreachable, «starting», 5xx) NOTHING is sent and nothing is
  kept for later; the verdict says so (`ha_down_observed`, with the evidence) and the speaker says «Home Assistant сейчас недоступен,
  команда не выполнена.» A silence of a few seconds is never called a reboot.
- **Adapter** (`ha_actions.py`): the reply is chosen from the verdict: «Яркость 40 процентов.» when verified, «Команду отправил,
  но не вижу, чтобы устройство её выполнило.» when Home Assistant said yes and the device did not move, «Не получилось проверить
  ответ Home Assistant.» when nothing could be read. The utterance trace keeps per call `verify`, `ha`, `down` and per reply `reply_why`.
- **Quasar layer**: its own calls go to Home Assistant directly through the same connection store (`ha-intents.ts` in aisis-quasar) and
  it confirms them by its own read-back.
- **Record**: Home Assistant's own logbook is the journal. The clip post and the session page show the logbook entries of the time window
  made by AISIS' own Home Assistant user (`ha_logbook.py`). `ha_fakes.py` is the fake Home Assistant of the tests (REST, the auth
  endpoints, the WebSocket); `test_ha_call.py` replays the incidents, `test_ha_replay.py` replays the clip through an isolated adapter.

### What the old local proxy did, and what became of each layer

`aisis-ha-cli` (port 8795, `hacli.py`) stood between every consumer and Home Assistant, with a fake token. It is gone; each layer was judged
by whether Home Assistant's own API cannot do it:

| Layer of the proxy | Verdict | Why |
|---|---|---|
| Forwarding with the real token | retired | Every component signs in itself through the shared client; the quasar layer reads the same connection store |
| State cache served while Home Assistant is down | retired | A cached state is not a reading; every consumer already treated it as «no reading». Names for recognition are rebuilt from Home Assistant when it is up (`extra_dict_dynamic` keeps its previous layer on a failed read) |
| Verification by polling the states | replaced | Home Assistant's own `state_changed` triggers over the WebSocket give the same answer sooner and without a timer (`ha_call.py`); the per-domain expectations of `ha_verify.py` stay: they are the definition of «done», not a copy of Home Assistant |
| Queue of calls while Home Assistant is down, replayed on its return | retired | Replaying a voice command minutes later is unsafe (an older write could land after a newer one; a light turns on at 03:00 after the nightly restart); the speaker says it was not done |
| Outage statistics, recovery ETA | retired | The ETA is no longer spoken; Home Assistant's availability is what it answers now |
| Private action journal (`ha-actions.jsonl`) | replaced | Home Assistant's logbook, filtered to AISIS' user |

The adapter keeps no copy of Home Assistant's state either: reads are fresh per question.

## Home Assistant connection (found by itself, signed in with two clicks)

Everything that talks to Home Assistant takes its address and credential from one place, `~/.config/aisis/ha/connection.json` (`0600`, in a `0700`
directory the quasar containers mount read-only), through one client, `code/ha_client.py` (stdlib). There is no hand-typed URL or token.

- **Finding the server.** mDNS `_home-assistant._tcp.local` (the announcement Home Assistant's zeroconf integration makes; its `uuid` identifies the
  instance; the local `internal_url` is preferred over the external `base_url`), then `homeassistant.local`, then the address that answered last.
  A candidate counts only if `/auth/providers` answers like Home Assistant. The stored address is changed only by the owner: the `uuid` is public
  (anybody on the network can copy it into an announcement), so when the stored address stops answering the client never moves the credential to an
  address the network merely claims. `/ha` shows the server found at another address, marked as the same Home Assistant when the `uuid` matches, and
  the owner signs in there. A token configured before the store existed (`HA_URL`, `HA_TOKEN` in `ha-dashboard.env`) becomes the stored connection
  once when the dashboard starts and nothing is stored yet.
- **Signing in.** Open the dashboard `/ha` page (`:8781/ha`), press **Подключить**. The browser goes to Home Assistant's own login page
  (`/auth/authorize`, the OAuth authorization-code flow of Home Assistant's mobile and third-party clients; client id = the dashboard's own origin),
  the owner logs in and approves, Home Assistant sends the browser back to `/ha/callback` with a code, and the code becomes a refresh token. Access
  tokens (30 min) are fetched from it on demand and renewed before they expire; refresh tokens do not rotate, so each process refreshes on its own.
  The only thing that cannot be automated is the login on Home Assistant's page: that is the owner's credential.
- **Status.** `/ha` and `/ha.json` show the steps (store, address, reach, auth, api) and name the first one that fails, with a **Подключить** button
  when the grant is gone. **Отключить** revokes the grant on Home Assistant and removes the store; Connect again reconnects (the old grant is revoked).
- **Advanced (folded on the page).** An address typed by hand (the login still happens on Home Assistant's page) and a pasted long-lived access
  token, for a network where mDNS or the login flow cannot run. The token is checked against `/api/config` before it is kept and is never refreshed.

## Ops runbook

- **Deploy units**: `scripts/install-systemd-units.sh` (repo root) copies every
  unit and drop-in of `systemd/` into `~/.config/systemd/user/`, reloads systemd,
  enables each unit with an `[Install]` section and starts the ones not running;
  it never restarts a running unit (it names the ones to restart). `--check` exits 3
  on drift, `--plan` only prints it. A fresh machine gets it from the `aisis`
  module of the `.files` profile (clones this repository, runs the installer).
- **Deployment worktree**: adapter, clip feed, dashboard, the STT gateway and the quasar LAN proxy run their code from
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
  probing adapter/stt/pm/kit/lan-proxy/embed units and the postgres,
  tls-voice, dns-relay containers. A `maintenance` file in
  `~/.local/share/aisis/bluegreen-state/` suspends it during deploys.
- **The `pm` component** is `aisis-station-pm`, this repository's bridge to the ChatGPT browser that the pm product (the
  owner's `agent-tools` repository, Guardian) owns: the brain's last resort behind Claude
  (`docs/architecture/voice-stack-layers.md`). The bridge exits when that browser goes away and refuses to start without
  it, so a restart cannot help while the browser is gone. The watchdog therefore restarts the bridge, with an alert that names
  it in full, only while something listens on the browser's debugging port: 9226, or the `AISIS_PM_CDP` of
  `~/.config/aisis/runtime.env`, else of the watchdog's own environment as it was when the watchdog started (the user
  manager's `environment.d`; the bridge reads it from there too), so restart the watchdog after changing it. With the browser stopped (the operator stopping the Guardian stack, a browser restart) the
  probe has no verdict: no incident, no alert, no restart, one `probe_no_verdict` line in the journal. systemd's own
  `Restart=on-failure` keeps trying. A browser that comes back gives the bridge twenty seconds to start by itself (no alert if
  it does); if the bridge is still down after that and six more ticks, the watchdog restarts it, with the usual alert.
  Before this rule (2026-11-27) the stopped browser produced «pm не отвечает, перезапускаю» and a restart every 30 s, which
  also put the unit's restart counter back to 0.
  Only a refused connection proves the browser is gone; a browser that accepts nothing is hung, and the bridge is restarted
  as usual. A setting that is there but is not an exact `http://127.0.0.1:<port>` endpoint (an empty one, a leading zero), or
  a file the watchdog cannot read exactly (unreadable, bytes that are not UTF-8, a carriage return, a quoted value that
  spans lines, holds a quote or a backslash, or is followed by text), claims no prerequisite: the bridge cannot start on it
  either, or the watchdog cannot tell, so it is reported and restarted as before, and the alert says so («http 0; browser port
  not checked (AISIS_PM_CDP unusable or runtime.env not readable exactly)»). Failures that precede a browser's absence do not
  count toward the next run of failures, and an incident that was open when the browser went away is closed (one
  `incident_suspended` journal line and one closing alert), so the next failure after its return is a new incident with its own alert. Any probe without a verdict (also `asr`) is journaled once per change of its
  detail, not once per probe.
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
  below is the fallback (`stt_wake_window skipped=<reason>` in the gateway log, followed by the numbers the decision saw: the
  name's peak, the room floor, where the burst starts and, for `not_alone`, the loudest frame in front of it).
  `AISIS_STT_WAKE_WINDOW=0` switches the cut off, and the place rule below with it (no history is measured without the window).
  A name the window left in the audio is still taken by its place (ticket 274, owner clip tg#31613: «Алиса» posted apart from the
  request, Whisper wrote «Редактор субтитров»): the speaker pauses behind the name for longer than the Endpoint's 800 ms (1.2-1.8 s
  in the stored sessions), so the VAD makes the name an utterance of its own. Everything inside the history is older than the
  spotter trigger, so an utterance of a stream with a measured history (1.5-2.45 s) that the window did not cut, with at most 600 ms of
  speech and whose last voiced frame lies 60-700 ms in front of the history's end (24 real names: 92-612 ms) is the name
  (`wake_window.name_by_position`): archived `status='activation'`, `reason='wake_position'`, never recognised, never a post,
  and its length and audio go to the request that follows as a cut name's do (journal line `stt_wake_position`). Nothing is taken
  by place in a stream without a history (a follow-up turn), in a burst with live speech (`skipped=burst`), in a stream the window cut,
  or from an utterance that reaches the history's end. `bench_wake_window.py position` replays the stored journal through the rule.
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
- **Prompt-echo guard** (`code/hint_echo.py`, ticket #258): on room noise Whisper sometimes continues its own initial prompt instead of hearing
  anything, and the gateway sent that on as a request (clip `20261120T133321-8c9f82`, owner verdict «шум»: «Комнаты, гирлянда, люстра.» ->
  the adapter's «Что сделать» line and an open microphone). A transcript is a prompt echo when every comma/full-stop piece of
  it is an entry of the prompt's LISTS (the label «Комнаты» / «Устройства» / «Исполнители», or one room, device or artist), there are at
  least two pieces, and a label is among them or there are at least three. The example commands and the climate sentence of the prompt are
  not entries (the recogniser may have heard them, and the on-device spotter transcribes «включи свет» word for word); «кухня, студия» (two
  rooms) stays a request. The prompt is read from the wyoming-whisper unit's env file (`AISIS_WHISPER_PROMPT_ENV`, default
  `~/.config/aisis/whisper-prompt.env`, re-read when it changes) plus the `context` prompt sent to an stt-cli recogniser. The audio level is not
  used: the stored noise clip and the owner's real verdicts have the same rms and contrast.
  `AISIS_STT_HINT_ECHO` (in `~/.config/aisis/runtime.env`) picks the mode: `shadow` (the default) only logs
  `stt_hint_echo_shadow would_drop heard=...` and drops nothing; `1` / `enforce` drops it (clip `status='blacklist'`, reason `hint_echo`, the
  cut is dropped like a caption and the session keeps listening); `0` switches it off. Count a shadow period with
  `journalctl --user -u aisis-station-stt | grep stt_hint_echo_shadow` and compare each line with the owner's clip verdicts before enforcing.
  Python-only: restart `aisis-station-stt`.
- **Proactive clarification** (`code/clarify.py`, ticket #307; owner tg#31635, tg#31640 and tg#32074: «Никаких «повтори». Она должна найти возможные логичные и ожидаемые действия»): the speaker never says a bare «Повтори?».
  A phrase that the grammar, the quasar layer and the skills did not read gets ONE short question about the likeliest action of this home («Поставить телевизор на паузу?», at most two options with «или»,
  the microphone stays open) and the answer goes through the repair dialog (`correction.py`: «да», a pick, «нет», «отмена»). The options come from the live Home Assistant catalog and states
  (a playing player pauses, a paused one continues, a lamp that is on switches off: one STANDARD service call on the catalog entity, answered from the state read back, no script and no per-device code),
  from the nearest pinned phrases of the adapter's own intents, from the speaker's recent commands and from the hour; a word is near when it differs only by what far-field speech blurs (a prefix,
  an unstressed vowel, a voiced consonant, a synonym of the grammar's lexicon, one edit). It asks before the brain from `0.75` and after the brain gave up («Повтори?», «Непонятно.», nothing, a Jev
  `garbled`, a grammar catch-all) from `0.5`; chit-chat and questions get nothing. Nothing runs without the spoken «да». With no option at all the line says what was heard and which kinds of action this
  home has («Прозвучало «…», но что сделать, не ясно: свет, кондиционер или телевизор?», «Ничего не расслышал. Что сделать: …?» for a turn without speech), the microphone open once a minute per station.
  `AISIS_CLARIFY=on|off` (default `on`; `off` keeps only that line). The Home Assistant access is the one seam `clarify.Home` (`HaHome` over the adapter's ha-cli calls). The trace line carries
  `correction.options` (phrase, score, source) and `asked: clarify`. The persona in `runtime.env` must not forbid guesses: `user_prompt.CAPABILITIES` tells the brain to answer «Непонятно.» and
  `reply_guard.is_unheard` turns that and any bare «Повтори?» into the clarifier's answer. Python-only: restart `aisis-station-adapter`.
- **Extra dictionary** (`code/extra_dict.py`, data `code/extra_dict_ru.json`, ticket #268; the runtime layer built from the home's own entity
  names, #269, is the last paragraph of this item): Whisper writes the wake word «Алиса» as another name («Олеса, включи свет.», clip 20261120T235150-330d2a). `wake_word.py` knows the
  grammar's `lexicon.wake` list and one sound rule (within two edits of «алиса», containing «ис», whole segment only), so «Олеса» (no «ис»),
  «Алекса» and «Олеся», the owner's examples, reached routing as a request. The file is a reviewed list of variants, each with the context it
  may be corrected in and the evidence it was added on; the code never guesses one.
  File: `{"version": 1, "locale": "ru", "entries": [...]}`; an entry is `id` (slug), `kind` (`wake`, `command`, `phrase`), `canonical`, `context`,
  `variants` (`[{"text": "олеса", "n": 1}]`, folded to lower case and ё = е, 1-6 words of at least 3 letters) and `evidence` (`source`: `owner` =
  named by the owner, `verdict` = an owner verdict in `clip_truth`, `observed` = stored transcripts without one; `note`). `n` is the number of stored
  clips that carry the variant in the context (an owner-named variant may have 0; `verdict` and `observed` need at least 1, and an observed command or
  phrase at least 2). Contexts: `wake` → `standalone` (the whole segment is the variant), `address` (at the head, directly followed by a stop,
  comma, dash or colon, or by an imperative of the grammar's lexicon: «Олеса, включи свет», «Алекса включи свет»), `start` (at the head, whatever
  follows; for non-words); `command` and `phrase` → `anywhere` (whole words) or `standalone` (the whole request: nothing in front of it but the name
  and a lead-in, nothing behind it). One lead-in in front of the name («Эй, Олеса») is skipped, as `wake_word` skips one (a correction behind two
  would leave a name its split cannot cut, so «Ну, эй, Олеса» is left as heard); a name said twice («Олеса, Олеся.») is both cut. So «Мы с Алексой ходили в кино», «Скажи Алексе, что я
  приду», «Привет, Олеса», «Алекса пошёл домой» and a name after another sentence stay as they were heard (the last one is the one stored case of
  «Олеса»: not handled on purpose). The loader refuses, naming the file and the entry: an unknown key, kind or context, a duplicate id, a variant
  claimed twice, a variant that is another entry's canonical, a variant (of any kind) that is, or holds, a name of the grammar's `lexicon.wake` (so «Толиса»,
  «Тариса», «Малис», «Лисы», all seen in the stored clips, are not repeated here, and no phrase can eat the activation word), a variant that is, or (wake
  kind) holds, a lead-in word («ну», «эй», «слушай», «ой», «а»: the split cuts those before it looks at the name), a canonical that does not start
  and end with a letter or digit, a command or phrase canonical that holds a name of `lexicon.wake` (a correction never writes the activation word), a
  wake canonical outside `lexicon.wake`, and evidence below the floors above.
  What it does: `judge_transcript` of the STT gateway replaces the variants in the heard text, then `wake_word.split` cuts the name as before, so
  the activation handling stays in one place («Олеся.» alone becomes an activation event, `rejected: wake_word`; «Олеса, включи свет.» becomes the request
  «Включи свет.» with the clip's `reason` `wake_cut:Алиса`). `command` and `phrase` entries are also written (atomically, to
  `$XDG_CACHE_HOME/aisis/extra-dict/extra-confusables-ru.json`) as a phonetic extra confusables file that the correction engine hands to the stt-cli
  core (`load(extra=[path])`): the repair dialog then offers the canonical for the heard variant (the sets work in both directions, as every
  confusables set does). A file the core refuses leaves the engine on its own sets (`extra_dict_confusables_unavailable class=... error=...`).
  `AISIS_EXTRA_DICT` (in `~/.config/aisis/runtime.env`, read by the STT gateway and the adapter) picks the mode: `shadow` (the default, also for an
  unknown value) logs `extra_dict_would_correct mode=shadow layer=static|dynamic kinds=... entries=... before=... after=... activation=...` (gateway) and
  `extra_dict_would_correct ... kind=confusable ...` (engine) and changes nothing sent downstream; `on` / `1` / `enforce` applies it
  (`extra_dict_corrected` in the log); `off` / `0` switches it off. A dictionary that cannot be loaded is logged (`extra_dict_unusable error=...`, by the
  gateway and by the engine) and the service runs without it. Count a shadow period with `journalctl --user -u aisis-station-stt | grep extra_dict_would_correct` and read each
  line before `on`. Python-only: restart `aisis-station-stt` and, for the engine's part (only when `AISIS_CORRECTION` is `shadow` or `on`), the
  adapter; the static file is read at start (the runtime layer below is re-read while the services run, no restart).
  Evidence (ledger read on 2026-11-22, read-only; `python3 bench_extra_dict.py mine` and `replay` reproduce it): 341 clips (2026-11-08 .. 11-22), 176
  with a transcript, 107 of them `sent` (the replay's set; the other 69 are captions the gateway dropped), owner verdicts in `clip_truth`: 5
  (2 corrected, 1 correct, 2 noise). «Алиса» opens 23 of the 176 transcripts. The other head words within three edits of «алиса»: «Толиса» 1 (at the
  head of a request the owner repeated within a minute as «Алиса, включи кондиционер …»), «Тариса» 1, «Малис» 1, «Лисы» 1 (the last three the whole
  segment), all four already in the lexicon, and «Лица» 1 (the start of a garbled request, covered by nobody and not adopted: one occurrence
  and no verdict). «Олеса» 1 (after another sentence, not at the head); «Алекса» and «Олеся» 0: they are in the file because the owner named them.
  The first `phrase` entry is the owner's verdict on clip 20261109T010646-564b98 («урок из 5» → «громкость 5»; n = 1, source `verdict`; `standalone`, so
  «Урок из 5 песен длится час» is not touched). The second is the owner's correction in Telegram of clip 20261126T172557-b4bff8 (ticket #306): the
  recogniser wrote «Ставьте их на паузу.» for «поставь телик на паузу» (source `owner`, n = 1, `standalone`). The repaired phrase takes the normal route: replayed
  offline against the active quasar image (`intent_grammar/tools/replay_quasar.mts`, no network, nothing executed) it is answered by the container's fast layer
  with the confirmation «Поставить на паузу?» (`media.pause` 0.75, entity «телевизор» 0.73; confirmed, it is handed on as «поставь на паузу»), where the heard
  text got no answer at all. The grammar has no pause intent of its own for a device and none is added here. No other
  command-word confusion has a verdict or a second clip yet, so none is in the file; `ключи свет` (the lost start of «включи» and «выключи») is
  deliberately not an entry: either reading is possible and the repair policy asks instead. Replay of the 107 sent clips on 2026-11-22 (`bench_extra_dict.py
  replay`, before the second phrase was added): 1 clip corrected (the first phrase's own source clip, which confirms nothing), 0 changed wrongly by
  an owner verdict. The same replay on 2026-11-26 covers a larger set, the 166 stored clips with a transcript (`sent` and `activation`), with both
  phrases: 2 corrected, each the source clip of its own entry, 0 changed wrongly, 0 wake corrections away from the head, and no other stored
  transcript holds «ставьте их». Separately, the gateway's shadow log since the deploy of 2026-11-22, which ran the dictionary without the second phrase,
  holds no `extra_dict_would_correct` line in about 316 finals: it says nothing about the second phrase, only that the first one and the wake entries
  did not fire on the household's speech. These are counts of one household's clips, not an accuracy figure. «Лисы» and «Алисы» are real words as well as names of `lexicon.wake` (`wake_word.REAL_WORDS`): at the head of a request they are cut
  only when addressed (a comma, stop or colon right behind, or an imperative of the grammar), so «Лисы живут в лесу» keeps its first word and
  «Лисы, включи свет» loses it; as the whole utterance they stay an activation.
  **Dynamic layer** (`code/extra_dict_dynamic.py`, ticket #269): a second layer, in the same format, built at runtime from the home itself and never
  committed. Vocabulary: the friendly names and rooms of Home Assistant (`voice_vocab.fetch_catalog`, the read-only `/api/states` list and the area
  template of Home Assistant: nothing is actuated; names that carry a digit run, a dot, a colon, an at-sign, a slash or a long hexadecimal token are never
  taken, so ids, addresses and serials cannot reach the file) and the imperatives and comparatives of the intent grammar (`on/off/set/play/cmd`
  verbs, «громче», «тише», «теплее» ...). Evidence: the stored Whisper transcripts of `voice_clips` (`sent`, not the speaker's own spotter text),
  read-only. An entry is a word that is none of the known words (the vocabulary, every word of the grammar bundle, the home's own names) but is
  within `MIN_SIMILARITY` 0.84 of exactly one vocabulary word (it must beat the second best by 0.06) and was written in at least `MIN_SUPPORT` = 3
  distinct clips: «люстара» for «люстра» (`command` for a verb, `phrase` for a name or room; source `observed`, context `anywhere`). Another form of
  a vocabulary word (a different ending, one added or cut: «спальне», «гирлянды», «громчее») is a real word said every day and is never taken as a
  mishearing. One or two occurrences make no correction, and the loader of a dynamic layer refuses an entry below that floor. The layer holds at
  most 100 entries, 120 names and 64 KiB. `python3 extra_dict_dynamic.py refresh` builds it and installs it atomically (temp file, fsync, rename,
  mode 0600) to `$XDG_CACHE_HOME/aisis/extra-dict/dynamic-ru.json` unless the content (everything but the time) is unchanged, which also makes a
  refresh the «entity registry changed» check; a Home Assistant that cannot be read (exit 3), a layer that fails validation or the size cap, or a
  failed write keeps the previous file. `aisis-extra-dict.timer` runs it 1 minute after boot and then hourly (`aisis-extra-dict.service`, oneshot, exit 3
  counts as success; its drop-in runs it from the deploy-main worktree like the other services). The gateway and the engine read the layer through
  `extra_dict.Live`: the static file plus the layer, re-read when the layer's file changes (looked at most every 30 s), a broken layer keeps the
  previous dictionary (`extra_dict_dynamic_unusable`, logged once), and the merge order is fixed: static entries always win (a dynamic variant the
  static file claims, or that is a static canonical, is dropped), and two dynamic entries for one variant resolve by support, then by canonical.
  The shadow flag is the same: `extra_dict_would_correct ... layer=dynamic`. The engine rebuilds its confusables file when the dictionary changes.
  Install: `scripts/install-systemd-units.sh` (it copies and enables the timer), then `python3 extra_dict_dynamic.py show` lists what the layer holds.
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
  `trigger='spotter'` (the transcript is the commands spotter's, not Whisper's) linked to the text dialog nearest in
  time (one of the same station first); the feed posts it like any other utterance, captioned «распознано споттером команд».
- **How an utterance was processed (diagnostics, session links)**: the adapter writes one record per call it answers into
  `utterance_traces` of `dialogs.sqlite3` (`utterance_trace.py`; the quasar hook's `/intent/decide` call is `via: hook`,
  the adapter's text request `via: turn`): layer (grammar / skill / calc / jev / brain / error / none; shown as «встроенный обработчик» for `skill`), the grammar's decision with
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
- **Recognition alternatives and the repair dialog in diagnostics (#182)**: a trace's `correction` record (decision, trigger, heard,
  alternatives with percentages, cues, the question asked with the owner's answer and outcome) reads in Russian in the post and on the
  session page (`trace_view.correction_lines`; rank 1 = decision, alternatives, dialog, so they outlast stage times in a full caption).
  A clip with a stored second opinion (`voiceclips.hypotheses_of`, table `clip_hypotheses`) but no correction in its trace (a spotter
  clip) gets «🔎 второе мнение (GigaAM): «выключи свет» 94% · «включи свет» 5%» instead. The session page adds the full table (percent,
  fit, source, engine, ms) and the dialog transcript; `/grammar` lists each dialog context (`contexts:` of the bundle) with its
  intents under `#context-<id>`. Percentages are the share among the listed readings and appear in diagnostics only, never in speech.
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
python3 services/yandex-station/code/announcement_cache.py
```

It reads the clips from the Home Assistant AISIS is connected to (the dashboard's `/ha` page); `--source <base URL>` imports from another instance.

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
greeting, thanks and refusal lines, the likeliest-action question or the «what was heard, what can be asked for» line for garbled short phrases (`code/clarify.py`), the clock and date, timers and
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
  `source: "spotter"`; it cannot prevent the action already taken, it is evidence for diagnostics and the next layer.
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
`code/requirements-stt-lib.txt` names release v0.5.0 of stt-cli (its tag sits on the merge commit of stt-cli pull request 156) and the
SHA-256 of the attached wheel: a tag can move, a hash cannot. Install it before turning `AISIS_HYP_ENABLED` on (the gateway runs as before
while that switch is unset):

```bash
uv pip install --python ~/.local/share/aisis/venv/bin/python --require-hashes --no-deps -r code/requirements-stt-lib.txt
```

The worker does not run in that venv: it runs in an onnx-asr engine environment that the core does not provision (on this host it is
`~/.local/share/stt-cli/engines/onnx-asr`, made by stt-cli's engine tooling). Check the pair before flipping the switch: `~/.local/share/stt-cli/engines/onnx-asr/bin/python -c "import onnx_asr"`
and `~/.local/share/aisis/venv/bin/python -c "import stt_cli.hypotheses"`. Then add `AISIS_HYP_ENABLED=1` to the gateway unit's
environment and restart `aisis-station-stt.service` (the adapter needs this version first so that `/asr/hypotheses` exists; until
then the POST is a logged, swallowed 404).

## Laya: the `/laya` proxy for the semantic shadow

`POST /laya` (bearer token, like every adapter route) proxies one classification to the local Laya classify server of task-cli
(`code/laya_route.py`, protocol version 2). The quasar container's semantic shadow calls it and only logs the verdict; the endpoint
never actuates anything. The socket is `LAYA_SOCKET` or the newest `laya-<version>.sock` under the task-cli state directory.

Request JSON: `message` (required string, up to 2000 characters), `questions` (object of at most 64 questions, default `{}`; more is a 400) and four optional fields.
`model` is `english` or `multilingual` and is forwarded as is. `max_len` is an integer from 512 to 2048 and is forwarded as is.
`fallback` is `chain` (default) or `none`. `chain` lets a tier that cannot answer hand over to the next tier *that the owner switched on* (see `AISIS_LAYA_FALLBACK` below; with nothing switched on, `chain` and `none` behave the same: Laya only). `language` is a hint (`ru`, `en-US`, …) that is turned into a `model` and not forwarded, because the Laya protocol has no such field.
Every request is sent with an explicit `model`: the caller's `model`; else, for an English hint (`en`, `en-US`) on text without Cyrillic
letters, the English checkpoint; else the multilingual one. That includes any other language, an unknown or missing hint, mixed Russian
and English text and an English hint on Cyrillic text (ambiguous). The script is never a reason for English: Serbian in Latin letters,
German or transliterated Russian stay multilingual. The reply is Laya's own: its `meta` names the checkpoint really used,
`max_len` and whether the input was truncated, and a `quality` object is added (below). A reply that does not confirm the requested
model is refused, so an older Laya that ignores the field can never pass for the multilingual one.

Failures are JSON `{"ok": false, "error": <code>, …}`; the proxy never invents a verdict, so the caller keeps its own deterministic
decision. When a failure ends the quality chain below, the body also carries the `quality` object (`outcome: "unknown"`):

| Status | `error` | Meaning |
|---|---|---|
| 400 | `invalid_request` (+ `field`) | The request or a field is invalid, or Laya itself refused a field. Nothing was sent. |
| 503 | `laya_socket_missing` / `laya_socket_refused` | No socket file, or only abandoned socket files nobody listens on. |
| 503 | `laya_model_unavailable` (+ `reason`) | The model is `loading` (with `Retry-After: 1`; ask again) or its load failed (`load_failed`, `dependency_missing`, `path_shadow`). |
| 504 | `laya_connect_timeout` / `laya_timeout` | The socket did not accept in 5 s, or Laya did not answer in 30 s. |
| 502 | `laya_empty_response`, `laya_bad_response`, `laya_connection_lost`, `laya_model_not_confirmed`, `laya_route_unsupported`, `laya_upstream_error` | The upstream answered nothing, nonsense, no model confirmation, or an error of its own. |
| 502 | `classifier_unknown` | Laya answered, but no tier passed its controls; `quality.typed_reason` says why (`control_failed:wrong_label`, `control_failed:route_unverified`, `label_outside_domain`, …). |

### Controls, fallback and the decision journal (aisis#250)

Routing a request to the right Laya checkpoint does not show that the answer is trustworthy, so a request whose `questions` are the
semantic shadow's `domain` choice question (`laya_quality.VOICE_COMMAND_DOMAIN`) runs through a quality envelope
(`code/laya_quality.py`, contract `classifier-journal/1.0`; the `request` record matches revision r4 byte for byte against the contract's example, the
`proposed` record does not yet carry r4's `facts_revision`/`expected_target`: aisis#302):

1. The main call goes to Laya on the routed model. Its route (model id, checkpoint, `meta.truncated`) is read from that call's own reply
   and kept; a malformed or empty reply, a label outside the question's vocabulary, a missing answer or a truncated input is no answer.
2. One positive and one negative control run as **separate** calls on the same model and `max_len`, drawn from the request id (so
   they are reproducible and cannot be re-rolled): synthetic phrases whose label is known, sent as ordinary requests. The expected
   label and the case id never reach a model. A control that reports another model or checkpoint than the main call is replaced
   (up to four candidates); a wrong label or a failure rejects the tier.
3. A rejected tier hands over to hosted Jev (`VERCEL_JEV_KEY`), then to `claude-haiku-5-5` (the adapter's Claude credentials), each
   with its own controls, **only when the owner switched them on** (below). `"fallback": "none"` in the body stops after Laya. The reply is
   then the accepted tier's own, and `quality.tier`/`provider`/`model` say who answered; Laya's `meta` is never attached to another
   tier's answer. A cloud tier is also not started for a caller that has already hung up.
4. When no tier passes: an explicit unknown (the Laya status code above when Laya itself had a transport problem, else
   `502 classifier_unknown`) and a critical incident; never a default label. The caller keeps its deterministic decision.

**Privacy: the cloud tiers are OFF by default.** The message is a voice transcript, and Jev and Haiku are hosted. `AISIS_LAYA_FALLBACK`
(adapter environment, restart to change) is a comma-separated list of the tiers that may be used: `jev`, `haiku`, `jev,haiku`, or empty/unset
(the default: nothing leaves the host). A tier that is switched off is skipped (`skipped`, reason `disabled:aisis_laya_fallback`) and files no
incident, since nothing is missing. A tier that is switched on but not configured (`VERCEL_JEV_KEY`, the Claude key sources) is an incident
naming the variable. The Haiku tier uses its own key cooldowns: a failing Haiku call never cools the keys of the live ASR corrector.

Questions that are not a registered domain are answered by Laya alone exactly as before, without controls, and are journaled and
counted as `excluded:no_controls_for_questions`.

The reply gains `quality`: `request_id`, `outcome` (`accepted` | `unknown`), `eligibility`, `domain`, `controls` (`passed` | `failed` |
`none`), `fallback` (an earlier tier was tried and failed), `recorded` (the journal write succeeded) and, when accepted, `tier`,
`provider`, `model`, `checkpoint`. There is no probability cutoff: Laya's confidences pass through untouched and no abstention is
applied without a calibration for the exact model revision.

Every request writes a `request` record and, for a registered domain, a `proposed` record into
`$XDG_STATE_HOME/agent-tools/classifier-quality/journal/` (0600 files, one per record, idempotent by `record_id`; `code/classifier_journal.py`).
They carry enums, hashes, tokens and numbers only: never the message, a control text or a model reply (the input is an HMAC under an
install-private key). The semantic shadow never acts on a verdict, so no `applied` record exists. Incidents (a later tier that is not
configured, naming the variable and never its value; a failed control; a failed call) go to `.../classifier-quality/events/` for the guardian,
folded to one per cause per hour. The journal keeps 90 days and 64 MiB, oldest first (pruned at most once an hour), and remembers how far
the history was cut (`.retention.json`) so a reader reports the gap instead of "no use".

`GET /healthz` gains `laya`: `quality` (per domain: `model_classified`, `with_controls`, `covered`, `accepted`, `unknown`, `fallback_used`,
`excluded` and `controls_coverage` = requests that carried controls and whose outcome was journaled, over model-classified requests),
`journal` (written, noop, conflict, failed, pruned, and `last_failure`: the exception class or reason of the latest failed write, `null`
when none; a rising `failed` with a `last_failure` means the journal is not being kept) and `incidents`. The counters live in the process and restart with it.
On shutdown the adapter waits at most 10 s for `/laya` requests in flight to finish their journal write and cancels the rest.

Tests: `code/test_adapter_laya.py`, `code/test_laya_quality.py`, `code/test_classifier_journal.py` (CI). Rollback: restart the adapter on the previous commit.

## Information intents with a dynamic answer

Three grammar intents answer from live data instead of a fixed line or the language model (spec `docs/specs/150-dynamic-intents-and-weighted-answers.md`):

- **«что ты умеешь»** (`assistant.capabilities`, `assistant.capabilities_topic`, `assistant.capabilities_more`): `code/capabilities.py` lists what is configured and
  reachable right now (timers, weather-cli, music, the rooms that have lights and air conditioners from Home Assistant), orders it by the hour, pages
  through it («а ещё?» or the same question again within two minutes) and ends with an example phrase the grammar is tested to read. Home Assistant
  not answering within 1.5 s leaves the home out and says so. A leading reaction («Тоже неплохо. А что ты умеешь?») changes nothing.
- **«какой кондиционер включён»** (`home.climate_status`, a `pre` intent, so the quasar layer cannot turn it into a question): `ha.climate_status` reads the
  `climate.*` states of Home Assistant (fresh per question, read-only, never a service call) and says which units are on, mode, set-point, room temperature and
  since when, rounded («около 20 минут», «больше часа», «с утра»); `code/home_status.py` holds the sentences and the rounding. Every failure is one
  short «состояние … узнать не удаётся».
- **a bare «кондиционер на втором этаже»** (`home.climate_bare`) is the same answer, in shadow: traced with `declined: … AISIS_STATUS_BARE`, nothing is said, the
  quasar layer's question «Включить или выключить кондиционер наверху?» stands. `AISIS_STATUS_BARE=on` (adapter environment, restart) switches it on.

Wording is impersonal: no sentence names who switches or does anything. Phrase fixture: `code/intent_grammar/grammar/info-ru.jsonl`. Tests:
`test_capabilities`, `test_home_status`, `test_ha_status`, `test_info_intents` (CI). Restart: the adapter (it loads the compiled bundle at start); no quasar deploy.

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
| `AISIS_JEV_REPROMPT` | `off` | `on`: a `garbled` at or above the threshold gets the clarifier's answer (the likeliest action as a question, else «Прозвучало «…», но что сделать, не ясно: …?»; never a request to repeat; microphone stays open; shares the grammar's one-line-a-minute cooldown, so it needs `AISIS_INTENT_GRAMMAR` not `off`). |
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
