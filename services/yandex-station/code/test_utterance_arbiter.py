"""Two speakers hear one utterance: exactly one session acts (utterance_arbiter)."""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from utterance_arbiter import ACT, DUPLICATE, Arbiter, same_utterance

SENTENCE = "Алиса, включи кондиционер на втором этаже на обогрев."


@pytest.mark.parametrize("first, second", [
    (SENTENCE, SENTENCE),
    (SENTENCE, "включи кондиционер на втором этаже на обогрев"),                      # wake word cut off on one speaker
    (SENTENCE, "Алиса, ключи кондиционер на втором этаже на обогрев."),               # one word misheard
    ("Алиса, громче", "Громче."),                                                        # one-word command
    ("Алиса, включи кондиционер на втором этаже.", "Алиса, включи кондиционер на втором этаже на обогрев."),
])
def test_same_sentence_heard_by_two_microphones(first, second):
    assert same_utterance(first, second)


@pytest.mark.parametrize("first, second", [
    ("Алиса, включи свет на кухне", "Алиса, выключи свет на кухне"),
    ("Алиса, поставь таймер на десять минут", "Алиса, поставь таймер на пять минут"),
    ("Алиса, громкость 5", "Алиса, громкость 7"),
    ("Алиса, громче", "Алиса, выключи свет на кухне"),
    ("Алиса, включи кондиционер на втором этаже", "Алиса, поставь таймер на десять минут"),
    ("", SENTENCE),
    ("Алиса.", SENTENCE),                                                                # the wake word alone says nothing
])
def test_different_commands_are_not_the_same_utterance(first, second):
    assert not same_utterance(first, second)


def run(coroutine):
    return asyncio.run(coroutine)


def test_a_lone_speaker_never_waits():
    async def scenario():
        arbiter = Arbiter(grace=5.0)
        only = arbiter.open()
        started = time.monotonic()
        verdict = await arbiter.claim(only, SENTENCE, level=10.0)
        return verdict, time.monotonic() - started
    verdict, waited = run(scenario())
    assert verdict == ACT
    assert waited < 0.5


def test_two_speakers_one_utterance_the_louder_acts():
    async def scenario():
        arbiter = Arbiter(grace=2.0)
        far, near = arbiter.open(), arbiter.open()
        verdicts = await asyncio.gather(arbiter.claim(far, SENTENCE, level=8.0),
                                        arbiter.claim(near, "включи кондиционер на втором этаже на обогрев", level=30.0))
        return verdicts, near.id, far.winner
    (far_verdict, near_verdict), near_id, winner = run(scenario())
    assert (far_verdict, near_verdict) == (DUPLICATE, ACT)
    assert winner == near_id


def test_equal_levels_the_session_that_opened_first_acts():
    async def scenario():
        arbiter = Arbiter(grace=2.0)
        first, second = arbiter.open(), arbiter.open()
        return await asyncio.gather(arbiter.claim(second, SENTENCE, level=20.0), arbiter.claim(first, SENTENCE, level=21.0))
    second_verdict, first_verdict = run(scenario())
    assert (first_verdict, second_verdict) == (ACT, DUPLICATE)


def test_different_commands_at_the_same_moment_both_act():
    async def scenario():
        arbiter = Arbiter(grace=2.0)
        kitchen, bedroom = arbiter.open(), arbiter.open()
        return await asyncio.gather(arbiter.claim(kitchen, "Алиса, выключи свет на кухне", level=20.0),
                                    arbiter.claim(bedroom, "Алиса, поставь таймер на десять минут", level=9.0))
    assert run(scenario()) == [ACT, ACT]


def test_a_person_repeating_a_command_on_one_speaker_is_not_a_duplicate():
    async def scenario():
        arbiter = Arbiter(grace=2.0)
        first = arbiter.open()
        assert await arbiter.claim(first, SENTENCE, level=20.0) == ACT
        await arbiter.close(first)
        second = arbiter.open()     # the same speaker, a new session: the intervals do not overlap
        return await arbiter.claim(second, SENTENCE, level=20.0)
    assert run(scenario()) == ACT


def test_sessions_opened_far_apart_are_not_partners_even_when_both_are_open():
    async def scenario():
        clock = [0.0]
        arbiter = Arbiter(window=2.0, grace=0.2, clock=lambda: clock[0])
        early = arbiter.open()
        clock[0] = 10.0
        late = arbiter.open()
        return await arbiter.claim(early, SENTENCE, level=20.0), await arbiter.claim(late, SENTENCE, level=5.0)
    assert run(scenario()) == (ACT, ACT)


