"""The glue against the REAL stt-cli core (stt_cli.hypotheses), when the pinned wheel is installed (requirements-stt-lib.txt).

The other tests use fakes of the contract and need no core; this one is what catches the core changing under the glue. It needs no model
and starts no worker: only the pieces the glue imports, plus a worker that cannot start.
"""
import asyncio
import json
import unittest

try:
    from stt_cli.hypotheses import confusables, giga, model
except ImportError:
    confusables = giga = model = None

import hypothesis_store
import second_opinion


@unittest.skipIf(model is None, "stt-cli core (requirements-stt-lib.txt) is not installed in this environment")
class RealCore(unittest.TestCase):
    def test_the_pieces_the_glue_imports_exist(self):
        core = second_opinion.load_core()
        self.assertTrue(callable(core.GigaCtc))
        for name in ("decode", "score_alternatives", "start", "close"):
            self.assertTrue(asyncio.iscoroutinefunction(getattr(core.GigaCtc, name)), name)
        self.assertTrue(hasattr(core.GigaCtc, "alive"))

    def test_the_confusable_sets_know_the_case_that_started_this(self):
        known = confusables.load()
        self.assertTrue(known.groups_in("Включи свет."))
        self.assertIn("выключи свет", [c.text for c in known.candidates("включи свет")])
        self.assertEqual(known.groups_in("какая сегодня погода"), [])

    def test_the_wait_policy_on_the_real_sets(self):
        so = second_opinion.SecondOpinion({"AISIS_HYP_ENABLED": "1", "AISIS_HYP_PYTHON": "/nonexistent"})
        self.assertTrue(so.waits_for("Выключи свет."))
        self.assertFalse(so.waits_for("Какая сегодня погода?"))
        self.assertFalse(so.waits_for("включи свет и расскажи пожалуйста что нового в мире на сегодня"))

    def test_the_store_compares_with_the_cores_same_text(self):
        store = hypothesis_store.HypothesisStore()
        nbest = model.NBest.from_logprobs([("выключи свет", -0.1), ("включи свет", -2.7)], engine="gigaam-v3-ctc").as_dict()
        self.assertTrue(store.put(None, "Включи свет.", json.loads(json.dumps(nbest)), source="whisper", ms=90, now=0.0))
        self.assertIsNotNone(store.get("st1", "включи, свет", now=1.0))
        self.assertIsNone(store.get("st1", "выключи свет", now=1.0))
        self.assertTrue(store.stats()["available"])

    def test_a_worker_that_cannot_start_is_a_status_and_a_backoff_not_an_exception(self):
        async def run():
            so = second_opinion.SecondOpinion({"AISIS_HYP_ENABLED": "1", "AISIS_HYP_PYTHON": "/nonexistent/python"})
            first = await so.resolve(so.begin(bytes(32000), 16000), "включи свет", 2000)
            second = await so.resolve(so.begin(bytes(32000), 16000), "включи свет", 2000)
            return first, second, so.health()
        first, second, health = asyncio.run(run())
        self.assertEqual(first.status, "error:GigaError")
        self.assertEqual(second.status, "unavailable")   # inside the backoff: not even tried
        self.assertEqual(health["state"], "down:GigaError")


if __name__ == "__main__":
    unittest.main()
