import sys
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from asr_eval import tier_b  # noqa: E402


def corpus(n=8):
    return [{"text": f"фраза номер {i}", "expect": {"kind": "command", "domain": "light" if i % 2 else "climate"}} for i in range(n)]


class PlanTests(unittest.TestCase):
    def test_every_row_gets_two_distinct_voices(self):
        for index in range(64):
            first, second = tier_b.voice_plan(index)
            self.assertNotEqual(first[0], second[0])

    def test_voices_and_rates_are_balanced(self):
        plans = [clip for index in range(468) for clip in tier_b.voice_plan(index)]
        voices = Counter(voice for voice, _ in plans)
        rates = Counter(scale for _, scale in plans)
        self.assertLessEqual(max(voices.values()) - min(voices.values()), 40)
        self.assertLessEqual(abs(rates[1.0] - rates[1.1]), 40)

    def test_snr_of(self):
        self.assertIsNone(tier_b.snr_of("clean"))
        self.assertEqual(tier_b.snr_of("snr5"), 5.0)
        with self.assertRaises(ValueError):
            tier_b.snr_of("loud")

    def test_manifest_has_unique_ids_and_both_conditions(self):
        rows = tier_b.manifest_rows(corpus(8))
        self.assertEqual(len(rows), 8 * 2 * 2)
        self.assertEqual(len({row["id"] for row in rows}), len(rows))
        self.assertEqual({row["condition"] for row in rows}, {"clean", "snr5"})


class MetricTests(unittest.TestCase):
    def test_normalize_ignores_case_punctuation_and_yo(self):
        self.assertEqual(tier_b.normalize("Включи, СВЕТ на кухне!"), "включи свет на кухне")
        self.assertEqual(tier_b.normalize("Ёлка"), "елка")
        self.assertEqual(tier_b.normalize(None), "")

    def test_word_and_char_errors(self):
        self.assertEqual(tier_b.word_errors("включи свет", "включи"), (1, 2))
        self.assertEqual(tier_b.word_errors("включи свет", "включи свет"), (0, 2))
        self.assertEqual(tier_b.char_errors("кот", "кит"), (1, 3))

    def test_wilson_bounds(self):
        self.assertIsNone(tier_b.wilson(0, 0))
        low, high = tier_b.wilson(95, 100)
        self.assertLess(low, 0.95)
        self.assertGreater(high, 0.95)
        self.assertAlmostEqual(tier_b.wilson(100, 100)[1], 1.0)


