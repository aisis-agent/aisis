"""utterance_trace (the record), trace_view (its Russian lines, clip matching) and session_links (stable URLs, the owner's address)."""
import json
import subprocess
import unittest

import session_links
import trace_view
import utterance_trace
from intent_grammar import Decision


def decision(**changes):
    base = dict(intent="timer.set", tier="execute", slots={"duration": 300}, action={"executor": "timer.start", "args": {"seconds": 300}},
                reply=None, labels=("TIMER",), confidence=1.0, fuzzy=False, pattern=2, text="таймер на пять минут")
    base.update(changes)
    return Decision(**base)


class Record(unittest.TestCase):
    def test_functions_are_a_no_op_outside_a_call(self):
        utterance_trace.note(intent="x")
        utterance_trace.call("ha", service="light.turn_on")
        utterance_trace.error("boom")
        with utterance_trace.stage("brain"):
            pass
        self.assertIsNone(utterance_trace.current())

    def test_a_call_collects_notes_calls_errors_and_stage_time(self):
        trace = utterance_trace.start("turn", "поставь таймер", "st1")
        utterance_trace.note(layer="grammar", intent="timer.set", missing=None)
        utterance_trace.call("timer", executor="timer.start", ok=True, ms=3, skipped=None)
        utterance_trace.error("first")
        utterance_trace.error("first")        # one line per distinct problem
        trace.stage_ms("route", 4)
        trace.stage_ms("route", 1)
        record = utterance_trace.end(trace)
        self.assertIsNone(utterance_trace.current())
        self.assertEqual((record["v"], record["via"], record["station"], record["text"]), (1, "turn", "st1", "поставь таймер"))
        self.assertEqual(record["intent"], "timer.set")
        self.assertNotIn("missing", record)
        self.assertEqual(record["calls"], [{"kind": "timer", "executor": "timer.start", "ok": True, "ms": 3}])
        self.assertEqual(record["errors"], ["first"])
        self.assertEqual(record["stages_ms"]["route"], 5)
        self.assertEqual(record["stages_ms"]["total"], record["ms"])
        json.dumps(record)

    def test_a_context_variable_keeps_concurrent_calls_apart(self):
        import asyncio

        async def one(name):
            trace = utterance_trace.start("turn", name)
            await asyncio.sleep(0.01)
            utterance_trace.note(intent=name)
            return utterance_trace.end(trace)

        async def both():
            return await asyncio.gather(one("a"), one("b"))

        first, second = asyncio.run(both())
        self.assertEqual((first["intent"], second["intent"]), ("a", "b"))

    def test_text_reply_and_lists_are_bounded(self):
        trace = utterance_trace.Trace("turn", "я" * 1000)
        trace.note(reply="а" * 1000)
        for n in range(50):
            trace.call("ha", n=n)
            trace.error(f"e{n}")
        record = trace.finish()
        self.assertEqual(len(record["text"]), utterance_trace.TEXT_CHARS)
        self.assertEqual(len(record["reply"]), utterance_trace.REPLY_CHARS)
        self.assertEqual((len(record["calls"]), len(record["errors"])), (utterance_trace.MAX_CALLS, utterance_trace.MAX_ERRORS))

    def test_stage_time_is_recorded_when_the_block_raises(self):
        trace = utterance_trace.start("turn", "x")
        with self.assertRaises(ValueError), utterance_trace.stage("brain"):
            raise ValueError
        self.assertIn("brain", utterance_trace.end(trace)["stages_ms"])

    def test_decision_fields_keep_the_parameters_and_drop_the_empty(self):
        fields = utterance_trace.decision_fields(decision(), priority=5)
        self.assertEqual(fields, {"intent": "timer.set", "tier": "execute", "conf": 1.0, "fuzzy": False, "pattern": 2,
                                  "slots": {"duration": 300}, "executor": "timer.start", "args": {"seconds": 300},
                                  "labels": ["TIMER"], "priority": 5})
        bare = utterance_trace.decision_fields(decision(action=None, slots={}, labels=()))
        self.assertNotIn("executor", bare)
        self.assertNotIn("args", bare)
        self.assertNotIn("labels", bare)


