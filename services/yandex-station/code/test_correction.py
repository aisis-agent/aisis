"""The correction engine and the repair dialog through the adapter's route: offers, every dialog state, the standard replies, the timeout,
an unrelated command, an emergency, an unmatched reply that goes on to the brain, both the quasar hook path and the speaker's own turn.

The shared core (`stt_cli.hypotheses`) is replaced by a small in-file fake of its published contract so these tests need no install; the
`RealCoreTests` at the end run the same scenarios through the real core when it is importable."""
import json
import re
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import correction
import intent_route
import ha_actions
import test_ha_actions
import utterance_trace
from intent_grammar import load

CLOCK_START = 1000.0
BUNDLE = Path(__file__).parent / "intent_grammar" / "grammar" / "ru.json"
SAFETY = load(BUNDLE).gate["safety"]["phrases"]


# ── a fake of the core's contract ───────────────────────────────────────────

def norm(text):
    return " ".join(re.sub(r"[^\w\s]|_", " ", text.lower().replace("ё", "е")).split())


@dataclass(frozen=True)
class Hyp:
    text: str
    posterior: float
    fit: float | None = 0.9
    source: str = "beam"
    engine: str = "gigaam-v3-ctc"

    @property
    def percent(self):
        return round(100 * self.posterior)


@dataclass(frozen=True)
class NB:
    hypotheses: tuple
    engine: str = "gigaam-v3-ctc"
    ms: int | None = None

    @property
    def top(self):
        return self.hypotheses[0] if self.hypotheses else None

    @property
    def margin(self):
        return 0.0 if not self.hypotheses else self.hypotheses[0].posterior - (self.hypotheses[1].posterior if len(self.hypotheses) > 1 else 0.0)

    def get(self, text):
        return next((h for h in self.hypotheses if norm(h.text) == norm(text)), None)

    @staticmethod
    def from_dict(raw):
        return NB(tuple(Hyp(h["text"], h["posterior"], h.get("fit"), h.get("source", "beam"), h.get("engine", "gigaam-v3-ctc"))
                        for h in raw["hypotheses"]), raw.get("engine", "gigaam-v3-ctc"), raw.get("ms"))


@dataclass(frozen=True)
class Cand:
    text: str
    swapped: tuple
    set_id: str
    kind: str
    label: str | None


PAIRS = (("включи", "выключи", "включить", "выключить"), ("громче", "тише", "погромче", "потише"), ("теплее", "холоднее", "теплее", "холоднее"))


class FakeConfusables:
    def candidates(self, text, limit=6):
        out = []
        words = text.split()
        for a, b, label_a, label_b in PAIRS:
            for i, word in enumerate(words):
                for here, there, label in ((a, b, label_b), (b, a, label_a)):
                    if norm(word) == here:
                        out.append(Cand(" ".join(words[:i] + [there] + words[i + 1:]), (word, there), f"{a}-{b}", "opposite", label))
        return out[:limit]

    def conflicts(self, a, b):
        return "x" if any(norm(c.text) == norm(b) for c in self.candidates(a)) else None


@dataclass(frozen=True)
class Cue:
    kind: str
    supports: str
    weight: float = 1.0


@dataclass(frozen=True)
class Policy:
    ask_floor: float = 0.30
    primary_weak: float = 0.60
    margin_max: float = 0.35
    garbled_fit: float = 0.35
    cue_floor: float = 0.05
    max_words: int = 8


@dataclass(frozen=True)
class Evidence:
    primary: str
    nbest: Any
    action_of: Any
    cues: tuple = ()
    vetoed: bool = False
    confusables: Any = None


@dataclass(frozen=True)
class Decision:
    kind: str
    trigger: str = ""
    reason: str = ""
    heard: str = ""
    options: tuple = ()


class FakeCore:
    """`decide` is scripted (`verdict`) or the simplest honest policy: veto, a no-op cue, a different action above the floor."""
    Policy, Cue, Evidence, Decision, NBest = Policy, Cue, Evidence, Decision, NB

    def __init__(self):
        self.calls = []
        self.verdict = None

    @staticmethod
    def normalize(text):
        return norm(text)

    def load(self):
        return FakeConfusables()

    def decide(self, evidence, policy=Policy()):
        self.calls.append((evidence, policy))
        if self.verdict is not None:
            return self.verdict(evidence, policy)
        if evidence.vetoed:
            return Decision("none", "", "vetoed", evidence.primary)
        for cue in evidence.cues:
            return Decision("ask", "contradiction", "", evidence.primary, (next(c for c in FakeConfusables().candidates(evidence.primary) if norm(c.text) == norm(cue.supports)),))
        verdict = self.from_nbest(evidence, policy)
        return verdict if verdict is not None else Decision("none", "", "agree", evidence.primary)

    @staticmethod
    def from_nbest(evidence, policy):
        """The verdict the second opinion alone argues for, or None: a different action on top, or a low margin between two actions."""
        top = evidence.nbest.top if evidence.nbest else None
        if top and norm(top.text) != norm(evidence.primary) and evidence.action_of(top.text) not in (None, evidence.action_of(evidence.primary)) \
                and top.posterior >= policy.ask_floor:
            return Decision("ask", "confusable-disagreement", "", evidence.primary, (top,))
        if top and norm(top.text) == norm(evidence.primary) and len(evidence.nbest.hypotheses) > 1:
            runner = evidence.nbest.hypotheses[1]
            if evidence.action_of(runner.text) not in (None, evidence.action_of(top.text)) and evidence.nbest.margin < policy.margin_max \
                    and runner.posterior >= policy.ask_floor:
                return Decision("ask-choice", "low-margin", "", evidence.primary, (top, runner))
        return None


