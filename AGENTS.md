# Agent guide

Repository instructions for coding agents (Claude Code, Codex, etc.). This is the
canonical file; the other agent-guide filename is a symlink to it, so every agent
reads the same guide.

## What this repository is

AISIS is a universal assistant; the part that runs in production today is the voice stack of a Yandex Station speaker in
`services/yandex-station/`. The rest of the repository is `docs/` (specs in `docs/specs/`, the live voice stack in
`docs/architecture/voice-stack-layers.md`, the planned layout in `docs/architecture/repo-layout.md`), `scripts/` (installers and
contract checks) and `.github/workflows/` (CI). Read `README.md` for the product shape and `services/yandex-station/README.md` for how
the speaker is wired, which process answers which phrase, and the operations runbook.

Inside `services/yandex-station/`:

- `code/` is the Python adapter, the speech-to-text gateway (`stt_wyoming.py`), the dashboard, the clip feed, the intent grammar
  (`code/intent_grammar/`, authored in `grammar/ru.yaml`, compiled to the committed `grammar/ru.json`) and the unit tests of that code
  (`test_*.py`; the overlays, `bluegreen/`, `corpus-runner/`, `scripts/` and `homeassistant/tests/` keep their own).
- `overlays/` are patches applied to the quasar container image; `corpus-runner/` and `bluegreen/` hold the isolated corpus evidence
  protocol and the blue/green deploy tooling; `systemd/` holds the user units; `homeassistant/` is the Home Assistant custom
  integration of the speaker.

## Running and testing

CI (`.github/workflows/ci.yml`) is the source of truth for what must pass; the commands below are examples, the complete module
lists are in the workflow steps. Run from the repository root unless noted:

- `python3 scripts/check_contracts.py` and `python3 -m unittest discover -s scripts -p "test_*.py"` check the repository contracts.
- In `services/yandex-station/code/` run the adapter tests with the speaker's virtual environment, which works for every module and is
  required by the gateway ones (`test_stt_*`, they need `aiohttp`):
  `~/.local/share/aisis/venv/bin/python -m unittest test_clarify test_clarify_adapter test_stt_extra_dict` (add the modules you touch;
  CI runs the rest, see the workflow steps).
- After every grammar edit, in `services/yandex-station/code/`: `python3 -m intent_grammar check`, `python3 -m intent_grammar compile`, then
  `python3 -m intent_grammar backtest --route combined --gate`. CI fails when the committed JSON bundle is stale
  (`python3 -m intent_grammar compile --check`).
- Overlay and corpus tests run with `python3 -m unittest discover -s <directory> -p "test_*.py"` (see CI for each directory);
  `node --test services/yandex-station/corpus-runner/*.test.mjs` runs the JavaScript ones.

Tests come before code. For every fix or feature write the failing test first, on the real phrase, trace or state that went wrong,
watch it fail for the stated reason, then write the smallest change that passes it. A fix that cannot name its failing test is not
finished.

Heavy runs go to the server, never to this workstation: the full corpus, speech benchmarks, node or esbuild builds, the full
test suites and anything that loads the machine for more than a few seconds run as `server-run -- <command>` (the checkout of the
current directory is mirrored there; `server-run --help` lists the options). Single focused test modules and the small corpus-runner
`node --test` suite stay local. Never run the heavy ones inside a running home-service container either; the corpus protocol in
`services/yandex-station/corpus-runner/README.md` describes the isolated run.

Work lands through a pull request from a linked worktree (`rig worktree create <name> --from origin/main`; the primary checkout is
read-only), with a ticket opened first (`task new`) and referenced as `Refs #N`, never `Closes`. Ship with `gh ship <N>`. The blocks
between `# >>> rig-managed` markers at the end of this file are written only by `rig apply commit --only agents_md`, never by hand.
Deployment of the speaker services is described in the operations runbook of `services/yandex-station/README.md`
(`scripts/install-systemd-units.sh`, the `deploy-main` worktree and `services/yandex-station/bluegreen/deploy.sh`); a change is
finished only after it is deployed, the service restarted and its health checked.

## Rule A: when the owner restates a spoken command in chat

A Telegram or chat message from the owner's own account that restates a recent spoken command (for example «поставь телик на паузу»
arriving right after a clip-feed message whose transcript is different) is a correction of speech recognition, not an instruction to
act on. Do not perform the command. The work is:

1. Find the dialog: the `stt_final` line in the syslog, the `utterance_trace` line, the trace in `dialogs.sqlite3` (tables
   `utterance_traces`, `voice_clips`, `clip_feed`, `asr_corrections`) and the clip-feed message itself.
