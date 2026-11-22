"""Diagnostics, the brain's prompts and the stored labels name what was done and which aisis layer did it; the speaker is only
microphones, a loudspeaker and a wake-word spotter, so no text attributes knowledge or action to it, and «навык» (a Yandex Alice
skill, which aisis does not use) names nothing. The check reads the label tables themselves, so a new label cannot slip in."""
import os
import re
import sqlite3
import tempfile
import unittest

import clipfeed
import dialog_context
import trace_view
import user_prompt
import voiceclips

SPEAKER_ACTS = re.compile(r"навык|ход колонки|сама колонка|колонка сама|самой колонк|сам квазар|колонк\w* (выполня|знает|спрос|сдела|умеет)",
                          re.IGNORECASE)
NO_SPEAKER_AT_ALL = re.compile(r"колонк", re.IGNORECASE)


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in list(value.keys()) + list(value.values()):
            yield from strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from strings(item)
    elif hasattr(value, "pattern"):
        return


class DiagnosticLabels(unittest.TestCase):
    TABLES = {"LAYERS": trace_view.LAYERS, "VIA": trace_view.VIA, "ASKED": trace_view.ASKED, "SPOTTER_RAN_IT": trace_view.SPOTTER_RAN_IT,
              "DECISIONS": trace_view.DECISIONS, "TRIGGERS": trace_view.TRIGGERS, "NO_QUESTION": trace_view.NO_QUESTION,
              "DIALOG_STATES": trace_view.DIALOG_STATES, "OUTCOMES": trace_view.OUTCOMES, "CUES": trace_view.CUES,
              "ENGINES": trace_view.ENGINES, "EVIDENCE_SOURCES": trace_view.EVIDENCE_SOURCES,
              "DECLINED": [text for _pattern, text in trace_view.DECLINED], "CUT_BY": clipfeed.CUT_BY,
              "SPOTTER_ENGINE": voiceclips.SPOTTER_ENGINE}

    def test_no_label_says_the_speaker_does_or_knows_something_or_names_an_alice_skill(self):
        for name, table in self.TABLES.items():
            for text in strings(table):
                self.assertIsNone(SPEAKER_ACTS.search(text), f"{name}: {text!r}")
                self.assertIsNone(NO_SPEAKER_AT_ALL.search(text), f"{name}: {text!r}")

    def test_the_adapters_own_handlers_are_named_as_such_in_the_route_and_the_stage_lines(self):
        trace = {"layer": "skill", "intent": "skill.weather", "tier": "query", "conf": 1.0, "stages_ms": {"skills": 40, "total": 45}}
        text = "\n".join(trace_view.lines(trace))
        self.assertIn("встроенный обработчик", text)
        self.assertIn("⏱ обработчики 40 · всего 45 мс", text)
        self.assertIsNone(SPEAKER_ACTS.search(text), text)

    def test_the_turn_through_the_adapter_is_a_text_request_not_the_speakers_turn(self):
        turn = {"via": "turn", "layer": "skill", "text": "какая погода"}
        self.assertEqual(trace_view.answered_by("какая погода", [turn], None, []), "встроенный обработчик (текстовый запрос к адаптеру)")


class PromptsToTheBrain(unittest.TestCase):
    def test_the_capabilities_block_describes_the_layers_not_a_speaker_that_acts(self):
        self.assertIsNone(NO_SPEAKER_AT_ALL.search(user_prompt.CAPABILITIES), user_prompt.CAPABILITIES)
        self.assertIsNone(SPEAKER_ACTS.search(user_prompt.CAPABILITIES))
        self.assertIn("до тебя", user_prompt.CAPABILITIES, "the brain still learns that these commands never reach it")
        self.assertIn("Алис", user_prompt.CAPABILITIES, "and still never sends the owner to an Alice command or app")

    def test_the_notes_to_the_brain_do_not_make_the_speaker_ask_or_own_the_dialogs(self):
        self.assertIsNone(NO_SPEAKER_AT_ALL.search(dialog_context.RECENT_HEADER), dialog_context.RECENT_HEADER)
        self.assertIsNone(NO_SPEAKER_AT_ALL.search(dialog_context.question_block("Вы имели в виду «включи свет»?")))


class StoredSpotterEngine(unittest.TestCase):
    LEGACY = "колонка сама (споттер команд)"

    def test_clips_stored_under_the_old_engine_label_are_renamed_once_and_keep_their_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "dialogs.sqlite3")
            raw = sqlite3.connect(db)
            raw.execute(voiceclips.SCHEMA[0])
            raw.executemany("INSERT INTO voice_clips(id, ts, path, status, engine, trigger) VALUES(?,?,?,?,?,?)",
                            [("a", 1.0, "a.opus", "sent", self.LEGACY, "spotter"), ("b", 2.0, "b.opus", "sent", "faster-whisper", "vad")])
            raw.commit()
            raw.close()
            voiceclips.connect(db).close()
            voiceclips.connect(db).close()      # idempotent
            conn = sqlite3.connect(db)
            engines = dict(conn.execute("SELECT id, engine FROM voice_clips"))
            conn.close()
            self.assertEqual(engines, {"a": voiceclips.SPOTTER_ENGINE, "b": "faster-whisper"})
            self.assertNotEqual(voiceclips.SPOTTER_ENGINE, self.LEGACY)


if __name__ == "__main__":
    unittest.main()