class Reading(unittest.TestCase):
    def test_route_line_names_the_layer_intent_band_and_confidence(self):
        line = trace_view.route_line({"layer": "grammar", "intent": "climate.set", "tier": "execute", "priority": 4, "conf": 0.8, "fuzzy": True})
        self.assertEqual(line, "грамматика → climate.set · execute · p4 · 0.80 fuzzy")
        self.assertEqual(trace_view.route_line({"layer": "brain", "brain": {"provider": "claude", "model": "m", "slot": "a"}}), "ИИ → claude m a")
        self.assertEqual(trace_view.route_line({"layer": "none"}), "не обработано адаптером")

    def test_lines_cover_clarification_calls_jev_brain_stages_and_errors(self):
        trace = {"layer": "grammar", "intent": "volume.slotless", "tier": "clarify", "asked": "clarify", "reply": "Какую громкость?", "listen": True,
                 "joined": "громкость семь", "declined": "executor x belongs to the quasar layer", "room": "Гостиная",
                 "calls": [{"kind": "ha", "service": "climate.set_temperature", "entity": "climate.k", "data": {"temperature": 24}, "status": 200, "ms": 12},
                           {"kind": "weather-cli", "argv": "weather say --now", "ok": False, "ms": 8000}],
                 "jev": {"mode": "on", "utterance": "garbled 0.99", "decision": "jev_garbled", "acted": True, "ms": 250},
                 "brain": {"provider": "claude", "slot": "a", "attempts": [{"slot": "a", "result": "complete", "s": 1.5}], "memories": 2},
                 "stages_ms": {"grammar": 1, "brain": 900, "total": 910}, "errors": ["weather-cli: timeout 8 s"]}
        text = "\n".join(trace_view.lines(trace))
        for needle in ("❓ уточнение: «Какую громкость?» (микрофон открыт)", "склеено с уточнением: «громкость семь»", "комната: Гостиная",
                       "HA climate.set_temperature climate.k {'temperature': 24} → 200 · 12 мс", "weather-cli weather say --now → сбой · 8000 мс",
                       "Jev on: garbled 0.99 · jev_garbled · действовал · 250 мс", "ИИ claude слот a · a complete 1.5 с · память 2",
                       "⏱ грамматика 1 · ИИ 900 · всего 910 мс", "⚠ weather-cli: timeout 8 s", "грамматика не выполнила"):
            self.assertIn(needle, text)

    def test_ranks_put_the_answer_before_the_stage_times(self):
        ranked = trace_view.ranked_lines({"layer": "grammar", "intent": "x", "stages_ms": {"total": 3}, "errors": ["e"]})
        ranks = dict((text, rank) for rank, text in ranked)
        self.assertLess(ranks["грамматика → x"], ranks["⏱ всего 3 мс"])
        self.assertEqual(ranks["⚠ e"], 1)

    def test_a_clip_gets_only_the_calls_with_its_text(self):
        clip = {"ts": 100.0, "transcript": "Который час?"}
        calls = [{"id": 1, "ts": 90.0, "text": "который час"},    # before the clip window
                 {"id": 2, "ts": 101.0, "text": "который час"}, {"id": 3, "ts": 101.5, "text": "другое"}, {"id": 4, "ts": 400.0, "text": "который час"}]
        self.assertEqual([t["id"] for t in trace_view.traces_of_clip(clip, calls)], [2])
        unrelated = {"ts": 100.0, "transcript": "громче"}
        self.assertEqual(trace_view.traces_of_clip(unrelated, [{"id": 5, "ts": 108.0, "text": "поставь кондиционер на 24"}]), [],
                         "a neighbour's call is not this clip's")
        self.assertEqual([t["id"] for t in trace_view.traces_of_clip(clip, calls, until=100.5)], [])


class Links(unittest.TestCase):
    def fake_tailscale(self, status=None, ip=None):
        def run(command, **kwargs):
            out = {"status": status, "ip": ip}[command[1]]
            return subprocess.CompletedProcess(command, 0 if out else 1, stdout=out or "", stderr="")
        return run

    def test_the_configured_address_wins(self):
        self.assertEqual(session_links.base_url({"AISIS_DASHBOARD_URL": "http://box.example:9000/"}), "http://box.example:9000")

    def test_the_magicdns_name_of_this_machine_is_the_default(self):
        run = self.fake_tailscale(status=json.dumps({"Self": {"DNSName": "ubuntu-pc.example.test."}}))
        self.assertEqual(session_links.base_url({}, run=run), "http://ubuntu-pc.example.test:8781")
        self.assertEqual(session_links.base_url({"AISIS_DASHBOARD_PORT": "9999"}, run=run), "http://ubuntu-pc.example.test:9999")

    def test_the_tailscale_ip_when_there_is_no_dns_name_and_nothing_when_unknown(self):
        self.assertEqual(session_links.base_url({}, run=self.fake_tailscale(status='{"Self":{}}', ip="100.1.2.3\n")), "http://100.1.2.3:8781")
        self.assertIsNone(session_links.base_url({}, run=self.fake_tailscale()))

        def missing(command, **kwargs):
            raise FileNotFoundError

        self.assertIsNone(session_links.base_url({}, run=missing))

    def test_paths_are_stable_per_dialog_and_per_clip(self):
        self.assertEqual(session_links.session_path("ab58437a-07c6-433a-a202-9ccc90402c6f", "c1"), "/session/ab58437a-07c6-433a-a202-9ccc90402c6f")
        self.assertEqual(session_links.session_path(None, "20261117T160337-f7efcc"), "/session/clip/20261117T160337-f7efcc")
        self.assertEqual(session_links.session_path("bad id/../x", "c1"), "/session/clip/c1")
        with self.assertRaises(ValueError):
            session_links.session_path()
        self.assertEqual(session_links.intent_path("timer.set"), "/grammar#intent-timer.set")
        self.assertTrue(session_links.github_line_url(42).endswith("/intent_grammar/grammar/ru.yaml#L42"))


if __name__ == "__main__":
    unittest.main()