class Store:
    def __init__(self):
        self.items = {}

    def put(self, station, primary, *pairs, ms=70):
        nbest = {"engine": "gigaam-v3-ctc", "ms": ms, "hypotheses": [{"text": t, "posterior": p, "fit": 0.9, "source": "beam", "engine": "gigaam-v3-ctc"} for t, p in sorted(pairs, key=lambda pair: -pair[1])]}
        self.items[(station, norm(primary))] = {"source": "spotter", "primary_text": primary, "nbest": nbest, "ms": ms}

    def get(self, station, text, now):
        return self.items.get((station, norm(text)))


class Clock:
    def __init__(self):
        self.t = CLOCK_START

    def __call__(self):
        return self.t


class FakeHa:
    def __init__(self):
        self.calls = []

    async def run(self, executor, args, station=None):
        self.calls.append((executor, args))
        return "Готово."


class Setup(unittest.IsolatedAsyncioTestCase):
    """A router with the dialog on, the fake core and a store; helpers for the two paths."""
    mode = "on"

    def make_core(self):
        return FakeCore()

    def setUp(self):
        self.clock, self.store, self.ha = Clock(), Store(), FakeHa()
        self.core = self.make_core()
        grammar = intent_route.load_grammar()
        self.router = intent_route.IntentRouter(grammar, ha=self.ha, hypotheses=self.store, correction_mode=self.mode, correction_core=self.core,
                                                monotonic=self.clock, pick=lambda options: options[0])
        self.engine = self.router.correction

    async def hook(self, text, station="st1", **extra):
        extra.setdefault("rerun", True)
        return await self.router.route("quasar", text, phase="pre", via_quasar=True, device=station, **extra)

    async def turn(self, text, station="st1"):
        return await self.router.route(station, text)

    def light(self):
        """The clip: the on-device spotter heard «включи свет», the second opinion says «выключи свет»."""
        self.store.put("st1", "включи свет", ("выключи свет", 0.66), ("включи свет", 0.34))

    async def open_confirm(self, path="hook"):
        self.light()
        route = await (self.hook if path == "hook" else self.turn)("включи свет")
        self.assertEqual(route.intent, "repair.ask", route)
        return route

    def state(self, station="st1"):
        dialog = self.engine.dialogs.get(station)
        return dialog.state if dialog else None


PATHS = ("hook", "turn")


