"""Nightly intent loop: measure, grow the labelled corpus, cluster what the speaker could not answer, and brief an agent.

`prepare` is deterministic and needs no model: it extracts the day's utterances from the dialog store, appends the newly
labelled real rows to the private corpus (owner feedback, and the opt-in Jev labeller), runs the production route over the
labelled corpora (the back-test with its gate), clusters what only the brain answered, and writes a private working
directory plus a brief. `run` then hands the brief to an omp session through the sanctioned detached launcher (skill
rig-detached-omp). When the gate is green the session proposes grammar changes and lands them through the normal merge path
(back-test gate, tests, one review, pull request, `gh ship`); it never bypasses a gate. When the gate is already red the
brief is the blocked one: `review brainstorm`, no commit, no pull request, no ship. `morning` hands the hatch requests the
night collected to the owner through the harness. `--dry-run` does all of it except what changes state outside the private
working directory: no corpus row, no baseline promotion, no Jev call, no session, no Telegram.

Everything under the output directory is private household data (utterances); only aggregates ever leave it.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import time
from collections import Counter
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import backtest
from .grammar import Grammar, load
from .normalize import Normalised

DEFAULT_BUNDLE = Path(__file__).parent / "grammar" / "ru.json"
DEFAULT_DB = "~/.local/share/aisis/dialogs.sqlite3"
DEFAULT_OUT = "~/.local/share/aisis/intent-nightly"
LAUNCHER = "~/.agents/skills/rig-detached-omp/rig-detached-omp"
CLUSTER_SIMILARITY = 0.5
MAX_CLUSTERS = 40
JEV_CLUSTER_LIMIT = 25          # clusters whose representative is shown to Jev per night
JEV_MIN_CONFIDENCE = 0.8        # below it Jev's answer is no label
CORPUS_FILE = "real-voice-nightly-labelled.jsonl"     # the nightly's own append-only file (a real source by name)
OWNER_LABELS_FILE = "owner-labels.jsonl"              # the owner's feedback inbox, in the output root
HATCH_FILE = "hatch-requests.md"
HATCH_ASKED = "hatch-asked"
# Words that carry no intent: they only blur the similarity of two phrasings of one request.
_STOP = frozenset(("я", "ты", "мне", "меня", "тебя", "нам", "это", "этот", "эту", "вот", "там", "тут", "как", "что", "и", "в", "на", "с", "а", "же",
                   "ли", "бы", "то", "ну", "да", "нет", "не", "по", "за", "из", "для", "про", "у", "о", "к", "от", "до", "при", "над", "под"))
_LAYER_NAME = {"fast": "L1", "brain": "BRAIN", "none": "NONE"}      # the corpus' vocabulary for the answering layer


class NightlyError(Exception):
    """A condition the owner has to see: the message never carries an utterance or a key."""


@dataclass(frozen=True)
class Heard:
    text: str
    ts: float
    layer: str       # fast (the quasar layer answered), brain (the language model answered), none (nothing was said back)


def extract(db_path: str | Path, since: float, until: float | None = None) -> list[Heard]:
    """Spoken utterances of a time window with the layer that answered each (the dialog store's turn log)."""
    path = Path(db_path).expanduser()
    if not path.exists():
        raise FileNotFoundError("dialog store not found")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        dialogs = connection.execute(
            "SELECT id FROM dialogs WHERE kind='voice' AND ts_start >= ? AND ts_start < ? ORDER BY ts_start",
            (since, until if until is not None else time.time() + 1)).fetchall()
        heard: list[Heard] = []
        for (dialog_id,) in dialogs:
            turns = connection.execute("SELECT ts, role, kind, text FROM dialog_turns WHERE dialog_id=? ORDER BY ts, id",
                                       (dialog_id,)).fetchall()
            for index, (ts, role, kind, text) in enumerate(turns):
                if role != "user" or kind != "speech" or not text.strip():
                    continue
                layer = "none"
                for _ts, nrole, nkind, _text in turns[index + 1:]:
                    if nrole == "user":
                        break
                    if nrole == "event" and nkind == "intent":
                        layer = "fast"
                        break
                    if nrole == "assistant":
                        layer = "brain"
                heard.append(Heard(text.strip(), ts, layer))
        return heard
    finally:
        connection.close()


# ── clustering ──────────────────────────────────────────────────────────────

def content_stems(grammar: Grammar, text: str) -> frozenset[str]:
    utterance: Normalised = grammar.normalise(text)
    return frozenset(s for s in utterance.stems if s not in _STOP and len(s) > 1)


@dataclass
class Cluster:
    stems: frozenset[str]
    texts: Counter

    @property
    def size(self) -> int:
        return sum(self.texts.values())


def cluster_texts(grammar: Grammar, texts: list[str]) -> list[Cluster]:
    """Greedy single pass over the most frequent phrasings: join the cluster with the best stem-set Jaccard >= 0.5."""
    clusters: list[Cluster] = []
    for text, count in Counter(texts).most_common():
        stems = content_stems(grammar, text)
        best, best_score = None, 0.0
        for cluster in clusters:
            union = len(stems | cluster.stems)
            score = len(stems & cluster.stems) / union if union else 0.0
            if score > best_score:
                best, best_score = cluster, score
        if best is not None and best_score >= CLUSTER_SIMILARITY:
            best.texts[text] += count
            best.stems = best.stems | stems
        else:
            clusters.append(Cluster(stems, Counter({text: count})))
    return sorted(clusters, key=lambda c: -c.size)


# ── corpus growth ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Labelled:
    """A newly labelled real utterance. `labeller` says who is responsible for the label: `owner` or `jev`."""
    text: str
    truth: str
    labeller: str
    ts: float
    layer: str
    note: str = ""


def scan_corpus(corpus_dir: Path) -> tuple[set[str], set[str], list[str]]:
    """Canonical texts and truth labels of every real corpus file, plus the unreadable lines as `file:line`.

    The same files the back-test reads as real (`backtest.corpus_source`); a line that is not a JSON object with a text is
    reported, never skipped, because it could be the very row a candidate duplicates."""
    keys: set[str] = set()
    truths: set[str] = set()
    corrupt: list[str] = []
    for path in sorted(corpus_dir.glob("*.jsonl")):
        if backtest.corpus_source(path.name) != "real" or path.name == backtest.REPLAY_FILE:
            continue
        for number, raw in enumerate(path.read_bytes().splitlines(), 1):
            if not raw.strip():
                continue
            try:
                record = json.loads(raw.decode("utf-8"))
            except ValueError:
                record = None
            if not isinstance(record, dict) or not isinstance(record.get("text"), str):
                corrupt.append(f"{path.name}:{number}")
                continue
            text = record["text"].strip()
            if text:
                keys.add(backtest.split_key(text))
            if record.get("truth"):
                truths.add(str(record["truth"]))
    return keys, truths, corrupt


def allowed_truths(grammar: Grammar, known: set[str]) -> set[str]:
    """Labels a new real row may carry: the grammar's own, the back-test's brain and unaddressed ones, and any already in the corpus."""
    config = grammar.backtest
    labels = {label for intent in grammar.intents for label in intent.labels}
    return labels | set(config.get("brain_labels") or ()) | set(config.get("unaddressed") or ()) | known


def read_owner_labels(path: Path, allowed: set[str], heard: dict[str, Heard], now: float) -> tuple[list[Labelled], list[str]]:
    """The owner's feedback inbox: lines `{"text": ..., "truth": ..., "ts"?: ...}`. A line that cannot be used is reported with
    its number and reason (never its text) and stays in the file; the inbox is never rewritten."""
    if not path.exists():
        return [], []
    found: list[Labelled] = []
    rejected: list[str] = []
    for number, raw in enumerate(path.read_bytes().splitlines(), 1):
        if not raw.strip():
            continue
        where = f"{path.name}:{number}"
        try:
            record = json.loads(raw.decode("utf-8"))
        except ValueError:
            rejected.append(f"{where}: not a JSON line")
            continue
        text = record.get("text") if isinstance(record, dict) else None
        truth = record.get("truth") if isinstance(record, dict) else None
        if not isinstance(text, str) or not text.strip() or not isinstance(truth, str) or not truth:
            rejected.append(f"{where}: needs a text and a truth")
        elif truth not in allowed:
            rejected.append(f"{where}: unknown label")
        else:
            seen = heard.get(backtest.split_key(text))
            ts = record.get("ts")
            found.append(Labelled(text.strip(), truth, "owner", float(ts) if isinstance(ts, (int, float)) else (seen.ts if seen else now),
                                  _LAYER_NAME[seen.layer] if seen else "UNKNOWN", str(record.get("note") or "")))
    return found, rejected


def append_owner_label(inbox: Path, text: str, truth: str, allowed: set[str]) -> None:
    """One line into the feedback inbox (mode 0600, append only). The label must be one the corpus can use."""
    if not text.strip():
        raise NightlyError("empty text")
    if truth not in allowed:
        raise NightlyError("unknown label (a corpus label of an intent, a brain label, or one already used in the corpus)")
    inbox.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _append_lines(inbox, [{"text": text.strip(), "truth": truth, "ts": time.time()}])


def jev_candidates(grammar: Grammar, answers: list[tuple[str, tuple[str, float]]], now: float) -> list[Labelled]:
    """Rows from Jev's answers (text, (intent id, confidence)): only a confident choice of a real intent with a label is a label."""
    by_id = {intent.id: intent for intent in grammar.intents}
    rows = []
    for text, (choice, confidence) in answers:
        intent = by_id.get(choice)
        if intent is not None and intent.labels and confidence >= JEV_MIN_CONFIDENCE:
            rows.append(Labelled(text, intent.labels[0], "jev", now, "UNKNOWN", f"jev {choice} {confidence:.2f}"))
    return rows


def _append_lines(path: Path, records: list[dict[str, Any]]) -> None:
    """Append JSON lines, never touching what is there: a last line without its newline gets one first, and the file is 0600."""
    payload = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    separator = ""
    if path.exists() and path.stat().st_size:
        with open(path, "rb") as existing:
            existing.seek(-1, os.SEEK_END)
            separator = "" if existing.read(1) == b"\n" else "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, (separator + payload).encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def grow_corpus(corpus_dir: Path, candidates: list[Labelled], *, dry_run: bool = False) -> dict[str, int]:
    """Append the candidates that the real corpus does not have yet (canonical text, `backtest.split_key`) to the nightly file.

    Append only: no row is rewritten or removed, a duplicate is skipped (counted), and a corpus with an unreadable line is
    not appended to at all (the line could be the duplicate), the error names the lines. The held-out side of a new row is
    the stable hash of its text, which `backtest.load_rows` computes; nothing here chooses it."""
    keys, _truths, corrupt = scan_corpus(corpus_dir) if corpus_dir.is_dir() else (set(), set(), [])
    if corrupt:
        raise NightlyError("the real corpus has unreadable lines, they stay as they are and nothing was appended: " + ", ".join(corrupt))
    fresh: list[Labelled] = []
    duplicates = 0
    for candidate in candidates:
        key = backtest.split_key(candidate.text)
        if key in keys:
            duplicates += 1
            continue
        keys.add(key)
        fresh.append(candidate)
    if fresh and not dry_run:
        corpus_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        _append_lines(corpus_dir / CORPUS_FILE, [
            {"text": row.text, "truth": row.truth, "layer": row.layer, "verdict": "unverified", "note": row.note, "ts": row.ts,
             "labeller": row.labeller} for row in fresh])
    return {"candidates": len(candidates), "new": len(fresh), "appended": 0 if dry_run else len(fresh), "duplicates": duplicates}


# ── preparation ─────────────────────────────────────────────────────────────

def _router(grammar: Grammar):
    import intent_route  # code/: the production route (grammar -> arithmetic -> weather -> catch-alls)
    return intent_route.IntentRouter(grammar)


def prepare(*, grammar_path: str | Path | None, corpus: str | None, db: str, hours: float, out_root: str,
            label: Callable[[str, dict[str, str]], tuple[str, float] | None] | None = None,
            now: float | None = None, dry_run: bool = False) -> Path:
    """Write the day's private working directory and return it. `label(text, intents)` is the optional Jev step.

    `dry_run` leaves the world as it was apart from the working directory: no corpus row, no baseline promotion, no
    report lock, no Jev call; the summary says what would have been appended."""
    now = now if now is not None else time.time()
    grammar = load(grammar_path or DEFAULT_BUNDLE)
    day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    out_dir = Path(out_root).expanduser()
    out = out_dir / day
    out.mkdir(parents=True, exist_ok=True, mode=0o700)

    heard = extract(db, now - hours * 3600, now)
    router = _router(grammar)
    classified = [(item, router.classify(item.text)) for item in heard]
    by_layer = Counter(item.layer for item in heard)
    answered_now = [item for item, decision in classified if decision is not None and item.layer != "fast"]
    open_items = [item for item, decision in classified if item.layer != "fast" and (decision is None or decision.intent.startswith(("asr.", "refuse.unknown")))]
    clusters = cluster_texts(grammar, [item.text for item in open_items])[:MAX_CLUSTERS]

    descriptions = {i.id: "; ".join(example.say for example in i.examples[:2]) for i in grammar.intents if i.examples}
    jev: dict[str, Any] | None = None if label is None else {"asked": 0, "failed": 0, "labelled": 0, "skipped": "dry run" if dry_run else None}
    answers: list[tuple[str, tuple[str, float]]] = []
    cluster_rows = []
    for number, c in enumerate(clusters, 1):
        representative = c.texts.most_common(1)[0][0]
        row = {"id": number, "size": c.size, "distinct": len(c.texts), "examples": [t for t, _ in c.texts.most_common(5)],
               "nearest_intent": None, "nearest_confidence": None}
        if jev is not None and not dry_run and number <= JEV_CLUSTER_LIMIT:
            jev["asked"] += 1
            try:
                labelled = label(representative, descriptions)
            except Exception:            # counted and reported by the caller: a failed night of labels is never silent
                jev["failed"] += 1
                labelled = None
            if labelled is not None:
                row["nearest_intent"], row["nearest_confidence"] = labelled
                answers.append((representative, labelled))
        cluster_rows.append(row)

    corpus_dir = backtest.corpus_dir(corpus)
    known_truths = scan_corpus(corpus_dir)[1] if corpus_dir.is_dir() else set()
    heard_by_key = {backtest.split_key(item.text): item for item in heard}
    owner_rows, rejected = read_owner_labels(out_dir / OWNER_LABELS_FILE, allowed_truths(grammar, known_truths), heard_by_key, now)
    jev_rows = jev_candidates(grammar, answers, now)
    if jev is not None:
        jev["labelled"] = len(jev_rows)
    growth = {**grow_corpus(corpus_dir, owner_rows + jev_rows, dry_run=dry_run), "rejected": rejected}

    report = None
    if corpus_dir.is_dir():
        rows, manifest = backtest.load_rows(corpus_dir, grammar)
        if rows:
            # compare -> save -> promote is one step under the baseline lock, as in `backtest`; a dry run reads the baseline only
            with nullcontext() if dry_run else backtest.baseline_lock():
                previous = backtest.load_previous(None)
                report = backtest.build_report(grammar, rows, manifest, previous=previous,
                                               decide=_measured(grammar), route="combined", require_baseline=True)
                promote = report["gate"]["ok"] and not dry_run
                backtest.save_report(report, out / "backtest.json", latest=backtest.reports_dir() / "latest.json" if promote else None)
            os.chmod(out / "backtest.json", 0o600)       # it holds the dev utterances

    summary: dict[str, Any] = {
        "date": day, "dry_run": dry_run, "window_hours": hours, "grammar_sha256": grammar.sha256,
        "utterances": len(heard), "by_layer": dict(by_layer),
        "grammar_would_answer_now": len(answered_now), "open": len(open_items), "clusters": len(clusters),
        "answered_without_model_share": round((by_layer["fast"] + len(answered_now)) / len(heard), 4) if heard else None,
        "corpus_growth": growth, "jev": jev,
        "backtest": None if report is None else {name: {k: v for k, v in split.items() if k in ("rows", "addressed", "coverage", "correct_rate", "completed_rate", "wrong_actions", "false_triggers")}
                                                  for name, split in report["splits"].items()},
        "goal": None if report is None else report.get("goal"),
        "gate": None if report is None else report["gate"],
        "regressed": None if report is None or not report.get("compare") else len(report["compare"]["regressed"]),
        "lost_rows": None if report is None or not report.get("compare") else report["compare"]["lost_rows"],
        "relabelled": None if report is None or not report.get("compare") else len(report["compare"]["relabelled"]),
        "legacy_floor": None if report is None else report.get("legacy_floor"),
    }
    _write(out / "summary.json", summary)
    _write(out / "clusters.json", cluster_rows)
    _write(out / "today.jsonl", [{"text": i.text, "ts": i.ts, "layer": i.layer} for i in heard], lines=True)
    brief = out / "brief.md"
    brief.write_text(render_brief(summary, out, corpus_dir), encoding="utf-8")
    os.chmod(brief, 0o600)
    return out


def _measured(grammar: Grammar):
    import intent_route
    return intent_route.measured_route(intent_route.measured_router(grammar), mode="combined")


def _write(path: Path, data: Any, *, lines: bool = False) -> None:
    text = "\n".join(json.dumps(item, ensure_ascii=False) for item in data) + "\n" if lines else json.dumps(data, ensure_ascii=False, indent=1) + "\n"
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)


