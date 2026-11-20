"""second_opinion: the warm second recogniser (GigaAM CTC) beside Whisper, behind the stt-cli core's contract.

The core is a fake (hypotheses_fakes): no model, no subprocess. What is pinned here is the glue: off by default, lazy worker with a
restart backoff, candidates around the primary and the second reading, a bounded wait, and that nothing ever raises.
"""
import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

import hypotheses_fakes as fakes
import second_opinion
from second_opinion import Opinion, SecondOpinion

ENGINE = "/opt/engines/onnx-asr/bin/python"
ON = {"AISIS_HYP_ENABLED": "1", "AISIS_HYP_PYTHON": ENGINE}
PCM = b"\x01\x00" * 16000


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make(env=None, script=None, clock=None, **kwargs):
    core = fakes.core(script)
    return SecondOpinion(env if env is not None else ON, load_core=lambda: core, clock=clock or Clock(), **kwargs), core


class Switches(unittest.IsolatedAsyncioTestCase):
    async def test_off_unless_enabled_and_the_core_is_never_loaded(self):
        loaded = []
        so = SecondOpinion({}, load_core=lambda: loaded.append(1))
        self.assertIsNone(so.begin(PCM, 16000))
        self.assertEqual(await so.resolve(None, "включи свет", 300), Opinion(None, None, "disabled"))
        self.assertFalse(so.waits_for("включи свет"))
        await so.warm()
        await so.close()
        self.assertEqual(loaded, [])
        self.assertEqual(so.health(), {"enabled": False, "state": "disabled", "model": "gigaam-v3-ctc", "last_ms": None})

    async def test_enabled_values(self):
        for value, expected in (("1", True), ("true", True), ("On", True), ("0", False), ("", False), ("off", False)):
            self.assertEqual(SecondOpinion({"AISIS_HYP_ENABLED": value}).enabled, expected, value)

    async def test_settings_come_from_the_environment(self):
        so, core = make({**ON, "AISIS_HYP_MODEL": "gigaam-v3-e2e-ctc", "AISIS_HYP_THREADS": "2", "AISIS_HYP_WAIT_MS": "120"})
        self.assertEqual(so.wait_ms, 120)
        await so.resolve(so.begin(PCM, 16000), "включи свет", 500)
        worker = core.GigaCtc.instances[0]
        self.assertEqual((worker.python, worker.model, worker.threads), (ENGINE, "gigaam-v3-e2e-ctc", 2))
        await so.close()

    async def test_defaults_and_bad_numbers(self):
        so = SecondOpinion({"AISIS_HYP_ENABLED": "1", "AISIS_HYP_THREADS": "many", "AISIS_HYP_WAIT_MS": "-5"})
        self.assertEqual((so.model, so.threads, so.wait_ms), ("gigaam-v3-ctc", 4, 300))

    async def test_default_python_is_the_stt_cli_onnx_asr_engine_when_it_exists(self):
        with tempfile.TemporaryDirectory() as home:
            env = {"AISIS_HYP_ENABLED": "1", "STT_HOME": home}
            self.assertIsNone(SecondOpinion(env).python)
            interpreter = os.path.join(home, "engines", "onnx-asr", "bin", "python")
            os.makedirs(os.path.dirname(interpreter))
            open(interpreter, "w").close()
            os.chmod(interpreter, 0o755)
            self.assertEqual(SecondOpinion(env).python, interpreter)


class Policy(unittest.TestCase):
    def test_waits_only_for_short_phrases_with_a_confusable_word(self):
        so, _ = make()
        self.assertTrue(so.waits_for("Включи свет."))
        self.assertTrue(so.waits_for("выключи свет в спальне пожалуйста да"))   # 6 words
        self.assertFalse(so.waits_for("какая сегодня погода в москве"))
        self.assertFalse(so.waits_for("включи свет и расскажи мне про погоду на завтра пожалуйста"))   # 10 words
        self.assertFalse(so.waits_for(""))

    def test_a_core_that_cannot_load_never_waits_and_never_raises(self):
        def broken():
            raise ImportError("no stt_cli")
        so = SecondOpinion(ON, load_core=broken)
        self.assertFalse(so.waits_for("включи свет"))


