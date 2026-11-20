"""Nightly loop: extraction, clustering, corpus growth, the private working directory, the agent brief (live and blocked),
the dry run, the launcher call, the morning hatch step and the units."""
import configparser
import contextlib
import io
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from intent_grammar import backtest, cli, nightly
from intent_grammar.grammar import load

BUNDLE = Path(__file__).parent / "intent_grammar" / "grammar" / "ru.json"
SYSTEMD = Path(__file__).parent.parent / "systemd"
NOW = 1_794_000_000.0
UNREACHABLE_FLOOR = {"min_coverage": {"real/all": 1.01}}       # no corpus can hold it: the gate is red


def make_store(path, dialogs):
    db = sqlite3.connect(path)
    db.executescript("""CREATE TABLE dialogs(id TEXT PRIMARY KEY, kind TEXT NOT NULL, ts_start REAL NOT NULL, ts_end REAL);
                        CREATE TABLE dialog_turns(id INTEGER PRIMARY KEY, dialog_id TEXT NOT NULL, ts REAL NOT NULL, role TEXT NOT NULL,
                                                  kind TEXT NOT NULL, text TEXT NOT NULL DEFAULT '', res TEXT NOT NULL DEFAULT '');""")
    for index, (kind, ts, turns) in enumerate(dialogs):
        db.execute("INSERT INTO dialogs VALUES (?,?,?,?)", (f"d{index}", kind, ts, ts + max(len(turns) - 1, 0)))
        for offset, (role, tkind, text) in enumerate(turns):
            db.execute("INSERT INTO dialog_turns(dialog_id, ts, role, kind, text) VALUES (?,?,?,?,?)", (f"d{index}", ts + offset, role, tkind, text))
    db.commit()
    db.close()


def jsonl(*records):
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n"


def mode(path):
    return stat.S_IMODE(Path(path).stat().st_mode)


def tree(root):
    """Every file under root with its bytes and mode: what a run must leave alone."""
    root = Path(root)
    return {str(p.relative_to(root)): (p.read_bytes(), mode(p)) for p in sorted(root.rglob("*")) if p.is_file()} if root.exists() else None


class StoreFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.db = self.dir / "dialogs.sqlite3"
        make_store(self.db, [
            ("voice", NOW - 100, [("user", "speech", "Выключи свет в студии."), ("event", "intent", "off lights")]),
            ("voice", NOW - 200, [("user", "speech", "расскажи про динозавров"), ("assistant", "reply", "Динозавры…")]),
            ("voice", NOW - 300, [("user", "speech", "поставь будильник на семь утра"), ("assistant", "reply", "…")]),
            ("voice", NOW - 400, [("user", "speech", "включи режим сон на кухне"), ("assistant", "reply", "…")]),
            ("voice", NOW - 500, [("user", "speech", "включи режим сон в спальне"), ("assistant", "reply", "…")]),
            ("voice", NOW - 600, [("user", "speech", "Алиса"), ("user", "speech", "мне скучно")]),
            ("text", NOW - 700, [("user", "speech", "это текстовый диалог")]),
            ("voice", NOW - 999_999, [("user", "speech", "слишком давно")]),
        ])
        self.corpus = self.dir / "corpus"
        self.out = self.dir / "out"
        self.reports = self.dir / "reports"
        env = mock.patch.dict(os.environ, {backtest.ENV_REPORT_DIR: str(self.reports)})
        env.start()
        self.addCleanup(env.stop)

    with_baseline = True

    def write_corpus(self):
        self.corpus.mkdir(exist_ok=True)
        (self.corpus / "real-voice-2026-11-labelled.jsonl").write_text(jsonl(
            {"text": "который час", "truth": "TIME", "ts": 1}, {"text": "расскажи про динозавров", "truth": "KNOWLEDGE", "ts": 2},
            {"text": "включи свет", "truth": "LIGHT_CMD", "ts": 3}), encoding="utf-8")
        if self.with_baseline:
            self.seed_baseline()

    def seed_baseline(self):
        """The owner's one-off `backtest --init-baseline`: the nightly run never creates a baseline itself."""
        grammar = load(BUNDLE)
        rows, manifest = backtest.load_rows(self.corpus, grammar)
        report = backtest.build_report(grammar, rows, manifest, decide=nightly._measured(grammar), route="combined")
        self.reports.mkdir(exist_ok=True)
        backtest.save_report(report, self.dir / "seed.json", latest=self.reports / "latest.json")

    def bundle(self, gate=None, mutate=None):
        """A copy of the shipped bundle with another gate: the nightly side of a red or green gate, whatever the corpus holds."""
        bundle = json.loads(BUNDLE.read_text(encoding="utf-8"))
        if gate is not None:
            bundle["data"]["gate"] = gate
        if mutate:
            mutate(bundle["data"])
        path = self.dir / "gate.json"
        path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def prepare(self, gate=None, **kwargs):
        self.write_corpus() if not self.corpus.exists() else None
        return nightly.prepare(grammar_path=self.bundle({} if gate is None else gate), corpus=str(self.corpus), db=str(self.db), hours=1.0,
                               out_root=str(self.out), now=NOW, **kwargs)

    def summary(self, out):
        return json.loads((out / "summary.json").read_text(encoding="utf-8"))


