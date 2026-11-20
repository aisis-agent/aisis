import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from asr_eval import latency, tier_c  # noqa: E402


def caption(text: str) -> bool:
    return "продолжение следует" in text.lower()


class ConsensusTests(unittest.TestCase):
    def test_two_agreeing_voters_make_speech(self):
        kind, medoid = tier_c.consensus([["включи", "свет"], ["включи", "свет"], []])
        self.assertEqual(kind, "speech")
        self.assertEqual(medoid, ["включи", "свет"])

    def test_one_voter_is_not_speech(self):
        self.assertEqual(tier_c.consensus([["привет"], [], []]), ("nonspeech", None))
        self.assertEqual(tier_c.consensus([[], []]), ("nonspeech", None))

    def test_disagreeing_voters_are_uncertain(self):
        self.assertEqual(tier_c.consensus([["включи", "свет", "на", "кухне"], ["сколько", "времени", "сейчас", "там"]]), ("uncertain", None))

    def test_captions_do_not_vote(self):
        self.assertEqual(tier_c.voiced_words("Продолжение следует...", caption), [])
        self.assertEqual(tier_c.voiced_words("", caption), [])
        self.assertEqual(tier_c.voiced_words("Включи, свет!", caption), ["включи", "свет"])


class MetricTests(unittest.TestCase):
    def test_wer(self):
        self.assertEqual(tier_c.wer(["a", "b"], ["a", "b"]), 0.0)
        self.assertEqual(tier_c.wer(["a", "b"], ["a"]), 0.5)
        self.assertEqual(tier_c.wer([], []), 0.0)
        self.assertEqual(tier_c.wer([], ["x", "y"]), 2.0)

    def test_norm_and_percentile(self):
        self.assertEqual(tier_c.norm("Ёлка, Ёж!"), ["елка", "еж"])
        self.assertIsNone(tier_c.percentile([], 0.95))
        self.assertEqual(tier_c.percentile(list(range(1, 101)), 0.5), 51)


class RoutedFalseAcceptTests(unittest.TestCase):
    """`routed_false_accepts` with the production router replaced: text 'да' makes the router act, any other text routes to nothing."""

    def fakes(self):
        class Row:
            def __init__(self, clip, text, truth, source, split, quasar, quasar_via=None):
                self.text, self.truth = text, truth
        intent_route = types.SimpleNamespace(load_grammar=lambda: "grammar", measured_router=lambda grammar: "router",
                                             measured_route=lambda router, mode: (lambda row: "act" if row.text == "да" else None))
        backtest = types.SimpleNamespace(Row=Row, classify=lambda row, decision, grammar: "false_trigger" if decision else "no_action")
        grammar_pkg = types.SimpleNamespace(backtest=backtest)
        return {"intent_route": intent_route, "intent_grammar": grammar_pkg, "intent_grammar.backtest": backtest}

    def test_counts_per_status_and_engine(self):
        meta = {"g1": {"status": "gate"}, "b1": {"status": "blacklist"}, "b2": {"status": "blacklist"}, "s1": {"status": "sent"}}
        results = {"e": {"g1": {"text": "да", "err": None}, "b1": {"text": "", "err": None}, "b2": {"text": "ну", "err": None},
                         "s1": {"text": "да", "err": None}}}
        with mock.patch.dict(sys.modules, self.fakes()), mock.patch("asr_eval.tier_b.replay_texts", return_value={}):
            lines = tier_c.routed_false_accepts(Path("/nonexistent"), ["e"], results, meta, None)
        self.assertIn("e | blacklist | 2 | 1 | 0 | 1 | 0", lines)     # one empty, none unavailable, one routed to nothing, no false trigger
        self.assertIn("e | gate | 1 | 0 | 0 | 0 | 1", lines)          # the text 'да' acts: a false trigger
        self.assertEqual(len([line for line in lines if line.startswith("e | ")]), 2)   # the `sent` clip is not a non-addressed clip

    def test_failed_and_missing_results_are_not_silence(self):
        """An engine that is down must not look like one that stayed quiet: no empty, no 'routed to nothing', no zero false triggers."""
        meta = {"g1": {"status": "gate"}, "g2": {"status": "gate"}, "g3": {"status": "gate"}}
        results = {"down": {"g1": {"text": "", "err": "ConnectionRefusedError"}, "g2": {"text": "", "err": None}}}   # g3 has no result at all
        with mock.patch.dict(sys.modules, self.fakes()), mock.patch("asr_eval.tier_b.replay_texts", return_value={}):
            lines = tier_c.routed_false_accepts(Path("/nonexistent"), ["down"], results, meta, None)
        self.assertIn("down | gate | 3 | 1 | 2 | 0 | 0", lines)   # g2 is a real empty answer; g1 (error) and g3 (missing) are unavailable


class LatencyTests(unittest.TestCase):
    def test_percentile_is_nearest_rank(self):
        self.assertEqual(latency.percentile(list(range(1, 101)), 0.95), 95)
        self.assertIsNone(latency.percentile([], 0.95))

    def test_summary_counts_only_clips_with_text(self):
        results = [{"text": "да", "latency_ms": 900}, {"text": "", "latency_ms": 100}, {"err": "no_final"}, {"text": "нет", "latency_ms": 1100}]
        summary = latency.summarize(results, "x")
        self.assertEqual((summary["clips"], summary["with_text"], summary["errors"]), (4, 2, 1))
        self.assertEqual(summary["p95_ms"], 1100)

    def test_select_filters_rows_and_limits(self):
        rows = [{"id": "a", "status": "sent"}, {"id": "b", "status": "gate"}, {"id": "c", "status": "sent"}]
        self.assertEqual([r["id"] for r in latency.select(rows, ["status=sent"], None)], ["a", "c"])
        self.assertEqual([r["id"] for r in latency.select(rows, ["status=sent"], 1)], ["a"])
        self.assertEqual(len(latency.select(rows, [], None)), 3)

    def test_isolated_env_never_points_at_live_state(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            env = latency.isolated_env("tcp://127.0.0.1:10412", Path(directory))
        self.assertEqual(env["AISIS_WYOMING_URI"], "tcp://127.0.0.1:10412")
        self.assertTrue(env["AISIS_DIALOGS_DB"].startswith(directory))
        self.assertTrue(env["AISIS_VOICE_ARCHIVE"].startswith(directory))


if __name__ == "__main__":
    unittest.main()