class Decoding(unittest.IsolatedAsyncioTestCase):
    async def test_opinion_carries_the_scored_nbest(self):
        so, core = make()
        opinion = await so.resolve(so.begin(PCM, 16000), "Включи свет.", 500)
        self.assertEqual(opinion.status, "ok")
        self.assertTrue(70 <= opinion.ms < 170, opinion.ms)   # the worker's decode (70 in the fake) plus the scoring wall time
        texts = opinion.nbest.as_dict()["hypotheses"]
        self.assertEqual([h["text"] for h in texts], ["Включи свет.", "выключи свет"])
        await so.close()

    async def test_candidates_are_the_primary_its_confusables_and_those_of_the_second_reading(self):
        so, core = make(script=lambda worker: setattr(worker, "top", "включи музыку"))
        await so.resolve(so.begin(PCM, 16000), "Включи свет.", 500)
        (_, texts), = core.GigaCtc.instances[0].scores
        self.assertEqual(texts, ["Включи свет.", "включи музыку", "выключи свет", "выключи музыку"])   # the primary and what the second recogniser heard, each with its swap
        await so.close()

    async def test_scored_texts_are_deduplicated_when_both_readings_agree(self):
        so, core = make(script=lambda worker: setattr(worker, "top", "включи свет"))
        await so.resolve(so.begin(PCM, 16000), "включи свет", 500)
        (_, texts), = core.GigaCtc.instances[0].scores
        self.assertEqual(texts, ["включи свет", "выключи свет"])
        await so.close()

    async def test_phrase_without_confusables_is_scored_alone_with_the_second_reading(self):
        so, core = make(script=lambda worker: setattr(worker, "top", "какая погода"))
        opinion = await so.resolve(so.begin(PCM, 16000), "Какая погода?", 500)
        self.assertEqual(opinion.status, "ok")
        (_, texts), = core.GigaCtc.instances[0].scores
        self.assertEqual(texts, ["Какая погода?"])
        await so.close()

    async def test_the_pcm_reaches_the_worker_at_16_khz(self):
        so, core = make()
        await so.resolve(so.begin(PCM, 16000), "включи свет", 500)
        self.assertEqual(core.GigaCtc.instances[0].decodes, [(len(PCM), 16000)])
        await so.close()

    async def test_other_rates_are_skipped_the_worker_decodes_16_khz_only(self):
        so, core = make()
        self.assertIsNone(so.begin(PCM, 48000))
        self.assertEqual(core.GigaCtc.instances, [])

    async def test_one_worker_serves_every_utterance_and_starts_lazily(self):
        so, core = make()
        self.assertEqual(core.GigaCtc.instances, [])   # nothing spawned by constructing it
        for _ in range(3):
            await so.resolve(so.begin(PCM, 16000), "включи свет", 500)
        self.assertEqual(len(core.GigaCtc.instances), 1)
        health = so.health()
        self.assertTrue(70 <= health.pop("last_ms") < 170)
        self.assertEqual(health, {"enabled": True, "state": "ready", "model": "gigaam-v3-ctc"})
        await so.close()

    async def test_warm_starts_the_worker_without_an_utterance(self):
        so, core = make()
        await so.warm()
        self.assertTrue(core.GigaCtc.instances[0].started)
        await so.close()
        self.assertTrue(core.GigaCtc.instances[0].closed)


class Bounded(unittest.IsolatedAsyncioTestCase):
    async def test_a_slow_decode_times_out_at_the_bound_and_finishes_for_a_later_resolve(self):
        so, core = make(script=lambda worker: setattr(worker, "delay", 0.25))
        task = so.begin(PCM, 16000)
        began = asyncio.get_running_loop().time()
        opinion = await so.resolve(task, "включи свет", 60)
        self.assertEqual(opinion, Opinion(None, None, "timeout"))
        self.assertLess(asyncio.get_running_loop().time() - began, 0.2)
        self.assertEqual((await so.resolve(task, "включи свет", 2000)).status, "ok")   # not cancelled: the late path still gets it
        await so.close()

    async def test_the_score_is_inside_the_same_bound(self):
        async def slow_score(self, handle, *phrases):
            await asyncio.sleep(0.5)
        so, core = make()
        began = asyncio.get_running_loop().time()
        with patch.object(core.GigaCtc, "score_alternatives", slow_score):
            opinion = await so.resolve(so.begin(PCM, 16000), "включи свет", 100)
        self.assertEqual(opinion.status, "timeout")
        self.assertLess(asyncio.get_running_loop().time() - began, 0.3)
        await so.close()

    async def test_a_scoring_that_outlives_the_wait_is_awaited_by_the_late_collection_not_started_again(self):
        so, core = make(script=lambda worker: setattr(worker, "score_delay", 0.25))
        task = so.begin(PCM, 16000)
        self.assertEqual(await so.resolve(task, "включи свет", 100), Opinion(None, None, "timeout"))
        late = await so.resolve(task, "включи свет", 2000)
        self.assertEqual(late.status, "ok")
        self.assertGreaterEqual(late.ms, 70 + 240)   # the decode plus the whole scoring, counted from where it started
        (worker,) = core.GigaCtc.instances
        self.assertEqual(len(worker.scores), 1)
        await so.close()

    async def test_a_late_collection_about_another_text_is_scored_for_that_text(self):
        so, core = make()
        task = so.begin(PCM, 16000)
        await so.resolve(task, "включи свет", 500)
        await so.resolve(task, "выключи свет", 500)
        (worker,) = core.GigaCtc.instances
        self.assertEqual([texts[0] for _, texts in worker.scores], ["включи свет", "выключи свет"])
        await so.close()


