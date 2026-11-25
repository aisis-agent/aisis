"""The adapter's writer of the classifier journal (classifier-journal/1.0, `request` records at revision r4) and of private incident events (aisis#250).

The records here are built by hand from the contract's field tables, not by the code under test, so a writer that drifts from the
contract fails. When the shared package (agent-tools `lib/agenttools_classifier_quality`) can be imported (set AGENTTOOLS_LIB to its
`lib` directory) the same records are also run through its strict validator, the one definition every reader uses.
"""
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest import mock

import classifier_journal as cj

REQUEST_ID = "7c1f0a52-3b6e-4d0e-9a41-2f8d5c6b7e10"
CALL_ID = "0d015c1e-8a3b-4f6d-9e2a-7b4c1d8f0001"
DECISION_ID = "3a9b6d2e-0c4f-4e8a-b1d7-5e2f9a8c4b61"
HASH = "a" * 64
COST0 = {"status": "known", "micro_usd": 0}


def request_record(**changes):
    record = {
        "schema_version": 1, "record_id": cj.record_id_for(REQUEST_ID, "request"), "phase": "request", "request_id": REQUEST_ID,
        "timestamp": "2026-11-24T20:00:00.000Z", "component": "aisis-adapter", "domain": "voice-command-domain", "language": "ru",
        "source": {"release": "unknown", "commit": "b" * 40, "diff_hash": None, "code_hash": HASH},
        "identity": {"prompt_hash": HASH, "runner": {"kind": "production", "job_ref": None}},
        "input_hash": HASH, "eligibility": "eligible",
        "model": {"provider": "laya", "name": "convaiinnovations/laya-multilingual", "engine": "laya-classic",
                  "checkpoint": "multilingual", "immutable_revision": "unknown"},
        "attempts": [
            {"call_id": CALL_ID, "stage": 0, "tier": "laya", "role": "main", "provider": "laya", "model": "convaiinnovations/laya-multilingual",
             "engine": "laya-classic", "checkpoint": "multilingual", "revision": "unknown", "status": "answered", "latency_ms": 180,
             "questions": 1, "cost": COST0},
            {"call_id": "0d025c1e-8a3b-4f6d-9e2a-7b4c1d8f0002", "stage": 0, "tier": "laya", "role": "control_positive", "provider": "laya",
             "model": "convaiinnovations/laya-multilingual", "status": "answered", "control_result": "pass", "questions": 1, "cost": COST0},
            {"call_id": "0d045c1e-8a3b-4f6d-9e2a-7b4c1d8f0004", "stage": 1, "tier": "jev", "role": "main", "provider": "jev",
             "model": "typesafe-ai/jev", "status": "unavailable", "reason_code": "missing_env:vercel_jev_key",
             "fallback_reason": "preflight", "cost": COST0}],
        "answer": {"primary_answered": True, "answering_call_id": CALL_ID, "answering_tier": "laya", "outcome": "accepted",
                   "typed_reason": None, "questions_asked": 1, "questions_answered": 1},
        "totals": {"latency_ms": 540, "cost": {"main": COST0, "controls": COST0, "failed_tiers": COST0, "audits": COST0}},
        "unknown_reasons": {"model.immutable_revision": "route_reports_no_revision"}}
    record.update(changes)
    return record


def proposed_record(**changes):
    record = {
        "schema_version": 1, "record_id": cj.record_id_for(REQUEST_ID, "proposed", DECISION_ID), "phase": "proposed",
        "request_id": REQUEST_ID, "timestamp": "2026-11-24T20:00:00.000Z", "component": "aisis-adapter", "decision_id": DECISION_ID,
        "use_case": "semantic-shadow", "domain": "voice-command-domain", "language": "ru", "call_id": CALL_ID, "question_ids": ["domain"],
        "policy_kind": "model", "policy_hash": HASH, "controlset_hash": HASH, "domain_spec_hash": HASH, "dataset_hash": "none",
        "split_hash": "none", "baseline_action": "unknown", "baseline_policy_hash": "unknown", "candidate_action": "light",
        "selection": {"reason_token": "main_answer_accepted", "abstained": False},
        "qualification_status": "none", "qualification_ref": "none", "rule_ref": "none"}
    record.update(changes)
    return record


def rebuilt(record, **changes):
    """`record` with `changes` and the identity re-derived, so only the content under test can make it invalid."""
    out = {**record, **changes}
    out["record_id"] = cj.record_id_for(out["request_id"], out["phase"], out.get("decision_id"))
    return out