class ExtractTests(StoreFixture):
    def test_layers_and_window(self):
        heard = nightly.extract(self.db, NOW - 3600, NOW)
        by_text = {h.text: h.layer for h in heard}
        self.assertEqual(by_text["Выключи свет в студии."], "fast")
        self.assertEqual(by_text["расскажи про динозавров"], "brain")
        self.assertEqual(by_text["Алиса"], "none")
        self.assertNotIn("слишком давно", by_text)
        self.assertNotIn("это текстовый диалог", by_text)

    def test_the_window_filters_utterance_times_not_dialog_starts(self):
        db = self.dir / "edges.sqlite3"
        make_store(db, [
            ("voice", 100, [("user", "speech", "до окна"), ("assistant", "reply", "…"), ("user", "speech", "внутри окна"), ("assistant", "reply", "…")]),
            ("voice", 1000, [("user", "speech", "ровно на границе"), ("user", "speech", "после окна")]),
        ])
        by_text = {h.text: h.layer for h in nightly.extract(db, 102, 1001)}
        self.assertEqual(by_text, {"внутри окна": "brain", "ровно на границе": "none"})     # [since, until): the end is open, the start closed

    def test_a_dialog_ending_before_the_window_contributes_nothing(self):
        db = self.dir / "old.sqlite3"
        make_store(db, [("voice", 10, [("user", "speech", "давно"), ("assistant", "reply", "…")])])
        self.assertEqual(nightly.extract(db, 500, 600), [])

    def test_missing_store(self):
        with self.assertRaises(FileNotFoundError):
            nightly.extract(self.dir / "none.sqlite3", 0)


class ClusterTests(unittest.TestCase):
    def test_phrasings_of_one_request_share_a_cluster(self):
        grammar = load(BUNDLE)
        clusters = nightly.cluster_texts(grammar, ["включи режим сон на кухне", "включи режим сон в спальне", "включи режим сон",
                                                    "какая столица Австралии", "включи режим сон на кухне"])
        self.assertEqual([c.size for c in clusters], [4, 1])
        self.assertEqual(clusters[0].texts["включи режим сон на кухне"], 2)