class Stalled(unittest.IsolatedAsyncioTestCase):
    """A worker that never finishes starting: the work queued behind it stays bounded and is abandoned, not kept until shutdown."""

    async def test_decodes_beyond_the_cap_are_refused_with_a_status(self):
        cap = second_opinion.MAX_OUTSTANDING
        so, _ = make(script=lambda worker: setattr(worker, "hang_start", True))
        tasks = [so.begin(PCM, 16000) for _ in range(cap + 5)]
        statuses = [(await so.resolve(task, "включи свет", 0)).status for task in tasks]
        self.assertEqual(statuses, ["timeout"] * cap + ["busy"] * 5)
        self.assertLessEqual(len(so._tasks), cap)
        await so.close()
        self.assertEqual(so._tasks, set())

    async def test_stalled_decodes_are_abandoned_after_their_lifetime_and_the_worker_starts_again_later(self):
        clock = Clock()
        so, core = make(script=lambda worker: setattr(worker, "hang_start", len(core.GigaCtc.instances) == 1), clock=clock)
        with patch.object(second_opinion, "TASK_TTL_S", 0.2):
            tasks = [so.begin(PCM, 16000) for _ in range(3)]
            await asyncio.sleep(0.4)
        self.assertEqual(so._tasks, set())
        self.assertTrue(core.GigaCtc.instances[0].closed)   # the start that hung was cleaned up
        self.assertEqual(so.health()["state"], "down:CancelledError")
        self.assertEqual([(await so.resolve(task, "включи свет", 0)).status for task in tasks], ["unavailable"] * 3)
        clock.now += second_opinion.RESTART_BACKOFF_S + 1
        self.assertEqual((await so.resolve(so.begin(PCM, 16000), "включи свет", 500)).status, "ok")
        self.assertEqual(len(core.GigaCtc.instances), 2)
        await so.close()

    async def test_a_hung_startup_warm_up_is_abandoned_and_the_next_decode_recovers(self):
        clock = Clock()
        so, core = make(script=lambda worker: setattr(worker, "hang_start", len(core.GigaCtc.instances) == 1), clock=clock)
        with patch.object(second_opinion, "TASK_TTL_S", 0.2):
            await asyncio.wait_for(so.warm(), 2)   # returns at the deadline instead of holding the lock for ever
        self.assertEqual(so._tasks, set())
        self.assertTrue(core.GigaCtc.instances[0].closed)
        self.assertFalse(so._lock.locked())
        clock.now += second_opinion.RESTART_BACKOFF_S + 1
        self.assertEqual((await so.resolve(so.begin(PCM, 16000), "включи свет", 500)).status, "ok")
        self.assertEqual(len(core.GigaCtc.instances), 2)
        await so.close()

    async def test_scorings_count_against_the_cap_after_their_decodes_finished(self):
        cap = second_opinion.MAX_OUTSTANDING
        so, core = make()
        first = [so.begin(PCM, 16000) for _ in range(cap)]
        await asyncio.wait(first)   # fast decodes: none counts as outstanding work any more, their callers still wait for Whisper
        self.assertEqual(so._tasks, set())
        worker = core.GigaCtc.instances[0]
        worker.delay = worker.score_delay = 60
        later = [so.begin(PCM, 16000) for _ in range(cap)]   # slow decodes fill the cap
        statuses = [(await so.resolve(task, "включи свет", 0)).status for task in first]   # Whisper answered: scorings are asked for
        self.assertEqual(statuses, ["busy"] * cap)
        self.assertEqual(len(so._tasks), cap)
        await so.close()
        self.assertEqual((so._tasks, len(later)), (set(), cap))

    async def test_a_scoring_that_never_answers_is_abandoned_too(self):
        so, _ = make(script=lambda worker: setattr(worker, "score_delay", 60))
        with patch.object(second_opinion, "TASK_TTL_S", 0.2):
            task = so.begin(PCM, 16000)
            self.assertEqual((await so.resolve(task, "включи свет", 100)).status, "timeout")
            await asyncio.sleep(0.4)
        self.assertEqual(so._tasks, set())
        self.assertEqual((await so.resolve(task, "включи свет", 100)).status, "unavailable")
        await so.close()