class OfferTests(Setup):
    async def test_nothing_is_asked_without_evidence(self):
        for path in PATHS:
            route = await getattr(self, path)("включи свет")
            self.assertEqual(route.kind, "none")
        self.assertIsNone(self.state())
        self.assertEqual(self.core.calls, [])      # no stored second opinion and no cue: the policy is not even consulted

    async def test_a_second_opinion_opens_a_confirm_question_on_both_paths(self):
        for path in PATHS:
            self.setUp()
            route = await self.open_confirm(path)
            self.assertEqual((route.kind, route.text, route.listen, route.context),
                             ("reply", "Мне послышалось «включи свет». Вы имели в виду «выключи свет»?", True, True), path)
            self.assertEqual(self.state(), "awaiting_confirm")
            self.assertTrue(route.as_json()["context"])

    async def test_a_noop_cue_opens_the_question_in_the_owners_words(self):
        noop = {"desired": "on", "label": "свет в студии", "targets": ["light.studio"]}
        route = await self.hook("включи свет", noop=noop)
        self.assertEqual(route.text, "Свет в студии уже включён. Вы хотели выключить?")
        self.assertEqual(self.state(), "awaiting_confirm")
        self.setUp()
        route = await self.hook("выключи свет", noop={"desired": "off", "label": "лампа", "targets": []})
        self.assertEqual(route.text, "Лампа уже выключена. Вы хотели включить?")

    async def test_a_malformed_noop_is_no_cue(self):
        for noop in ({"desired": "maybe", "label": "свет"}, "oops", {"label": "свет"}, []):
            self.setUp()
            self.assertEqual((await self.hook("включи свет", noop=noop)).kind, "none", noop)

    async def test_the_heard_phrase_said_at_once_is_an_answer_on_the_channel_that_asked(self):
        for path in PATHS:
            self.setUp()
            await self.open_confirm(path)
            route = await (self.hook if path == "hook" else self.turn)("включи свет")
            self.assertEqual(route.intent, "repair.rerun" if path == "hook" else "repair.say_again", path)
            self.assertIsNone(self.state(), path)

    async def test_the_other_channels_delivery_of_the_same_utterance_is_not_an_answer(self):
        for first, second in (("hook", "turn"), ("turn", "hook")):
            self.setUp()
            asked = await self.open_confirm(first)
            self.clock.t += 1
            again = await (self.hook if second == "hook" else self.turn)("включи свет")
            self.assertEqual((again.text, again.listen, self.state()), (asked.text, True, "awaiting_confirm"), (first, second))

    async def test_the_noop_probe_after_a_question_is_the_same_delivery_not_an_answer(self):
        self.light()
        first = await self.hook("включи свет")
        self.clock.t += 1
        again = await self.hook("включи свет", noop={"desired": "on", "label": "свет", "targets": []})
        self.assertEqual((again.text, again.listen), (first.text, True))
        self.assertEqual(self.state(), "awaiting_confirm")
        self.assertEqual(len(self.core.calls), 1)      # one question per utterance, the policy asked once

    async def test_the_evidence_the_core_is_given(self):
        self.light()
        await self.hook("включи свет")
        evidence, policy = self.core.calls[0]
        self.assertEqual(evidence.primary, "включи свет")
        self.assertFalse(evidence.vetoed)
        self.assertEqual(evidence.nbest.top.text, "выключи свет")
        self.assertEqual(policy, Policy())
        self.assertNotEqual(evidence.action_of("включи свет"), evidence.action_of("выключи свет"))
        self.assertIsNotNone(evidence.action_of("включи свет"))
        self.assertIsNone(evidence.action_of("расскажи анекдот про кота"))
        self.assertEqual(evidence.action_of("включи свет"), evidence.action_of("включи свет"))
        self.assertEqual(evidence.cues, ())

    async def test_choice_and_relisten_wording_and_states(self):
        self.core.verdict = lambda e, p: Decision("ask-choice", "low-margin", "", e.primary, (Hyp("включи свет", 0.5), Hyp("выключи свет", 0.5)))
        self.light()
        route = await self.hook("включи свет")
        self.assertEqual((route.text, self.state()), ("Включить или выключить?", "awaiting_choice"))
        self.setUp()
        self.core.verdict = lambda e, p: Decision("relisten", "garbled", "", e.primary)
        self.light()
        route = await self.hook("включи свет")
        self.assertEqual((route.text, route.listen, self.state()), ("Не расслышал. Повторите, пожалуйста.", True, "awaiting_repeat"))

    async def test_three_readings_ask_between_the_two_likely_alternatives(self):
        self.core.verdict = lambda e, p: Decision("ask-choice", "confusable-disagreement", "", e.primary,
                                                  (Hyp("включи свет", 0.2), Hyp("выключи свет", 0.45), Hyp("громче свет", 0.35)))
        self.light()
        route = await self.hook("включи свет")
        self.assertEqual(route.text, "Выключи свет или громче свет?")        # no label for a pair the data does not know: the phrases
        self.assertEqual([r.text for r in self.engine.dialogs["st1"].readings], ["выключи свет", "громче свет"])

    async def test_no_spoken_question_carries_a_percentage_or_a_digit(self):
        for verdict in (None,
                        lambda e, p: Decision("ask-choice", "low-margin", "", e.primary, (Hyp("включи свет", 0.5), Hyp("выключи свет", 0.5))),
                        lambda e, p: Decision("relisten", "garbled", "", e.primary)):
            self.setUp()
            self.core.verdict = verdict
            self.light()
            route = await self.hook("включи свет", noop={"desired": "on", "label": "свет в студии", "targets": []}) if verdict is None \
                else await self.hook("включи свет")
            self.assertIsNotNone(route.text)
            self.assertNotRegex(route.text, r"\d|%|процент")

    async def test_a_decision_the_core_withholds_opens_nothing(self):
        self.store.put("st1", "включи свет", ("включи свет", 0.9), ("выключи свет", 0.1))   # below the floor
        route = await self.hook("включи свет")
        self.assertEqual((route.kind, self.state()), ("none", None))

    async def test_a_core_failure_leaves_the_old_path(self):
        def boom(evidence, policy):
            raise RuntimeError("core broke")
        self.core.verdict = boom
        self.light()
        route = await self.hook("включи свет")
        self.assertEqual((route.kind, self.state()), ("none", None))

    async def test_a_broken_dialog_still_lets_the_normal_route_answer(self):
        class Broken:
            def get(self, station, text):
                raise OSError("store gone")
        self.engine.hypotheses = Broken()
        for path in PATHS:
            route = await getattr(self, path)("который час")
            self.assertEqual((route.kind, route.intent), ("reply", "time.now"), path)
        self.engine.hypotheses = self.store
        await self.open_confirm()
        self.engine.reply = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("fsm broke"))
        route = await self.hook("который час")
        self.assertEqual((route.kind, route.intent), ("reply", "time.now"))
        self.assertIsNone(self.state())                     # nothing is left open by a step that crashed

    async def test_the_engine_is_off_when_the_core_cannot_be_built(self):
        class Broken(FakeCore):
            def load(self):
                raise ImportError("no stt_cli")
        router = intent_route.IntentRouter(intent_route.load_grammar(), correction_mode="on", correction_core=Broken())
        self.assertIsNone(router.correction)
        self.assertIsNone(intent_route.IntentRouter(intent_route.load_grammar()).correction)      # default: off
        self.assertIsNone(intent_route.IntentRouter(intent_route.load_grammar(), correction_mode="off", correction_core=FakeCore()).correction)

    async def test_shadow_decides_and_traces_but_never_speaks_or_opens_a_context(self):
        self.mode = "shadow"
        self.setUp()
        self.light()
        trace = utterance_trace.start("hook", "включи свет")
        route = await self.hook("включи свет")
        utterance_trace.end(trace)
        self.assertEqual((route.kind, route.context, self.state()), ("none", False, None))
        self.assertEqual(self.core.calls[0][0].primary, "включи свет")
        record = trace.data["correction"]
        self.assertTrue(record["shadow"])
        self.assertEqual(record["decision"], "ask")
        self.assertEqual(record["dialog"]["ask"], "Мне послышалось «включи свет». Вы имели в виду «выключи свет»?")
        self.assertEqual((await self.hook("да")).kind, "none")