def gate_failures(summary: dict[str, Any]) -> list[str]:
    """The failed gate rules of the prepared report. No report (no labelled corpus) is a blocked gate too: a night that
    could not measure must not be briefed as a green one."""
    if summary.get("gate") is None:
        return ["no back-test report: the labelled corpus is missing or empty"]
    return list(summary["gate"]["failures"])


# ── the agent brief ─────────────────────────────────────────────────────────

BRIEF = """# Nightly intent loop {date} ({mode})

You maintain the intent grammar of the AISIS voice speaker (ticket #107). Today's measurements are ready. {job}
Work only in your own worktree: `rig worktree create intent-nightly-{date}` from the aisis repository, then `cd` into it.
Never edit the main checkout, never print or log secrets (`.env`, any key), and never put a household utterance into git, a PR, an issue
or a Telegram message: aggregates, intent ids and phrases you invent yourself only.

## Inputs (private files, read-only; they hold real utterances)
- `{out}/summary.json`: today's counts by answering layer, back-test numbers and the 90 % goal with its gap (the superseded 0.85 held-out floor is printed there as `legacy_floor`, NOT met: the gate does not enforce it), gate state, rows that got worse / were lost / were relabelled against the previous run, rows added to the corpus.
- `{out}/clusters.json`: today's utterances the speaker could not answer without a model, clustered (size, examples, nearest existing intent by Jev if known).
- `{out}/backtest.json`: the full back-test report; labelled corpora are in `{corpus}`.

## Steps
{steps}
"""

