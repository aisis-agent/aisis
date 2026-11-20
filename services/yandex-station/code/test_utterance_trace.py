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
        self.assertEqual(trace_view.route_line({"layer": "none"}),
                         "грамматика не знает эту фразу → дальше решал квазар (быстрый интент или ИИ)")

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

    def test_a_phrase_handed_on_to_the_quasar_layer_is_not_written_like_an_error(self):
        """«не обработано адаптером» read as a failure; it means the grammar abstained or declined and the quasar layers decided."""
        abstain = {"via": "hook", "layer": "none"}
        self.assertEqual(trace_view.route_line(abstain), "грамматика не знает эту фразу → дальше решал квазар (быстрый интент или ИИ)")
        declined = {"via": "hook", "layer": "none", "intent": "volume.up", "tier": "execute", "priority": 4, "conf": 1.0,
                    "declined": "volume change is run by the quasar layer"}
        self.assertEqual(trace_view.route_line(declined), "грамматика узнала фразу (volume.up · execute · p4 · 1.00), но не выполнила её "
                                                         "→ дальше решал квазар (быстрый интент или ИИ)")
        self.assertIn("↷ грамматика не выполнила: громкость меняет сам квазар", trace_view.lines(declined))
        self.assertNotIn("не обработано адаптером", " ".join(trace_view.lines(declined) + trace_view.lines(abstain)))

    def test_a_shadow_call_says_it_executed_nothing(self):
        known = {"via": "hook-shadow", "layer": "none", "intent": "reminder.in", "tier": "execute", "priority": 0, "conf": 1.0}
        self.assertEqual(trace_view.route_line(known), "грамматика узнала фразу (reminder.in · execute · p0 · 1.00); пробный прогон, ничего не выполнялось")
        self.assertEqual(trace_view.route_line({"via": "hook-shadow", "layer": "none"}),
                         "грамматика не знает эту фразу; пробный прогон, ничего не выполнялось")

    def test_the_reason_a_phrase_was_declined_is_plain_russian(self):
        reasons = {"reprompt cooldown: one «не расслышал» a minute per station": "переспрос не чаще раза в минуту",
                   "compound: a clause is not run by the adapter": "составная команда: часть исполняет квазар",
                   "fuzzy guess: needs the owner's «да»": "нечёткое совпадение: нужно подтверждение «да»",
                   "volume change is run by the quasar layer": "громкость меняет сам квазар",
                   "volume arguments break the contract": "параметры громкости не подходят адаптеру",
                   "executor light.turn_on belongs to the quasar layer": "действие light.turn_on исполняет квазар",
                   "timers are not configured": "таймеры не настроены", "Home Assistant actions are off": "действия Home Assistant выключены",
                   "nothing to repeat": "повторять нечего", "executor timer.start gave no answer": "действие timer.start не дало ответа",
                   "something new": "something new"}
        for reason, expected in reasons.items():
            self.assertEqual(trace_view.declined_text(reason), expected)

    def test_a_handed_on_phrase_names_who_answered_when_it_is_known(self):
        hook = {"via": "hook", "layer": "none", "text": "Какая влажность на улице?"}
        quasar = [{"kind": "запрос", "text": "какая влажность на улице", "res": "Влажность на улице 100 процентов. (listen=false)"}]
        self.assertEqual(trace_view.answered_by(hook["text"], [hook], quasar, []), "быстрый интент квазара → «Влажность на улице 100 процентов.»")
        turn = {"via": "turn", "layer": "brain", "text": "Какая влажность на улице"}
        self.assertEqual(trace_view.answered_by(hook["text"], [hook, turn], quasar, []), "ИИ (ход колонки)", "the adapter's own record is the better witness")
        self.assertEqual(trace_view.answered_by(hook["text"], [hook], [], [{"prompt": "какая влажность на улице", "answer": "а"}]), "ИИ")
        self.assertIsNone(trace_view.answered_by(hook["text"], [hook], [{"kind": "запрос", "text": "другое", "res": "x"}], []))
        line = trace_view.route_line(hook, answered="ИИ")
        self.assertEqual(line, "грамматика не знает эту фразу → ответ: ИИ")

    def test_a_null_fast_intent_result_is_no_answer_for_every_reader(self):
        """clipfeed.describe_result moves on from a `null` result to the brain; the trace wording must read it the same way."""
        hook = {"via": "hook", "layer": "none", "text": "Какая погода?"}
        null = [{"kind": "запрос", "text": "какая погода", "res": "null"}, {"kind": "запрос", "text": "какая погода", "res": ""}]
        brain = [{"prompt": "какая погода", "answer": "Солнечно"}]
        self.assertIsNone(trace_view.quasar_answer("какая погода", null))
        self.assertEqual(trace_view.answered_by(hook["text"], [hook], null, brain), "ИИ")
        self.assertIsNone(trace_view.answered_by(hook["text"], [hook], null, []))
        clip = {"transcript": "Какая погода?"}
        self.assertEqual(trace_view.untraced_reason(clip, {"actions": null, "brain": brain}), "записи вызова адаптера нет, но в диалоге есть ответ ИИ")
        silent = [{"kind": "запрос", "text": "какая погода", "res": "(silent)"}]
        self.assertEqual(trace_view.quasar_answer("какая погода", silent), "", "a silent answer is an answer without words")

    def test_a_crashed_adapter_routing_is_not_reported_as_an_unknown_phrase(self):
        crashed = {"via": "hook", "layer": "none", "text": "Включи свет", "errors": ["route: TypeError"]}
        self.assertEqual(trace_view.route_line(crashed), "адаптер не смог разобрать фразу (ошибка ниже) → " + trace_view.HANDED_ON)

    def test_a_phrase_with_no_adapter_record_says_why_when_it_can(self):
        clip = {"transcript": "Какая влажность на улице?"}
        asked = [{"kind": "запрос", "text": "какая влажность на улице", "res": "Влажность на улице 100 процентов."}]
        self.assertEqual(trace_view.untraced_reason(clip, {"actions": asked, "brain": []}),
                         "записи вызова адаптера нет: фразу обработал сам квазар (быстрый интент → «Влажность на улице 100 процентов.»)")
        self.assertEqual(trace_view.untraced_reason(clip, {"actions": [], "brain": [{"prompt": "какая влажность на улице", "answer": "а"}]}),
                         "записи вызова адаптера нет, но в диалоге есть ответ ИИ")
        self.assertEqual(trace_view.untraced_reason(clip, {"actions": [], "brain": []}),
                         "записи вызова адаптера нет, и ответа квазара в диалоге нет: возможно, фраза не дошла до разбора "
                         "(или квазар разбирал исправленный текст)")
        self.assertEqual(trace_view.untraced_reason(clip, None), "записи вызова адаптера нет и диалога квазара нет: возможно, фраза не дошла до разбора")
        self.assertEqual(trace_view.untraced_reason({"status": "gate", "transcript": ""}, None),
                         "записи вызова адаптера нет: запись отброшена до разбора (не речь или галлюцинация), адаптер её не видел")
        self.assertEqual(trace_view.untraced_reason({"status": "sent", "transcript": ""}, None), "записи вызова адаптера нет: текста нет, разбирать нечего")

    def test_a_clip_gets_only_the_calls_with_its_text(self):
        clip = {"ts": 100.0, "transcript": "Который час?"}
        calls = [{"id": 1, "ts": 90.0, "text": "который час"},    # before the clip window
                 {"id": 2, "ts": 101.0, "text": "который час"}, {"id": 3, "ts": 101.5, "text": "другое"}, {"id": 4, "ts": 400.0, "text": "который час"}]
        self.assertEqual([t["id"] for t in trace_view.traces_of_clip(clip, calls)], [2])
        unrelated = {"ts": 100.0, "transcript": "громче"}
        self.assertEqual(trace_view.traces_of_clip(unrelated, [{"id": 5, "ts": 108.0, "text": "поставь кондиционер на 24"}]), [],
                         "a neighbour's call is not this clip's")
        self.assertEqual([t["id"] for t in trace_view.traces_of_clip(clip, calls, until=100.5)], [])

    def test_neighbouring_clips_never_share_an_answer(self):
        first, second = {"ts": 100.0}, {"ts": 102.0}
        answers = [{"at": 101.5, "text": "Первый"}, {"at": 102.5, "text": "Второй"}]
        self.assertEqual([a["text"] for a in trace_view.evidence_of_clip(first, answers, "at", until=102.0)], ["Первый"])
        self.assertEqual([a["text"] for a in trace_view.evidence_of_clip(second, answers, "at", first=False)], ["Второй"])
        on_boundary = [{"at": 102.0, "text": "Второй"}]      # exactly the next clip's time: the next clip's, not both
        self.assertEqual(trace_view.evidence_of_clip(first, on_boundary, "at", until=102.0), [])
        self.assertEqual(len(trace_view.evidence_of_clip(second, on_boundary, "at", first=False)), 1)

    def test_the_last_clip_is_answerable_for_the_feeds_whole_deadline(self):
        clip = {"ts": 100.0}
        late = [{"at": 125.0, "text": "поздно"}, {"at": 150.0, "text": "слишком"}]
        self.assertEqual([a["text"] for a in trace_view.evidence_of_clip(clip, late, "at")], ["поздно"])


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