class ValidatorTests(unittest.TestCase):
    def refused(self, record, path_part=None):
        with self.assertRaises(cj.JournalInvalid) as raised:
            cj.validate(record)
        if path_part:
            self.assertIn(path_part, raised.exception.path)

    def test_the_contracts_request_and_proposed_records_are_valid(self):
        cj.validate(request_record())
        cj.validate(proposed_record())

    def test_an_unregistered_key_is_refused_anywhere(self):
        self.refused(request_record(prompt_text="hello"), "prompt_text")
        record = request_record()
        record["answer"]["raw_reply"] = "x"
        self.refused(record, "answer.raw_reply")
        record = request_record()
        record["attempts"][0]["message"] = "x"
        self.refused(record, "attempts[0].message")

    def test_a_free_text_value_cannot_sit_in_a_token_field(self):
        self.refused(rebuilt(request_record(), domain="включи свет на кухне"), "domain")
        record = request_record()
        record["attempts"][0]["reason_code"] = "the user said turn on the light"
        self.refused(record, "reason_code")
        record = proposed_record()
        record["candidate_action"] = "turn on the light"
        self.refused(record, "candidate_action")
        record = proposed_record()
        record["question_ids"] = ["turn on the light"]
        self.refused(record, "question_ids")

    def test_secret_shaped_values_are_refused(self):
        for secret in ("sk-ant-api03-abcdefghijklmnop", "ghp_abcdefghijklmnopqrstuvwxyz0123", "Bearer abcdefghijkl"):
            with self.subTest(secret=secret):
                self.refused(rebuilt(request_record(), component=secret))
        record = request_record()
        record["model"]["name"] = "sk-ant-api03-abcdefghijklmnop"
        self.refused(record, "model.name")

    def test_hashes_ids_and_timestamps_have_their_formats(self):
        self.refused(rebuilt(request_record(), input_hash="not-a-hash"), "input_hash")
        self.refused(rebuilt(request_record(), request_id="not-a-uuid"))
        self.refused(rebuilt(request_record(), timestamp="2026-11-24 20:00:00"), "timestamp")
        self.refused(rebuilt(request_record(), schema_version=2), "schema_version")
        self.refused(rebuilt(request_record(), eligibility="maybe"), "eligibility")
        self.refused(rebuilt(request_record(), language="Russian"), "language")
        record = request_record()
        record["source"]["commit"] = "abc123"
        self.refused(record, "source.commit")

    def test_unknown_is_a_value_and_null_is_not(self):
        record = request_record()
        record["model"]["engine"] = "unknown"
        cj.validate(record)
        record["model"]["engine"] = None
        self.refused(record, "model.engine")
        record = request_record()
        record["answer"]["questions_answered"] = None
        self.refused(record, "questions_answered")

    def test_an_unknown_cost_is_null_and_never_zero(self):
        record = request_record()
        record["attempts"][0]["cost"] = {"status": "unknown", "micro_usd": 0}
        self.refused(record, "cost")
        record["attempts"][0]["cost"] = {"status": "unknown", "micro_usd": None}
        cj.validate(record)
        record["attempts"][0]["cost"] = {"status": "known", "micro_usd": None}
        self.refused(record, "cost")

    def test_the_identity_must_be_the_derived_one(self):
        self.refused(request_record(record_id=str(uuid.uuid4())), "record_id")
        self.refused(proposed_record(record_id=cj.record_id_for(REQUEST_ID, "proposed", str(uuid.uuid4()))), "record_id")

    def test_a_control_result_belongs_to_control_attempts_only(self):
        record = request_record()
        record["attempts"][0]["control_result"] = "pass"
        self.refused(record, "control_result")
        record = request_record()
        del record["attempts"][1]["control_result"]
        self.refused(record, "control_result")

    def test_the_jev_seat_is_an_actual_jev_provider(self):
        record = request_record()
        record["attempts"][2]["provider"] = "anthropic"
        self.refused(record, "provider")

    def test_the_answer_must_name_a_main_attempt_that_answered(self):
        record = request_record()
        record["answer"]["answering_call_id"] = record["attempts"][1]["call_id"]
        self.refused(record, "answering_call_id")
        record = request_record()
        record["answer"]["answering_call_id"] = str(uuid.uuid4())
        self.refused(record, "answering_call_id")
        record = request_record()
        record["answer"]["primary_answered"] = False
        self.refused(record)
        record["answer"].update(answering_call_id=None, outcome="unknown", questions_answered=0)
        cj.validate(record)

    def test_answered_questions_never_exceed_asked_ones(self):
        record = request_record()
        record["answer"]["questions_answered"] = 2
        self.refused(record, "questions_answered")

    def test_the_model_provider_is_the_answering_providers(self):
        record = request_record()
        record["model"]["provider"] = "jev"
        self.refused(record, "model.provider")

    def test_qualification_rules_hold_across_fields(self):
        cj.validate(proposed_record(qualification_status="provisional"))
        self.refused(proposed_record(qualification_status="qualified"), "qualification_ref")
        cj.validate(proposed_record(qualification_status="qualified", qualification_ref=HASH))
        self.refused(proposed_record(qualification_status="none", qualification_ref=HASH), "qualification_ref")
        self.refused(proposed_record(qualification_status="not_applicable"), "rule_ref")
        self.refused(proposed_record(rule_ref=HASH), "rule_ref")

    def test_an_abstention_proposes_no_action(self):
        self.refused(proposed_record(selection={"reason_token": "abstained", "abstained": True}), "candidate_action")
        cj.validate(proposed_record(selection={"reason_token": "abstained", "abstained": True}, candidate_action="unknown"))

    def test_a_model_threshold_needs_its_calibration_revision(self):
        selection = {"reason_token": "x", "abstained": False, "thresholds_applied": {"floor": {"value": 0.5, "calibrated_for_revision": None}}}
        self.refused(proposed_record(selection=selection), "thresholds_applied")
        selection["thresholds_applied"]["floor"]["calibrated_for_revision"] = "rev-1"
        cj.validate(proposed_record(selection=selection))

    def test_a_record_over_16_kib_is_refused(self):
        record = request_record()
        record["attempts"] = [{**record["attempts"][1], "call_id": str(uuid.uuid4())} for _ in range(64)]
        record["attempts"][0] = request_record()["attempts"][0]
        self.refused(record, "")

    def test_only_the_phases_the_adapter_writes_are_accepted(self):
        self.refused({**proposed_record(), "phase": "applied"}, "phase")
        self.refused([], "")