STEPS_LIVE = """1. Read `summary.json`: the gate was green when the night began and no row had got worse.
2. For each cluster, biggest first, at most 10: decide one of: (a) an existing intent should read it: add a pattern, a lexicon form or a
   synonym; (b) a new intent is justified (repeated, deterministic to execute, not open-ended): add it with tier, labels, action, reply, examples and negatives;
   (c) two intents overlap: consolidate; (d) it belongs to the brain (knowledge, advice, chit-chat, garbled): leave it and say why in the PR body. A phrase seen once is not a pattern.
   Edit only `services/yandex-station/code/intent_grammar/grammar/ru.yaml` (and the tests that pin it). Write new examples and negatives as paraphrases of your own.
3. In `services/yandex-station/code`: `python3 -m intent_grammar check && python3 -m intent_grammar compile`, then
   `python3 -m unittest test_intent_grammar test_intent_route`, then `python3 -m intent_grammar backtest --route combined --gate --no-latest --previous {latest}`.
   The proposal survives only if the gate holds: no row got worse than in the previous promoted report (row by row, no tolerance: handled -> missed/wrong and missed -> wrong fail,
   wrong -> abstained is an improvement), none lost or relabelled, the held-out checks (`gate.holdout`: the interval test and the point floor switch on with the addressed-row count), no new false trigger or wrong action, pinned examples pass, and the emergency safety set (`gate.safety`) still reads as `refuse.emergency`.
   A change that breaks the gate is dropped or narrowed; never lower a gate floor, never edit the corpus, never look at held-out texts. Nothing survives, or the diff
   is trivial (whitespace, comments, one synonym with no measured effect)? Propose nothing: say why in the report (step 6) and skip step 4.
4. At least one change survives: commit and land it, one logical change per commit, conventional messages.
   a. Stage only the grammar, its compiled bundle and the tests, then run ONE review of the staged diff:
      `review diff --staged --task 107 --commit --detach` and wait with `review wait <job-id>` (the host queue is slow; never submit a duplicate; never
      use a hatch). Fix the findings that are real and push back, with reasons, on the ones that are not; a fix that changes the diff gets one more review.
   b. `git push -u origin HEAD`, then `gh pr create` titled `feat(intents): nightly proposal {date}` with `Refs #107` (never Closes/Fixes) in the body: counts
      and before/after coverage and correctness (dev and held-out), the changes by intent id, the clusters left to the brain.
   c. Merge it the normal way, from the repository root, not from the worktree: `gh ship <PR> --task '#107'`. It checks CI, threads, the review quorum
      and the review-dwell window and refuses early ones: wait and run it again. Do not skip a gate flag. If it merges, note the merge commit.
5. A gate blocks you (the back-test gate, a test, a review finding you cannot honestly dismiss, a CI check, a `gh ship` refusal): never bypass it and never
   just stop. Run `review brainstorm "<the gate, what it demands, what blocks it>" --task 107` (see `review help brainstorm`) to work out how to satisfy
   the gate properly from the architecture point of view, a refactor included when that is what the gate asks for, do it, and continue from step 3.
   Put the brainstorm outcome and what you did in the PR body (no PR: in the report). {hatch}
6. Report (aggregates only, no phrases, no local paths): add a comment to the open issue titled "Nightly intent loop reports" in this repository, creating it
   if absent, then send exactly ONE short Telegram message with `tg`: date, share answered without a model, coverage and correctness against the 90 % goal,
   held-out numbers, gate state, the PR number and merged/not merged or «без предложений».
7. Write the handoff file `{handoff}` as JSON: date, mode, gate_ok, pr (number or null), merged (true/false), proposal (what changed by intent id, or why nothing),
   gates_blocked (list), hatch_requested (true/false), issue_comment_url, coverage numbers. Then stop. Finish within 3 hours (the review queue is shared and can be slow)."""

