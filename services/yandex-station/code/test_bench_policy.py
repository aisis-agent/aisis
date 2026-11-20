"""The policy calibration (bench_policy.py): the statistics, the grammar's action_of, the engine's veto and risk handling around `repair.decide`,
the per-split counting, the n-best cache (resume, a failing worker), the selection criterion and the grid. No recogniser, worker process, audio or
grammar of the real bundle except where a test says so: the GigaAM client is a fake."""
import asyncio
import contextlib
import io
import json
import math
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

try:
    import confusables_corpus as corpus
    from stt_cli.hypotheses.giga import GigaError
    from stt_cli.hypotheses.model import NBest
    from stt_cli.hypotheses.repair import Policy

    import bench_policy as bp
except ImportError as error:        # stt_cli (the shared core) is not pinned by aisis yet: PYTHONPATH=<stt-cli checkout>
    bp = None
    SKIP = f"needs stt_cli.hypotheses and confusables_corpus on the path: {error}"
else:
    SKIP = ""


def decision(intent="x", tier="execute", executor=None, args=None, parts=()):
    action = {"executor": executor, "args": args or {}} if executor else None
    return SimpleNamespace(intent=intent, tier=tier, action=action, parts=tuple(parts))


LIGHT_ON = decision("light.on", executor="ha.light", args={"state": "on", "room": None})
LIGHT_OFF = decision("light.off", executor="ha.light", args={"state": "off", "room": None})
KETTLE_ON = decision("device.on", executor="ha.device", args={"device": "kettle", "state": "on"})
KETTLE_OFF = decision("device.off", executor="ha.device", args={"device": "kettle", "state": "off"})
SCRIPT = {"включи свет": LIGHT_ON, "выключи свет": LIGHT_OFF, "включи чайник": KETTLE_ON, "выключи чайник": KETTLE_OFF,
          "продолжение следует": decision("asr.hallucination", tier="drop"), "вызови скорую": decision("refuse.emergency", tier="canned")}


def fake_actions(script=None):
    known = script or SCRIPT
    return bp.Actions(lambda text: known.get(corpus.normalize(text)))


def nbest(*items):
    """(text, share) pairs -> an NBest whose posteriors are those shares (log-probabilities of a normalised list)."""
    return NBest.from_logprobs([(text, math.log(share)) for text, share in items], engine="fake", fits={text: 0.9 for text, _ in items})


def case(primary, items, polarity="b", pair_set="light", kind="whisper", **extra):
    base = {"id": "c1", "split": "synthetic", "kind": kind, "primary": primary, "nbest": nbest(*items), "pair_set": pair_set, "polarity": polarity,
            "voice": "denis", "augmentation": "station", "truth": "выключи свет", "reachable": True}
    return bp.Case(**{**base, **extra})


@unittest.skipIf(bp is None, SKIP)
class Statistics(unittest.TestCase):
    def test_wilson_matches_the_textbook_values(self):
        low, high = bp.wilson(5, 10)
        self.assertAlmostEqual(low, 0.2366, places=3)
        self.assertAlmostEqual(high, 0.7634, places=3)
        low, high = bp.wilson(0, 10)
        self.assertEqual(low, 0.0)
        self.assertAlmostEqual(high, 0.2775, places=3)

    def test_wilson_with_no_trials_knows_nothing(self):
        self.assertEqual(bp.wilson(0, 0), (0.0, 1.0))

    def test_wilson_stays_inside_the_unit_interval_at_the_edges(self):
        for k, n in ((0, 7), (7, 7), (1, 4032)):
            low, high = bp.wilson(k, n)
            self.assertTrue(0.0 <= low <= k / n <= high <= 1.0, (k, n))

    def test_a_cell_always_carries_its_n(self):
        self.assertEqual(bp.frac(7, 4032), "7/4032 (0.2%)")
        self.assertEqual(bp.frac(0, 0), "0/0 (-)")
        self.assertRegex(bp.frac(3, 10, True), r"^3/10 \(30\.0%\) \[10\.8-60\.3\]$")


@unittest.skipIf(bp is None, SKIP)
class GrammarActions(unittest.TestCase):
    def test_the_key_is_the_executor_and_the_sorted_arguments_and_the_same_for_the_same_action(self):
        a = decision(executor="ha.light", args={"room": None, "state": "on"})
        b = decision(executor="ha.light", args={"state": "on", "room": None})
        self.assertEqual(bp.action_key(a), bp.action_key(b))
        self.assertEqual(bp.action_key(a), 'ha.light|{"room": null, "state": "on"}')
        self.assertNotEqual(bp.action_key(LIGHT_ON), bp.action_key(LIGHT_OFF))

    def test_no_decision_and_non_actions_have_no_key(self):
        self.assertIsNone(bp.action_key(None))
        for tier in ("drop", "reprompt", "clarify"):
            self.assertIsNone(bp.action_key(decision(tier=tier, executor="ha.light")), tier)
        self.assertIsNone(bp.action_key(decision("refuse.calls", tier="canned")))

    def test_a_compound_is_one_key_only_when_every_clause_is_an_action(self):
        both = decision(parts=(LIGHT_ON, LIGHT_OFF))
        self.assertEqual(bp.action_key(both), bp.action_key(LIGHT_ON) + "+" + bp.action_key(LIGHT_OFF))
        self.assertIsNone(bp.action_key(decision(parts=(LIGHT_ON, decision("refuse.calls", tier="canned")))))

    def test_the_strict_variant_counts_only_execute_and_query(self):
        guess = decision("media.pause", tier="confirm", executor="media.key", args={"key": "pause"})
        self.assertIsNotNone(bp.action_key(guess))
        self.assertIsNone(bp.execute_query_key(guess))
        self.assertIsNotNone(bp.execute_query_key(LIGHT_ON))
        self.assertIsNotNone(bp.execute_query_key(decision("q", tier="query", executor="skill.math")))

    def test_emergency_and_dropped_speech_are_protected_and_a_kettle_is_risky(self):
        actions = fake_actions()
        self.assertTrue(actions.protected("Вызови скорую!"))
        self.assertTrue(actions.protected("Продолжение следует."))
        self.assertFalse(actions.protected("включи свет"))
        self.assertTrue(actions.risky("включи чайник"))
        self.assertFalse(actions.risky("включи свет"))
        self.assertFalse(actions.risky("что-то совсем другое"))

    def test_the_text_is_classified_once(self):
        calls = []
        actions = bp.Actions(lambda text: calls.append(text) or LIGHT_ON)
        for _ in range(3):
            actions.key("включи свет"), actions.protected("включи свет"), actions.risky("включи свет")
        self.assertEqual(calls, ["включи свет"])

    def test_the_shipped_grammar_tells_the_light_pair_apart_and_has_no_fan(self):
        try:
            actions = bp.grammar_actions()
        except (SystemExit, ImportError) as error:
            self.skipTest(f"grammar bundle unavailable: {error}")
        self.assertIsNotNone(actions.key("включи свет"))
        self.assertNotEqual(actions.key("включи свет"), actions.key("выключи свет"))
        self.assertIsNone(actions.key("включи вентилятор"))
        self.assertFalse(actions.risky("включи свет"))
        self.assertTrue(actions.risky("включи чайник"))
        self.assertTrue(actions.protected("вызови скорую"))