class CorpusGrowthTests(unittest.TestCase):
    """The held-out ratchet is only as honest as the corpus is stable: rows are added, never changed or removed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.corpus = Path(self.tmp.name) / "corpus"
        self.corpus.mkdir()
        env = mock.patch.dict(os.environ, {backtest.ENV_REPORT_DIR: str(Path(self.tmp.name) / "reports")})
        env.start()
        self.addCleanup(env.stop)
        self.hand = self.corpus / "real-voice-2026-11-labelled.jsonl"
        self.hand.write_text(jsonl({"text": "Включи свет!", "truth": "LIGHT_CMD", "ts": 1, "labeller": "IntentAudit-manual"}), encoding="utf-8")
        self.grammar = load(BUNDLE)

    def row(self, text, truth="LIGHT_CMD", labeller="owner"):
        return nightly.Labelled(text, truth, labeller, 1794000000.0, "L1", "")

    def nightly_rows(self):
        path = self.corpus / nightly.CORPUS_FILE
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def test_rows_are_only_added_and_existing_bytes_never_change(self):
        before = self.hand.read_bytes()
        first = nightly.grow_corpus(self.corpus, [self.row("поставь будильник на семь", "ALARM")])
        self.assertEqual(first, {"candidates": 1, "new": 1, "appended": 1, "duplicates": 0})
        nightly.grow_corpus(self.corpus, [self.row("сделай потише", "VOLUME")])
        self.assertEqual(self.hand.read_bytes(), before)
        rows = self.nightly_rows()
        self.assertEqual([r["text"] for r in rows], ["поставь будильник на семь", "сделай потише"])
        self.assertEqual({r["labeller"] for r in rows}, {"owner"})                  # every row says who labelled it
        self.assertTrue(all(r["verdict"] == "unverified" for r in rows))

    def test_a_row_stays_byte_identical_after_later_nights(self):
        nightly.grow_corpus(self.corpus, [self.row("поставь будильник на семь", "ALARM")])
        first_line = (self.corpus / nightly.CORPUS_FILE).read_bytes()
        nightly.grow_corpus(self.corpus, [self.row("сделай потише", "VOLUME")])
        self.assertTrue((self.corpus / nightly.CORPUS_FILE).read_bytes().startswith(first_line))

    def test_duplicates_are_skipped_by_canonical_text(self):
        result = nightly.grow_corpus(self.corpus, [
            self.row("включи свет"),                     # the hand-labelled row: case and punctuation differ
            self.row("ВКЛЮЧИ  СВЕТ?!"),                  # same again
            self.row("Мама, где ёжик"), self.row("мама где ежик"),      # ё folded, a duplicate inside the batch
        ])
        self.assertEqual(result, {"candidates": 4, "new": 1, "appended": 1, "duplicates": 3})
        self.assertEqual([r["text"] for r in self.nightly_rows()], ["Мама, где ёжик"])
        self.assertEqual(nightly.grow_corpus(self.corpus, [self.row("мама, где ёжик")])["new"], 0)     # a rerun adds nothing

    def test_a_row_of_another_real_file_is_a_duplicate_too(self):
        (self.corpus / "real-voice-2026-12-labelled.jsonl").write_text(jsonl({"text": "сделай потише", "truth": "VOLUME", "ts": 5}), encoding="utf-8")
        self.assertEqual(nightly.grow_corpus(self.corpus, [self.row("Сделай потише.")])["duplicates"], 1)

    def test_a_corrupt_line_is_reported_and_nothing_is_appended(self):
        broken = self.corpus / "real-voice-2026-12-labelled.jsonl"
        broken.write_bytes('{"text": "ok", "truth": "TIME"}\n{"text": "полстроки\n[1, 2]\n'.encode("utf-8"))
        snapshot = tree(self.corpus)
        with self.assertRaises(nightly.NightlyError) as caught:
            nightly.grow_corpus(self.corpus, [self.row("новая фраза")])
        self.assertIn("real-voice-2026-12-labelled.jsonl:2", str(caught.exception))
        self.assertIn("real-voice-2026-12-labelled.jsonl:3", str(caught.exception))
        self.assertNotIn("полстроки", str(caught.exception))
        self.assertEqual(tree(self.corpus), snapshot)               # the corrupt lines stay, no row was added

    def test_the_file_is_0600_even_when_it_was_not(self):
        nightly.grow_corpus(self.corpus, [self.row("сделай потише", "VOLUME")])
        self.assertEqual(mode(self.corpus / nightly.CORPUS_FILE), 0o600)
        (self.corpus / nightly.CORPUS_FILE).chmod(0o644)
        nightly.grow_corpus(self.corpus, [self.row("сделай громче", "VOLUME")])
        self.assertEqual(mode(self.corpus / nightly.CORPUS_FILE), 0o600)

    def test_a_last_line_without_its_newline_is_not_glued_to_the_next_row(self):
        path = self.corpus / nightly.CORPUS_FILE
        path.write_bytes(json.dumps({"text": "включи кофеварку", "truth": "DEVICE", "ts": 1}, ensure_ascii=False).encode("utf-8"))
        nightly.grow_corpus(self.corpus, [self.row("сделай потише", "VOLUME")])
        self.assertEqual([r["text"] for r in self.nightly_rows()], ["включи кофеварку", "сделай потише"])

    def test_new_rows_reach_the_back_test_on_the_side_of_their_hash(self):
        nightly.grow_corpus(self.corpus, [self.row(f"сделай {word}", "VOLUME") for word in ("потише", "погромче", "тише", "громче", "тишина", "шумнее")])
        rows, manifest = backtest.load_rows(self.corpus, self.grammar)
        new = [r for r in rows if r.text.startswith("сделай ")]
        self.assertEqual(len(new), 6)
        for row in new:
            self.assertEqual(row.split, backtest.split_of(row.text, backtest.holdout_percent(self.grammar)))
            self.assertEqual(row.source, "real")
        self.assertIn(nightly.CORPUS_FILE, [m["file"] for m in manifest])
        again, _ = backtest.load_rows(self.corpus, self.grammar)
        self.assertEqual({r.id: r.split for r in rows}, {r.id: r.split for r in again})

    def test_existing_row_ids_do_not_move_when_rows_are_added(self):
        before = {r.id for r in backtest.load_rows(self.corpus, self.grammar)[0]}
        nightly.grow_corpus(self.corpus, [self.row("сделай потише", "VOLUME")])
        after = {r.id for r in backtest.load_rows(self.corpus, self.grammar)[0]}
        self.assertTrue(before < after)

    def test_two_overlapping_runs_add_a_phrase_once(self):
        barrier = threading.Barrier(2)
        results = []

        def run():
            barrier.wait()
            results.append(nightly.grow_corpus(self.corpus, [self.row("сделай потише", "VOLUME")]))

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(r["new"] for r in results), [0, 1])
        self.assertEqual([r["text"] for r in self.nightly_rows()], ["сделай потише"])

    def test_a_dry_run_appends_nothing(self):
        snapshot = tree(self.corpus)
        result = nightly.grow_corpus(self.corpus, [self.row("сделай потише", "VOLUME")], dry_run=True)
        self.assertEqual(result, {"candidates": 1, "new": 1, "appended": 0, "duplicates": 0})
        self.assertEqual(tree(self.corpus), snapshot)

    def test_a_missing_corpus_directory_is_created_private(self):
        target = Path(self.tmp.name) / "fresh" / "corpus"
        nightly.grow_corpus(target, [self.row("сделай потише", "VOLUME")])
        self.assertEqual(mode(target), 0o700)
        self.assertEqual(mode(target / nightly.CORPUS_FILE), 0o600)


class OwnerFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.inbox = Path(self.tmp.name) / nightly.OWNER_LABELS_FILE
        self.allowed = {"LIGHT_CMD", "VOLUME"}

    def read(self, heard=None):
        return nightly.read_owner_labels(self.inbox, self.allowed, heard or {}, NOW)

    def test_good_lines_become_owner_rows_and_bad_ones_are_reported_not_dropped(self):
        self.inbox.write_bytes(
            jsonl({"text": "включи свет в ванной", "truth": "LIGHT_CMD", "ts": 7}).encode() + b"{not json\n" +
            jsonl({"text": "сделай тише", "truth": "NO_SUCH_LABEL"}).encode() + jsonl({"text": "", "truth": "VOLUME"}).encode() +
            b"\xff\xfe\n" + jsonl({"text": "сделай погромче", "truth": "VOLUME"}).encode())
        before = self.inbox.read_bytes()
        rows, rejected = self.read()
        self.assertEqual([(r.text, r.truth, r.labeller, r.ts) for r in rows],
                         [("включи свет в ванной", "LIGHT_CMD", "owner", 7.0), ("сделай погромче", "VOLUME", "owner", NOW)])
        self.assertEqual(rejected, [f"{nightly.OWNER_LABELS_FILE}:2: not a JSON line", f"{nightly.OWNER_LABELS_FILE}:3: unknown label",
                                    f"{nightly.OWNER_LABELS_FILE}:4: needs a text and a truth", f"{nightly.OWNER_LABELS_FILE}:5: not a JSON line"])
        self.assertEqual(self.inbox.read_bytes(), before)           # the inbox is never rewritten
        self.assertNotIn("сделай тише", " ".join(rejected))

    def test_the_layer_and_time_come_from_what_was_heard(self):
        self.inbox.write_text(jsonl({"text": "Выключи свет!", "truth": "LIGHT_CMD"}), encoding="utf-8")
        heard = {backtest.split_key("выключи свет"): nightly.Heard("выключи свет", 123.0, "fast")}
        rows, _ = self.read(heard)
        self.assertEqual((rows[0].layer, rows[0].ts), ("L1", 123.0))

    def test_label_appends_privately_and_refuses_an_unknown_label(self):
        nightly.append_owner_label(self.inbox, " включи свет ", "LIGHT_CMD", self.allowed)
        nightly.append_owner_label(self.inbox, "сделай тише", "VOLUME", self.allowed)
        self.assertEqual(mode(self.inbox), 0o600)
        self.assertEqual([json.loads(line)["text"] for line in self.inbox.read_text(encoding="utf-8").splitlines()], ["включи свет", "сделай тише"])
        with self.assertRaises(nightly.NightlyError):
            nightly.append_owner_label(self.inbox, "что-то", "LIGHT_CMDD", self.allowed)

    def test_jev_labels_count_only_when_confident_and_a_real_intent(self):
        grammar = load(BUNDLE)
        labels = next(i.labels[0] for i in grammar.intents if i.id == "alarm.set")
        rows = nightly.jev_candidates(grammar, [("поставь будильник", ("alarm.set", 0.95)), ("слабый", ("alarm.set", 0.5)),
                                                ("чужой", ("_none", 0.99)), ("выдуманный", ("no.such.intent", 0.99))], NOW)
        self.assertEqual([(r.text, r.truth, r.labeller) for r in rows], [("поставь будильник", labels, "jev")])
        self.assertIn("alarm.set", rows[0].note)

    def test_a_context_only_intent_is_never_a_standalone_label(self):
        """«да» is `repair.accept` only inside an open question; as a corpus row of its own it could never be classified that way."""
        grammar = load(BUNDLE)
        contextual = [i for i in grammar.intents if i.contexts]
        self.assertTrue(contextual)
        answers = [(f"ответ {n}", (i.id, 0.99)) for n, i in enumerate(contextual)]
        self.assertEqual(nightly.jev_candidates(grammar, answers, NOW), [])
        labels = {label for i in contextual for label in i.labels}
        self.assertTrue(labels)
        self.assertFalse(labels & nightly.allowed_truths(grammar, set()))
        self.assertTrue(nightly.allowed_truths(grammar, set()))


class PrepareTests(StoreFixture):
    def test_writes_private_files_and_an_aggregate_only_summary(self):
        out = self.prepare()
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["backtest.json", "brief.md", "clusters.json", "summary.json", "today.jsonl"])
        for name in ("backtest.json", "brief.md", "clusters.json", "summary.json", "today.jsonl"):
            self.assertEqual(mode(out / name), 0o600, name)
        self.assertEqual(mode(out), 0o700)
        summary = self.summary(out)
        self.assertEqual(summary["utterances"], 7)
        self.assertEqual(summary["by_layer"], {"fast": 1, "brain": 4, "none": 2})
        self.assertIn("real/all", summary["backtest"])
        self.assertTrue(summary["gate"]["ok"])
        self.assertNotIn("динозавр", json.dumps(summary, ensure_ascii=False))     # aggregates only
        self.assertIn("динозавр", (out / "clusters.json").read_text(encoding="utf-8"))

    def test_held_out_phrases_never_reach_the_clusters_or_the_working_files(self):
        held, dev = "включи режим сон на кухне", "включи режим сон в спальне"
        holdout = backtest.holdout_percent(load(BUNDLE))
        self.assertEqual(backtest.split_of(held, holdout), "holdout")
        self.assertEqual(backtest.split_of(dev, holdout), "dev")
        out = self.prepare()
        for name in ("clusters.json", "today.jsonl", "brief.md"):
            self.assertNotIn(held, (out / name).read_text(encoding="utf-8"), name)
        self.assertIn(dev, (out / "today.jsonl").read_text(encoding="utf-8"))
        summary = self.summary(out)
        self.assertGreaterEqual(summary["held_out_withheld"], 2)       # that phrase and «Алиса»
        self.assertEqual(summary["utterances"], 7)                      # still counted in the aggregates

    def test_a_decision_the_runtime_would_not_execute_is_not_counted_as_answered(self):
        db = self.dir / "route.sqlite3"
        make_store(db, [
            ("voice", NOW - 100, [("user", "speech", "включи свет в студии"), ("assistant", "reply", "…")]),
            ("voice", NOW - 200, [("user", "speech", "который час"), ("assistant", "reply", "…")]),
        ])
        self.write_corpus()
        out = nightly.prepare(grammar_path=self.bundle({}), corpus=str(self.corpus), db=str(db), hours=1.0, out_root=str(self.out), now=NOW)
        summary = self.summary(out)
        self.assertEqual(summary["grammar_would_answer_now"], 1)       # the time question; the light command is classified but not executable

    def test_the_goal_is_reported_with_its_gap_and_never_gated(self):
        out = self.prepare(gate={"goal": {"split": "real/all", "coverage": 0.9, "correct": 0.9}})
        summary = self.summary(out)
        self.assertTrue(summary["gate"]["ok"])
        goal = summary["goal"]
        self.assertEqual((goal["split"], goal["target"]), ("real/all", {"coverage": 0.9, "correct": 0.9}))
        self.assertEqual(set(goal["all"]["coverage"]), {"value", "ci95", "gap", "met"})

    def test_the_report_is_the_phase_two_schema_with_its_split_and_gate(self):
        report = json.loads((self.prepare() / "backtest.json").read_text(encoding="utf-8"))
        self.assertEqual(report["schema"], backtest.REPORT_SCHEMA)
        self.assertEqual(report["split"]["version"], backtest.SPLIT_VERSION)
        self.assertEqual(report["route"], "combined")
        self.assertIn("gate", report)

    def test_a_green_night_promotes_the_baseline_a_red_one_does_not(self):
        self.prepare()
        self.assertEqual(mode(self.reports / "latest.json"), 0o600)
        green = (self.reports / "latest.json").read_bytes()
        out = self.prepare(gate=UNREACHABLE_FLOOR)
        self.assertFalse(self.summary(out)["gate"]["ok"])
        self.assertEqual((self.reports / "latest.json").read_bytes(), green)

    def test_the_labeller_is_offered_no_context_only_intent(self):
        offered = {}
        self.prepare(label=lambda text, intents: offered.update(intents) or ("alarm.set", 0.97))
        self.assertIn("alarm.set", offered)
        self.assertFalse([name for name in offered if name.startswith("repair.")], offered.keys())

    def test_clusters_carry_the_jev_label_when_a_labeller_is_given(self):
        seen = []

        def label(text, intents):
            seen.append((text, len(intents)))
            return "alarm.set", 0.97
        out = self.prepare(label=label)
        clusters = json.loads((out / "clusters.json").read_text(encoding="utf-8"))
        self.assertTrue(all(c["nearest_intent"] == "alarm.set" for c in clusters))
        self.assertGreater(seen[0][1], 40)        # the whole intent list was offered
        summary = self.summary(out)
        self.assertEqual(summary["jev"], {"asked": len(clusters), "failed": 0, "labelled": len(clusters), "skipped": None})

    def test_jev_rows_join_the_corpus_marked_with_their_labeller(self):
        self.prepare(label=lambda text, intents: ("alarm.set", 0.97))
        rows = [json.loads(line) for line in (self.corpus / nightly.CORPUS_FILE).read_text(encoding="utf-8").splitlines()]
        self.assertTrue(rows)
        self.assertEqual({r["labeller"] for r in rows}, {"jev"})
        self.assertEqual(mode(self.corpus / nightly.CORPUS_FILE), 0o600)

    def test_jev_failures_are_counted_not_swallowed(self):
        def failing(text, intents):
            raise nightly.NightlyError("jev call failed: TimeoutError")
        out = self.prepare(label=failing)
        jev = self.summary(out)["jev"]
        self.assertGreater(jev["asked"], 0)
        self.assertEqual((jev["failed"], jev["labelled"]), (jev["asked"], 0))
        self.assertFalse((self.corpus / nightly.CORPUS_FILE).exists())

    def test_owner_feedback_grows_the_corpus_and_the_summary_says_how(self):
        self.write_corpus()
        inbox = self.out / nightly.OWNER_LABELS_FILE
        self.out.mkdir()
        inbox.write_text(jsonl({"text": "Выключи свет в студии.", "truth": "LIGHT_CMD"}, {"text": "включи свет", "truth": "LIGHT_CMD"}) + "{broken\n",
                         encoding="utf-8")
        out = self.prepare()
        growth = self.summary(out)["corpus_growth"]
        self.assertEqual((growth["new"], growth["appended"], growth["duplicates"]), (1, 1, 1))     # «включи свет» is in the corpus already
        self.assertEqual(growth["rejected"], [f"{nightly.OWNER_LABELS_FILE}:3: not a JSON line"])
        row = json.loads((self.corpus / nightly.CORPUS_FILE).read_text(encoding="utf-8"))
        self.assertEqual((row["text"], row["labeller"], row["layer"], row["ts"]), ("Выключи свет в студии.", "owner", "L1", NOW - 100))
        self.assertIn("real-voice-nightly-labelled.jsonl", {m["file"] for m in json.loads((out / "backtest.json").read_text(encoding="utf-8"))["corpus"]})

    def test_a_corrupt_corpus_stops_the_night_loudly(self):
        self.write_corpus()
        with open(self.corpus / "real-voice-2026-11-labelled.jsonl", "a", encoding="utf-8") as handle:
            handle.write("{broken\n")
        with self.assertRaises(nightly.NightlyError):
            self.prepare()

    def test_the_live_brief_names_the_rules(self):
        brief = (self.prepare() / "brief.md").read_text(encoding="utf-8")
        for needle in ("(live)", "rig worktree create intent-nightly-", "--gate", "review diff --staged --task 107 --commit --detach", "review wait",
                       "gh ship <PR> --task '#107'", "from the repository root", "ONE short", "handoff.json", "Nightly intent loop reports",
                       "never lower a gate floor", "never look at held-out texts", "`gate.holdout`", "`gate.safety`", "90 % goal",
                       "review brainstorm", "MORNING", "`require-review-before-commit` hatch is never an option", "hatch-requests.md",
                       "Refs #107", "Propose nothing"):
            self.assertIn(needle, brief)
        self.assertNotIn("DRY RUN", brief)
        self.assertNotIn("RED", brief)

    def test_a_second_night_compares_with_the_first(self):
        first = self.prepare()
        second = self.prepare()
        self.assertEqual(self.summary(second)["regressed"], 0)
        self.assertEqual(first, second)           # one directory per day

    def test_the_baseline_comparison_is_in_the_summary(self):
        summary = self.summary(self.prepare())
        self.assertEqual((summary["regressed"], summary["lost_rows"], summary["relabelled"]), (0, {}, 0))
        self.assertEqual(summary["legacy_floor"], None)       # the shipped bundle's real/holdout split is too small here to report

    def test_a_lost_or_relabelled_row_blocks_the_night(self):
        self.prepare()
        path = self.corpus / "real-voice-2026-11-labelled.jsonl"
        path.write_text(jsonl({"text": "который час", "truth": "KNOWLEDGE", "ts": 1}, {"text": "включи свет", "truth": "LIGHT_CMD", "ts": 3}),
                        encoding="utf-8")                      # «который час» relabelled, «расскажи про динозавров» deleted
        summary = self.summary(self.prepare())
        self.assertEqual((summary["relabelled"], summary["lost_rows"]), (1, {"real": 1}))
        failures = nightly.gate_failures(summary)
        self.assertTrue(any("gone from the corpus" in f for f in failures), failures)
        self.assertTrue(any("changed their label" in f for f in failures), failures)


class NoBaselineTests(StoreFixture):
    """The nightly run measures against a baseline the owner made with `backtest --init-baseline`; it never makes one."""
    with_baseline = False

    def test_a_first_night_without_a_baseline_is_blocked_and_promotes_nothing(self):
        out = self.prepare()
        self.assertTrue(any("no baseline" in f for f in nightly.gate_failures(self.summary(out))))
        self.assertIn("(gate blocked)", (out / "brief.md").read_text(encoding="utf-8"))
        self.assertIn("`--init-baseline` is the owner's act", (out / "brief.md").read_text(encoding="utf-8"))
        self.assertFalse((self.reports / "latest.json").exists())


class BlockedGateTests(StoreFixture):
    """A red gate, or a failed emergency phrase, is never landed over: the night brainstorms, and no PR or ship is briefed."""
    LANDING = ("review diff --staged", "gh ship <PR>", "git push -u", "gh pr create` titled", "git commit` one", "Merge it the normal way")

    def assert_blocked(self, out, *failure_needles):
        brief = (out / "brief.md").read_text(encoding="utf-8")
        self.assertIn("(gate blocked)", brief)
        self.assertIn("review brainstorm", brief)
        self.assertIn("Do NOT run `review diff`, `git commit`, `git push`, `gh pr create` or `gh ship`", brief)
        for landing in self.LANDING:
            self.assertNotIn(landing, brief)
        failures = nightly.gate_failures(self.summary(out))
        self.assertTrue(failures)
        for needle in failure_needles:
            self.assertTrue(any(needle in line for line in failures), (needle, failures))
            self.assertIn(needle, brief)
        return brief

    def test_a_failed_gate_gets_the_brainstorm_brief_without_landing(self):
        out = self.prepare(gate=UNREACHABLE_FLOOR)
        self.assert_blocked(out, "real/all: coverage")

    def test_a_safety_failure_gets_the_brainstorm_brief_without_landing(self):
        out = self.prepare(gate={"safety": {"intent": "refuse.emergency", "phrases": ["Вызови скорую", "расскажи анекдот про кота"]}})
        brief = self.assert_blocked(out, "SAFETY: «расскажи анекдот про кота»")
        self.assertNotIn("SAFETY: «Вызови скорую»", brief)         # the phrase that is read correctly is not a failure

    def test_a_night_without_a_corpus_is_blocked_not_green(self):
        self.corpus.mkdir()
        out = self.prepare()
        self.assertIsNone(self.summary(out)["gate"])
        self.assert_blocked(out, "no back-test report")

    def test_a_green_gate_keeps_the_landing_path(self):
        brief = (self.prepare() / "brief.md").read_text(encoding="utf-8")
        self.assertIn("gh ship <PR> --task '#107'", brief)
        self.assertNotIn("gate blocked", brief)


class NightlyCommandTests(StoreFixture):
    """`nightly run` end to end with a fake launcher, a recording Telegram and a recording harness."""

    def run_cli(self, *flags, gate=None, env=None, top=()):
        self.write_corpus() if not self.corpus.exists() else None
        record = self.dir / "record"
        launcher = self.dir / "launcher"
        launcher.write_text(f'#!/bin/sh\ncp "$2" {record}\n', encoding="utf-8")
        launcher.chmod(0o755)
        argv = [*top, "--grammar", self.bundle({} if gate is None else gate), "nightly", "run", "--db", str(self.db), "--hours", "1000000",
                "--corpus", str(self.corpus), "--out", str(self.out), "--repo", str(self.dir), "--launcher", str(launcher), *flags]
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env or {}), mock.patch.object(nightly, "tg_note") as tg, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(argv)
        return SimpleNamespace(code=code, stdout=stdout.getvalue(), stderr=stderr.getvalue(), tg=tg, record=record)

    def test_status_before_any_run(self):
        self.assertEqual(cli.main(["nightly", "status", "--out", str(self.out)]), 1)

    def test_run_launches_the_live_brief(self):
        result = self.run_cli()
        self.assertEqual(result.code, 0, result.stderr)
        brief = result.record.read_text(encoding="utf-8")
        self.assertIn("(live)", brief)
        self.assertIn("gh ship <PR> --task '#107'", brief)
        result.tg.assert_not_called()

    def test_a_blocked_gate_launches_the_brainstorm_brief_and_tells_the_owner(self):
        result = self.run_cli(gate=UNREACHABLE_FLOOR)
        self.assertEqual(result.code, 0, result.stderr)
        brief = result.record.read_text(encoding="utf-8")
        self.assertIn("(gate blocked)", brief)
        self.assertNotIn("gh ship <PR>", brief)
        self.assertIn("gate BLOCKED", result.stderr)
        result.tg.assert_called_once()
        self.assertIn("гейт красный", result.tg.call_args.args[0])
        self.assertNotIn("безопасность", result.tg.call_args.args[0])

    def test_a_safety_failure_is_called_out_to_the_owner(self):
        result = self.run_cli(gate={"safety": {"intent": "refuse.emergency", "phrases": ["расскажи анекдот про кота"]}})
        self.assertIn("(gate blocked)", result.record.read_text(encoding="utf-8"))
        self.assertIn("безопасность", result.tg.call_args.args[0])

    def test_dry_run_changes_nothing_and_launches_nothing(self):
        self.write_corpus()
        self.out.mkdir()
        (self.out / nightly.OWNER_LABELS_FILE).write_text(jsonl({"text": "включи ночник", "truth": "LIGHT_CMD"}), encoding="utf-8")
        corpus_before, inbox_before, reports_before = tree(self.corpus), (self.out / nightly.OWNER_LABELS_FILE).read_bytes(), tree(self.reports)
        result = self.run_cli("--dry-run", gate=UNREACHABLE_FLOOR)           # even a red night: nothing leaves the machine
        self.assertEqual(result.code, 0, result.stderr)
        self.assertFalse(result.record.exists())                              # the session was not launched
        result.tg.assert_not_called()
        self.assertEqual(tree(self.corpus), corpus_before)                    # no row appended
        self.assertEqual((self.out / nightly.OWNER_LABELS_FILE).read_bytes(), inbox_before)
        self.assertEqual(tree(self.reports), reports_before)                  # the baseline is not promoted, no lock, no archive
        day = next(p for p in self.out.iterdir() if p.is_dir())
        summary = self.summary(day)
        self.assertEqual((summary["dry_run"], summary["corpus_growth"]["new"], summary["corpus_growth"]["appended"]), (True, 1, 0))
        self.assertTrue((day / "brief.md").exists())                          # the brief it would have launched is there to read
        self.assertIn("dry run: the agent session is not launched", result.stdout)

    def test_dry_run_with_jev_makes_no_call(self):
        calls = []
        with mock.patch.object(nightly, "jev_labeller", return_value=lambda text, intents: calls.append(text) or ("alarm.set", 0.99)):
            result = self.run_cli("--dry-run", "--jev", env={"VERCEL_JEV_KEY": "k"})
        self.assertEqual((result.code, calls), (0, []))
        day = next(p for p in self.out.iterdir() if p.is_dir())
        self.assertEqual(self.summary(day)["jev"]["skipped"], "dry run")

    def test_jev_without_a_key_fails_loudly_and_prepares_nothing(self):
        for flags in (("--jev",), ("--jev", "--dry-run")):
            with self.subTest(flags=flags):
                result = self.run_cli(*flags, env={"VERCEL_JEV_KEY": ""})
                self.assertEqual(result.code, 2)
                self.assertIn("--jev needs VERCEL_JEV_KEY", result.stderr)
                self.assertFalse(self.out.exists())
                self.assertEqual(result.tg.called, "--dry-run" not in flags)        # the real run also says it on Telegram
                self.assertFalse(result.record.exists())

    def test_jev_without_the_module_fails_loudly(self):
        with mock.patch.dict(sys.modules, {"jev": None}):
            result = self.run_cli("--jev", env={"VERCEL_JEV_KEY": "k"})
        self.assertEqual(result.code, 2)
        self.assertIn("Jev module", result.stderr)
        self.assertFalse(result.record.exists())

    def test_jev_failures_and_unused_feedback_are_loud(self):
        def failing(text, intents):
            raise nightly.NightlyError("jev call failed: TimeoutError")
        self.write_corpus()
        self.out.mkdir()
        (self.out / nightly.OWNER_LABELS_FILE).write_text("{broken\n", encoding="utf-8")
        with mock.patch.object(nightly, "jev_labeller", return_value=failing):
            result = self.run_cli("--jev", env={"VERCEL_JEV_KEY": "k"})
        self.assertEqual(result.code, 0)
        self.assertIn("WARNING: Jev failed on", result.stderr)
        self.assertIn("WARNING: feedback lines not used", result.stderr)
        result.tg.assert_called_once()

    def test_the_session_is_launched_without_the_key(self):
        with mock.patch.object(nightly, "launch", wraps=nightly.launch) as launch, \
                mock.patch.object(nightly, "jev_labeller", return_value=lambda text, intents: ("alarm.set", 0.99)):
            result = self.run_cli("--jev", env={"VERCEL_JEV_KEY": "secret-value"})
        self.assertEqual(result.code, 0, result.stderr)
        launch.assert_called_once()
        self.assertNotIn("secret-value", result.record.read_text(encoding="utf-8"))

    def test_label_queues_an_owner_row_and_refuses_an_unknown_label(self):
        self.write_corpus()
        base = ["--grammar", self.bundle({}), "nightly", "label"]
        flags = ["--corpus", str(self.corpus), "--out", str(self.out)]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main([*base, "включи ночник", "LIGHT_CMD", *flags]), 0)
        self.assertEqual(json.loads((self.out / nightly.OWNER_LABELS_FILE).read_text(encoding="utf-8"))["truth"], "LIGHT_CMD")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.main([*base, "включи ночник", "NOT_A_LABEL", *flags]), 2)
            self.assertEqual(cli.main([*base, "только текст", *flags]), 2)
        self.assertIn("unknown label", err.getvalue())


class LaunchTests(unittest.TestCase):
    def test_session_env_drops_keys_and_tokens(self):
        env = {"PATH": "/bin", "HOME": "/h", "VERCEL_JEV_KEY": "k", "CEREBRAS_KEY": "c", "GH_TOKEN": "t", "AISIS_ADAPTER_TOKEN": "a", "DB_PASSWORD": "p"}
        self.assertEqual(nightly.session_env(env), {"PATH": "/bin", "HOME": "/h"})

    def test_launch_calls_the_detached_launcher_with_the_brief(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name) / "2026-11-17"
        out.mkdir()
        (out / "brief.md").write_text("brief", encoding="utf-8")
        record = Path(tmp.name) / "record"
        launcher = Path(tmp.name) / "launcher"
        launcher.write_text(f'#!/bin/sh\necho "$@" > {record}\nenv > {record}.env\n', encoding="utf-8")
        launcher.chmod(0o755)
        with mock.patch.dict(os.environ, {"VERCEL_JEV_KEY": "secret-value"}):
            done = nightly.launch(out, tmp.name, str(launcher))
        self.assertEqual(done.returncode, 0)
        self.assertEqual(record.read_text().split(), ["intent-nightly-2026-11-17", str(out / "brief.md"), tmp.name])
        self.assertNotIn("secret-value", Path(str(record) + ".env").read_text())

    def test_last_run(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.assertIsNone(nightly.last_run(tmp.name))
        day = Path(tmp.name) / "2026-11-16"
        day.mkdir()
        (day / "summary.json").write_text('{"utterances": 3}', encoding="utf-8")
        (day / "handoff.json").write_text('{"pr": 12}', encoding="utf-8")
        self.assertEqual(nightly.last_run(tmp.name), {"day": "2026-11-16", "summary": {"utterances": 3}, "handoff": {"pr": 12}})


class MorningTests(unittest.TestCase):
    """The night only writes hatch-requests.md; the morning asks the owner once, through the harness, and never grants anything."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for day, request in (("2026-11-15", "gate X refuses a correct state: command and output"), ("2026-11-16", None)):
            (self.root / day).mkdir()
            if request:
                (self.root / day / nightly.HATCH_FILE).write_text(request, encoding="utf-8")

    def test_a_request_is_asked_once_and_marked(self):
        asked = []

        def ask(title, body):
            asked.append((title, body))
            return subprocess.CompletedProcess([], 0)
        self.assertEqual(nightly.collect_hatch_requests(str(self.root), ask), {"asked": ["2026-11-15"], "failed": []})
        self.assertEqual(asked, [("Intent nightly 2026-11-15: gate hatch request, owner decision", "gate X refuses a correct state: command and output")])
        self.assertEqual(mode(self.root / "2026-11-15" / nightly.HATCH_ASKED), 0o600)
        self.assertEqual(nightly.collect_hatch_requests(str(self.root), ask), {"asked": [], "failed": []})
        self.assertEqual(len(asked), 1)                                             # tomorrow does not ask again

    def test_a_refused_ask_is_reported_and_retried_next_morning(self):
        refusing = lambda title, body: subprocess.CompletedProcess([], 1)           # noqa: E731
        self.assertEqual(nightly.collect_hatch_requests(str(self.root), refusing), {"asked": [], "failed": ["2026-11-15"]})
        self.assertFalse((self.root / "2026-11-15" / nightly.HATCH_ASKED).exists())

        def missing(title, body):
            raise FileNotFoundError("harness")
        self.assertEqual(nightly.collect_hatch_requests(str(self.root), missing)["failed"], ["2026-11-15"])
        good = lambda title, body: subprocess.CompletedProcess([], 0)               # noqa: E731
        self.assertEqual(nightly.collect_hatch_requests(str(self.root), good)["asked"], ["2026-11-15"])

    def test_the_command_reports_a_failed_ask_loudly(self):
        stderr = io.StringIO()
        with mock.patch.object(nightly, "harness_task", return_value=subprocess.CompletedProcess([], 1)), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            code = cli.main(["nightly", "morning", "--out", str(self.root)])
        self.assertEqual(code, 1)
        self.assertIn("2026-11-15", stderr.getvalue())

    def test_the_command_asks_through_the_harness_cli(self):
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["nightly", "morning", "--out", str(self.root)]), 0)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], ["harness", "tasks", "new"])
        self.assertIn("--title", argv)