STEPS_BLOCKED = """The gate is RED before any change of tonight. Failed rules (rule names and numbers only):
{failures}

Tonight nothing is committed, pushed, put into a pull request or shipped, whatever you find: a red gate on main is the owner's to see first.
1. Run `review brainstorm "<the failed rules above, what each demands, what you suspect>" --task 107` (see `review help brainstorm`) to work out how to
   satisfy the gate properly from the architecture point of view, a refactor included. Never lower a floor, never edit the corpus, never look at held-out
   texts, never remove a phrase from the safety set: a gate that fails is satisfied, not argued with. A missing or unusable baseline (`no baseline`, `baseline unusable`) is not yours to create: `--init-baseline` is the owner's act, so report it and fix nothing around it.
2. Do the fix in your worktree and check it with `python3 -m intent_grammar check && python3 -m intent_grammar compile`,
   `python3 -m unittest test_intent_grammar test_intent_route` and `python3 -m intent_grammar backtest --route combined --gate --no-latest --previous {latest}`.
3. Write `{out}/proposal.patch` (the `git diff` of your worktree), `{out}/brainstorm.md` (what the brainstorm settled and what you did) and `{out}/report.md`
   (aggregates only: which rules failed, what the fix does, the gate state with it). Do NOT run `review diff`, `git commit`, `git push`, `gh pr create` or `gh ship`;
   no issue comment and no Telegram: the loop already told the owner. Leave the worktree in place.
4. {hatch}
5. Write the handoff file `{handoff}` as JSON: date, mode, gate_ok (false), pr (null), merged (false), proposal (what the patch changes, or why nothing),
   gates_blocked (the failed rules), hatch_requested (true/false), coverage numbers. Then stop. Finish within 3 hours."""