FIXTURES = Path(__file__).parent / "fixtures" / "classifier-journal-r2"


class ContractExampleTests(unittest.TestCase):
    """The contract's own published examples are valid records here, with the identities derived by the contract's namespace: the writer
    and the shared reader agree on what a record is. `request.json` is the example of revision r4 (unchanged since r2); `proposed.json` is
    the r2 example, which r4 superseded with `facts_revision`/`expected_target` (not written yet: aisis#302)."""

    def load(self, name):
        return json.loads((FIXTURES / name).read_text())

    def test_the_published_request_example_is_valid_and_its_identity_matches(self):
        cj.validate(self.load("request.json"))

    def test_the_published_proposed_example_is_valid_and_its_identity_matches(self):
        cj.validate(self.load("proposed.json"))

    def test_the_example_with_content_added_is_refused(self):
        example = self.load("request.json")
        example["attempts"][0]["raw_reply"] = "x"
        with self.assertRaises(cj.JournalInvalid):
            cj.validate(example)


class SharedSchemaConformanceTests(unittest.TestCase):
    """Identity and bytes agree with the shared package of agent-tools when it can be imported (set AGENTTOOLS_LIB to its `lib`)."""

    @classmethod
    def setUpClass(cls):
        lib = os.environ.get("AGENTTOOLS_LIB")
        if lib:
            sys.path.insert(0, lib)
        try:
            from agenttools_classifier_quality import journal_schema
        except ImportError:
            raise unittest.SkipTest("agenttools_classifier_quality is not importable (set AGENTTOOLS_LIB)") from None
        cls.shared = journal_schema

    def test_namespace_identity_and_canonical_bytes_are_the_shared_ones(self):
        self.assertEqual(cj.RECORD_NAMESPACE, self.shared.RECORD_NAMESPACE)
        self.assertEqual(cj.record_id_for(REQUEST_ID, "proposed", DECISION_ID), self.shared.record_id_for(REQUEST_ID, "proposed", DECISION_ID))
        self.assertEqual(cj.canonical_bytes(request_record()), self.shared.canonical_bytes(request_record()))


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name) / "classifier-quality"
        self.journal = cj.Journal(self.root)

    def files(self):
        return sorted(p.name for p in (self.root / "journal").iterdir())

    def test_one_file_per_record_named_by_its_identity_and_private(self):
        record = request_record()
        self.assertEqual(self.journal.write(record), cj.WRITTEN)
        path = self.root / "journal" / f"{record['record_id']}.json"
        self.assertEqual(self.files(), [path.name])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(json.loads(path.read_bytes()), record)
        self.assertLessEqual(path.stat().st_size, 16 * 1024)

    def test_an_identical_retry_is_a_noop_whatever_the_dict_order(self):
        record = request_record()
        self.assertEqual(self.journal.write(record), cj.WRITTEN)
        self.assertEqual(self.journal.write(dict(reversed(list(record.items())))), cj.NOOP)
        self.assertEqual(len(self.files()), 1)

    def test_the_same_record_written_from_many_threads_is_one_file_one_write_and_exact_counters(self):
        record = request_record()
        gate = threading.Barrier(16)
        statuses = []

        def worker():
            gate.wait()
            statuses.append(self.journal.write(record))

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(statuses), [cj.NOOP] * 15 + [cj.WRITTEN])
        self.assertEqual((self.journal.counts[cj.WRITTEN], self.journal.counts[cj.NOOP]), (1, 15))
        self.assertEqual(len(self.files()), 1)

    def test_a_different_payload_under_one_identity_is_a_conflict_kept_apart_and_never_overwrites(self):
        record = request_record()
        self.journal.write(record)
        changed = request_record(timestamp="2026-11-24T20:00:01.000Z")
        self.assertEqual(self.journal.write(changed), cj.CONFLICT)
        original = json.loads((self.root / "journal" / f"{record['record_id']}.json").read_bytes())
        self.assertEqual(original["timestamp"], "2026-11-24T20:00:00.000Z")
        self.assertEqual(len(list((self.root / "journal" / "conflicts").iterdir())), 1)
        self.assertEqual(self.journal.counts[cj.CONFLICT], 1)

    def test_a_new_phase_is_a_new_record(self):
        self.journal.write(request_record())
        self.assertEqual(self.journal.write(proposed_record()), cj.WRITTEN)
        self.assertEqual(len(self.files()), 2)

    def test_an_invalid_record_is_failed_and_nothing_is_written(self):
        self.assertEqual(self.journal.write(request_record(prompt_text="x")), cj.FAILED)
        self.assertFalse((self.root / "journal").exists() and self.files())
        self.assertEqual(self.journal.counts[cj.FAILED], 1)

    def test_an_unwritable_state_directory_is_failed_not_raised(self):
        blocker = Path(self.dir.name) / "file"
        blocker.write_text("")
        self.assertEqual(cj.Journal(blocker / "x").write(request_record()), cj.FAILED)

    def test_a_symlinked_journal_directory_is_refused(self):
        target = Path(self.dir.name) / "elsewhere"
        target.mkdir()
        self.root.mkdir()
        (self.root / "journal").symlink_to(target)
        self.assertEqual(self.journal.write(request_record()), cj.FAILED)
        self.assertEqual(list(target.iterdir()), [])

    def test_no_temp_file_is_left_behind(self):
        self.journal.write(request_record())
        self.journal.write(request_record())
        self.assertFalse([n for n in os.listdir(self.root / "journal") if n.startswith(".")])