@unittest.skipIf(bp is None, SKIP)
class Ids(unittest.TestCase):
    def test_the_opposite_clip_has_the_same_phrasing_voice_and_augmentation(self):
        row = {"id": "light-kitchen-a2-irina-snr10", "pair_set": "light-kitchen", "polarity": "a"}
        self.assertEqual(bp.opposite_id(row), "light-kitchen-b2-irina-snr10")
        self.assertEqual(bp.opposite_id({**row, "id": "yes-no-b0-denis-far", "pair_set": "yes-no", "polarity": "b"}), "yes-no-a0-denis-far")

    def test_every_manifest_clip_has_an_opposite_in_the_manifest(self):
        ids = {row["id"] for row in corpus.manifest_rows()}
        self.assertTrue(all(bp.opposite_id(row) in ids for row in corpus.manifest_rows()))


@unittest.skipIf(bp is None, SKIP)
class Seen(unittest.TestCase):
    def test_the_primary_is_right_flipped_or_neither(self):
        self.assertEqual(case("Выключи свет.", [("выключи свет", 1)]).seen, "right")
        self.assertEqual(case("Включи свет.", [("выключи свет", 1)]).seen, "flip")
        self.assertEqual(case("Продолжение следует.", [("выключи свет", 1)]).seen, "neither")
        self.assertEqual(case("включи и выключи свет", [("выключи свет", 1)]).seen, "neither")

    def test_exact_means_the_truth_phrase_itself_and_a_misheard_word_is_only_the_right_member(self):
        exact = case("Выключи свет.", [("выключи свет", 1)])
        self.assertTrue(exact.exact)
        self.assertEqual(exact.classes(), ("right", "exact"))
        misheard = case("выключи свет в спании", [("выключи свет", 1)], pair_set="light-bedroom", truth="выключи свет в спальне")
        self.assertEqual((misheard.seen, misheard.exact, misheard.classes()), ("right", False, ("right",)))
        self.assertEqual(case("Включи свет.", [("выключи свет", 1)]).classes(), ("flip",))

    def test_a_real_clip_has_no_member_to_judge(self):
        self.assertEqual(bp.Case("r1", "real", "whisper", "привет", nbest()).seen, "unlabelled")