HATCH_RULE = """Only a gate that is demonstrably broken (it refuses a state that is correct, and you can cite command and output): file a ticket (`task new`) with that
   evidence, append the request to `{out}/hatch-requests.md` and name it in the report. The owner decides hatches in the MORNING (the loop's `morning` step puts
   the file in front of them through the harness): during the night never set a `RIG_HATCH_REQUEST_*` variable, and the `require-review-before-commit` hatch is never an option at all."""


def render_brief(summary: dict[str, Any], out: Path, corpus_dir: Path) -> str:
    fields = {"date": summary["date"], "out": out, "corpus": corpus_dir, "latest": backtest.reports_dir() / "latest.json", "handoff": out / "handoff.json"}
    failures = gate_failures(summary)
    hatch = HATCH_RULE.format(**fields)
    if failures:
        steps = STEPS_BLOCKED.format(failures="\n".join(f"- {line}" for line in failures), hatch=hatch, **fields)
        return BRIEF.format(mode="gate blocked", steps=steps, job="The gate is red, so tonight is a repair job with no landing.", **fields)
    steps = STEPS_LIVE.format(hatch=hatch, **fields)
    return BRIEF.format(mode="live", steps=steps, **fields, job="Propose grammar changes and land them through the normal merge path, with the same gates as any other change: "
                                                                "back-test, tests, the review board, `gh ship`. You never bypass a gate.")