class UnitTests(unittest.TestCase):
    def read(self, name):
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.optionxform = str
        parser.read(SYSTEMD / name)
        return parser

    def active_lines(self, name):
        return [line for line in (SYSTEMD / name).read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")]

    def test_timer_runs_nightly_and_catches_up(self):
        timer = self.read("aisis-intent-nightly.timer")
        self.assertEqual(timer["Timer"]["OnCalendar"], "*-*-* 03:30:00")
        self.assertEqual(timer["Timer"]["Persistent"], "true")
        self.assertEqual(timer["Install"]["WantedBy"], "timers.target")

    def test_service_runs_the_cli_from_the_station_code_directory(self):
        service = self.read("aisis-intent-nightly.service")["Service"]
        self.assertEqual(service["Type"], "oneshot")
        self.assertTrue(service["ExecStart"].endswith("-m intent_grammar nightly run $AISIS_NIGHTLY_ARGS"))
        self.assertTrue(service["WorkingDirectory"].endswith("/services/yandex-station/code"))
        self.assertTrue(service["EnvironmentFile"].startswith("-"))     # optional: no file, no Jev, still runs
        self.assertEqual(service["KillMode"], "process")                # the detached omp session outlives the oneshot

    def test_jev_is_off_by_default_and_documented_as_opt_in(self):
        self.assertFalse([line for line in self.active_lines("aisis-intent-nightly.service") if "--jev" in line or "AISIS_NIGHTLY_ARGS=" in line])
        text = (SYSTEMD / "aisis-intent-nightly.service").read_text(encoding="utf-8")
        for needle in ("OPT-IN", "default OFF", "AISIS_NIGHTLY_ARGS=--jev", "VERCEL_JEV_KEY=", "Vercel", "intent-nightly.env", "stops the run loudly"):
            self.assertIn(needle, text)

    def test_the_morning_timer_asks_after_the_night(self):
        timer = self.read("aisis-intent-morning.timer")
        self.assertEqual(timer["Timer"]["OnCalendar"], "*-*-* 08:30:00")
        self.assertEqual(timer["Timer"]["Persistent"], "true")
        service = self.read("aisis-intent-morning.service")["Service"]
        self.assertTrue(service["ExecStart"].endswith("-m intent_grammar nightly morning"))
        self.assertFalse([line for line in self.active_lines("aisis-intent-morning.service") if "--jev" in line])

    def test_no_unit_names_a_local_path(self):
        for name in ("aisis-intent-nightly.service", "aisis-intent-morning.service"):
            self.assertIsNone(re.search(r"/home/|/Users/", (SYSTEMD / name).read_text(encoding="utf-8")), name)


if __name__ == "__main__":
    unittest.main()