class VetoTests(Setup):
    async def test_an_emergency_phrase_as_the_primary_reading_never_opens_a_dialog(self):
        for phrase in SAFETY:
            self.setUp()
            self.core.verdict = lambda e, p: Decision("ask", "confusable-disagreement", "", e.primary, (Hyp("выключи свет", 0.9),))
            self.store.put("st1", phrase, ("выключи свет", 0.9), (phrase, 0.1))
            route = await self.hook(phrase)
            self.assertNotEqual(route.intent, "repair.ask", phrase)
            self.assertIsNone(self.state(), phrase)
            self.assertTrue(self.core.calls[0][0].vetoed, phrase)

    async def test_an_emergency_phrase_as_a_listed_alternative_vetoes_the_question(self):
        for phrase in SAFETY:
            self.setUp()
            self.core.verdict = lambda e, p: Decision("ask", "confusable-disagreement", "", e.primary, (Hyp("выключи свет", 0.9),))
            self.store.put("st1", "включи свет", ("выключи свет", 0.6), (phrase, 0.3), ("включи свет", 0.1))
            route = await self.hook("включи свет")
            self.assertEqual(route.kind, "none", phrase)
            self.assertIsNone(self.state(), phrase)
            self.assertTrue(self.core.calls[0][0].vetoed, phrase)

    async def test_an_emergency_phrase_as_the_proposed_option_is_dropped_even_if_the_core_proposes_it(self):
        for phrase in SAFETY:
            self.setUp()
            self.core.verdict = lambda e, p: Decision("ask", "contradiction", "", e.primary, (Cand(phrase, ("a", "b"), "x", "opposite", "позвонить"),))
            route = await self.hook("включи свет", noop={"desired": "on", "label": "свет", "targets": []})
            self.assertEqual(route.kind, "none", phrase)
            self.assertIsNone(self.state(), phrase)

    async def test_an_emergency_primary_is_still_answered_by_the_grammar(self):
        self.store.put("st1", "вызови скорую", ("выключи свет", 0.9))
        for path in PATHS:
            route = await getattr(self, path)("вызови скорую")
            self.assertEqual((route.kind, route.intent), ("reply", "refuse.emergency"))
            self.assertIn("сто двенадцать", route.text)

    async def test_a_dropped_reading_vetoes_too(self):
        self.core.verdict = lambda e, p: Decision("ask", "confusable-disagreement", "", e.primary, (Hyp("выключи свет", 0.9),))
        self.store.put("st1", "включи свет", ("выключи свет", 0.6), ("Продолжение следует", 0.3), ("включи свет", 0.1))
        self.assertEqual((await self.hook("включи свет")).kind, "none")
        self.assertTrue(self.core.calls[0][0].vetoed)
        self.setUp()
        self.store.put("st1", "Продолжение следует", ("выключи свет", 0.9))
        self.assertEqual((await self.hook("Продолжение следует")).intent, "unaddressed.caption")
        self.assertTrue(self.core.calls[0][0].vetoed)


class RiskTests(Setup):
    def kettle(self):
        self.store.put("st1", "включи чайник", ("включи чайник", 0.86), ("выключи чайник", 0.14))

    async def test_a_risky_action_is_held_on_any_disagreement_even_with_a_wide_margin(self):
        self.kettle()
        route = await self.hook("включи чайник")
        policy = self.core.calls[0][1]
        self.assertLessEqual(policy.ask_floor, 0.05)
        self.assertGreaterEqual(policy.primary_weak, 1.0)
        self.assertGreaterEqual(policy.margin_max, 1.0)
        self.assertEqual((route.text, self.state()), ("Включить или выключить?", "awaiting_choice"))
        self.setUp()
        self.store.put("st1", "включи свет", ("включи свет", 0.86), ("выключи свет", 0.14))
        self.assertEqual((await self.hook("включи свет")).kind, "none")
        self.assertEqual(self.core.calls[0][1], Policy())       # a light keeps the policy's own thresholds

    async def test_a_choice_never_hands_a_risky_action_to_the_overlay(self):
        self.kettle()
        await self.hook("включи чайник")
        route = await self.hook("выключи")
        self.assertEqual((route.kind, route.text, route.listen), ("reply", "Скажите ещё раз: выключи чайник.", True))
        self.assertEqual(self.ha.calls, [])

    async def test_a_yes_runs_a_risky_action_only_through_the_normal_route(self):
        self.store.put("st1", "сделай теплее", ("сделай холоднее", 0.7), ("сделай теплее", 0.3))
        route = await self.turn("сделай теплее")
        self.assertEqual(route.intent, "repair.ask")
        self.assertEqual(self.ha.calls, [])
        route = await self.turn("да")
        self.assertEqual(route.kind, "reply")
        self.assertTrue(route.text.startswith("Хорошо, сделай холоднее."))
        self.assertEqual(len(self.ha.calls), 1)
        self.assertEqual(self.ha.calls[0][0], "ha.climate_delta")

    async def test_a_risky_proposed_alternative_makes_the_dialog_risky_even_for_a_safe_heard_reading(self):
        self.core.verdict = lambda e, p: Decision("ask", "confusable-disagreement", "", e.primary, (Hyp("выключи чайник", 0.8),))
        self.store.put("st1", "включи свет", ("выключи чайник", 0.8), ("включи свет", 0.2))
        await self.hook("включи свет")
        route = await self.hook("да")
        self.assertEqual((route.kind, route.text), ("reply", "Скажите ещё раз: выключи чайник."))

    async def test_the_set_of_safe_executors_is_light_volume_media_and_dialog_only(self):
        for executor in ("ha.device", "ha.climate", "ha.climate_delta", "timer.start", "alarm.set", "timer.cancel"):
            self.assertNotIn(executor, correction.SAFE_EXECUTORS)
        for executor in ("ha.light", "speaker.volume", "speaker.volume_delta", "media.key", "dialog.end"):
            self.assertIn(executor, correction.SAFE_EXECUTORS)


