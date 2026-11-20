"""The correction engine and the repair dialog of the speaker (#182): ask instead of acting on a doubtful hearing.

A recogniser says «включи свет», the light is already on, and the speaker answers «Свет уже включён». The owner said «Выключи
свет». This module turns the evidence the speaker has (a second opinion with percentages stored by the gateway, or a no-op the
quasar layer found) into at most ONE spoken question, and keeps the small per-station state machine that reads the answer.

Layers (nothing here re-implements them):
  stt_cli.hypotheses   the shared core: `repair.decide` (the policy), the confusables data, the NBest model. Imported lazily; a missing
                       package turns the engine off, the old path continues.
  intent_grammar       the contexts `repair.confirm|choice|repeat` and their standard replies (grammar/ru.yaml); the per-turn words
                       of a question (the alternative, the heard verb) ride in the `ActiveContext` this module builds.
  intent_route         runs the dialog's outcomes (it owns every executor and the overlay's `rerun` route); see `IntentRouter.route`.

States per station (`Dialog.state`), window `WINDOW_S`, injected clock (`now` is the router's monotonic clock):
  awaiting_confirm   «Мне послышалось «X». Вы имели в виду «Y»?»   да -> run Y   нет -> repeat   X -> run X   повтори -> again   отмена -> end
  awaiting_choice    «Включить или выключить?»                     a verb -> run it   нет -> repeat   повтори -> again   отмена -> end
  awaiting_repeat    «Не расслышал. Повторите, пожалуйста.»        only «отмена» is a reply; anything else is a fresh utterance
Anything the context does not read leaves it (outcome `fallthrough`) and takes its normal route; the timeout is checked lazily when
the next utterance arrives. One retry (a repeat request, a question said again, a «да»/«нет» that the second opinion contradicts), then
«Ладно, не буду.» and the context is closed.

Safety, in code and tests: nothing runs from a hypothesis without a spoken «да» (or the reading said back); the engine vetoes every
utterance whose primary reading or any listed hypothesis or proposed alternative is an emergency phrase (grammar intent
`refuse.emergency`) or a dropped one (hallucination captions, fillers), and never opens a context over them; an action whose executor is
outside `SAFE_EXECUTORS` is held and asked on ANY disagreement and is never handed to the overlay's `rerun`; a context-less «да» is no reply.
The same protected readings veto the ANSWER: a «да» (or a kept or chosen reading) whose audio the second opinion also hears as an emergency phrase,
a cancel, an opposite answer or speech to drop runs nothing (asked «Скажите «да» или «нет».» once, then «Ладно, не буду.»).
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

import utterance_trace
from intent_grammar import ActiveContext, Decision

MODES = ("off", "shadow", "on")
WINDOW_S = 30.0          # how long a question waits for its answer (the router's CLARIFY_WINDOW_S)
DUPLICATE_S = 4.0        # the same utterance delivered again this soon after the question is the same delivery (hook, turn, no-op probe), not an answer
COOLDOWN_S = 60.0        # after a dialog about a phrase, the same phrase opens no new dialog for this long: a wrong hearing that repeats cannot loop

EMERGENCY_INTENT = "refuse.emergency"
# Executors whose mistaken run is a light, a volume step, a media key or the end of a dialog: the dialog may offer a swap on a
# modest disagreement and the overlay may rerun the corrected phrase. Everything else (kettle, sockets, TV and projector, climate,
# timers and alarms, locks) is risky: held and asked on any disagreement, and a «да» only ever runs it through the normal route.
SAFE_EXECUTORS = frozenset(("ha.light", "ha.light_color", "ha.light_brightness", "speaker.volume", "speaker.volume_delta",
                            "speaker.volume_query", "media.key", "music.play", "dialog.end", "dialog.repeat_last"))

CONFIRM, CHOICE, REPEAT = "awaiting_confirm", "awaiting_choice", "awaiting_repeat"
CONTEXT_OF = {CONFIRM: "repair.confirm", CHOICE: "repair.choice", REPEAT: "repair.repeat"}

RELISTEN_TEXT = "Не расслышал. Повторите, пожалуйста."
REPEAT_TEXT = "Повторите, пожалуйста."
GIVE_UP_TEXT = "Ладно, не буду."
YES_OR_NO_TEXT = "Скажите «да» или «нет»."
SAY_AGAIN_TEXT = "Скажите ещё раз: {phrase}."
DONE_TEXT = "Хорошо, {phrase}."

_PUNCTUATION = " .,!?…;:-—«»\"'"


def shown(text: str) -> str:
    """A phrase as it is spoken inside a question: no edge punctuation."""
    return text.strip(_PUNCTUATION)


@dataclass(frozen=True)
class Reading:
    """One phrase the dialog may run: its text, the short label a choice question uses and what a reply may say for it
    (the whole phrase and the verb alone: «выключи свет», «выключи»)."""
    text: str
    label: str | None
    says: tuple[str, ...]


@dataclass
class Dialog:
    """One open question of one station."""
    station: str
    state: str
    question: str
    heard: str                              # the primary reading as recognised
    readings: tuple[Reading, ...]           # confirm: (alternative,); choice: (first, second); repeat: ()
    heard_says: tuple[str, ...]             # what a reply may say to keep the heard reading
    asked_at: float
    risky: bool                             # an action outside SAFE_EXECUTORS is involved: never handed to the overlay's rerun
    origin: dict[str, Any]                  # the trace of the turn that asked (trigger, evidence), repeated in the answer turns
    retries: int = 0
    opened_at: float = 0.0                  # when the question was first asked (the duplicate-delivery window counts from here)
    answered: bool = False                  # any reply was read: a later identical utterance is an answer, not a duplicate delivery
    asked_again: bool = False               # «повтори» is answered once
    reasked: bool = False                   # the «да»/«нет» that the second opinion contradicted is asked once more
    canon: str = ""                         # normalised heard text (duplicate delivery and cooldown)
    via_quasar: bool = False                # the question was asked on the quasar hook (not the speaker's own turn): the channel an immediate repeat is judged against

    def context(self) -> ActiveContext:
        """The grammar context this question is open in, with this turn's words."""
        if self.state == CONFIRM:
            alt = self.readings[0]
            return ActiveContext(CONTEXT_OF[CONFIRM], {"alt": alt.says, "heard": self.heard_says})
        if self.state == CHOICE:
            return ActiveContext(CONTEXT_OF[CHOICE], {"first": self.readings[0].says, "second": self.readings[1].says})
        return ActiveContext(CONTEXT_OF[REPEAT])