def test_a_partner_that_hears_only_noise_does_not_hold_the_other_one_back():
    async def scenario():
        arbiter = Arbiter(grace=5.0)
        speaker, noisy = arbiter.open(), arbiter.open()
        started = time.monotonic()
        claim = asyncio.ensure_future(arbiter.claim(speaker, SENTENCE, level=20.0))
        await asyncio.sleep(0.05)
        assert not claim.done()                      # waits while the partner is still recognizing
        await arbiter.abstain(noisy)                 # the partner's transcript was a hallucination
        return await claim, time.monotonic() - started
    verdict, waited = run(scenario())
    assert verdict == ACT
    assert waited < 1.0


def test_a_silent_partner_delays_the_answer_by_the_grace_at_most():
    async def scenario():
        arbiter = Arbiter(grace=0.3)
        speaker, _silent = arbiter.open(), arbiter.open()
        started = time.monotonic()
        verdict = await arbiter.claim(speaker, SENTENCE, level=20.0)
        return verdict, time.monotonic() - started
    verdict, waited = run(scenario())
    assert verdict == ACT
    assert 0.25 <= waited < 1.0


def test_a_late_partner_loses_to_the_session_that_already_acted_even_when_louder():
    async def scenario():
        arbiter = Arbiter(grace=0.2)
        quiet, loud = arbiter.open(), arbiter.open()
        first = await arbiter.claim(quiet, SENTENCE, level=6.0)      # the loud partner is still recognizing, grace runs out
        second = await arbiter.claim(loud, SENTENCE, level=60.0)
        return first, second, quiet.id, loud.winner
    first, second, quiet_id, winner = run(scenario())
    assert (first, second) == (ACT, DUPLICATE)
    assert winner == quiet_id


def test_three_speakers_one_utterance_one_action():
    async def scenario():
        arbiter = Arbiter(grace=2.0)
        sessions = [arbiter.open() for _ in range(3)]
        levels = [12.0, 40.0, 25.0]
        return await asyncio.gather(*(arbiter.claim(s, SENTENCE, level=l) for s, l in zip(sessions, levels)))
    assert run(scenario()) == [DUPLICATE, ACT, DUPLICATE]


def test_a_partner_whose_claim_was_cancelled_does_not_swallow_the_answer():
    async def scenario():
        arbiter = Arbiter(grace=5.0)
        loud, quiet = arbiter.open(), arbiter.open()
        waiting = asyncio.ensure_future(arbiter.claim(loud, SENTENCE, level=60.0))   # waits: the quiet partner is still recognizing
        await asyncio.sleep(0.05)
        waiting.cancel()                                                              # its websocket went away
        await asyncio.gather(waiting, return_exceptions=True)
        await arbiter.close(loud)
        return await arbiter.claim(quiet, SENTENCE, level=5.0)
    assert run(scenario()) == ACT


def test_a_loser_is_not_rejected_until_the_chosen_winner_has_committed():
    async def scenario():
        arbiter = Arbiter(grace=0.15)
        quiet, loud, silent = arbiter.open(), arbiter.open(), arbiter.open()   # the third never says anything
        first = asyncio.ensure_future(arbiter.claim(quiet, SENTENCE, level=6.0))
        await asyncio.sleep(0.05)
        second = asyncio.ensure_future(arbiter.claim(loud, SENTENCE, level=60.0))
        await asyncio.sleep(0.08)
        second.cancel()                                  # the louder speaker's websocket drops before it committed
        await asyncio.gather(second, return_exceptions=True)
        await arbiter.close(loud)
        return await first
    assert run(scenario()) == ACT                        # somebody must answer


def test_levels_within_three_db_tie_whatever_the_bucket_boundaries():
    async def scenario():
        arbiter = Arbiter(grace=2.0)
        earlier, later = arbiter.open(), arbiter.open()
        return await asyncio.gather(arbiter.claim(earlier, SENTENCE, level=100.0), arbiter.claim(later, SENTENCE, level=110.0))
    assert run(scenario()) == [ACT, DUPLICATE]           # 0.83 dB apart: the session that opened first acts