class ConfirmFlowTests(Setup):
    async def test_yes_hands_the_alternative_to_the_overlay_when_it_announced_rerun(self):
        await self.open_confirm()
        route = await self.hook("да")
        self.assertEqual((route.kind, route.text, route.context, route.listen), ("rerun", "выключи свет", False, False))
        self.assertEqual(route.as_json()["route"], "rerun")
        self.assertNotIn("context", route.as_json())
        self.assertIsNone(self.state())

    async def test_every_yes_word_and_the_alternative_itself_accept(self):
        for reply in ("да", "ага", "угу", "конечно", "именно", "верно", "точно", "так", "давай", "правильно", "выключи", "выключи свет", "выключить"):
            self.setUp()
            await self.open_confirm()
            route = await self.hook(reply)
            self.assertEqual((route.kind, route.text), ("rerun", "выключи свет"), reply)

    async def test_without_rerun_support_a_quasar_owned_action_is_asked_for_again(self):
        await self.open_confirm()
        route = await self.hook("да", rerun=False)
        self.assertEqual((route.kind, route.text, route.listen), ("reply", "Скажите ещё раз: выключи свет.", True))
        self.assertIsNone(self.state())

    async def test_on_the_speakers_own_turn_a_quasar_owned_action_is_asked_for_again(self):
        await self.open_confirm("turn")
        route = await self.turn("да")
        self.assertEqual((route.kind, route.text, route.listen), ("reply", "Скажите ещё раз: выключи свет.", True))

    async def test_an_adapter_owned_alternative_runs_here_with_or_without_rerun(self):
        for path, extra in (("hook", {"rerun": True}), ("hook", {"rerun": False}), ("turn", {})):
            self.setUp()
            self.store.put("st1", "сделай холоднее", ("сделай холоднее", 0.2), ("сделай теплее", 0.8))
            await (self.hook if path == "hook" else self.turn)("сделай холоднее")
            route = await (self.hook if path == "hook" else self.turn)("да", **extra)
            self.assertEqual(route.kind, "reply", (path, extra))
            self.assertEqual(route.text, "Хорошо, сделай теплее. Готово.")
            self.assertEqual(self.ha.calls[-1][0], "ha.climate_delta")

    async def test_the_heard_reading_kept_is_run_and_never_asked_about_again(self):
        await self.open_confirm()
        route = await self.hook("включи")
        self.assertEqual((route.kind, route.text), ("rerun", "включи свет"))
        again = await self.hook("включи свет", noop={"desired": "on", "label": "свет", "targets": []})
        self.assertEqual(again.kind, "none")           # the quasar layer says «уже включён» as before: no loop
        self.assertEqual(self.state(), None)

    async def test_the_accepted_alternative_is_muted_too(self):
        await self.open_confirm()
        await self.hook("да")
        self.store.put("st1", "выключи свет", ("включи свет", 0.9), ("выключи свет", 0.1))
        self.assertEqual((await self.hook("выключи свет", noop={"desired": "off", "label": "свет", "targets": []})).kind, "none")

    async def test_no_asks_to_repeat_and_the_next_phrase_takes_the_normal_route(self):
        await self.open_confirm()
        route = await self.hook("нет")
        self.assertEqual((route.text, route.listen, self.state()), ("Повторите, пожалуйста.", True, "awaiting_repeat"))
        self.assertTrue(route.context)
        fresh = await self.hook("Алиса, привет")
        self.assertEqual((fresh.kind, fresh.intent), ("reply", "smalltalk.greeting"))
        self.assertIsNone(self.state())

    async def test_a_second_doubt_after_the_repeat_is_a_polite_give_up(self):
        await self.open_confirm()
        await self.hook("не то")
        self.light()
        route = await self.hook("включи свет")
        self.assertEqual((route.kind, route.text, route.listen, route.context), ("reply", "Ладно, не буду.", False, False))
        self.assertIsNone(self.state())
        self.clock.t += 5
        self.assertEqual((await self.hook("включи свет")).kind, "none")     # the phrase is quiet for a while: no loop

    async def test_again_says_the_question_once_then_gives_up(self):
        first = await self.open_confirm()
        self.clock.t += 6
        route = await self.hook("повтори")
        self.assertEqual((route.text, route.listen, self.state()), (first.text, True, "awaiting_confirm"))
        self.clock.t += 6
        route = await self.hook("что")
        self.assertEqual((route.text, route.listen), ("Ладно, не буду.", False))
        self.assertIsNone(self.state())

    async def test_cancel_ends_the_dialog_and_nothing_happens(self):
        for reply in ("отмена", "забудь", "не надо ничего", "оставь", "стоп"):
            self.setUp()
            await self.open_confirm()
            route = await self.hook(reply)
            self.assertEqual((route.kind, route.context), ("dialog_end", False), reply)
            self.assertIsNone(self.state())
            self.assertEqual(self.ha.calls, [])
        self.setUp()
        await self.open_confirm("turn")
        self.assertEqual((await self.turn("отмена")).kind, "silent")

    async def test_a_yes_after_the_window_is_no_answer(self):
        await self.open_confirm()
        self.clock.t += correction.WINDOW_S + 1
        route = await self.hook("да")
        self.assertEqual(route.kind, "none")
        self.assertIsNone(self.state())

    async def test_a_yes_just_inside_the_window_still_answers(self):
        await self.open_confirm()
        self.clock.t += correction.WINDOW_S - 1
        self.assertEqual((await self.hook("да")).kind, "rerun")

    async def test_an_unrelated_command_closes_the_dialog_and_runs_normally(self):
        for path in PATHS:
            self.setUp()
            await self.open_confirm(path)
            route = await (self.hook if path == "hook" else self.turn)("который час")
            self.assertEqual((route.kind, route.intent), ("reply", "time.now"), path)
            self.assertIsNone(self.state(), path)
            self.assertIsNone(self.router.take_question("st1"), path)

    async def test_an_unmatched_reply_goes_on_with_the_question_for_the_brain(self):
        for path in PATHS:
            self.setUp()
            await self.open_confirm(path)
            route = await (self.hook if path == "hook" else self.turn)("а расскажи лучше про дракона")
            self.assertEqual(route.kind, "none", path)
            self.assertIsNone(self.state(), path)
            self.assertEqual(self.router.take_question("st1"), "Мне послышалось «включи свет». Вы имели в виду «выключи свет»?", path)
            self.assertIsNone(self.router.take_question("st1"), path)         # once

    async def test_the_question_is_forgotten_by_the_brain_after_the_window(self):
        await self.open_confirm()
        await self.hook("а расскажи лучше про дракона")
        self.clock.t += correction.WINDOW_S + 1
        self.assertIsNone(self.router.take_question("st1"))

    async def test_an_emergency_in_every_state_is_not_a_reply_and_is_never_swallowed(self):
        verdicts = {"awaiting_confirm": None,
                    "awaiting_choice": lambda e, p: Decision("ask-choice", "low-margin", "", e.primary, (Hyp("включи свет", 0.5), Hyp("выключи свет", 0.5))),
                    "awaiting_repeat": lambda e, p: Decision("relisten", "garbled", "", e.primary)}
        for state, verdict in verdicts.items():
            for phrase in SAFETY:
                for path in PATHS:
                    self.setUp()
                    self.core.verdict = verdict
                    self.light()
                    await (self.hook if path == "hook" else self.turn)("включи свет")
                    self.assertEqual(self.state(), state)
                    route = await (self.hook if path == "hook" else self.turn)(phrase)
                    self.assertEqual((route.kind, route.intent), ("reply", "refuse.emergency"), (state, phrase, path))
                    self.assertIn("сто двенадцать", route.text)
                    self.assertIsNone(self.state(), (state, phrase, path))
                    self.assertIsNone(self.router.take_question("st1"))
                    self.assertEqual(self.ha.calls, [])

    async def test_a_context_less_yes_or_no_does_nothing(self):
        for text in ("да", "нет", "угу", "не то", "отмена", "выключи"):
            for path in PATHS:
                route = await getattr(self, path)(text)
                self.assertNotIn(route.intent or "", ("repair.ask", "repair.rerun", "repair.say"), text)
                self.assertNotEqual(route.kind, "rerun", text)
        self.assertEqual(self.ha.calls, [])
        self.assertIsNone(self.state())

    async def test_the_dialog_is_per_device(self):
        await self.open_confirm()
        route = await self.hook("да", station="st2")
        self.assertEqual(route.kind, "none")
        self.assertEqual(self.state(), "awaiting_confirm")

    async def test_a_reply_the_second_opinion_hears_the_other_way_is_asked_once_more(self):
        await self.open_confirm()
        self.store.put("st1", "да", ("нет", 0.6), ("да", 0.4))
        route = await self.hook("да")
        self.assertEqual((route.kind, route.text, route.listen), ("reply", "Скажите «да» или «нет».", True))
        self.assertEqual(self.state(), "awaiting_confirm")
        self.clock.t += 5
        route = await self.hook("да")
        self.assertEqual((route.text, self.state()), ("Ладно, не буду.", None))
        self.assertEqual(self.ha.calls, [])

    async def test_a_contradicting_reading_below_the_floor_does_not_block_the_reply(self):
        await self.open_confirm()
        self.store.put("st1", "да", ("да", 0.9), ("нет", 0.1))
        self.assertEqual((await self.hook("да")).kind, "rerun")

    async def test_an_agreeing_second_opinion_on_the_reply_is_not_a_conflict(self):
        await self.open_confirm()
        self.store.put("st1", "да", ("да", 0.7), ("ага", 0.3))
        self.assertEqual((await self.hook("да")).kind, "rerun")

    async def test_a_no_the_second_opinion_hears_as_yes_is_asked_once_more_and_never_runs(self):
        await self.open_confirm()
        self.store.put("st1", "нет", ("да", 0.55), ("нет", 0.45))
        route = await self.hook("нет")
        self.assertEqual(route.text, "Скажите «да» или «нет».")

    async def test_a_protected_reading_of_the_reply_vetoes_a_yes(self):
        """The reply text says «да»; the second opinion on the same audio hears an emergency, a cancel or speech to drop: nothing runs."""
        protected = (("вызови скорую", 0.8), ("помогите", 0.2), ("не надо ничего", 0.9), ("отмена", 0.6), ("Продолжение следует", 0.5))
        for path in PATHS:
            for phrase, posterior in protected:
                self.setUp()
                await self.open_confirm(path)
                self.store.put("st1", "да", (phrase, posterior), ("да", 1 - posterior))
                route = await (self.hook if path == "hook" else self.turn)("да")
                self.assertEqual((route.kind, route.text), ("reply", "Скажите «да» или «нет»."), (path, phrase))
                self.assertEqual((self.ha.calls, self.state()), ([], "awaiting_confirm"), (path, phrase))
                second = await (self.hook if path == "hook" else self.turn)("да")
                self.assertEqual((second.kind, second.text), ("reply", "Ладно, не буду."), (path, phrase))
                self.assertEqual((self.ha.calls, self.state()), ([], None), (path, phrase))

    async def test_a_protected_reading_of_the_reply_vetoes_keeping_the_heard_phrase(self):
        await self.open_confirm()
        self.store.put("st1", "включи", ("вызови скорую", 0.8), ("включи", 0.2))
        route = await self.hook("включи")
        self.assertEqual((route.kind, route.text), ("reply", "Скажите «да» или «нет»."))

    async def test_a_protected_reading_below_the_floor_still_vetoes_only_for_an_emergency(self):
        await self.open_confirm()
        self.store.put("st1", "да", ("да", 0.9), ("не надо ничего", 0.1))
        self.assertEqual((await self.hook("да")).kind, "rerun")
        self.setUp()
        await self.open_confirm()
        self.store.put("st1", "да", ("да", 0.95), ("помогите", 0.05))
        self.assertEqual((await self.hook("да")).text, "Скажите «да» или «нет».")

    async def test_an_emergency_said_inside_an_open_dialog_is_the_grammars_and_closes_it(self):
        await self.open_confirm()
        self.store.put("st1", "вызови скорую", ("выключи свет", 0.9))
        route = await self.hook("вызови скорую")
        self.assertEqual((route.kind, route.intent), ("reply", "refuse.emergency"))
        self.assertEqual((self.state(), self.ha.calls), (None, []))

    async def test_a_confirmed_roomless_correction_runs_in_the_room_of_the_speaker(self):
        """Room resolution is the real one (test_ha_actions' registry): the speaker's own id, never the hook's station label."""
        rooms = test_ha_actions
        fake = rooms.FakeHa()
        router = intent_route.IntentRouter(intent_route.load_grammar(), ha=ha_actions.HaActions(fake.get, fake.post), hypotheses=self.store,
                                           correction_mode="on", correction_core=self.core, monotonic=self.clock, pick=lambda options: options[0])
        self.store.put(rooms.LIVING, "сделай холоднее", ("сделай холоднее", 0.2), ("сделай теплее", 0.8))
        for text in ("сделай холоднее", "да"):
            route = await router.route("quasar", text, phase="pre", via_quasar=True, rerun=True, device=rooms.LIVING)
        self.assertNotIn("комнате", route.text)
        self.assertEqual([path for path, _body in fake.services][:1], ["/api/services/climate/set_temperature"])
        self.assertEqual(fake.services[0][1]["entity_id"], "climate.kondei_1")


