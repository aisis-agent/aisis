"""Jev behind RouteDecisionProvider: parsing, hard timeout, circuit breaker, fail-open, the key never logged."""
import asyncio
import contextlib
import io
import logging
import unittest
from collections import Counter
from pathlib import Path

import jev
import intent_route
from jev import Choice, ChoiceAnswer, CircuitBreaker, JevClient, JevError, JevRouteProvider, RouteTurn

KEY = "test-only-jev-key-0123456789"


def answers(utterance="talk", tier="fast", effort="none", confidence=1.0, route="escalate"):
    def one(name, choice):
        return {"type": "choice", "name": name, "choice": choice, "confidence": confidence,
                "probabilities": [{"value": choice, "probability": confidence}]}
    return {"answers": [one("utterance", utterance), one("tier", tier), one("effort", effort), one("route", route)],
            "model": "typesafe-ai/jev"}


class FakePost:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    async def __call__(self, url, headers, body):
        self.calls.append((url, headers, body))
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            return await reply()
        return reply


def provider(post, **kwargs):
    breaker = kwargs.pop("breaker", None)
    return JevRouteProvider(JevClient(post, KEY, timeout=kwargs.pop("timeout", 0.4)), breaker=breaker, **kwargs)


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_shape_and_parse(self):
        post = FakePost((200, answers("command", "fast", "none", 0.97)))
        got = await JevClient(post, KEY).choose("поставь таймер", [jev.UTTERANCE, jev.TIER, jev.EFFORT])
        self.assertEqual(got["utterance"], ChoiceAnswer("command", 0.97))
        url, headers, body = post.calls[0]
        self.assertEqual(url, "https://ai-gateway.vercel.sh/v1/decisions")
        self.assertEqual(headers["Authorization"], "Bearer " + KEY)
        self.assertEqual((body["model"], body["input"]), ("typesafe-ai/jev", "поставь таймер"))
        self.assertEqual([q["name"] for q in body["questions"]], ["utterance", "tier", "effort"])
        self.assertEqual(body["questions"][0]["choices"][0], {"value": "command", "description": jev.UTTERANCE.options["command"]})

    async def test_confidence_falls_back_to_the_probability_list(self):
        reply = answers("talk")
        for answer in reply["answers"]:
            del answer["confidence"]
        got = await JevClient(FakePost((200, reply)), KEY).choose("x", [jev.UTTERANCE, jev.TIER, jev.EFFORT])
        self.assertEqual(got["tier"].confidence, 1.0)

    async def test_every_failure_is_a_jev_error_without_the_key_or_text(self):
        async def hang():
            await asyncio.sleep(5)
        cases = {"timeout": FakePost(hang), "http 403": FakePost((403, {"error": "no card"})),
                 "http 500": FakePost((500, None)), "malformed": FakePost((200, {"answers": []})),
                 "bad confidence": FakePost((200, {"answers": [{"name": "utterance", "choice": "talk", "confidence": 7}]})),
                 "unknown option": FakePost((200, {"answers": [{"name": "utterance", "choice": "nope", "confidence": 1}]})),
                 "transport OSError": FakePost(OSError(f"connect failed for Bearer {KEY} / секретная фраза"))}
        for reason, post in cases.items():
            with self.subTest(reason):
                with self.assertRaises(JevError) as raised:
                    await JevClient(post, KEY, timeout=0.05).choose("секретная фраза", [jev.UTTERANCE])
                self.assertNotIn(KEY, str(raised.exception))
                self.assertNotIn("секретная", str(raised.exception))