@dataclass(frozen=True)
class Offer:
    """What `consult` found: a question to speak, a give-up, or (shadow) only the verdict."""
    say: str
    listen: bool
    outcome: str = ""                       # "" for a question; gave_up when the retry was already used
    shadow: bool = False                    # the decision was made and traced, nothing is said or opened


@dataclass(frozen=True)
class Step:
    """What a reply inside a dialog means for the router."""
    outcome: str = ""                       # accepted|denied|kept|chose:<text>|cancelled|gave_up|repeat; "" = the dialog goes on unchanged
    say: str | None = None
    listen: bool = False
    run: str | None = None                  # the phrase to run for real (alternative, kept reading or chosen one)
    risky: bool = False                     # `run` belongs to a risky action: never the overlay's rerun
    end: bool = False                       # «отмена»: the dialog ends, nothing is said


def _compound_key(parts: Iterable[Decision]) -> str | None:
    """The key of a compound's clauses joined; None as soon as one clause has no executable action."""
    keys = [action_key(part) for part in parts]
    if None in keys:
        return None
    return "+".join(key for key in keys if key)


def action_key(decision: Decision | None) -> str | None:
    """The executable action of a grammar decision, canonical: texts with the same key are the same action. None: no action."""
    if decision is None:
        return None
    if decision.parts:
        return _compound_key(decision.parts)
    if decision.action is None or decision.tier in ("drop", "reprompt", "clarify"):
        return None
    action = decision.action
    return f"{action['executor']}|{json.dumps(action.get('args') or {}, sort_keys=True, ensure_ascii=False)}"


