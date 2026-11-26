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

CI (`.github/workflows/ci.yml`) is the source of truth for what must pass. The commands, run from the repository root unless noted:

- `python3 scripts/check_contracts.py` and `python3 -m unittest discover -s scripts -p "test_*.py"` check the repository contracts.
- In `services/yandex-station/code/`, run the unit tests with the speaker's virtual environment
  (`~/.local/share/aisis/venv/bin/python -m unittest test_extra_dict test_stt_extra_dict ...`; the exact module lists are in the CI
  steps), because the gateway tests need `aiohttp`.
- After every grammar edit, in `services/yandex-station/code/`: `python3 -m intent_grammar check`, `python3 -m intent_grammar compile`, then
  `python3 -m intent_grammar backtest --route combined --gate`. CI fails when the committed JSON bundle is stale
  (`python3 -m intent_grammar compile --check`).
- Overlay and corpus tests run with `python3 -m unittest discover -s <directory> -p "test_*.py"` (see CI for each directory);
  `node --test services/yandex-station/corpus-runner/*.test.mjs` runs the JavaScript ones.
- Never run the full corpus or the speech benchmarks on the workstation or inside a running home-service container; the corpus
  protocol in `services/yandex-station/corpus-runner/README.md` offloads them.

Work lands through a pull request from a linked worktree (`rig worktree create <name> --from origin/main`), with a ticket opened
first (`task new`) and referenced as `Refs #N`, never `Closes`. Ship with `gh ship <N>`. Deployment of the speaker services is
described in the operations runbook of `services/yandex-station/README.md` (`scripts/install-systemd-units.sh`, the `deploy-main`
worktree and `services/yandex-station/bluegreen/deploy.sh`).

## When the owner restates a spoken command in chat

A Telegram or chat message from the owner's own account that restates a recent spoken command (for example «поставь телик на паузу» arriving right after a
clip-feed message whose transcript is different) is a correction of speech recognition, not an instruction to act on. Do not
perform the command. First find the dialog and the cause: the `stt_final` line in the syslog, the `utterance_trace` line, the trace in
`dialogs.sqlite3` (tables `utterance_traces`, `voice_clips`, `clip_feed`, `asr_corrections`) and the clip-feed message itself.
Then improve the system for the whole class of mistakes, test first (a failing test on the real phrase pair, then the fix at the
layer that owns it, such as the extra dictionary `code/extra_dict_ru.json`, the vocabulary or the grammar), and finish autonomously:
merge, deploy, and prove it live with the replay of the real phrase. Reply to the owner only with what was found, what changed and
the live proof.

## Device control belongs to Home Assistant

All device control goes through the Home Assistant API via the shared Home Assistant client of this repository, in the standard
Home Assistant way: service calls, scripts and Home Assistant automations. New behaviour that pauses, resumes, switches or otherwise
drives a household device on its own timer or event is built as a Home Assistant automation, not as a side script, proxy, cache or
private state file in this repository. A behaviour of that kind that already exists here (for example the television pause while the
speaker listens) keeps working until its Home Assistant replacement has been shown working on the live system through a real run; only
then is it removed, in its own change, and never as a side effect of another task. Home Assistant discovery and onboarding are automatic: no hand-typed keys and no
manual steps beyond the confirmation in Home Assistant itself. The speaker's own functions (music, timers, announcements, its own
clock display) are product features and stay here.

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