class BreakerTests(unittest.TestCase):
    def test_opens_after_consecutive_failures_and_probes_once_per_window(self):
        now = [0.0]
        breaker = CircuitBreaker(failures=3, open_for=30, clock=lambda: now[0])
        for _ in range(2):
            breaker.failure()
        self.assertTrue(breaker.allow())
        breaker.failure()
        self.assertFalse(breaker.allow())
        now[0] = 29
        self.assertFalse(breaker.allow())
        now[0] = 31
        self.assertTrue(breaker.allow())      # the probe
        self.assertFalse(breaker.allow())     # only one per window
        breaker.success()
        self.assertTrue(breaker.allow())

    def test_a_success_resets_the_count(self):
        breaker = CircuitBreaker(failures=2, open_for=30, clock=lambda: 0.0)
        breaker.failure()
        breaker.success()
        breaker.failure()
        self.assertTrue(breaker.allow())


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def decide(self, reply, **kwargs):
        return await provider(FakePost(reply), **kwargs).decide(RouteTurn("текст"))

    async def test_talk_becomes_a_model_route_with_tier_and_effort(self):
        got = await self.decide((200, answers("talk", "deep", "high", 0.99)))
        self.assertEqual((got.kind, got.model_alias, got.effort, got.reason_code, got.utterance),
                         ("model", "deep", "high", "jev_talk", "talk"))

    async def test_unsure_tier_or_effort_degrade_to_the_middle_never_up(self):
        reply = answers("talk", "deep", "high", 0.99)
        reply["answers"][1]["confidence"] = 0.6
        reply["answers"][2]["confidence"] = 0.5
        got = await self.decide((200, reply))
        self.assertEqual((got.model_alias, got.effort), ("balanced", "medium"))

    async def test_command_garbled_and_not_addressed_are_deterministic_routes(self):
        for utterance, reason in (("command", "jev_command_unsupported"), ("garbled", "jev_garbled"), ("not_addressed", "jev_not_addressed")):
            got = await self.decide((200, answers(utterance)))
            self.assertEqual((got.kind, got.reason_code, got.utterance), ("deterministic", reason, utterance))

    async def test_low_confidence_abstains(self):
        p = provider(FakePost((200, answers("garbled", confidence=0.9))))
        self.assertIsNone(await p.decide(RouteTurn("x")))
        self.assertEqual(p.stats["low_confidence"], 1)

    async def test_failures_abstain_and_open_the_breaker(self):
        post = FakePost((403, {"error": "payment required"}))
        breaker = CircuitBreaker(failures=2, open_for=60, clock=lambda: 0.0)
        p = provider(post, breaker=breaker)
        with self.assertLogs("aisis.jev", level="WARNING") as logs:
            self.assertIsNone(await p.decide(RouteTurn("x")))
            self.assertIsNone(await p.decide(RouteTurn("x")))
        self.assertIn("http 403", logs.output[0])
        self.assertIsNone(await p.decide(RouteTurn("x")))     # open: no call at all
        self.assertEqual(len(post.calls), 2)
        self.assertEqual((p.stats["failed"], p.stats["skipped_open"]), (2, 1))

    async def test_hard_timeout_is_enforced(self):
        async def slow():
            await asyncio.sleep(3)
        p = provider(FakePost(slow), timeout=0.05)
        started = asyncio.get_running_loop().time()
        with self.assertLogs("aisis.jev", level="WARNING"):
            self.assertIsNone(await p.decide(RouteTurn("x")))
        self.assertLess(asyncio.get_running_loop().time() - started, 0.5)

    async def test_an_unexpected_bug_never_escapes(self):
        class Boom:
            async def ask(self, *args):
                raise ZeroDivisionError("bug")
        with self.assertLogs("aisis.jev", level="WARNING") as logs:
            verdict = await JevRouteProvider(Boom()).assess(RouteTurn("x"))
        self.assertEqual(verdict.reason, "error ZeroDivisionError")
        self.assertIn("ZeroDivisionError", logs.output[0])

    async def test_the_key_and_the_transcript_are_never_logged(self):
        with self.assertLogs("aisis.jev", level="DEBUG") as logs:
            await provider(FakePost(OSError(f"boom {KEY}"))).decide(RouteTurn("очень личная фраза"))
            await provider(FakePost((200, answers()))).decide(RouteTurn("очень личная фраза"))
            logging.getLogger("aisis.jev").debug("sentinel")
        joined = "\n".join(logs.output)
        self.assertNotIn(KEY, joined)
        self.assertNotIn("личная", joined)


class EnvTests(unittest.IsolatedAsyncioTestCase):
    def test_off_by_default_and_without_a_key(self):
        settings = jev.JevSettings()
        self.assertIsNone(jev.from_env({"VERCEL_JEV_KEY": KEY}, object(), settings))
        self.assertIsNone(jev.from_env({"AISIS_JEV": "on"}, object(), settings))
        self.assertIsNone(jev.from_env({"AISIS_JEV": "off", "VERCEL_JEV_KEY": KEY}, object(), settings))
        self.assertIsInstance(jev.from_env({"AISIS_JEV": "shadow", "VERCEL_JEV_KEY": KEY}, object(), settings), JevRouteProvider)

    def test_the_budget_never_exceeds_the_hard_timeout(self):
        slow = jev.from_env({"AISIS_JEV": "on", "VERCEL_JEV_KEY": KEY, "AISIS_JEV_TIMEOUT": "5"}, object(), jev.JevSettings())
        self.assertEqual(slow.client.timeout, jev.HARD_TIMEOUT_S)
        garbled = jev.from_env({"AISIS_JEV": "on", "VERCEL_JEV_KEY": KEY, "AISIS_JEV_TIMEOUT": "fast"}, object(), jev.JevSettings())
        self.assertEqual(garbled.client.timeout, jev.HARD_TIMEOUT_S)

    async def test_aiohttp_post_adapter(self):
        class Response:
            status = 200
            async def json(self, content_type=None):
                return {"ok": True}
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
        class Session:
            def post(self, url, headers, json):
                self.seen = (url, json)
                return Response()
        session = Session()
        self.assertEqual(await jev.aiohttp_post(session)("u", {}, {"a": 1}), (200, {"ok": True}))
        self.assertEqual(session.seen, ("u", {"a": 1}))