# ── optional Jev step ───────────────────────────────────────────────────────

def jev_labeller(key: str, timeout: float = 2.0) -> Callable[[str, dict[str, str]], tuple[str, float]]:
    """`label(text, intents)` through jev.label_with_jev: (choice, confidence). Raises NightlyError when the Jev module is not
    installed, and from the label call on any failure: `prepare` counts those, the caller reports them."""
    try:
        import asyncio
        import urllib.error
        import urllib.request
        import jev
    except ImportError:
        raise NightlyError("--jev needs the Jev module (jev.py, the Jev route), which is not installed in this checkout") from None

    async def post(url: str, headers: dict[str, str], body: dict[str, Any]) -> tuple[int, Any]:
        def call() -> tuple[int, Any]:
            request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as err:
                return err.code, None
        return await asyncio.to_thread(call)

    client = jev.JevClient(post, key, timeout=timeout)

    def label(text: str, intents: dict[str, str]) -> tuple[str, float]:
        try:
            answer = asyncio.run(jev.label_with_jev(client, text, intents))
        except Exception as err:
            raise NightlyError(f"jev call failed: {type(err).__name__}") from None     # never the message: it could carry the text
        return answer.choice, answer.confidence

    return label


# ── launch ──────────────────────────────────────────────────────────────────