def executors_of(decision: Decision | None) -> frozenset[str]:
    if decision is None:
        return frozenset()
    if decision.parts:
        return frozenset(executor for part in decision.parts for executor in executors_of(part))
    return frozenset((decision.action["executor"],)) if decision.action else frozenset()


def import_core() -> Any:
    """The shared hypotheses core (stt-cli's `stt_cli.hypotheses`), imported when the engine is built so a host without it still starts."""
    import stt_cli.hypotheses as core
    return core


def state_word(label: str, on: bool) -> str:
    """«включён» agreeing with the head noun of the label («свет» -> включён, «лампа» -> включена, «лампы» -> включены)."""
    head = label.split()[0] if label.split() else ""
    stem = "включ" if on else "выключ"
    if head.endswith(("а", "я")):
        return stem + "ена"
    if head.endswith(("ы", "и")):
        return stem + "ены"
    if head.endswith(("о", "е")):
        return stem + "ено"
    return stem + "ён" if on else stem + "ен"


def trace_nbest(nbest: Any) -> list[dict[str, Any]]:
    """The listed readings for the trace: text, percent (the share among the listed readings, never spoken), source, engine, fit."""
    return [{"text": h.text, "percent": h.percent, "source": h.source, "engine": h.engine,
             "fit": None if h.fit is None else round(h.fit, 3)} for h in nbest.hypotheses]