@unittest.skipIf(bp is None, SKIP)
class EngineDecision(unittest.TestCase):
    def setUp(self):
        self.actions = fake_actions()

    def test_a_flipped_primary_with_the_truth_on_top_is_asked_with_the_truth(self):
        c = bp.arm(case("Включи свет.", [("выключи свет", 0.9), ("включи свет", 0.1)]), self.actions)
        result = bp.outcome(c, Policy(), self.actions)
        self.assertEqual((result.seen, result.kind, result.trigger, result.proposal, result.same_text), ("flip", "ask", "confusable-disagreement", "truth", True))

    def test_a_right_primary_that_the_second_opinion_contradicts_is_a_false_ask_proposing_the_other_member(self):
        c = bp.arm(case("Выключи свет.", [("включи свет", 0.8), ("выключи свет", 0.2)]), self.actions)
        result = bp.outcome(c, Policy(), self.actions)
        self.assertEqual((result.seen, result.kind, result.proposal), ("right", "ask", "other"))

    def test_agreement_stays_quiet_with_the_reason(self):
        c = bp.arm(case("Выключи свет.", [("выключи свет", 1.0)]), self.actions)
        result = bp.outcome(c, Policy(), self.actions)
        self.assertEqual((result.kind, result.label), ("none", "quiet:agree"))

    def test_below_the_floor_the_alternative_is_not_asked_and_the_floor_is_the_policy_field(self):
        c = bp.arm(case("Включи свет.", [("включи свет", 0.8), ("выключи свет", 0.2)]), self.actions)
        self.assertEqual(bp.outcome(c, Policy(), self.actions).label, "quiet:agree")      # top is the primary: only a low margin could ask
        c = bp.arm(case("Включи свет.", [("выключи свет", 0.25), ("включи свет", 0.2), ("что-то", 0.55)], pair_set="light"), self.actions)
        self.assertEqual(bp.outcome(c, Policy(ask_floor=0.9), self.actions).kind, "none")

    def test_a_risky_executor_is_asked_on_any_disagreement_whatever_the_thresholds(self):
        def spread(on, off):   # the truth reading is on top with a share under the floor: nothing else is more likely
            return [(on, 0.27), ("что-то один", 0.26), ("что-то два", 0.25), (off, 0.22)]
        kettle = bp.arm(case("Выключи чайник.", spread("включи чайник", "выключи чайник"), polarity="a", pair_set="kettle", truth="включи чайник"), self.actions)
        self.assertTrue(kettle.risky)
        self.assertEqual(bp.outcome(kettle, Policy(ask_floor=1.0), self.actions).proposal, "truth")
        light = bp.arm(case("Выключи свет.", spread("включи свет", "выключи свет"), polarity="a", truth="включи свет"), self.actions)
        self.assertFalse(light.risky)
        self.assertEqual(bp.outcome(light, Policy(), self.actions).label, "quiet:below-floor")

    def test_an_emergency_or_a_dropped_hypothesis_vetoes_even_the_primary_that_is_harmless(self):
        for hostile in ("вызови скорую", "продолжение следует"):
            c = bp.arm(case("Включи свет.", [("выключи свет", 0.6), (hostile, 0.4)]), self.actions)
            self.assertTrue(c.vetoed, hostile)
            self.assertEqual(bp.outcome(c, Policy(), self.actions).label, "quiet:vetoed")

    def test_a_proposed_option_that_is_protected_is_dropped(self):
        script = {**SCRIPT, "выключи свет": decision("refuse.emergency", tier="canned")}
        actions = fake_actions(script)
        c = case("Включи свет.", [("выключи свет", 0.9), ("включи свет", 0.1)])
        c = bp.arm(c, actions)
        self.assertTrue(c.vetoed)

    def test_a_choice_between_two_actions_is_labelled_by_whether_it_offers_the_truth(self):
        c = bp.arm(case("Включи свет.", [("включи свет", 0.5), ("выключи свет", 0.5)], polarity="b"), self.actions)
        result = bp.outcome(c, Policy(), self.actions)
        self.assertEqual((result.kind, result.trigger, result.proposal), ("ask-choice", "low-margin", "choice-truth"))

    def test_a_relisten_has_no_proposal(self):
        low = NBest.from_logprobs([("ъъъ", 0.0)], engine="fake", fits={"ъъъ": 0.05})
        c = bp.arm(bp.Case("c1", "synthetic", "whisper", "бульк", low, "light", "b", "denis", "station", "выключи свет", True), self.actions)
        result = bp.outcome(c, Policy(), self.actions)
        self.assertEqual((result.kind, result.trigger, result.proposal), ("relisten", "garbled", ""))

    def test_engine_policy_only_loosens_for_the_risky(self):
        policy = Policy(ask_floor=0.7, primary_weak=0.2, margin_max=0.1)
        self.assertEqual(bp.engine_policy(policy, False), policy)
        risky = bp.engine_policy(policy, True)
        self.assertEqual((risky.ask_floor, risky.primary_weak, risky.margin_max, risky.garbled_fit), (policy.cue_floor, 1.0, 1.0, policy.garbled_fit))


@unittest.skipIf(bp is None, SKIP)
class Tally(unittest.TestCase):
    def test_counts_are_per_kind_group_and_class_and_every_ask_has_its_trigger(self):
        actions = fake_actions()
        cases = [bp.arm(c, actions) for c in (
            case("Включи свет.", [("выключи свет", 0.9), ("включи свет", 0.1)], id="flip1"),
            case("Выключи свет.", [("выключи свет", 1.0)], id="right1"),
            case("Выключи свет.", [("включи свет", 0.8), ("выключи свет", 0.2)], id="right2"),
            case("Включи свет.", [("выключи свет", 0.9), ("включи свет", 0.1)], id="flip2", voice="irina", reachable=False),
            case("Выключи свет сейчас.", [("включи свет", 0.8), ("выключи свет", 0.2)], id="right3"))]
        table = bp.tally(cases, Policy(), actions)
        everything = table[("whisper", "all")]
        self.assertEqual((everything[("*", "n")], everything[("*", "asked")]), (5, 4))
        self.assertEqual((everything[("right", "n")], everything[("right", "asked")], everything[("right", "out:other")]), (3, 2, 2))
        self.assertEqual((everything[("exact", "n")], everything[("exact", "asked")]), (2, 1))       # «сейчас» is another word: right member, not exact
        self.assertEqual((everything[("flip", "n")], everything[("flip", "out:truth")], everything[("flip", "same-text")]), (2, 2, 2))
        self.assertEqual(everything[("flip", "trig:confusable-disagreement")], 2)
        self.assertEqual(table[("whisper", "voice:irina")][("*", "n")], 1)
        self.assertEqual(table[("whisper", "reach:no")][("flip", "n")], 1)
        self.assertEqual(table[("whisper", "risk:safe")][("*", "n")], 5)
        self.assertEqual(sorted(group for kind, group in table if kind == "whisper" and group.startswith("aug:")), ["aug:station"])

    def test_the_report_names_every_group_with_its_n_and_labels_the_constructed_split(self):
        actions = fake_actions()
        cases = [bp.arm(c, actions) for c in (case("Включи свет.", [("выключи свет", 0.9), ("включи свет", 0.1)]),
                                              case("Включи свет.", [("выключи свет", 0.9), ("включи свет", 0.1)], kind="constructed"))]
        text = "\n".join(bp.report_synthetic(cases, Policy(), actions))
        self.assertIn("constructed-flips", text)
        self.assertIn("1/1 (100.0%)", text)
        self.assertIn("| station | 1 |", text)
        self.assertIn("not the owner's voice", text)


