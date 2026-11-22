"""The brain line of a trace: every key as one icon, the time spent on keys, and a short note for each key that is not available."""
from datetime import datetime, timezone
import os
import time
import unittest

import trace_view

TS = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc).timestamp()


def at(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


def keys(*states):
    return [{"slot": slot, "ok": until is None, "until": until} for slot, until in states]


class BrainLine(unittest.TestCase):
    def setUp(self):
        previous = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()

        def restore():
            if previous is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous
            time.tzset()
        self.addCleanup(restore)

    def line(self, brain, ts=TS):
        return trace_view.brain_line({"provider": "claude", "model": "m", **brain}, ts)

    def test_a_key_paused_until_a_date_reads_as_icons_time_and_the_date(self):
        brain = {"slot": "calendar", "attempts": [{"slot": "finance", "result": "billing_unavailable", "s": 0.392},
                                                  {"slot": "calendar", "result": "complete", "s": 1.591}],
                 "keys": keys(("finance", at("2026-12-01T00:00:00")), ("calendar", None), ("report", None))}
        self.assertEqual(self.line(brain), "ИИ claude m слот calendar · 🔑 ❌✅✅ · 1.983 с · ключ 1 (finance) пропущен до 1 дек")

    def test_a_key_skipped_without_a_request_costs_no_time(self):
        brain = {"slot": "calendar", "attempts": [{"slot": "finance", "result": "cooldown"}, {"slot": "calendar", "result": "complete", "s": 1.5}],
                 "keys": keys(("finance", at("2026-12-01T00:00:00")), ("calendar", None), ("report", None))}
        self.assertEqual(self.line(brain), "ИИ claude m слот calendar · 🔑 ❌✅✅ · 1.5 с · ключ 1 (finance) пропущен до 1 дек")

    def test_a_pause_within_the_day_names_the_clock_time_and_a_longer_one_the_date(self):
        for until, expected in ((TS + 3 * 3600, "до 15:00"), (TS + 12 * 3600, "до 00:00"), (TS + 24 * 3600, "до 23 ноя")):
            with self.subTest(until=until):
                brain = {"attempts": [{"slot": "a", "result": "cooldown"}], "keys": keys(("a", until))}
                self.assertIn("ключ 1 (a) пропущен " + expected, self.line(brain))

    def test_a_short_failure_names_its_reason_instead_of_a_time(self):
        brain = {"slot": "b", "attempts": [{"slot": "a", "result": "server_unavailable", "s": 0.2}, {"slot": "b", "result": "complete", "s": 1.0}],
                 "keys": keys(("a", TS + 15), ("b", None))}
        self.assertEqual(self.line(brain), "ИИ claude m слот b · 🔑 ❌✅ · 1.2 с · ключ 1 (a): сервер недоступен")

    def test_every_key_down_shows_the_reserve_answered(self):
        brain = {"provider": "chatgpt_web", "attempts": [{"slot": s, "result": "billing_unavailable", "s": 0.3} for s in "abc"],
                 "keys": keys(*((s, TS + 3600) for s in "abc"))}
        text = self.line(brain)
        self.assertIn("🔑 ❌❌❌ · 0.9 с · ключ 1 (a) пропущен до 13:00; ключ 2 (b) пропущен до 13:00; ключ 3 (c) пропущен до 13:00", text)

    def test_without_the_key_list_the_attempts_alone_give_the_icons(self):
        brain = {"slot": "a", "attempts": [{"slot": "a", "result": "complete", "s": 1.5}], "memories": 2}
        self.assertEqual(self.line(brain), "ИИ claude m слот a · 🔑 ✅ · 1.5 с · память 2")
        failed = {"attempts": [{"slot": "a", "result": "rate_limit", "s": 0.1}, {"slot": "b", "result": "complete", "s": 1.0}]}
        self.assertIn("🔑 ❌✅ · 1.1 с · ключ 1 (a): лимит запросов", self.line(failed))

    def test_an_unknown_result_code_is_shown_as_is(self):
        self.assertIn("ключ 1 (a): weird_code", self.line({"attempts": [{"slot": "a", "result": "weird_code", "s": 0.1}]}))

    def test_a_brain_without_keys_has_no_key_part(self):
        self.assertEqual(self.line({"provider": "chatgpt_web"}), "ИИ chatgpt_web m")

    def test_malformed_keys_never_break_the_line(self):
        for keys_value in ("x", [None, 3, {"slot": 5}], [{"slot": "a", "ok": True, "until": "soon"}]):
            with self.subTest(keys=keys_value):
                self.assertTrue(self.line({"keys": keys_value, "attempts": [{"slot": "a", "result": "complete", "s": 1}]}).startswith("ИИ claude m"))

    def test_an_until_outside_the_calendar_is_not_a_date_and_never_raises(self):
        for until in (1e12, -1e12, 1.76e12, 0):
            with self.subTest(until=until):
                line = self.line({"keys": [{"slot": "a", "ok": False, "until": until}], "attempts": [{"slot": "a", "result": "cooldown", "s": 0.1}]})
                self.assertIn("ключ 1 (a)", line)
                self.assertNotIn("пропущен до", line)

    def test_a_slot_that_is_not_a_string_never_raises(self):
        brain = {"keys": [{"slot": "a", "ok": False}], "attempts": [{"slot": ["a"], "result": "cooldown", "s": 0.1}, {"slot": {"x": 1}, "result": "complete"}]}
        self.assertTrue(self.line(brain).startswith("ИИ claude m"))
        self.assertTrue(self.line({"attempts": brain["attempts"]}).startswith("ИИ claude m"))

    def test_extras_are_joined_with_the_same_dot_as_the_rest(self):
        brain = {"persona": True, "memories": 5, "prompt_chars": 4721}
        self.assertEqual(self.line(brain), "ИИ claude m · персона · память 5 · промпт 4721 симв.")

    def test_lines_pass_the_trace_time_so_a_pause_reads_the_same_whenever_it_is_opened(self):
        trace = {"layer": "brain", "ts": TS, "brain": {"provider": "claude", "slot": "b", "attempts": [{"slot": "a", "result": "cooldown"}, {"slot": "b", "result": "complete", "s": 1}],
                                                        "keys": keys(("a", TS + 3 * 3600), ("b", None))}}
        self.assertIn("🤖 ИИ claude слот b · 🔑 ❌✅ · 1 с · ключ 1 (a) пропущен до 15:00", trace_view.lines(trace))


if __name__ == "__main__":
    unittest.main()