class Correction:
    """The engine of one speaker: `consult` an utterance for a question, then read the answer with `reply`."""

    def __init__(self, classify: Callable[[str], Decision | None], decide_in: Callable[[str, ActiveContext], Decision | None],
                 hypotheses: Any | None = None, *, mode: str = "on", core: Any | None = None, policy: Any | None = None,
                 window_s: float = WINDOW_S) -> None:
        if mode not in MODES or mode == "off":
            raise ValueError(f"correction mode must be shadow or on, got {mode!r}")
        self.mode = mode
        self.classify = classify
        self.decide_in = decide_in
        self.hypotheses = hypotheses
        self.core = core if core is not None else import_core()
        self.policy = policy if policy is not None else self.core.Policy()
        self.confusables = self.core.load()
        self.window_s = window_s
        self.dialogs: dict[str, Dialog] = {}
        self.expired: dict[str, Dialog] = {}               # the dialog whose window ran out, until the next utterance reports it
        self.chains: dict[str, float] = {}                  # station -> until: a repeat was asked, the next doubtful utterance gives up
        self.quiet: dict[tuple[str, str], float] = {}       # (station, normalised phrase) -> until: no new dialog about it
        self.hints: dict[str, tuple[str, float]] = {}       # station -> (question, asked at): an unanswered question for the brain

    # ── state ──────────────────────────────────────────────────────────────
    def active(self, station: str, now: float) -> Dialog | None:
        """The open dialog of the station; one whose window ran out is closed here and reported by `take_expired`."""
        dialog = self.dialogs.get(station)
        if dialog is not None and now - dialog.asked_at > self.window_s:
            del self.dialogs[station]
            self.expired[station] = dialog
            self.chains.pop(station, None)
            return None
        return dialog

    def take_expired(self, station: str) -> Dialog | None:
        return self.expired.pop(station, None)

    def close(self, station: str) -> None:
        self.dialogs.pop(station, None)

    def take_question(self, station: str, now: float) -> str | None:
        """The question a reply did not answer, once, while its window lasts: the brain gets it with the turn text."""
        hint = self.hints.pop(station, None)
        return hint[0] if hint is not None and now - hint[1] <= self.window_s else None

    def drop_hint(self, station: str) -> None:
        self.hints.pop(station, None)

    def mute(self, station: str, texts: tuple[str, ...], now: float) -> None:
        """No new dialog about these phrases for COOLDOWN_S (the phrase that was just run or just refused comes back the same)."""
        for text in texts:
            self.quiet[(station, self._canon(text))] = now + COOLDOWN_S

    def _canon(self, text: str) -> str:
        return self.core.normalize(text)

    # ── evidence ───────────────────────────────────────────────────────────
    def _readings(self, text: str) -> tuple[Decision, ...]:
        """The grammar's reading of the text and of each clause of a compound."""
        decision = self.classify(text)
        return () if decision is None else (decision, *decision.parts)

    def _emergency(self, text: str) -> bool:
        return any(part.intent == EMERGENCY_INTENT for part in self._readings(text))

    def _protected(self, text: str) -> bool:
        """The grammar reads the text as an emergency or as speech to drop: no dialog may touch it."""
        return any(part.intent == EMERGENCY_INTENT or part.tier == "drop" for part in self._readings(text))

    def stored(self, station: str, text: str, now: float) -> tuple[Any | None, dict[str, Any] | None]:
        """The gateway's second opinion on the audio behind `text` (hypothesis_store.HypothesisStore: live for a few seconds, `now` is the clock)."""
        if self.hypotheses is None:
            return None, None
        payload = self.hypotheses.get(station, text, now)
        if not payload:
            return None, None
        return self.core.NBest.from_dict(payload["nbest"]), payload

    def _cues(self, text: str, noop: Mapping[str, Any] | None) -> tuple[tuple[Any, ...], Any | None]:
        """The no-op cue of the quasar layer as a core `Cue` plus the alternative it argues for (the opposite reading of the text)."""
        if not isinstance(noop, Mapping) or noop.get("desired") not in ("on", "off"):
            return (), None
        for candidate in self.confusables.candidates(text):
            if candidate.kind == "opposite":
                return (self.core.Cue("noop", candidate.text, 1.0),), candidate
        return (), None

    def _policy(self, text: str, nbest: Any | None) -> tuple[Any, bool]:
        """The policy for this utterance and whether it is risky: a reading outside SAFE_EXECUTORS is asked about on ANY disagreement."""
        texts = [text] + ([h.text for h in nbest.hypotheses] if nbest is not None else [])
        risky = self._risky(texts)
        if not risky:
            return self.policy, False
        return dataclasses.replace(self.policy, ask_floor=self.policy.cue_floor, primary_weak=1.0, margin_max=1.0), True

    def _risky(self, texts: Iterable[str]) -> bool:
        """Some reading's action is outside SAFE_EXECUTORS."""
        return any(not executors_of(self.classify(t)) <= SAFE_EXECUTORS for t in texts)

    def _vetoed(self, text: str, nbest: Any | None) -> bool:
        """The grammar reads the text, or any listed hypothesis, as an emergency or as speech to drop."""
        return self._protected(text) or (nbest is not None and any(self._protected(h.text) for h in nbest.hypotheses))

    # ── the question ───────────────────────────────────────────────────────
    def consult(self, station: str, text: str, *, now: float, via_quasar: bool, noop: Mapping[str, Any] | None = None) -> Offer | None:
        """Does this utterance deserve a question? Builds the evidence, lets the core policy decide, and (mode `on`) opens the dialog.
        None: ask nothing, the normal route carries on. The verdict goes to the utterance trace either way (`trace.correction`)."""
        chain_until = self.chains.pop(station, 0.0)   # a repeat was asked and this is (the answer to) it: the one retry
        if self.quiet.get((station, self._canon(text)), 0.0) > now:
            return None
        nbest, payload = self.stored(station, text, now)
        cues, candidate = self._cues(text, noop)
        if nbest is None and not cues:
            return None
        veto = self._vetoed(text, nbest)
        policy, risky = self._policy(text, nbest)
        evidence = self.core.Evidence(primary=text, nbest=nbest, action_of=lambda t: action_key(self.classify(t)), cues=cues, vetoed=veto,
                                      confusables=self.confusables)
        decision = self.core.decide(evidence, policy)
        origin = self._origin(text, decision, nbest, payload, cues)
        veto = veto or any(self._protected(option.text) for option in decision.options)   # the proposed alternative too (a candidate the core made up)
        if veto or decision.kind == "none":
            utterance_trace.note(correction={**origin, "decision": "none", "reason": "vetoed"} if veto else origin)
            return None
        risky = risky or self._risky(option.text for option in decision.options)
        dialog = self._dialog(station, text, decision, origin, risky, noop if candidate is not None else None, now)
        if dialog is None:
            utterance_trace.note(correction={**origin, "decision": "none", "reason": "no-wording"})
            return None
        dialog.via_quasar = via_quasar
        return self._open(station, text, dialog, origin, chain_until, now)

    def _origin(self, text: str, decision: Any, nbest: Any | None, payload: dict[str, Any] | None, cues: tuple[Any, ...]) -> dict[str, Any]:
        """What the trace of a consultation says about the evidence and the core's verdict (`shadow`: nothing is said or opened)."""
        origin = {"trigger": decision.trigger or None, "reason": decision.reason or None, "decision": decision.kind, "heard": text,
                  "nbest": trace_nbest(nbest) if nbest is not None else [], "nbest_ms": (payload or {}).get("ms"),
                  "cue": [{"kind": cue.kind, "supports": cue.supports} for cue in cues]}
        if self.mode == "shadow":
            origin["shadow"] = True
        return origin

    def _open(self, station: str, text: str, dialog: Dialog, origin: dict[str, Any], chain_until: float, now: float) -> Offer:
        """Say the question (shadow: only trace it) and open its dialog; a second doubt right after a repeat request is a polite end."""
        if self.mode == "shadow":
            utterance_trace.note(correction={**origin, "dialog": {"state": dialog.state, "ask": dialog.question, "answer": None,
                                                                    "outcome": None, "retries": 0}})
            return Offer(dialog.question, True, shadow=True)
        if chain_until > now:   # the retry was used: a second doubt in a row is a polite end
            self.mute(station, (text,), now)
            utterance_trace.note(correction={**origin, "dialog": self._dialog_trace(dialog, None, "gave_up")})
            return Offer(GIVE_UP_TEXT, False, outcome="gave_up")
        self.dialogs[station] = dialog
        utterance_trace.note(correction={**origin, "dialog": self._dialog_trace(dialog, None, None)})
        return Offer(dialog.question, True)

    def _dialog(self, station: str, text: str, decision: Any, origin: dict[str, Any], risky: bool, noop: Mapping[str, Any] | None,
                now: float) -> Dialog | None:
        options = list(decision.options)
        common = dict(station=station, heard=text, asked_at=now, opened_at=now, risky=risky, origin=origin, canon=self._canon(text))
        if decision.kind == "relisten":
            return Dialog(state=REPEAT, question=RELISTEN_TEXT, readings=(), heard_says=(), **common)
        if decision.kind == "ask" and len(options) == 1:
            alt = options[0]
            alt_reading, heard_reading = self._reading(alt.text, text), self._reading(text, alt.text)
            label = getattr(alt, "label", None) or alt_reading.label
            question = self._confirm_text(text, alt.text, label, noop)
            return Dialog(state=CONFIRM, question=question, readings=(alt_reading,), heard_says=heard_reading.says, **common)
        if decision.kind == "ask-choice" and len(options) >= 2:
            first, second = (options[1], options[2]) if len(options) >= 3 else (options[0], options[1])   # three: the heard reading is the weak one
            one, two = self._reading(first.text, second.text), self._reading(second.text, first.text)
            question = f"{shown(one.label or one.text).capitalize()} или {shown(two.label or two.text)}?"
            return Dialog(state=CHOICE, question=question, readings=(one, two), heard_says=(), **common)
        return None

    def _reading(self, text: str, counterpart: str) -> Reading:
        """`text` as one side of a choice with `counterpart`: its spoken label and the verb that tells it apart, from the confusables."""
        label, verb = None, None
        for candidate in self.confusables.candidates(counterpart, limit=50):
            if self._canon(candidate.text) == self._canon(text):
                label, verb = candidate.label, candidate.swapped[1]
                break
        says = tuple(dict.fromkeys(phrase for phrase in (shown(text), shown(verb) if verb else None) if phrase))
        return Reading(shown(text), label, says)

    @staticmethod
    def _confirm_text(heard: str, alt: str, label: str | None, noop: Mapping[str, Any] | None) -> str:
        """«Мне послышалось «включи свет». Вы имели в виду «выключи свет»?»; after the quasar layer's no-op:
        «Свет в студии уже включён. Вы хотели выключить?». Never a percentage."""
        if noop is not None and isinstance(noop.get("label"), str) and noop["label"].strip():
            subject = shown(noop["label"])[:80]
            if subject:
                state = state_word(subject, noop["desired"] == "on")
                return f"{subject[0].upper()}{subject[1:]} уже {state}. Вы хотели {shown(label) if label else shown(alt)}?"
        return f"Мне послышалось «{shown(heard)}». Вы имели в виду «{shown(alt)}»?"

    # ── the answer ─────────────────────────────────────────────────────────
    def reply(self, station: str, text: str, *, now: float, via_quasar: bool, probe: bool = False) -> Step | None:
        """What `text` means inside the open dialog of `station`. None: it is no reply; the dialog is closed (outcome `fallthrough`,
        the question kept for the brain) and the text takes its normal route. Call only while `active` says a dialog is open.
        `via_quasar`: the channel of this delivery; `probe`: the quasar layer's no-op call (see `_same_delivery`)."""
        dialog = self.dialogs[station]
        if self._same_delivery(dialog, text, now, via_quasar, probe):
            return Step(say=dialog.question, listen=True)
        return self._answer(station, dialog, text, now)

    def _same_delivery(self, dialog: Dialog, text: str, now: float, via_quasar: bool, probe: bool) -> bool:
        """The utterance that opened the question, delivered again before anything answered it: the same audio arriving on the other
        channel (hook and turn path) or the quasar layer's no-op probe call. The same words on the channel that asked, without the
        probe, are the speaker's answer («включи свет» to «Включить или выключить?»)."""
        return (not dialog.answered and self._canon(text) == dialog.canon and now - dialog.opened_at < DUPLICATE_S
                and (probe or via_quasar != dialog.via_quasar))

    def _answer(self, station: str, dialog: Dialog, text: str, now: float) -> Step | None:
        dialog.answered = True
        context = dialog.context()
        decision = self.decide_in(text, context)
        intent = decision.intent if decision is not None and decision.tier == "execute" and not decision.fuzzy else None
        if intent is None or not intent.startswith("repair."):
            return self._leave(station, dialog, text, "fallthrough", now)
        step = self._step(dialog, intent, decision, text, now, context)
        if step.end or step.outcome:
            self._finish(station, dialog, step, text, now)
        else:
            self._note(dialog, text, step.outcome or None)
        return step

    def _step(self, dialog: Dialog, intent: str, decision: Decision, text: str, now: float, context: ActiveContext) -> Step:
        if intent == "repair.cancel" or dialog.state == REPEAT:   # the repeat context reads nothing else
            return Step(outcome="cancelled", end=True)
        if intent != "repair.again" and self._doubted(dialog, intent, text, context, now):
            return self._ask_once_more(dialog, now)
        if intent == "repair.again":
            if dialog.asked_again:
                return Step(outcome="gave_up", say=GIVE_UP_TEXT)
            dialog.asked_again, dialog.asked_at, dialog.retries = True, now, dialog.retries + 1
            return Step(say=dialog.question, listen=True)
        if intent == "repair.deny":
            return Step(outcome="denied", say=REPEAT_TEXT, listen=True)
        if intent == "repair.accept":
            return Step(outcome="accepted", run=dialog.readings[0].text, risky=dialog.risky)
        if intent == "repair.keep":
            return Step(outcome="kept", run=dialog.heard, risky=dialog.risky)
        pick = (decision.action.get("args") or {}).get("pick") if decision.action else None
        chosen = dialog.readings[0 if pick == "first" else 1]
        return Step(outcome=f"chose:{chosen.text}", run=chosen.text, risky=dialog.risky)

    @staticmethod
    def _ask_once_more(dialog: Dialog, now: float) -> Step:
        """The reply is not acted on: «Скажите «да» или «нет»» once, then a polite end."""
        if dialog.reasked:
            return Step(outcome="gave_up", say=GIVE_UP_TEXT)
        dialog.reasked, dialog.asked_at, dialog.retries = True, now, dialog.retries + 1
        return Step(say=YES_OR_NO_TEXT, listen=True)

    def _doubted(self, dialog: Dialog, intent: str, text: str, context: ActiveContext, now: float) -> bool:
        """The second opinion on the reply itself argues against acting on it. Any listed hypothesis that the grammar reads as an
        emergency vetoes a reply that runs something (at any posterior, as for a question); one at the policy floor that reads as
        speech to drop, a cancel or the opposite answer (Whisper «да», the other model «нет») vetoes the reply too."""
        nbest, _payload = self.stored(dialog.station, text, now)
        if nbest is None:
            return False
        runs = intent != "repair.deny"
        for hypothesis in nbest.hypotheses:
            if self._canon(hypothesis.text) == self._canon(text):
                continue
            if runs and self._emergency(hypothesis.text):
                return True
            if hypothesis.posterior >= self.policy.ask_floor and self._argues_against(hypothesis.text, context, runs):
                return True
        return False

    def _argues_against(self, hypothesis: str, context: ActiveContext, runs: bool) -> bool:
        """The reading `hypothesis` of the reply's audio contradicts the reply: a cancel or the opposite answer, or (a reading the dialog
        does not know at all) speech to drop such as a hallucinated caption. A filler the dialog reads as a yes («ага») is no conflict."""
        other = self.decide_in(hypothesis, context)
        if other is not None and not other.fuzzy and other.intent.startswith("repair."):
            return other.intent in (("repair.deny", "repair.cancel") if runs else ("repair.accept",))
        return runs and self._protected(hypothesis)

    def _leave(self, station: str, dialog: Dialog, text: str, outcome: str, now: float) -> None:
        """A text the dialog does not read: close it, keep the question for the brain."""
        del self.dialogs[station]
        self.hints[station] = (dialog.question, dialog.asked_at)
        if dialog.state == REPEAT:
            outcome = "repeat"   # the fresh utterance IS the repeat the speaker asked for; the retry is used up
            self.chains[station] = now + self.window_s
            self.hints.pop(station, None)
        self._note(dialog, text, outcome)
        return None

    def _finish(self, station: str, dialog: Dialog, step: Step, text: str, now: float) -> None:
        """An outcome that ends the dialog (or turns it into a repeat request)."""
        del self.dialogs[station]
        if step.outcome == "denied":
            dialog = dataclasses.replace(dialog, state=REPEAT, question=REPEAT_TEXT, asked_at=now, retries=dialog.retries + 1)
            self.dialogs[station] = dialog
            self._note(dialog, text, "denied")
            return
        if step.outcome in ("accepted", "kept") or step.outcome.startswith("chose:"):
            self.mute(station, (dialog.heard, *(r.text for r in dialog.readings)), now)
        elif step.outcome in ("gave_up", "cancelled"):
            self.mute(station, (dialog.heard,), now)
        self.chains.pop(station, None)
        self._note(dialog, text, step.outcome)

    # ── trace ──────────────────────────────────────────────────────────────
    @staticmethod
    def _dialog_trace(dialog: Dialog, answer: str | None, outcome: str | None) -> dict[str, Any]:
        return {"state": dialog.state, "ask": dialog.question, "answer": answer, "outcome": outcome, "retries": dialog.retries}

    def _note(self, dialog: Dialog, answer: str | None, outcome: str | None) -> None:
        utterance_trace.note(correction={**dialog.origin, "dialog": self._dialog_trace(dialog, answer, outcome)})

    def note_expired(self, dialog: Dialog) -> None:
        """The window of an earlier question ran out: this utterance's trace says so (the question was never answered)."""
        self._note(dialog, None, "timeout")