class RetentionTests(unittest.TestCase):
    """The journal keeps 90 days and 64 MiB, oldest first, and remembers what it cut so a reader can say "history before X is gone"."""

    NOW = 1_800_000_000.0

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name) / "classifier-quality"
        self.clock = [self.NOW]
        self.journal = cj.Journal(self.root, clock=lambda: self.clock[0])

    def add(self, age_days):
        record = rebuilt(request_record(), request_id=str(uuid.uuid4()))
        self.assertEqual(self.journal.write(record), cj.WRITTEN)
        path = self.root / "journal" / f"{record['record_id']}.json"
        stamp = self.NOW - age_days * 86400
        os.utime(path, (stamp, stamp))
        return path

    def marker(self):
        return json.loads((self.root / "journal" / ".retention.json").read_text())

    def test_records_past_90_days_go_oldest_first_and_the_gap_is_remembered(self):
        old, older, fresh = self.add(100), self.add(120), self.add(1)
        self.assertEqual(self.journal.prune(self.NOW), 2)
        self.assertEqual((old.exists(), older.exists(), fresh.exists()), (False, False, True))
        marker = self.marker()
        self.assertEqual(marker["pruned_total"], 2)
        self.assertEqual(marker["pruned_through"], cj.iso_utc(self.NOW - 100 * 86400))
        self.assertEqual(stat.S_IMODE((self.root / "journal" / ".retention.json").stat().st_mode), 0o600)

    def test_the_size_bound_removes_the_oldest_and_keeps_the_newest(self):
        paths = [self.add(age) for age in (5, 4, 3, 2, 1)]
        size = paths[0].stat().st_size
        self.assertEqual(self.journal.prune(self.NOW, max_bytes=size * 2), 3)
        self.assertEqual([p.exists() for p in paths], [False, False, False, True, True])

    def test_a_later_prune_adds_to_the_total_and_never_moves_the_gap_back(self):
        self.add(100)
        self.journal.prune(self.NOW)
        self.add(95)
        self.journal.prune(self.NOW)
        self.assertEqual(self.marker()["pruned_total"], 2)
        self.assertEqual(self.marker()["pruned_through"], cj.iso_utc(self.NOW - 95 * 86400))
        self.add(200)
        self.journal.prune(self.NOW)
        self.assertEqual(self.marker()["pruned_through"], cj.iso_utc(self.NOW - 95 * 86400))

    def test_nothing_is_removed_inside_the_bounds_and_no_marker_is_written(self):
        fresh = self.add(1)
        self.assertEqual(self.journal.prune(self.NOW), 0)
        self.assertTrue(fresh.exists())
        self.assertFalse((self.root / "journal" / ".retention.json").exists())

    def test_only_record_files_are_touched(self):
        self.add(1)
        folder = self.root / "journal"
        (folder / "conflicts").mkdir()
        stranger = folder / "notes.txt"
        stranger.write_text("keep")
        conflict = folder / "conflicts" / ("0" * 8 + "-0000-0000-0000-" + "0" * 12 + "-" + "0" * 16 + ".json")
        conflict.write_text("{}")
        for path in (stranger, conflict):
            os.utime(path, (self.NOW - 400 * 86400,) * 2)
        self.journal.prune(self.NOW)
        self.assertTrue(stranger.exists() and conflict.exists())

    def test_a_missing_directory_is_nothing_to_prune_not_an_error(self):
        self.assertEqual(self.journal.prune(self.NOW), 0)

    def test_a_write_prunes_at_most_once_an_hour(self):
        old = self.add(100)
        self.assertTrue(old.exists())  # the first write pruned before this record aged
        self.clock[0] += 60
        self.add(1)
        self.assertTrue(old.exists())
        self.clock[0] += 3600
        self.add(1)
        self.assertFalse(old.exists())
        self.assertEqual(self.journal.pruned, 1)


class InputHashTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name) / "classifier-quality"

    def test_the_hash_is_keyed_stable_and_normalized(self):
        first = cj.input_hash(self.root, "Включи  свет")
        self.assertRegex(first, "^[0-9a-f]{64}$")
        self.assertEqual(first, cj.input_hash(self.root, "включи свет"))
        self.assertNotEqual(first, cj.input_hash(self.root, "выключи свет"))
        key = self.root / "journal-input.key"
        self.assertEqual((stat.S_IMODE(key.stat().st_mode), len(key.read_bytes())), (0o600, 32))

    def test_another_install_key_gives_another_hash_so_the_text_cannot_be_guessed_from_the_journal(self):
        other = Path(self.dir.name) / "other"
        self.assertNotEqual(cj.input_hash(self.root, "включи свет"), cj.input_hash(other, "включи свет"))

    def test_the_hash_is_not_a_plain_digest_of_the_text(self):
        import hashlib
        self.assertNotEqual(cj.input_hash(self.root, "включи свет"), hashlib.sha256("включи свет".encode()).hexdigest())

    def test_a_key_file_others_can_read_is_not_trusted(self):
        cj.input_hash(self.root, "x")
        os.chmod(self.root / "journal-input.key", 0o644)
        self.assertEqual(cj.input_hash(self.root, "x"), "unknown")

    def test_an_unwritable_root_is_unknown_never_a_default_hash(self):
        blocker = Path(self.dir.name) / "file"
        blocker.write_text("")
        self.assertEqual(cj.input_hash(blocker / "x", "x"), "unknown")


class IncidentSinkTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name) / "classifier-quality"
        self.now = [1_700_000_000.0]
        self.sink = cj.IncidentSink(self.root, clock=lambda: self.now[0])

    def emit(self, **changes):
        args = dict(component="aisis-adapter", domain="voice-command-domain", provider="jev", model="typesafe-ai/jev",
                    kind="provider_unavailable", severity="warning", reason_code="missing_env:vercel_jev_key", version_identity="v1",
                    request_id=REQUEST_ID, provenance={"tier": "jev", "role": "main"}, counts={"calls": 1})
        args.update(changes)
        return self.sink.emit(**args)

    def events(self):
        return [json.loads(p.read_bytes()) for p in sorted((self.root / "events").iterdir())]

    def test_an_event_has_the_incident_v1_fields_and_names_the_variable_not_a_value(self):
        self.assertTrue(self.emit())
        (event,) = self.events()
        self.assertEqual(sorted(event), sorted(["schema_version", "event_id", "request_id", "timestamp", "component", "domain", "provider",
                                                "model", "kind", "severity", "reason_code", "fingerprint", "provenance", "aggregate_counts"]))
        self.assertEqual((event["schema_version"], event["reason_code"], event["request_id"]), (1, "missing_env:vercel_jev_key", REQUEST_ID))
        self.assertRegex(event["fingerprint"], "^[0-9a-f]{64}$")
        self.assertRegex(event["timestamp"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        path = next((self.root / "events").iterdir())
        self.assertEqual((stat.S_IMODE(path.stat().st_mode), stat.S_IMODE(path.parent.stat().st_mode)), (0o600, 0o700))
        self.assertRegex(path.name, r"^\d{8}T\d{9}Z-w-[0-9a-f-]{36}\.json$")

    def test_a_repeat_inside_an_hour_is_folded_and_counted_on_the_next_event(self):
        self.assertTrue(self.emit())
        self.assertFalse(self.emit(request_id=str(uuid.uuid4())))
        self.assertFalse(self.emit(request_id=str(uuid.uuid4())))
        self.assertEqual((self.sink.written, self.sink.suppressed), (1, 2))
        self.now[0] += 3601
        self.assertTrue(self.emit())
        self.assertEqual(self.events()[-1]["aggregate_counts"]["suppressed_repeats"], 2)

    def test_the_same_cause_at_another_severity_is_its_own_incident(self):
        self.assertTrue(self.emit())
        self.assertTrue(self.emit(severity="critical"))
        self.assertTrue(next(p for p in (self.root / "events").iterdir() if "-c-" in p.name))

    def test_requests_on_worker_threads_at_the_start_of_an_outage_write_one_incident(self):
        gate = threading.Barrier(16)

        def worker():
            gate.wait()
            self.emit(request_id=str(uuid.uuid4()))

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual((self.sink.written, self.sink.suppressed, len(self.events())), (1, 15, 1))

    def test_a_changed_release_is_a_new_fingerprint(self):
        self.emit()
        self.assertTrue(self.emit(version_identity="v2"))

    def test_an_unwritable_store_is_counted_and_never_raised(self):
        blocker = Path(self.dir.name) / "file"
        blocker.write_text("")
        sink = cj.IncidentSink(blocker / "x")
        self.assertFalse(sink.emit(component="c", domain="d", provider="p", model="m", kind="provider_failed", severity="critical",
                                   reason_code="x", version_identity="v", request_id=REQUEST_ID, provenance={}, counts={}))
        self.assertEqual(sink.failed, 1)

    def test_warnings_are_removed_before_a_critical_incident_when_the_cap_is_hit(self):
        with mock.patch.object(cj, "MAX_EVENT_FILES", 3):
            self.emit(severity="critical", reason_code="a")
            for index in range(4):
                self.now[0] += 1
                self.emit(reason_code=f"w{index}")
        names = sorted(p.name for p in (self.root / "events").iterdir())
        self.assertEqual(len(names), 3)
        self.assertTrue(any("-c-" in name for name in names))

    def test_the_shared_guardian_schema_accepts_an_event(self):
        lib = os.environ.get("AGENTTOOLS_GUARDIAN_LIB")
        if not lib:
            self.skipTest("set AGENTTOOLS_GUARDIAN_LIB to the guardian package's `lib` to run against its incident schema")
        sys.path.insert(0, lib)
        from agenttools_classifier_quality import schema
        self.emit()
        (path,) = (self.root / "events").iterdir()
        schema.validate_event(path.read_bytes())


if __name__ == "__main__":
    unittest.main()