def launch(out: Path, repo: str | Path, launcher: str = LAUNCHER) -> subprocess.CompletedProcess[str]:
    """Detach the omp session with the brief; returns at once (the launcher forks the child)."""
    name = "intent-nightly-" + out.name
    return subprocess.run([str(Path(launcher).expanduser()), name, str(out / "brief.md"), str(Path(repo).expanduser())],
                          capture_output=True, text=True, timeout=60, check=False, env=session_env(os.environ))


_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", re.IGNORECASE)


def session_env(env: Any) -> dict[str, str]:
    """The agent session's environment: everything but keys and tokens (the Jev key stays in this process, never in a model's reach)."""
    return {name: value for name, value in env.items() if not _SECRET_NAME.search(name)}


def tg_note(text: str) -> None:
    """One plain Telegram line (failures of the loop itself); best effort."""
    try:
        subprocess.run(["tg", text], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


# ── morning: hatch requests ─────────────────────────────────────────────────

def harness_task(title: str, body: str) -> subprocess.CompletedProcess[str]:
    """A local process ticket on the owner's harness board."""
    return subprocess.run(["harness", "tasks", "new", "--title", title, "--body", body],
                          capture_output=True, text=True, timeout=60, check=False)


def collect_hatch_requests(out_root: str = DEFAULT_OUT, ask: Callable[[str, str], Any] | None = None) -> dict[str, list[str]]:
    """Put every night's `hatch-requests.md` that the owner has not seen in front of them as a harness ticket.

    The night only writes the file; this step, run in the morning, is the one that asks, once per night's file (a `hatch-asked`
    marker beside it). A failed ask leaves no marker and is reported: tomorrow asks again. The loop never uses a hatch itself."""
    root = Path(out_root).expanduser()
    asked: list[str] = []
    failed: list[str] = []
    for day in sorted(p for p in root.glob("*") if p.is_dir() and _DATE.match(p.name)) if root.is_dir() else []:
        request = day / HATCH_FILE
        if not request.is_file() or (day / HATCH_ASKED).exists():
            continue
        body = request.read_text(encoding="utf-8")[:8000]
        try:
            done = (ask or harness_task)(f"Intent nightly {day.name}: gate hatch request, owner decision", body)
            ok = done.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        if not ok:
            failed.append(day.name)
            continue
        marker = day / HATCH_ASKED
        marker.write_text(datetime.now().astimezone().isoformat() + "\n", encoding="utf-8")
        os.chmod(marker, 0o600)
        asked.append(day.name)
    return {"asked": asked, "failed": failed}


_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def last_run(out_root: str = DEFAULT_OUT) -> dict[str, Any] | None:
    """The newest day's summary (and handoff, when the agent wrote one), for `nightly status`."""
    root = Path(out_root).expanduser()
    days = sorted(p for p in root.glob("*") if p.is_dir() and _DATE.match(p.name)) if root.is_dir() else []
    if not days:
        return None
    day = days[-1]
    summary = json.loads((day / "summary.json").read_text(encoding="utf-8")) if (day / "summary.json").exists() else {}
    handoff = json.loads((day / "handoff.json").read_text(encoding="utf-8")) if (day / "handoff.json").exists() else None
    return {"day": day.name, "summary": summary, "handoff": handoff}