class ChoiceFlowTests(Setup):
    def setUp(self):
        super().setUp()
        self.core.verdict = lambda e, p: Decision("ask-choice", "low-margin", "", e.primary, (Hyp("включи свет", 0.5), Hyp("выключи свет", 0.5)))

    async def open_choice(self, path="hook"):
        self.light()
        route = await (self.hook if path == "hook" else self.turn)("включи свет")
        self.assertEqual(self.state(), "awaiting_choice")
        return route

    async def test_a_verb_picks_its_reading(self):
        for reply, want in (("выключи", "выключи свет"), ("выключить", "выключи свет"), ("выключи свет", "выключи свет"), ("второе", "выключи свет"),
                            ("включи", "включи свет"), ("включить", "включи свет"), ("первое", "включи свет")):
            self.setUp()
            await self.open_choice()
            route = await self.hook(reply)
            self.assertEqual((route.kind, route.text), ("rerun", want), reply)

    async def test_no_asks_to_repeat_again_and_cancel_work(self):
        first = await self.open_choice()
        route = await self.hook("не то")
        self.assertEqual((route.text, self.state()), ("Повторите, пожалуйста.", "awaiting_repeat"))
        self.setUp()
        first = await self.open_choice()
        route = await self.hook("повтори")
        self.assertEqual((route.text, self.state()), (first.text, "awaiting_choice"))
        self.setUp()
        await self.open_choice()
        self.assertEqual((await self.hook("отмена")).kind, "dialog_end")

    async def test_a_yes_is_no_choice(self):
        await self.open_choice()
        route = await self.hook("да")
        self.assertEqual(route.kind, "none")
        self.assertIsNone(self.state())
        self.assertEqual(self.router.take_question("st1"), "Включить или выключить?")

    async def test_the_choice_is_run_on_the_turn_path_as_a_request_to_say_it_again(self):
        await self.open_choice("turn")
        route = await self.turn("выключи")
        self.assertEqual((route.text, route.listen), ("Скажите ещё раз: выключи свет.", True))

    async def test_the_first_reading_said_at_once_is_an_answer_not_a_duplicate(self):
        for path, kind in (("hook", "rerun"), ("turn", "reply")):
            self.setUp()
            await self.open_choice(path)
            route = await (self.hook if path == "hook" else self.turn)("включи свет")
            self.assertEqual(route.kind, kind, path)
            self.assertEqual(route.text if path == "hook" else route.intent, "включи свет" if path == "hook" else "repair.say_again", path)
            self.assertIsNone(self.state(), path)