class VerdictTests(unittest.IsolatedAsyncioTestCase):
    """What one call came to, with its reason, latency and cost: the material of the shadow log."""

    async def test_a_confident_answer_is_a_decision_with_latency_and_cost(self):
        reply = answers("command", confidence=0.97)
        reply["provider_metadata"] = {"gateway": {"cost": "0.0000257"}}
        verdict = await provider(FakePost((200, reply))).assess(RouteTurn("x"))
        self.assertEqual((verdict.reason, verdict.decision.reason_code), ("ok", "jev_command_unsupported"))
        self.assertEqual(verdict.candidate, verdict.decision)
        self.assertEqual(verdict.cost, 0.0000257)
        self.assertIsInstance(verdict.latency_ms, int)

    async def test_a_decision_below_the_threshold_is_abstained_but_kept_for_the_log(self):
        p = provider(FakePost((200, answers("garbled", confidence=0.9))))
        verdict = await p.assess(RouteTurn("x"))
        self.assertEqual((verdict.reason, verdict.decision), ("below_threshold", None))
        self.assertEqual((verdict.candidate.reason_code, verdict.candidate.confidence), ("jev_garbled", 0.9))
        self.assertEqual(verdict.answers["utterance"], ChoiceAnswer("garbled", 0.9))

    async def test_the_threshold_is_configurable(self):
        loose = provider(FakePost((200, answers("garbled", confidence=0.85))), confident=0.8)
        strict = provider(FakePost((200, answers("garbled", confidence=0.97))), confident=0.99)
        self.assertEqual((await loose.assess(RouteTurn("x"))).reason, "ok")
        self.assertEqual((await strict.assess(RouteTurn("x"))).reason, "below_threshold")

    async def test_the_tier_hint_degrades_at_the_same_threshold(self):
        reply = answers("talk", "deep", "high", 0.85)
        self.assertEqual((await provider(FakePost((200, reply)), confident=0.8).decide(RouteTurn("x"))).model_alias, "deep")
        self.assertEqual((await provider(FakePost((200, reply))).assess(RouteTurn("x"))).reason, "below_threshold")

    async def test_failures_carry_their_reason_and_never_a_cost(self):
        async def hang():
            await asyncio.sleep(5)
        cases = {"timeout": FakePost(hang), "http 403": FakePost((403, None)), "malformed answer": FakePost((200, {"answers": []})),
                 "transport OSError": FakePost(OSError("boom"))}
        for reason, post in cases.items():
            with self.subTest(reason), self.assertLogs("aisis.jev", level="WARNING"):
                verdict = await provider(post, timeout=0.05).assess(RouteTurn("x"))
            self.assertEqual((verdict.reason, verdict.decision, verdict.candidate, verdict.cost), (reason, None, None, None))
            self.assertIsInstance(verdict.latency_ms, int)

    async def test_an_open_breaker_is_a_reason_not_a_call(self):
        post = FakePost((500, None))
        p = provider(post, breaker=CircuitBreaker(failures=1, open_for=60, clock=lambda: 0.0))
        with self.assertLogs("aisis.jev", level="WARNING"):
            await p.assess(RouteTurn("x"))
        verdict = await p.assess(RouteTurn("x"))
        self.assertEqual((verdict.reason, verdict.latency_ms, len(post.calls)), ("breaker_open", None, 1))

    async def test_cost_is_none_unless_the_gateway_reports_a_sane_number(self):
        for metadata in (None, {}, {"gateway": {}}, {"gateway": {"cost": "free"}}, {"gateway": {"cost": -1}},
                         {"gateway": {"cost": float("inf")}}, {"gateway": {"cost": None}}, "x"):
            reply = answers()
            reply["provider_metadata"] = metadata
            verdict = await provider(FakePost((200, reply))).assess(RouteTurn("x"))
            self.assertIsNone(verdict.cost, metadata)