@unittest.skipIf(bp is None, SKIP)
class RealClips(unittest.TestCase):
    def test_real_clips_are_counted_not_rated_and_the_asked_clips_listed(self):
        actions = fake_actions()
        real = bp.arm(bp.Case("r1", "real", "whisper", "Включи свет.", nbest(("выключи свет", 0.9), ("включи свет", 0.1)), truth="Выключи свет"), actions)
        text = "\n".join(bp.report_real([real], Policy(), actions))
        self.assertIn("n=1 clips", text)
        self.assertIn("| r1 | Включи свет. | ask:", text)
        self.assertIn("n=1, too few for any rate", text)
        self.assertNotIn("%)", text)

    def test_a_newline_or_pipe_in_an_owner_truth_or_a_transcript_stays_in_its_table_cell(self):
        actions = fake_actions()
        nasty = "включи\nсвет | да"
        asked = bp.arm(bp.Case("r1", "real", "whisper", "Включи свет.", nbest(("выключи свет", 0.9), ("включи свет", 0.1)), truth=nasty), actions)
        junk = bp.arm(bp.Case("r2", "real", "whisper", "Продолжение следует.", nbest(("включи свет", 0.9), ("выключи свет", 0.1)), truth=nasty), actions)
        text = "\n".join(bp.report_real([asked], Policy(), actions) + bp.report_junk([junk], Policy(), actions))
        rows = [line for line in text.splitlines() if line.startswith(("| r1 ", "| r2 "))]
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertTrue(row.endswith("| включи свет \\| да |"), row)

    def test_real_cases_stay_out_of_the_synthetic_tables(self):
        actions = fake_actions()
        real = bp.arm(bp.Case("r1", "real", "whisper", "Включи свет.", nbest(("выключи свет", 0.9), ("включи свет", 0.1))), actions)
        self.assertEqual(bp.report_synthetic([real], Policy(), actions).count("x"), 0)
        self.assertEqual(bp.tally([real], Policy(), actions)[("whisper", "real")][("*", "n")], 1)
        self.assertNotIn(("whisper", "all"), bp.tally([real], Policy(), actions))

    def test_an_owner_noise_verdict_is_listed_not_dropped_as_unlabelled(self):
        actions = fake_actions()
        noise = bp.arm(bp.Case("r1", "real", "whisper", "Включи свет.", nbest(("выключи свет", 0.9), ("включи свет", 0.1)), truth=""), actions)
        unlabelled = bp.arm(bp.Case("r2", "real", "whisper", "Включи свет.", nbest(("выключи свет", 0.9), ("включи свет", 0.1))), actions)
        text = "\n".join(bp.report_real([noise, unlabelled], Policy(), actions))
        self.assertIn("(n=1, too few for any rate)", text)
        self.assertIn("- r1: truth «(noise: the owner heard no speech)»", text)
        self.assertNotIn("- r2:", text)