class RepeatFlowTests(Setup):
    def setUp(self):
        super().setUp()
        self.core.verdict = lambda e, p: Decision("relisten", "garbled", "", e.primary)

    async def open_repeat(self, path="hook"):
        self.store.put("st1", "бла бла", ("бла", 0.5))
        route = await (self.hook if path == "hook" else self.turn)("бла бла")
        self.assertEqual(self.state(), "awaiting_repeat", route)

    async def test_only_cancel_is_a_reply(self):
        for reply in ("да", "нет", "повтори", "выключи свет"):
            self.setUp()
            await self.open_repeat()
            route = await self.hook(reply)
            self.assertNotIn(route.intent or "", ("repair.say", "repair.ask", "repair.rerun"), reply)
            self.assertIsNone(self.state(), reply)
        self.setUp()
        await self.open_repeat()
        self.assertEqual((await self.hook("отмена")).kind, "dialog_end")

    async def test_a_fresh_utterance_takes_the_normal_route(self):
        for path in PATHS:
            self.setUp()
            await self.open_repeat(path)
            route = await (self.hook if path == "hook" else self.turn)("который час")
            self.assertEqual((route.kind, route.intent), ("reply", "time.now"), path)
            self.assertIsNone(self.state())

    async def test_a_second_unclear_phrase_gives_up(self):
        await self.open_repeat()
        self.store.put("st1", "бла бла бла", ("бла", 0.5))
        route = await self.hook("бла бла бла")
        self.assertEqual((route.text, route.listen), ("Ладно, не буду.", False))

    async def test_the_window_also_bounds_the_repeat(self):
        await self.open_repeat()
        self.clock.t += correction.WINDOW_S + 1
        self.store.put("st1", "бла бла бла", ("бла", 0.5))
        route = await self.hook("бла бла бла")
        self.assertEqual(route.text, "Не расслышал. Повторите, пожалуйста.")       # a new dialog, not the give-up of the old one