class ScoringTests(unittest.TestCase):
    """The router is replaced: `judge` returns `correct` when the transcript equals the corpus text and `missed` otherwise."""

    def setUp(self):
        self.corpus = corpus(4)
        self.manifest = [{"id": f"c{i}-{cond}", "row": i, "voice": "denis", "condition": cond, "text": self.corpus[i]["text"]}
                         for i in range(4) for cond in ("clean", "snr5")]
        exact = {c["id"]: {"id": c["id"], "text": c["text"], "err": None} for c in self.manifest}
        broken = {c["id"]: {"id": c["id"], "text": "" if c["condition"] == "snr5" else c["text"], "err": None} for c in self.manifest}
        self.results = {"good": exact, "weak": broken}
        patcher = mock.patch.object(tier_b, "judge", side_effect=lambda _g, _d, record, _i, _l, text, _r: "correct" if text == record["text"] else "missed")
        patcher.start()
        self.addCleanup(patcher.stop)

    def score(self, **kwargs):
        return tier_b.score_clips(self.manifest, self.corpus, self.results, ["good", "weak"], None, None, {}, **kwargs)

    def test_summary_cells(self):
        table = tier_b.summarise(self.score())
        self.assertEqual(table["good"]["clean"]["n"], 4)
        self.assertEqual(table["good"]["snr5"]["intent_correct_abs"]["rate"], 1.0)
        self.assertEqual(table["weak"]["snr5"]["intent_correct_abs"]["rate"], 0.0)
        self.assertEqual(table["weak"]["snr5"]["empty"], 4)
        self.assertEqual(table["weak"]["clean"]["intent_preserved"]["rate"], 1.0)

    def test_bar_verdicts_compare_with_the_baseline(self):
        verdicts = tier_b.bar_verdicts(tier_b.summarise(self.score()), baseline="good")
        self.assertTrue(verdicts["good"]["clean_ge_95"])
        self.assertTrue(verdicts["weak"]["clean_ge_95"])
        self.assertFalse(verdicts["weak"]["snr5_ge_85"])
        self.assertFalse(verdicts["weak"]["snr5_not_worse_than_good"])

    @staticmethod
    def pairs(condition, engine_good, base_good, start=0):
        """Scored rows of one engine and the baseline over the same clips (all routable from the exact text)."""
        rows = []
        for offset, (mine, theirs) in enumerate(zip(engine_good, base_good)):
            clip = f"{condition}-{start + offset}"
            for engine, good in (("new", mine), ("old", theirs)):
                rows.append({"engine": engine, "id": clip, "condition": condition, "outcome": "correct" if good else "missed", "baseline_outcome": "correct"})
        return rows

    def test_sign_test_is_exact_and_two_sided(self):
        self.assertEqual(tier_b.sign_test_p(0, 0), 1.0)
        self.assertAlmostEqual(tier_b.sign_test_p(8, 0), 2 / 256)
        self.assertAlmostEqual(tier_b.sign_test_p(3, 0), 0.25)
        self.assertAlmostEqual(tier_b.sign_test_p(5, 5), 1.0)

    def test_relative_bar_needs_a_measurable_improvement_in_every_condition(self):
        wins, ties = [True] * 10 + [True] * 20, [False] * 10 + [True] * 20       # 10 clips only the new engine gets right
        rows = self.pairs("clean", wins, ties) + self.pairs("snr5", wins, ties)
        paired = tier_b.paired_comparisons(rows, ["new", "old"], "old")
        self.assertEqual(paired["new"]["clean"], {"better": 10, "worse": 0, "pairs": 30, "p": round(2 / 1024, 6)})
        self.assertNotIn("old", paired)
        self.assertTrue(tier_b.relative_bars(paired)["new"]["relative_bar"])

    def test_relative_bar_fails_on_a_tie_a_small_gain_or_one_condition(self):
        tie = self.pairs("clean", [True] * 20, [True] * 20) + self.pairs("snr5", [True] * 20, [True] * 20)
        self.assertFalse(tier_b.relative_bars(tier_b.paired_comparisons(tie, ["new", "old"], "old"))["new"]["relative_bar"])
        small = self.pairs("clean", [True] * 3 + [True] * 17, [False] * 3 + [True] * 17) + self.pairs("snr5", [True] * 3 + [True] * 17, [False] * 3 + [True] * 17)
        self.assertFalse(tier_b.relative_bars(tier_b.paired_comparisons(small, ["new", "old"], "old"))["new"]["relative_bar"])
        strong = [True] * 10 + [True] * 10
        lopsided = self.pairs("clean", strong, [False] * 10 + [True] * 10) + self.pairs("snr5", [False] * 10 + [True] * 10, strong)   # better clean, worse at 5 dB
        verdict = tier_b.relative_bars(tier_b.paired_comparisons(lopsided, ["new", "old"], "old"))["new"]
        self.assertEqual(verdict, {"clean_improves_on_baseline": True, "snr5_improves_on_baseline": False, "relative_bar": False})

    def test_rows_the_router_cannot_handle_from_the_exact_text_are_not_paired(self):
        rows = self.pairs("clean", [True] * 10, [False] * 10)
        for row in rows:
            row["baseline_outcome"] = "missed"
        self.assertEqual(tier_b.paired_comparisons(rows, ["new", "old"], "old"), {"new": {}})

    def test_an_interrupted_run_is_issued_no_bar(self):
        """One correct clip per condition is 100 % of what was done and proves nothing."""
        bars = {"good": {"clean_ge_95": True, "snr5_ge_85": True}, "weak": {"clean_ge_95": True, "snr5_ge_85": True}}
        held = tier_b.withhold_incomplete(bars, {"good": 0, "weak": 6}, baseline="good")
        self.assertEqual(held["good"], bars["good"])
        self.assertEqual(held["weak"], {"clean_ge_95": False, "snr5_ge_85": False, "incomplete": True})
        every = tier_b.withhold_incomplete(bars, {"good": 3, "weak": 0}, baseline="good")
        self.assertTrue(all(verdict["incomplete"] for verdict in every.values()))

    def test_weakest_domain_is_the_one_that_failed(self):
        domains = tier_b.weakest_domains(self.score(), "weak", "snr5", count=1)
        self.assertEqual(domains[0][1], 0.0)

    def test_per_phrase_file_carries_no_text(self):
        for row in tier_b.per_phrase_rows(self.score(), with_text=False):
            self.assertEqual(set(row), {"row", "voice", "condition", "engine", "domain", "outcome", "preserved", "wer"})
        self.assertIn("transcript", tier_b.per_phrase_rows(self.score(), with_text=True)[0])

    def test_rows_production_cannot_route_from_the_exact_text_do_not_count_against_the_recogniser(self):
        """Row 0 is a typo-style row: even the exact text is `missed`. The routable rate ignores it, the absolute rate cannot reach 100 %."""
        def judge(_g, _d, record, _i, _l, text, _r):
            return "missed" if record["text"] == self.corpus[0]["text"] else ("correct" if text == record["text"] else "missed")
        with mock.patch.object(tier_b, "judge", side_effect=judge):
            cell = tier_b.summarise(self.score())["good"]["clean"]
        self.assertEqual(cell["intent_correct_abs"]["rate"], 0.75)
        self.assertEqual((cell["routable_n"], cell["intent_correct_routable"]["rate"]), (3, 1.0))
        self.assertEqual(cell["text_baseline_abs"]["rate"], 0.75)