2. Find the cause: what the recogniser heard and which layer lost the word (acoustics, extra dictionary, vocabulary, grammar, router).
3. Fix the whole class of mistakes, not the one sentence, test first as above, at the layer that owns it (the extra dictionary
   `code/extra_dict_ru.json`, the vocabulary, the grammar).
4. Finish autonomously through merge, deploy and a live proof with a replay of the real phrase that does not execute the command.
   Reply to the owner only with what was found, what changed and the live proof.

## Rule B: all smart-home control goes through the Home Assistant API

Every device of the house (lights, television, climate, media players, scripts, scenes) is driven only through the Home Assistant
API by the shared Home Assistant client (module `ha_client`): service calls, scripts and Home Assistant automations. A new side
script, proxy, cache or private state file of device state, or a new timer of our own that acts on a device, is not allowed. Code
that still reaches Home Assistant another way (the legacy `hacli.py` proxy and its cache, direct calls in the dashboard) is being
migrated to the client and is not a pattern to copy. Behaviour that pauses, resumes or switches a device on a timer or an event is
built as a native Home Assistant automation, and this repository only fires the events it needs. If something does not work through
the API, or the architecture is unclear, investigate it in Home Assistant itself (states, attributes, services, logs) and solve it
there; do not work around it here. Discovery of the Home Assistant server and onboarding are automatic (mDNS discovery and the
standard Home Assistant login flow, confirmed once in Home Assistant itself): no hand-typed keys. The television pause while the
speaker listens (`code/tvpause.py`) is being replaced by the native automation; it
stays until the replacement is confirmed on the live system or the owner says to remove it, and then it is removed in its own change.
The speaker's own functions (music, timers, announcements, its own clock display) are product features and stay here.

## Rule C: the speaker never asks for a bare repeat

When a phrase cannot be read, the speaker never asks for a bare repeat («Повтори», «Не расслышала» with nothing else). It asks one
short, informative question that names the plausible actions (two at most), built from what the home really has: `code/clarify.py`
takes the entity catalog and live states of Home Assistant, the pinned phrases of the grammar and the recent dialog, and nothing
runs until the owner confirms it by voice («да» or a choice word). A reprompt that opens with what was not heard is fine as long as
it goes on to name the actions («Ничего не расслышал. Что сделать: …?»). The yes/no/choice confirmation stays in the repair dialog
(`correction.py`); reprompt wording that is still written elsewhere (the quasar container handlers) is being moved into
`clarify.py` and is not a pattern to copy.

## Writing reports for people (Telegram, PR bodies, spec summaries)

These are read by a human, not another agent, so optimize for being understood:

- Never invent abbreviations or compress terms into fragments. Write the full term.
- Prefer fewer points explained in full sentences over more points compressed into
  jargon. A short list of clear sentences beats a long list of cryptic stubs.
- Expand every non-obvious term at first use (name it in full, then abbreviate if you
  must reuse it).
- When a message exceeds the channel's length limit, cut secondary content — drop whole
  points — rather than compressing the wording of what remains.

# >>> rig-managed orchestrator (do not edit) >>>
# Orchestrator, not worker. Do not implement inline. Do not run `gh` except an unchained
# `gh ship <N>`, or read-only `gh issue` list/view, `gh pr` list/view/checks/diff, `gh run`
# list/view/watch, `gh repo view`, GET-only `gh api` (no -f/-F/--input). Do not `git
# worktree add` — `rig worktree create`.
# Read-only (`git status`, `ls`, `cat`, `grep`, `git worktree list`) is fine. Dispatch a subagent for the rest. Skill: delegate-work-to-subagents. Hook: orchestrator-stays-thin.
# Key reports MUST go to Telegram (`tg --tag report`): quote the user's original words, include ticket/PR URLs, name the measure already started, draft via `review just-ask`. Never "not in prod" with no next action.
# After a successful `gh ship` of a load-bearing feature, merge is not done: (1) turn it on
# in settings if it has a config knob, (2) `rig apply commit` on this machine, (3) prove it
# WORKS on this machine (command output, Telegram, deny text — SHA/on-disk is not a proof), (4) re-check the original user quote is solved.
# A helper yield is not done — do not tick the todo until the original ask is live-proven; wake parked helpers via hub send.
# <<< rig-managed orchestrator (do not edit) <<<

# >>> rig-managed autonomous-mode (do not edit) >>>
# Mode autonomous: extra-approval gate OFF. Do not invent a hold-for-human.
# Ship when gates pass. Skill: autonomous-mode (skill://autonomous-mode).
# <<< rig-managed autonomous-mode (do not edit) <<<