class TraceTests(Setup):
    async def traced(self, text, path="hook"):
        trace = utterance_trace.start("hook" if path == "hook" else "turn", text, "st1")
        try:
            route = await (self.hook if path == "hook" else self.turn)(text)
        finally:
            utterance_trace.end(trace)
        return route, trace.data.get("correction")

    async def test_the_question_turn_records_trigger_evidence_and_dialog(self):
        self.light()
        route, record = await self.traced("включи свет")
        self.assertEqual(set(record), {"trigger", "reason", "decision", "heard", "nbest", "nbest_ms", "cue", "dialog"})
        self.assertEqual((record["trigger"], record["decision"], record["heard"], record["nbest_ms"]), ("confusable-disagreement", "ask", "включи свет", 70))
        self.assertEqual(record["nbest"][0], {"text": "выключи свет", "percent": 66, "source": "beam", "engine": "gigaam-v3-ctc", "fit": 0.9})
        self.assertEqual(record["dialog"], {"state": "awaiting_confirm", "ask": route.text, "answer": None, "outcome": None, "retries": 0})
        json.dumps(record, ensure_ascii=False)

    async def test_a_noop_cue_is_in_the_trace(self):
        trace = utterance_trace.start("hook", "включи свет")
        await self.hook("включи свет", noop={"desired": "on", "label": "свет", "targets": []})
        utterance_trace.end(trace)
        self.assertEqual(trace.data["correction"]["cue"], [{"kind": "noop", "supports": "выключи свет"}])
        self.assertEqual(trace.data["correction"]["trigger"], "contradiction")

    async def test_a_withheld_decision_still_records_why(self):
        self.store.put("st1", "включи свет", ("включи свет", 0.9), ("выключи свет", 0.1))
        route, record = await self.traced("включи свет")
        self.assertEqual((route.kind, record["decision"], record["reason"], record["trigger"], "dialog" in record), ("none", "none", "agree", None, False))

    async def test_an_emergency_veto_is_traced(self):
        self.store.put("st1", "вызови скорую", ("выключи свет", 0.9))
        _route, record = await self.traced("вызови скорую")
        self.assertEqual((record["decision"], record["reason"]), ("none", "vetoed"))

    async def test_every_outcome_is_recorded_with_the_answer(self):
        cases = (("да", "accepted"), ("включи", "kept"), ("нет", "denied"), ("отмена", "cancelled"), ("расскажи про дракона", "fallthrough"),
                 ("повтори", None))
        for reply, outcome in cases:
            self.setUp()
            await self.open_confirm()
            self.clock.t += 6
            _route, record = await self.traced(reply)
            dialog = record["dialog"]
            self.assertEqual((dialog["answer"], dialog["outcome"]), (reply, outcome), reply)
            self.assertEqual(set(dialog), {"state", "ask", "answer", "outcome", "retries"})
            self.assertEqual(record["heard"], "включи свет")
        self.setUp()
        await self.open_confirm()
        self.clock.t += 6
        await self.hook("повтори")
        self.clock.t += 6
        _route, record = await self.traced("что")
        self.assertEqual(record["dialog"]["outcome"], "gave_up")
        self.setUp()
        self.core.verdict = lambda e, p: Decision("ask-choice", "low-margin", "", e.primary, (Hyp("включи свет", 0.5), Hyp("выключи свет", 0.5)))
        self.light()
        await self.hook("включи свет")
        _route, record = await self.traced("выключи")
        self.assertEqual(record["dialog"]["outcome"], "chose:выключи свет")

    async def test_repeat_timeout_and_retries(self):
        await self.open_confirm()
        await self.hook("нет")
        _route, record = await self.traced("который час")
        self.assertEqual(record["dialog"]["outcome"], "repeat")
        self.assertEqual(record["dialog"]["retries"], 1)
        self.setUp()
        await self.open_confirm()
        self.clock.t += correction.WINDOW_S + 1
        _route, record = await self.traced("да")
        self.assertEqual((record["dialog"]["outcome"], record["dialog"]["answer"]), ("timeout", None))

    async def test_a_reply_without_a_dialog_has_no_correction_record(self):
        _route, record = await self.traced("да")
        self.assertIsNone(record)


class RealCoreTests(unittest.IsolatedAsyncioTestCase):
    """The clip of tg#30662 through the real core: the policy, the confusables and the percentages are the core's, the wording and the dialog are ours."""

    def setUp(self):
        try:
            import stt_cli.hypotheses as core
        except ImportError:
            self.skipTest("stt_cli.hypotheses is not installed (develop against the stt-cli worktree with PYTHONPATH)")
        self.core = core
        self.clock, self.store = Clock(), Store()
        self.router = intent_route.IntentRouter(intent_route.load_grammar(), hypotheses=self.store, correction_mode="on", monotonic=self.clock,
                                                pick=lambda options: options[0])

    async def test_the_clip_gets_the_repair_question_and_a_yes_runs_the_alternative(self):
        self.store.put("st1", "Включи свет.", ("выключи свет", 0.62), ("включи свет", 0.38))
        route = await self.router.route("quasar", "Включи свет.", phase="pre", via_quasar=True, rerun=True, device="st1")
        self.assertEqual(route.text, "Мне послышалось «Включи свет». Вы имели в виду «выключи свет»?")
        self.assertNotRegex(route.text, r"\d|%")
        route = await self.router.route("quasar", "да", phase="pre", via_quasar=True, rerun=True, device="st1")
        self.assertEqual((route.kind, route.text), ("rerun", "выключи свет"))

    async def test_the_noop_cue_with_no_acoustic_evidence(self):
        route = await self.router.route("quasar", "включи свет", phase="pre", via_quasar=True, rerun=True, device="st1",
                                        noop={"desired": "on", "label": "свет в студии", "targets": ["light.studio"]})
        self.assertEqual(route.text, "Свет в студии уже включён. Вы хотели выключить?")
        route = await self.router.route("quasar", "выключить", phase="pre", via_quasar=True, rerun=True, device="st1")
        self.assertEqual((route.kind, route.text), ("rerun", "выключи свет"))

    async def test_agreeing_recognisers_ask_nothing(self):
        self.store.put("st1", "включи свет", ("включи свет", 0.97), ("выключи свет", 0.03))
        route = await self.router.route("quasar", "включи свет", phase="pre", via_quasar=True, device="st1")
        self.assertEqual(route.kind, "none")

    async def test_every_emergency_phrase_is_vetoed_by_the_real_core(self):
        for phrase in SAFETY:
            self.store.put("st1", phrase, ("выключи свет", 0.7), (phrase, 0.3))
            route = await self.router.route("quasar", phrase, phase="pre", via_quasar=True, device="st1")
            self.assertEqual(route.intent, "refuse.emergency", phrase)
            self.store.put("st1", "включи свет", ("выключи свет", 0.5), (phrase, 0.4), ("включи свет", 0.1))
            route = await self.router.route("quasar", "включи свет", phase="pre", via_quasar=True, device="st1")
            self.assertEqual(route.kind, "none", phrase)
            self.assertIsNone(self.router.correction.dialogs.get("st1"), phrase)


if __name__ == "__main__":
    unittest.main()
