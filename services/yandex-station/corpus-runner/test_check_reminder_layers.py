"""The corpus bridge checks adapter semantics without weakening its fast-layer contract."""
import dataclasses
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import check_reminder_layers as checker
except ModuleNotFoundError:
    checker = None

ADAPTER = Path(__file__).resolve().parents[1] / "code"


def row_bytes(text, *, kind="cmd"):
    return json.dumps(
        {"text": text, "expect": {"kind": kind, "domain": "timer", "tier": "execute"}},
        ensure_ascii=False,
    ).encode()


class ReminderLayerTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(checker, "The actual adapter-layer contract checker is missing")
        self.grammar, self.sources = checker.load_adapter(ADAPTER)

    def contract(self, raw, seconds=600, note="прочитать книгу"):
        text = json.loads(raw)["text"]
        return checker.CaseContract(
            row=1,
            row_sha256=hashlib.sha256(raw).hexdigest(),
            text_sha256=hashlib.sha256(text.encode()).hexdigest(),
            seconds=seconds,
            note_sha256=hashlib.sha256(note.encode()).hexdigest(),
        )

    def test_actual_grammar_keeps_reminder_as_note_without_device_execution(self):
        raw = row_bytes("напомни прочитать книгу через десять минут")
        proof = checker.check_case(self.grammar, raw, self.contract(raw))
        self.assertEqual(proof["adapter_intent"], "reminder.in")
        self.assertEqual(proof["adapter_tier"], "execute")
        self.assertEqual(proof["seconds"], 600)
        self.assertEqual(proof["note_sha256"], hashlib.sha256("прочитать книгу".encode()).hexdigest())
        self.assertEqual(proof["action_executor"], "timer.start")
        self.assertEqual(proof["action_keys"], ["note", "seconds"])
        self.assertEqual(proof["fast_expectation"], {"kind": "llm", "tier": "llm"})
        self.assertTrue(proof["no_immediate_device_action"])
        self.assertFalse(proof["fuzzy"])
        self.assertEqual(proof["parts"], 0)
        self.assertNotIn("прочитать", json.dumps(proof, ensure_ascii=False))

    def test_general_reminder_grammar_preserves_a_two_hour_duration(self):
        raw = row_bytes("через два часа напомни мне проверить почту")
        proof = checker.check_case(
            self.grammar, raw, self.contract(raw, seconds=7200, note="проверить почту")
        )
        self.assertEqual(proof["seconds"], 7200)
        self.assertEqual(proof["adapter_intent"], "reminder.in")

    def test_changed_raw_row_is_not_covered_by_an_old_hash(self):
        raw = row_bytes("напомни прочитать книгу через десять минут")
        with self.assertRaisesRegex(checker.ContractError, "row hash"):
            checker.check_case(self.grammar, raw + b" ", self.contract(raw))

    def test_original_execute_expectation_cannot_be_relabelled(self):
        raw = row_bytes("напомни прочитать книгу через десять минут", kind="llm")
        with self.assertRaisesRegex(checker.ContractError, "original expectation"):
            checker.check_case(self.grammar, raw, self.contract(raw))

    def test_changed_phrase_is_not_covered_by_an_old_text_hash(self):
        raw = row_bytes("напомни прочитать книгу через десять минут")
        contract = dataclasses.replace(self.contract(raw), text_sha256="0" * 64)
        with self.assertRaisesRegex(checker.ContractError, "text hash"):
            checker.check_case(self.grammar, raw, contract)

    def test_actual_duration_must_match_the_independent_contract(self):
        raw = row_bytes("напомни прочитать книгу через десять минут")
        with self.assertRaisesRegex(checker.ContractError, "duration"):
            checker.check_case(self.grammar, raw, self.contract(raw, seconds=7200))

    def test_actual_note_must_match_the_independent_contract(self):
        raw = row_bytes("напомни прочитать книгу через десять минут")
        with self.assertRaisesRegex(checker.ContractError, "note"):
            checker.check_case(self.grammar, raw, self.contract(raw, note="другая задача"))

    def test_a_regular_device_command_cannot_be_accepted_as_a_reminder(self):
        raw = row_bytes("включи свет")
        with self.assertRaisesRegex(checker.ContractError, "reminder"):
            checker.check_case(self.grammar, raw, self.contract(raw))

    def test_a_reminder_action_cannot_immediately_control_a_device(self):
        # Real matcher, deliberately invalid input grammar: the checker owns this boundary.
        data = json.loads((ADAPTER / "intent_grammar/grammar/ru.json").read_text())["data"]
        reminder = next(intent for intent in data["intents"] if intent["id"] == "reminder.in")
        reminder["action"] = {"executor": "ha.switch", "args": {"entity_id": "switch.example"}}
        engine = sys.modules[type(self.grammar).__module__]
        grammar = engine.from_data(data, self.grammar.sha256)
        raw = row_bytes("напомни прочитать книгу через десять минут")
        with self.assertRaisesRegex(checker.ContractError, "exact deferred timer note"):
            checker.check_case(grammar, raw, self.contract(raw))

    def test_a_changed_action_note_cannot_pass_on_slots_alone(self):
        data = json.loads((ADAPTER / "intent_grammar/grammar/ru.json").read_text())["data"]
        reminder = next(intent for intent in data["intents"] if intent["id"] == "reminder.in")
        reminder["action"]["args"]["note"] = "different note"
        engine = sys.modules[type(self.grammar).__module__]
        grammar = engine.from_data(data, self.grammar.sha256)
        raw = row_bytes("напомни прочитать книгу через десять минут")
        with self.assertRaisesRegex(checker.ContractError, "exact deferred timer note"):
            checker.check_case(grammar, raw, self.contract(raw))

    def test_compound_reminders_are_not_one_exact_deferred_action(self):
        raw = row_bytes("напомни прочитать книгу через десять минут и напомни проверить почту через два часа")
        with self.assertRaises(checker.ContractError):
            checker.check_case(self.grammar, raw, self.contract(raw))

    def test_compiled_grammar_cannot_hide_changed_yaml_provenance(self):
        with tempfile.TemporaryDirectory(prefix="reminder-adapter-test-") as directory:
            adapter = Path(directory)
            for name in checker.SOURCE_FILES:
                destination = adapter / "intent_grammar" / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ADAPTER / "intent_grammar" / name, destination)
            source = adapter / "intent_grammar/grammar/ru.yaml"
            source.write_bytes(source.read_bytes() + b"\n")
            with self.assertRaisesRegex(checker.ContractError, "YAML provenance"):
                checker.load_adapter(adapter)

    def test_bound_grammar_requires_all_eight_actual_source_files(self):
        self.assertEqual(
            set(self.sources),
            {
                "intent_grammar/__init__.py",
                "intent_grammar/grammar.py",
                "intent_grammar/normalize.py",
                "intent_grammar/pattern.py",
                "intent_grammar/numerals.py",
                "intent_grammar/timewords.py",
                "intent_grammar/grammar/ru.json",
                "intent_grammar/grammar/ru.yaml",
            },
        )
        for record in self.sources.values():
            self.assertRegex(record["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            self.grammar.sha256,
            self.sources["intent_grammar/grammar/ru.yaml"]["sha256"],
        )

    def test_cli_fails_closed_without_a_matching_private_corpus(self):
        with tempfile.TemporaryDirectory(prefix="reminder-public-test-") as directory:
            corpus = Path(directory) / "synthetic.jsonl"
            corpus.write_bytes(row_bytes("напомни прочитать книгу через десять минут") + b"\n")
            run = subprocess.run(
                [
                    sys.executable, "-I", "-S", "-B", str(Path(checker.__file__)),
                    "--corpus", str(corpus), "--adapter-dir", str(ADAPTER),
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(run.stdout, "")
        self.assertIn("private_log=", run.stderr)
        self.assertNotIn("прочитать", run.stderr)


if __name__ == "__main__":
    unittest.main()