class NeverRaises(unittest.IsolatedAsyncioTestCase):
    async def test_decode_error_is_a_status_naming_the_class(self):
        so, _ = make(script=lambda worker: setattr(worker, "fail_decode", RuntimeError("boom")))
        self.assertEqual(await so.resolve(so.begin(PCM, 16000), "включи свет", 500), Opinion(None, None, "error:RuntimeError"))

    async def test_score_error_is_a_status_naming_the_class(self):
        so, _ = make(script=lambda worker: setattr(worker, "fail_score", ValueError("bad")))
        self.assertEqual(await so.resolve(so.begin(PCM, 16000), "включи свет", 500), Opinion(None, None, "error:ValueError"))
        await so.close()

    async def test_core_that_cannot_load_is_unavailable(self):
        def broken():
            raise ImportError("no stt_cli")
        so = SecondOpinion(ON, load_core=broken)
        self.assertEqual(await so.resolve(so.begin(PCM, 16000), "включи свет", 500), Opinion(None, None, "unavailable"))
        self.assertEqual(so.health()["state"], "down:ImportError")

    async def test_no_engine_interpreter_is_unavailable(self):
        with tempfile.TemporaryDirectory() as home:
            so = SecondOpinion({"AISIS_HYP_ENABLED": "1", "STT_HOME": home}, load_core=fakes.core)
            self.assertEqual(await so.resolve(so.begin(PCM, 16000), "включи свет", 500), Opinion(None, None, "unavailable"))

    async def test_a_resolve_cancelled_by_the_caller_cancels_nothing_else(self):
        so, core = make(script=lambda worker: setattr(worker, "delay", 0.2))
        task = so.begin(PCM, 16000)
        waiting = asyncio.ensure_future(so.resolve(task, "включи свет", 5000))
        await asyncio.sleep(0.05)
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        self.assertFalse(task.cancelled())
        await task
        await so.close()


class Restart(unittest.IsolatedAsyncioTestCase):
    async def test_a_failed_start_is_not_retried_until_the_backoff_passed(self):
        clock = Clock()
        so, core = make(script=lambda worker: setattr(worker, "fail_start", OSError("no engine")), clock=clock)
        self.assertEqual((await so.resolve(so.begin(PCM, 16000), "включи свет", 500)).status, "error:OSError")
        clock.now += second_opinion.RESTART_BACKOFF_S - 1
        self.assertEqual((await so.resolve(so.begin(PCM, 16000), "включи свет", 500)).status, "unavailable")
        self.assertEqual(len(core.GigaCtc.instances), 1)   # not even tried
        clock.now += 2
        await so.resolve(so.begin(PCM, 16000), "включи свет", 500)
        self.assertEqual(len(core.GigaCtc.instances), 2)

    async def test_a_crashed_worker_is_restarted_at_most_once_per_backoff(self):
        clock = Clock()
        fail_first = {"n": 0}

        def script(worker):
            fail_first["n"] += 1
            if fail_first["n"] == 1:
                worker.fail_decode = RuntimeError("worker died")
        so, core = make(script=script, clock=clock)
        self.assertEqual((await so.resolve(so.begin(PCM, 16000), "включи свет", 500)).status, "error:RuntimeError")
        self.assertEqual((await so.resolve(so.begin(PCM, 16000), "включи свет", 500)).status, "unavailable")   # inside the backoff: no new process
        self.assertEqual(len(core.GigaCtc.instances), 1)
        clock.now += second_opinion.RESTART_BACKOFF_S + 1
        self.assertEqual((await so.resolve(so.begin(PCM, 16000), "включи свет", 500)).status, "ok")
        self.assertEqual(len(core.GigaCtc.instances), 2)
        self.assertTrue(core.GigaCtc.instances[0].closed)   # the dead one was cleaned up
        self.assertEqual(so.health()["state"], "ready")
        await so.close()


class Closing(unittest.IsolatedAsyncioTestCase):
    async def test_close_stops_the_worker_and_pending_decodes(self):
        so, core = make(script=lambda worker: setattr(worker, "delay", 5))
        task = so.begin(PCM, 16000)
        await asyncio.sleep(0.05)
        await so.close()
        self.assertTrue(core.GigaCtc.instances[0].closed)
        self.assertTrue(task.cancelled() or task.done())
        self.assertEqual(await so.resolve(None, "x", 1), Opinion(None, None, "disabled"))


if __name__ == "__main__":
    unittest.main()