class RouteQuestionTests(unittest.IsolatedAsyncioTestCase):
    """The route-type question of FastModelBench, asked next to the other three and only logged."""

    def test_it_is_the_benchmarks_wording_with_its_four_routes(self):
        self.assertEqual(list(jev.ROUTE.options), ["deterministic", "fast", "reprompt", "escalate"])
        self.assertEqual(jev.ROUTE.instructions, "Выбери маршрут голосовой фразы, распознанной колонкой.")
        self.assertTrue(jev.ROUTE.options["escalate"].startswith("всё остальное: факты о мире"))
        self.assertEqual(jev.QUESTIONS, [jev.UTTERANCE, jev.TIER, jev.EFFORT, jev.ROUTE])

    async def test_one_call_asks_all_four_and_the_route_answer_is_advisory(self):
        reply = answers("talk", "fast", "none", 0.99)
        reply["answers"].append({"type": "choice", "name": "route", "choice": "reprompt", "confidence": 0.99})
        post = FakePost((200, reply))
        verdict = await provider(post).assess(RouteTurn("x"))
        self.assertEqual([q["name"] for q in post.calls[0][2]["questions"]], ["utterance", "tier", "effort", "route"])
        self.assertEqual(verdict.answers["route"], ChoiceAnswer("reprompt", 0.99))
        self.assertEqual(verdict.decision.reason_code, "jev_talk")        # a «reprompt» route changes nothing by itself


class SettingsTests(unittest.TestCase):
    def test_defaults_are_advisory_at_0_95(self):
        settings = jev.settings_from_env({})
        self.assertEqual(settings, jev.JevSettings(0.95, False, False))
        self.assertFalse(settings.acts)

    def test_switches_are_independent(self):
        self.assertEqual(jev.settings_from_env({"AISIS_JEV_REFUSE": "on"}), jev.JevSettings(0.95, True, False))
        self.assertEqual(jev.settings_from_env({"AISIS_JEV_REPROMPT": " ON "}), jev.JevSettings(0.95, False, True))
        self.assertTrue(jev.settings_from_env({"AISIS_JEV_REFUSE": "on", "AISIS_JEV_REPROMPT": "on"}).acts)

    def test_the_threshold_is_validated_to_0_5_through_1_0(self):
        for raw, want in (("0.5", 0.5), ("0.8", 0.8), ("1", 1.0), ("1.0", 1.0), (" 0.99 ", 0.99)):
            self.assertEqual(jev.settings_from_env({"AISIS_JEV_THRESHOLD": raw}).threshold, want, raw)
        for raw in ("0.49", "0", "-1", "1.01", "2", "high", "nan", "inf", "0,9"):
            with self.subTest(raw), self.assertLogs("aisis.jev", level="WARNING") as logs:
                self.assertEqual(jev.settings_from_env({"AISIS_JEV_THRESHOLD": raw}).threshold, jev.CONFIDENT)
            self.assertIn("name=AISIS_JEV_THRESHOLD", logs.output[0])

    def test_an_unknown_switch_value_is_off_and_logged_by_name(self):
        for raw in ("yes", "1", "true", "ON!"):
            with self.subTest(raw), self.assertLogs("aisis.jev", level="WARNING") as logs:
                self.assertFalse(jev.settings_from_env({"AISIS_JEV_REFUSE": raw}).refuse)
            self.assertIn("name=AISIS_JEV_REFUSE", logs.output[0])

    def test_from_env_hands_the_threshold_to_the_provider(self):
        provider_ = jev.from_env({"AISIS_JEV": "on", "VERCEL_JEV_KEY": KEY}, object(), jev.JevSettings(0.8))
        self.assertEqual(provider_.confident, 0.8)


class EmergencyGuardTests(unittest.TestCase):
    EMERGENCIES = ("Мне плохо, вызови скорую", "Позвони в полицию", "вызови скорую помощь", "у нас пожар", "мне плохо с сердцем",
                   "я не могу дышать", "ребёнок тонет", "он отравился", "она потеряла сознание", "позвони сто двенадцать",
                   "набери 112", "Алиса, помогите!", "у дедушки инсульт", "вызови врача срочно")
    ORDINARY = ("закажи мне пиццу на вечер с доставкой", "расскажи мне про динозавров подробно", "составь список покупок на неделю",
                "поставь будильник на семь утра", "позвони маме", "какая завтра погода")

    def test_emergency_phrases_trip_the_guard(self):
        for phrase in self.EMERGENCIES:
            self.assertTrue(jev.is_emergency(phrase), phrase)

    def test_ordinary_phrases_do_not(self):
        for phrase in self.ORDINARY:
            self.assertFalse(jev.is_emergency(phrase), phrase)

    def test_every_must_pass_safety_phrase_of_the_grammar_trips_the_guard(self):
        """The guard is its own list: this keeps it from drifting behind the grammar's pinned emergency set."""
        grammar = intent_route.load_grammar()
        phrases = grammar.gate["safety"]["phrases"]
        self.assertGreater(len(phrases), 10)
        for phrase in phrases:
            self.assertTrue(jev.is_emergency(phrase), phrase)


class LogLineTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_line_has_turn_hash_latency_cost_reason_and_the_below_threshold_decision(self):
        reply = answers("command", "balanced", "low", 0.9)
        reply["provider_metadata"] = {"gateway": {"cost": 0.0000257}}
        verdict = await provider(FakePost((200, reply))).assess(RouteTurn("секретная фраза"))
        line = jev.describe(verdict, jev.turn_hash("секретная фраза"))
        self.assertIn("utterance=command tier=balanced effort=low conf=0.90", line)
        self.assertIn("decision=jev_command_unsupported outcome=below_threshold", line)
        self.assertIn("cost=0.0000257", line)
        self.assertRegex(line, r"ms=\d+ ")
        self.assertIn(f"turn={jev.turn_hash('секретная фраза')}", line)
        self.assertNotIn("секретная", line)

    async def test_a_failure_line_names_the_reason_without_spaces(self):
        with self.assertLogs("aisis.jev", level="WARNING"):
            verdict = await provider(FakePost((403, None))).assess(RouteTurn("x"))
        line = jev.describe(verdict, "abc", guard="emergency")
        self.assertIn("utterance=none tier=none effort=none conf=0.00", line)
        self.assertIn("decision=none outcome=http_403", line)
        self.assertIn("cost=- turn=abc guard=emergency", line)

    def test_the_turn_hash_is_stable_short_and_not_the_text(self):
        self.assertEqual(jev.turn_hash("Привет"), jev.turn_hash("  привет "))
        self.assertEqual(len(jev.turn_hash("привет")), 10)
        self.assertNotIn("привет", jev.turn_hash("привет"))


class NightlyLabelTests(unittest.IsolatedAsyncioTestCase):
    async def test_label_with_jev_picks_an_existing_intent_or_none(self):
        reply = {"answers": [{"type": "choice", "name": "intent", "choice": "volume.set", "confidence": 0.95}]}
        post = FakePost((200, reply))
        got = await jev.label_with_jev(JevClient(post, KEY), "сделай погромче на пять",
                                       {"volume.set": "громкость на пять", "time.now": "который час"})
        self.assertEqual(got, ChoiceAnswer("volume.set", 0.95))
        options = [c["value"] for c in post.calls[0][2]["questions"][0]["choices"]]
        self.assertEqual(options, ["volume.set", "time.now", jev.NEW_INTENT])


class BenchEvalTests(unittest.TestCase):
    """The 149-row route evaluation of bench_jev.py: its parsing and its numbers, on synthetic rows (no network)."""

    def test_the_eval_file_is_the_benchmarks_149_labelled_rows(self):
        import bench_jev
        rows = bench_jev.eval_rows(str(Path(__file__).resolve().parents[3] / "docs" / "research" / "fast-llm-eval.jsonl"), None)
        self.assertEqual(len(rows), 149)
        self.assertEqual(Counter(row["route"] for row in rows), {"fast": 38, "reprompt": 30, "escalate": 71, "deterministic": 10})
        self.assertTrue(all(row["want"] is None for row in rows))

    def test_report_counts_wrong_fast_by_threshold_for_both_questions(self):
        import bench_jev
        def row(label, route, route_conf, tier, tier_conf):
            return {"label": label, "route": route, "route_conf": route_conf, "tier": tier, "tier_conf": tier_conf}
        rows = [row("escalate", "escalate", 0.9, "fast", 0.6), row("escalate", "fast", 0.4, "fast", 0.95),
                row("reprompt", "reprompt", 0.9, "fast", 0.8), row("fast", "fast", 0.99, "fast", 0.99)]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            bench_jev.report_route(rows)
        text = out.getvalue()
        self.assertIn("route accuracy: 3/4 = 75.0 %", text)
        self.assertRegex(text, r"\n\s+0\.00\s+1\s+0\s+2\s+1\n")       # no threshold: route 1 wrong-fast; tier 2 escalate, 1 reprompt
        self.assertRegex(text, r"\n\s+0\.90\s+0\s+0\s+1\s+0\n")       # at 0.9 only the tier question still has one


if __name__ == "__main__":
    unittest.main()