@unittest.skipIf(bp is None, SKIP)
class Robustness(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.dir, ignore_errors=True))

    def test_the_reports_are_owner_only_even_over_an_existing_world_readable_file_and_the_directory_is_created(self):
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        existing = self.dir / "policy-report.md"
        existing.write_text("old")
        existing.chmod(0o644)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bp.main(["report", "--corpus", str(self.dir)]), 0)
            self.assertEqual(bp.main(["grid", "--corpus", str(self.dir / "absent" / "corpus")]), 0)
        self.assertEqual(stat.S_IMODE(existing.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.dir / "absent" / "corpus" / "policy-grid.md").stat().st_mode), 0o600)

    def test_an_interrupted_cache_is_resumed_and_a_repeated_clip_counts_once(self):
        cache = self.dir / "nbest.jsonl"
        clips = [bp.Clip("a", "synthetic", None, None, lambda: b"a"), bp.Clip("b", "synthetic", None, None, lambda: b"b")]
        asyncio.run(bp.build_cache(clips[:1], FakeGiga(), cache, log=lambda _: None))
        with cache.open("a", encoding="utf-8") as out:
            out.write('{"id": "b", "spl')                     # the write an interrupted run leaves
        self.assertEqual([row["id"] for row in bp.read_results(cache)], ["a"])
        stats = asyncio.run(bp.build_cache(clips, FakeGiga(), cache, log=lambda _: None))
        self.assertEqual(stats["new"], 1)
        self.assertEqual([row["id"] for row in bp.read_results(cache)], ["a", "b"])
        self.assertTrue(cache.with_name("nbest.jsonl.lock").exists())

    def test_load_cases_tolerates_a_truncated_final_line_and_counts_a_repeated_clip_once(self):
        results = [{"id": "light-a0-denis-station", "text": "включи свет", "pair_set": "light", "polarity": "a", "voice": "denis", "augmentation": "station",
                    "whisper": {"text": "Выключи свет."}}]
        (self.dir / "results.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in results * 2) + '{"id": "light-b0', encoding="utf-8")
        clips = [bp.Clip("light-a0-denis-station", "synthetic", "Выключи свет.", None, lambda: b"x")]
        cache = self.dir / "nbest.jsonl"
        asyncio.run(bp.build_cache(clips, FakeGiga(), cache, log=lambda _: None))
        line = cache.read_text(encoding="utf-8")
        cache.write_text(line + line + '{"id": "z", "sp', encoding="utf-8")
        cases = bp.load_cases(self.dir, fake_actions())
        self.assertEqual(sorted(c.kind for c in cases), ["giga", "whisper"])

    def test_a_real_row_with_an_empty_truth_keeps_it_apart_from_no_truth(self):
        (self.dir / "real.jsonl").write_text(json.dumps({"id": "r1", "truth": ""}) + "\n" + json.dumps({"id": "r2", "truth": None}) + "\n", encoding="utf-8")
        clips = [bp.Clip("r1", "real", "Привет", None, lambda: b"y"), bp.Clip("r2", "real", "Привет", None, lambda: b"z")]
        asyncio.run(bp.build_cache(clips, FakeGiga(), self.dir / "nbest.jsonl", log=lambda _: None))
        truths = {c.id: c.truth for c in bp.load_cases(self.dir, fake_actions()) if c.kind == "whisper"}
        self.assertEqual(truths, {"r1": "", "r2": None})


@unittest.skipIf(bp is None, SKIP)
class JunkPrimary(unittest.TestCase):
    def setUp(self):
        self.actions = fake_actions()
        self.heard = [("включи свет", 0.9), ("выключи свет", 0.1)]

    def test_a_dropped_primary_vetoes_today_and_asks_once_the_veto_is_lifted(self):
        c = bp.arm(bp.Case("c", "real", "whisper", "Продолжение следует.", nbest(*self.heard)), self.actions)
        self.assertEqual(bp.outcome(c, Policy(), self.actions).label, "quiet:vetoed")
        lifted = bp.without_dropped_primary_veto(c, self.actions)
        self.assertFalse(lifted.vetoed)
        self.assertEqual(bp.outcome(lifted, Policy(), self.actions).kind, "ask")

    def test_an_emergency_primary_is_never_unvetoed(self):
        c = bp.arm(bp.Case("c", "real", "whisper", "Вызови скорую", nbest(*self.heard)), self.actions)
        self.assertTrue(bp.without_dropped_primary_veto(c, self.actions).vetoed)

    def test_a_protected_hypothesis_still_vetoes_when_the_primary_veto_is_lifted(self):
        c = bp.arm(bp.Case("c", "real", "whisper", "Продолжение следует.", nbest(("вызови скорую", 0.9), ("включи свет", 0.1))), self.actions)
        self.assertTrue(bp.without_dropped_primary_veto(c, self.actions).vetoed)

    def test_the_rows_count_each_class_and_how_many_the_second_opinion_reads_as_an_action(self):
        cases = [bp.arm(c, self.actions) for c in (
            bp.Case("a", "real", "whisper", "Продолжение следует.", nbest(*self.heard)),
            bp.Case("b", "real", "whisper", "Продолжение следует.", nbest(("что-то", 1.0))),
            bp.Case("c", "real", "whisper", "бульк", nbest(*self.heard)),
            bp.Case("d", "real", "whisper", "Выключи свет.", nbest(*self.heard)),
            bp.Case("e", "real", "whisper", "Вызови скорую", nbest(*self.heard)),
            bp.Case("f", "real", "giga", "бульк", nbest(*self.heard)))]
        rows = {(row.split, row.name): row for row in bp.junk_primary_rows(cases, Policy(), self.actions)}
        dropped = rows[("real", "dropped")]
        self.assertEqual((dropped.n, dropped.hears), (2, 1))
        self.assertEqual(dict(dropped.today), {"quiet:vetoed": 2})
        self.assertEqual(dict(dropped.lifted_when_hears), {"ask": 1, "quiet:vetoed": 1})     # the clip where nothing is heard stays vetoed
        self.assertEqual(sum(dropped.lifted.values()), 2)
        self.assertEqual((rows[("real", "unread")].n, rows[("real", "unread")].hears), (1, 1))
        text = "\n".join(bp.report_junk(cases, Policy(), self.actions))
        self.assertIn("| real | dropped | 2 | 1 |", text)
        self.assertIn("| a | Продолжение следует. | включи свет |", text)


@unittest.skipIf(bp is None, SKIP)
class RuleCandidates(unittest.TestCase):
    def setUp(self):
        self.actions = fake_actions()

    def test_rule_b_puts_the_best_listed_action_ahead_of_a_top_that_is_no_action(self):
        c = bp.arm(case("Выключи свет.", [("научи", 0.5), ("включи свет", 0.3), ("ещё", 0.2)], polarity="a", truth="включи свет"), self.actions)
        self.assertEqual(bp.outcome(c, Policy(), self.actions).label, "quiet:no-action")
        moved = bp.rule_lead_alternative(c, self.actions)
        self.assertEqual([(h.text, h.posterior) for h in moved.nbest.hypotheses], [(h.text, h.posterior) for h in c.nbest.hypotheses[1:]])
        self.assertEqual(bp.outcome(moved, Policy(), self.actions).proposal, "truth")

    def test_rule_b_keeps_every_posterior_so_a_below_floor_alternative_is_still_not_asked(self):
        c = bp.arm(case("Выключи свет.", [("научи", 0.85), ("выключи свет", 0.10), ("включи свет", 0.05)], polarity="b", truth="включи свет"), self.actions)
        moved = bp.rule_lead_alternative(c, self.actions)
        self.assertEqual({h.text: h.posterior for h in moved.nbest.hypotheses}, {h.text: h.posterior for h in c.nbest.hypotheses if h.text != "научи"})
        self.assertNotIn(bp.outcome(moved, Policy(), self.actions).label.split(":")[0], bp.ASKED)

    def test_rule_b_leaves_alone_a_top_that_is_an_action_or_the_primary_and_a_list_without_an_alternative(self):
        for items in ([("включи свет", 0.6), ("научи", 0.4)], [("выключи свет", 0.6), ("включи свет", 0.4)], [("научи", 0.6), ("ещё", 0.4)]):
            c = bp.arm(case("Выключи свет.", items, polarity="a"), self.actions)
            self.assertIs(bp.rule_lead_alternative(c, self.actions), c, items)

    def test_rule_b_never_proposes_the_primarys_own_action(self):
        c = bp.arm(case("Выключи свет.", [("научи", 0.5), ("выключи свет", 0.3), ("включи свет", 0.2)], polarity="a"), self.actions)
        moved = bp.rule_lead_alternative(c, self.actions)
        self.assertEqual([(h.text, h.posterior) for h in moved.nbest.hypotheses], [(h.text, h.posterior) for h in c.nbest.hypotheses[1:]])
        offered = bp.engine_decide(moved, Policy(), self.actions).options
        self.assertTrue(all(option.text != "выключи свет" for option in offered))

    def test_rule_a_lifts_the_veto_only_for_a_dropped_primary_while_the_second_opinion_hears_an_action(self):
        hears = bp.arm(bp.Case("a", "real", "whisper", "Продолжение следует.", nbest(("включи свет", 0.9), ("выключи свет", 0.1))), self.actions)
        self.assertFalse(bp.rule_dropped_primary(hears, self.actions).vetoed)
        silent = bp.arm(bp.Case("b", "real", "whisper", "Продолжение следует.", nbest(("что-то", 1.0))), self.actions)
        self.assertIs(bp.rule_dropped_primary(silent, self.actions), silent)
        emergency = bp.arm(bp.Case("c", "real", "whisper", "Вызови скорую", nbest(("включи свет", 1.0))), self.actions)
        self.assertIs(bp.rule_dropped_primary(emergency, self.actions), emergency)

    def test_the_report_has_a_row_per_rule_and_names_the_real_clips_a_rule_changes(self):
        cases = [bp.arm(c, self.actions) for c in (
            bp.Case("r1", "real", "whisper", "Продолжение следует.", nbest(("включи свет", 0.9), ("выключи свет", 0.1))),
            case("Выключи свет.", [("выключи свет", 1.0)]),
            case("Включи свет.", [("научи", 0.5), ("выключи свет", 0.4), ("включи свет", 0.1)], kind="constructed"))]
        text = "\n".join(bp.report_rules(cases, [Policy()], self.actions))
        for label, _ in bp.RULES:
            self.assertIn(f"| {label} |", text)
        self.assertIn("r1: quiet:vetoed -> ask", text)
        self.assertIn("0/1 (0.0%) [0.0-79.3]", text)


class FakeGiga:
    """The GigaCtc surface build_cache uses: decode -> Decoded-like, score_alternatives -> NBest, start; fails on demand."""

    def __init__(self, fail_on=()):
        self.fail_on = set(fail_on)
        self.decoded = []
        self.restarts = 0

    async def decode(self, pcm):
        if pcm in self.fail_on:
            raise GigaError("worker died")
        self.decoded.append(pcm)
        return SimpleNamespace(handle=len(self.decoded), ms=7, nbest=nbest(("выключи свет", 1.0)))

    async def score_alternatives(self, handle, *phrases):
        return nbest(("выключи свет", 0.9), (phrases[0] if phrases else "включи свет", 0.1))

    async def start(self):
        self.restarts += 1


@unittest.skipIf(bp is None, SKIP)
class Cache(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        self.cache = self.dir / "nbest.jsonl"

    def clips(self):
        return [bp.Clip("a", "synthetic", "Включи свет.", "Включи свет", lambda: b"a"), bp.Clip("b", "synthetic", None, None, lambda: b"b"),
                bp.Clip("c", "real", "Привет", None, lambda: b"c")]

    def build(self, giga, **kwargs):
        return asyncio.run(bp.build_cache(self.clips(), giga, self.cache, log=lambda _: None, **kwargs))

    def lines(self):
        return [json.loads(line) for line in self.cache.read_text(encoding="utf-8").splitlines()]

    def test_a_clip_gets_the_primaries_it_has_text_for(self):
        stats = self.build(FakeGiga())
        self.assertEqual((stats["new"], stats["failed"]), (3, 0))
        by_id = {line["id"]: line for line in self.lines()}
        self.assertEqual(sorted(by_id["a"]["primaries"]), ["constructed", "giga", "whisper"])
        self.assertEqual(sorted(by_id["b"]["primaries"]), ["giga"])
        self.assertEqual(sorted(by_id["c"]["primaries"]), ["giga", "whisper"])
        self.assertEqual(by_id["a"]["primaries"]["whisper"]["text"], "Включи свет.")
        self.assertEqual(by_id["c"]["split"], "real")
        self.assertEqual(NBest.from_dict(by_id["a"]["primaries"]["whisper"]["nbest"]).top.text, "выключи свет")

    def test_a_second_run_decodes_nothing_new(self):
        self.build(FakeGiga())
        again = FakeGiga()
        self.assertEqual(self.build(again)["new"], 0)
        self.assertEqual(again.decoded, [])
        self.assertEqual(len(self.lines()), 3)

    def test_a_limit_resumes_where_it_stopped(self):
        self.build(FakeGiga(), limit=2)
        self.assertEqual([line["id"] for line in self.lines()], ["a", "b"])
        self.build(FakeGiga())
        self.assertEqual([line["id"] for line in self.lines()], ["a", "b", "c"])

    def test_a_failing_worker_is_restarted_and_the_clip_is_retried_by_the_next_run(self):
        giga = FakeGiga(fail_on={b"b"})
        stats = self.build(giga)
        self.assertEqual((stats["new"], stats["failed"], giga.restarts), (2, 1, 1))
        self.assertEqual([line["id"] for line in self.lines()], ["a", "c"])
        self.assertEqual(self.build(FakeGiga())["new"], 1)

    def test_load_cases_reads_the_cache_with_truth_and_the_grammar_facts(self):
        results = [{"id": "light-a0-denis-station", "text": "включи свет", "pair_set": "light", "polarity": "a", "voice": "denis", "augmentation": "station",
                    "whisper": {"text": "Выключи свет."}},
                   {"id": "light-b0-denis-station", "text": "выключи свет", "pair_set": "light", "polarity": "b", "voice": "denis", "augmentation": "station",
                    "whisper": {"text": "Выключи свет."}}]
        (self.dir / "results.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in results), encoding="utf-8")
        (self.dir / "real.jsonl").write_text(json.dumps({"id": "r1", "truth": "Привет"}) + "\n", encoding="utf-8")
        clips = [bp.Clip("light-a0-denis-station", "synthetic", "Выключи свет.", "выключи свет", lambda: b"x"), bp.Clip("r1", "real", "Привет", None, lambda: b"y")]
        asyncio.run(bp.build_cache(clips, FakeGiga(), self.cache, log=lambda _: None))
        cases = bp.load_cases(self.dir, fake_actions())
        got = {(c.id, c.kind): c for c in cases}
        self.assertEqual(sorted(kind for _, kind in got), ["constructed", "giga", "giga", "whisper", "whisper"])
        flip = got[("light-a0-denis-station", "whisper")]
        self.assertEqual((flip.seen, flip.polarity, flip.truth, flip.reachable, flip.risky, flip.vetoed), ("flip", "a", "включи свет", True, False, False))
        self.assertEqual(got[("r1", "whisper")].truth, "Привет")
        self.assertEqual(got[("r1", "whisper")].split, "real")

    def test_the_synthetic_loader_hands_the_recogniser_pcm_and_the_cache_builds_from_it(self):
        import numpy as np
        rows = [{"id": f"light-{pol}0-denis-station", "text": text, "pair_set": "light", "polarity": pol, "voice": "denis", "augmentation": "station",
                 "path": f"light-{pol}0.wav", "whisper": {"text": "Выключи свет."}} for pol, text in (("a", "включи свет"), ("b", "выключи свет"))]
        rows[1]["trailing_ms"] = 300
        (self.dir / "results.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        tone = (8000 * np.sin(np.arange(16000 * 2) * 0.1)).astype("<i2")
        for row in rows:
            corpus.write_wav(self.dir / row["path"], tone)
        clips = bp.synthetic_clips(self.dir)
        self.assertTrue(all(isinstance(clip.load(), bytes) and clip.load() for clip in clips))
        stats = asyncio.run(bp.build_cache(clips, FakeGiga(), self.cache, log=lambda _: None))
        self.assertEqual((stats["new"], stats["failed"]), (2, 0))
        self.assertEqual(sorted(c.kind for c in bp.load_cases(self.dir, fake_actions()) if c.id == rows[0]["id"]), ["constructed", "giga", "whisper"])

    def test_a_clip_whose_opposite_result_arrives_later_is_decoded_again_and_its_constructed_primary_is_kept(self):
        first = [bp.Clip("a", "synthetic", "Включи свет.", None, lambda: b"a")]
        asyncio.run(bp.build_cache(first, FakeGiga(), self.cache, log=lambda _: None))
        self.assertEqual(sorted(bp.cache_rows(self.cache)["a"]["primaries"]), ["giga", "whisper"])
        later = [bp.Clip("a", "synthetic", "Включи свет.", "выключи свет", lambda: b"a")]
        self.assertEqual(asyncio.run(bp.build_cache(later, FakeGiga(), self.cache, log=lambda _: None))["new"], 1)
        self.assertEqual(sorted(bp.cache_rows(self.cache)["a"]["primaries"]), ["constructed", "giga", "whisper"])
        self.assertEqual(asyncio.run(bp.build_cache(later, FakeGiga(), self.cache, log=lambda _: None))["new"], 0)

    def test_a_changed_whisper_transcript_under_the_same_id_is_decoded_again_and_the_latest_entry_wins(self):
        old = [bp.Clip("a", "synthetic", "Включи свет.", None, lambda: b"a")]
        asyncio.run(bp.build_cache(old, FakeGiga(), self.cache, log=lambda _: None))
        new = [bp.Clip("a", "synthetic", "Выключи свет.", None, lambda: b"a")]
        self.assertEqual(asyncio.run(bp.build_cache(new, FakeGiga(), self.cache, log=lambda _: None))["new"], 1)
        self.assertEqual(bp.cache_rows(self.cache)["a"]["primaries"]["whisper"]["text"], "Выключи свет.")
        self.assertEqual(asyncio.run(bp.build_cache(new, FakeGiga(), self.cache, log=lambda _: None))["new"], 0)

    def test_a_changed_opposite_text_is_decoded_again_too(self):
        old = [bp.Clip("a", "synthetic", None, "выключи свет", lambda: b"a")]
        asyncio.run(bp.build_cache(old, FakeGiga(), self.cache, log=lambda _: None))
        new = [bp.Clip("a", "synthetic", None, "включи чайник", lambda: b"a")]
        self.assertEqual(asyncio.run(bp.build_cache(new, FakeGiga(), self.cache, log=lambda _: None))["new"], 1)
        self.assertEqual(bp.cache_rows(self.cache)["a"]["primaries"]["constructed"]["text"], "включи чайник")

    def test_the_nbest_run_holds_the_corpus_lock_while_it_loads_and_decodes(self):
        import fcntl
        (self.dir / "results.jsonl").write_text("", encoding="utf-8")
        probes = []

        class Probe(FakeGiga):
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        def trying(*_, **__):
            with (self.dir / "results.jsonl.lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    probes.append("held")
                else:
                    probes.append("free")
            return Probe()

        self.addCleanup(setattr, bp, "GigaCtc", bp.GigaCtc)
        bp.GigaCtc = trying
        self.assertEqual(bp.main(["nbest", "--corpus", str(self.dir)]), 0)
        self.assertEqual(probes, ["held"])
        with (self.dir / "results.jsonl.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)             # released afterwards

    def test_the_cache_is_owner_only_new_or_existing(self):
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        clips = [bp.Clip("a", "synthetic", None, None, lambda: b"a")]
        asyncio.run(bp.build_cache(clips, FakeGiga(), self.cache, log=lambda _: None))
        self.assertEqual(stat.S_IMODE(self.cache.stat().st_mode), 0o600)
        self.cache.chmod(0o644)
        asyncio.run(bp.build_cache(clips + [bp.Clip("b", "synthetic", None, None, lambda: b"b")], FakeGiga(), self.cache, log=lambda _: None))
        self.assertEqual(stat.S_IMODE(self.cache.stat().st_mode), 0o600)

    def test_a_grid_without_exact_primaries_or_without_constructed_flips_selects_nothing(self):
        actions = fake_actions()
        flips_only = [bp.arm(case("Включи свет.", [("выключи свет", 0.9), ("включи свет", 0.1)], kind="constructed"), actions)]
        for cases in ([], flips_only):
            lines, _, chosen = bp.report_grid(cases, actions)
            self.assertIsNone(chosen)
            self.assertIn("Insufficient data", "\n".join(lines))
            self.assertNotIn("Selected:", "\n".join(lines))


@unittest.skipIf(bp is None, SKIP)
class Grid(unittest.TestCase):
    def point(self, false_ask, recall, **fields):
        return bp.Point(Policy(**fields), (false_ask, 1000), (false_ask, 1000), (0, 7), (0, 1000), (recall, 1000), (recall, 1000))

    def test_the_selection_maximises_recall_under_the_false_ask_constraint(self):
        points = [self.point(5, 800, ask_floor=0.3), self.point(11, 990, ask_floor=0.05), self.point(10, 900, ask_floor=0.1), self.point(0, 100, ask_floor=0.9)]
        self.assertEqual(bp.select(points).policy.ask_floor, 0.1)          # 10/1000 = 1 % is allowed; 11/1000 is not
        self.assertIsNone(bp.select([self.point(50, 900)]))

    def test_a_tie_goes_to_fewer_false_asks_then_to_the_more_conservative_floor(self):
        points = [self.point(5, 900, ask_floor=0.1), self.point(3, 900, ask_floor=0.05), self.point(3, 900, ask_floor=0.2)]
        self.assertEqual(bp.select(points).policy.ask_floor, 0.2)

    def test_a_tie_in_everything_else_goes_to_the_lower_primary_weak(self):
        points = [self.point(3, 900, primary_weak=0.9), self.point(3, 900, primary_weak=0.3), self.point(3, 900, primary_weak=0.6)]
        self.assertEqual(bp.select(points).policy.primary_weak, 0.3)

    def test_the_frontier_keeps_only_points_nothing_beats(self):
        points = [self.point(0, 500), self.point(2, 500), self.point(4, 800), self.point(5, 700), self.point(9, 950)]
        self.assertEqual([(p.exact[0], p.built[0]) for p in bp.frontier(points)], [(0, 500), (4, 800), (9, 950)])

    def test_the_plateau_is_the_feasible_points_within_a_point_of_the_best(self):
        points = [self.point(5, 900), self.point(5, 895), self.point(5, 880), self.point(50, 999)]
        self.assertEqual(len(bp.plateau(points, points[0])), 2)

    def test_the_recommendation_is_the_shipped_policy_when_it_is_on_the_plateau_and_else_the_nearest_point(self):
        shipped = self.point(5, 890)
        far = self.point(5, 900, ask_floor=0.7, primary_weak=0.3, margin_max=0.7, garbled_fit=0.7)
        near = self.point(5, 895, ask_floor=0.4)
        self.assertEqual(bp.recommend([far, shipped, near], far).policy, Policy())
        self.assertEqual(bp.recommend([far, near, self.point(5, 700)], far).policy, near.policy)

    def test_the_grid_is_the_product_of_the_four_fields_with_max_words_fixed(self):
        policies = bp.grid_policies(Policy())
        self.assertEqual(len(policies), math.prod(len(v) for v in bp.GRID.values()))
        self.assertEqual({p.max_words for p in policies}, {Policy().max_words})
        self.assertEqual(len(set(policies)), len(policies))

    def test_a_point_counts_each_class_on_its_own_denominator_and_leaves_real_clips_out(self):
        actions = fake_actions()
        cases = [bp.arm(c, actions) for c in (
            case("Выключи свет.", [("включи свет", 0.8), ("выключи свет", 0.2)], id="right"),
            case("Выключи свет.", [("выключи свет", 1.0)], id="right2"),
            case("Включи свет.", [("выключи свет", 0.9), ("включи свет", 0.1)], id="flip"),
            case("Включи свет.", [("выключи свет", 0.9), ("включи свет", 0.1)], kind="constructed", id="built"),
            case("Включи свет.", [("включи свет", 1.0)], kind="constructed", id="built-unreachable", reachable=False),
            case("Выключи свет.", [("включи свет", 0.8), ("выключи свет", 0.2)], kind="giga", id="g"))]
        real = bp.arm(bp.Case("r", "real", "whisper", "Включи свет.", nbest(("выключи свет", 0.9))), actions)
        point = bp.run_grid(cases + [real], actions, [Policy()])[0]
        self.assertEqual((point.right, point.exact, point.flip, point.giga_exact), ((1, 2), (1, 2), (1, 1), (1, 1)))
        self.assertEqual((point.built, point.built_all), ((1, 1), (1, 2)))

    def test_raising_the_floor_never_adds_questions_on_a_safe_executor(self):
        actions = fake_actions()
        cases = [bp.arm(case("Выключи свет.", [("включи свет", share), ("выключи свет", 1 - share)]), actions) for share in (0.1, 0.25, 0.4, 0.55, 0.7, 0.85)]
        asked = [bp.evaluate_point(cases, Policy(ask_floor=floor), actions).right[0] for floor in (0.05, 0.3, 0.5, 0.9)]
        self.assertEqual(asked, sorted(asked, reverse=True))

    def test_the_grid_report_runs_end_to_end_on_a_small_case_list(self):
        actions = fake_actions()
        cases = []
        for voice in ("denis", "irina", "dmitri", "ruslan"):
            cases += [bp.arm(c, actions) for c in (
                case("Включи свет.", [("выключи свет", 0.9), ("включи свет", 0.1)], voice=voice, kind="constructed"),
                case("Выключи свет.", [("выключи свет", 1.0)], voice=voice))]
        lines, table, chosen = bp.report_grid(cases, actions)
        self.assertIsNotNone(chosen)
        text = "\n".join(lines)
        self.assertIn("Shipped defaults", text)
        self.assertIn("Voice hold-out", text)
        self.assertIn("| ask_floor=0.3 (shipped) |", text)
        self.assertIn("| recommended |", text)
        self.assertEqual(len(table), 2 + len(bp.grid_policies(Policy())))


if __name__ == "__main__":
    unittest.main()