class QuasarSemanticsTests(unittest.TestCase):
    """`judge` with the real back-test classification and a stand-in router that answers like production does for a phrase the quasar
    container handles: an opaque decision with an observed tier (no label, no arguments)."""
    OFF = "выключи свет на кухне"
    ON = "включи свет на кухне"
    RECORD = {"text": OFF, "expect": {"kind": "fast", "tier": "execute", "domain": "light"}}

    def replay(self):
        off = tier_b.Replayed(True, "fast-execute", '{"device": {"kind": "off"}}')
        return {self.OFF: off, self.OFF + ".": off, self.ON: tier_b.Replayed(True, "fast-execute", '{"device": {"kind": "on"}}')}

    def judge(self, text):
        from types import SimpleNamespace
        grammar = SimpleNamespace(backtest={"unaddressed": {}, "brain_labels": []})
        decide = lambda row: SimpleNamespace(intent="quasar.handled", tier=row.quasar_tier, labels=(), parts=()) if row.quasar else None
        return tier_b.judge(grammar, decide, self.RECORD, 0, "t", text, self.replay())

    def test_the_exact_text_and_its_punctuation_are_correct(self):
        self.assertEqual(self.judge(self.OFF), "correct")
        self.assertEqual(self.judge(self.OFF + "."), "correct")

    def test_the_opposite_command_is_wrong_though_the_container_executes_it_too(self):
        self.assertEqual(self.judge(self.ON), "wrong")

    def test_a_phrase_the_container_does_not_handle_is_missed(self):
        self.assertEqual(self.judge("что-то другое"), "missed")


class FixtureChecks(unittest.TestCase):
    def test_a_corpus_that_changed_since_the_build_is_refused(self):
        manifest = [{"id": "c0", "row": 0, "text": "фраза номер 0"}, {"id": "c1", "row": 1, "text": "фраза номер 1"}]
        tier_b.check_fixtures(manifest, corpus(2))
        with self.assertRaises(ValueError):
            tier_b.check_fixtures(manifest, list(reversed(corpus(2))))     # reordered
        with self.assertRaises(ValueError):
            tier_b.check_fixtures(manifest, corpus(1))                     # a row removed

    def test_a_baseline_without_a_routable_clip_does_not_crash_the_bars(self):
        cell = lambda rate: {"intent_correct_routable": {"rate": rate}}
        verdicts = tier_b.bar_verdicts({"old": {"clean": cell(None)}, "new": {"clean": cell(0.9)}}, baseline="old")
        self.assertNotIn("clean_not_worse_than_old", verdicts["new"])
        self.assertFalse(verdicts["new"]["clean_ge_95"])



if __name__ == "__main__":
    unittest.main()
