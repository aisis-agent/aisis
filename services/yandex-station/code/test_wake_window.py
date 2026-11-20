"""The activation word cut from the audio (wake_window.py): where the history burst ends, where the name ends inside it, and
the gate that holds the first audio of a stream until that is known.

The layout behind the tests is the station's real one (gateway journal 2026-11-10 … 11-17, clip store): an activation stream
opens with the ~2.3 s the station buffered before its spotter fired, sent faster than real time, then live audio; the name
ends 0.2-0.4 s before the end of that history and the request's first word can start 0.08 s after the name. A follow-up
stream (no wake word) has no history. Levels below are RMS per 20 ms frame in the station's own units: a quiet room is 1-6,
the name 8-40.
"""
import array
import json
import os
import unittest

import wake_window

RATE = 16000
FRAME = RATE // 50
HISTORY = 2320                   # ms, the measured mean (sd 40 ms over 98 sessions)


def room(frames, level=3.0):
    return [level] * frames


def burst(frames, peak):
    """A word's envelope: rises to `peak` at a third of its length, then falls to half of it (never flat, never silent inside)."""
    rise = max(1, frames // 3)
    return [peak * (0.5 + 0.5 * (i + 1) / rise) if i < rise else peak * (1.0 - 0.5 * (i - rise) / max(1, frames - rise))
            for i in range(frames)]


def history(name_end_ms, name_ms=380, gap_ms=None, request_ms=700, total_ms=HISTORY + 400, peak=30.0):
    """Levels of an activation stream: a click, a quiet room, the name ending at `name_end_ms`, then (after `gap_ms`) a request."""
    frames = total_ms // 20
    out = room(frames)
    out[0:3] = [14.0, 9.0, 6.0]                                    # the click every stream starts with
    name_frames = name_ms // 20
    start = (name_end_ms - name_ms) // 20
    out[start:start + name_frames] = burst(name_frames, peak)
    if gap_ms is not None:
        request_start = (name_end_ms + gap_ms) // 20
        request_frames = request_ms // 20
        out[request_start:request_start + request_frames] = burst(request_frames, peak * 0.8)
    del out[frames:]                                               # a slice assigned past the end would lengthen the stream
    return out


def pcm_of(levels, rate=RATE):
    """Audio whose per-frame RMS is `levels` (a square wave: RMS equals the amplitude)."""
    samples = array.array("h")
    frame = rate // 50
    for level in levels:
        samples.extend(int(level) if (n // 40) % 2 == 0 else -int(level) for n in range(frame))
    return samples.tobytes()


def delayed_connect():
    """Levels of the stream of 2026-11-20 11:18 (the owner: «Алиса, включить кондиционер на втором этаже», recorded as clip tg#31246).
    The station opened its socket late, so the burst held the history and the first second of the request: 3.65 s at once. The name
    ends at 2.0 s, then 0.44 s of quiet; the request's first word has a weak onset (two 40 ms puffs at a fifth of its peak), its
    stressed vowel at 2.72-3.08 s and 80 ms of quiet behind it; the rest of the request follows. The true history ended at 2.32 s."""
    out = room(250, 1.0)
    out[0:3] = [14.0, 9.0, 6.0]
    out[85:100] = burst(15, 60.0)        # the name, 1700-2000 ms
    out[124:126] = [8.0, 8.0]            # the request's first word, its onset, 2480-2520 ms
    out[134:136] = [8.0, 8.0]            # 2680-2720 ms
    out[136:154] = burst(18, 42.0)       # its stressed vowel and end, 2720-3080 ms
    out[162:221] = burst(59, 25.0)       # the rest of the request, 3240-4420 ms
    return out


class HistoryBurst(unittest.TestCase):
    def test_the_history_is_the_audio_that_arrived_ahead_of_real_time(self):
        self.assertEqual(wake_window.history_ms(audio_ms=2920, elapsed_ms=600), 2320)

    def test_a_stream_that_arrives_in_real_time_has_no_history(self):
        """A follow-up turn (no wake word): the audio is live from the first frame."""
        self.assertIsNone(wake_window.history_ms(audio_ms=640, elapsed_ms=600))

    def test_a_lead_that_is_not_a_station_history_is_not_one(self):
        self.assertIsNone(wake_window.history_ms(audio_ms=7600, elapsed_ms=600))     # a sender that is simply fast
        self.assertIsNone(wake_window.history_ms(audio_ms=1500, elapsed_ms=600))     # 0.9 s: shorter than any real history

    def test_a_lead_that_holds_live_speech_is_not_a_history(self):
        """3.65 s arrived within 0.18 s: the history (2.32 s) and the first 1.3 s of the request. That is 3.48 s ahead of the clock, far
        longer than any history the station sent (2.02-2.43 s); read as one, the window sits 1.2 s too late (2026-11-20 11:18)."""
        self.assertIsNone(wake_window.history_ms(audio_ms=3654, elapsed_ms=176))
        self.assertIsNone(wake_window.history_ms(audio_ms=2900 + 300, elapsed_ms=600))   # 2.6 s ahead: 0.3 s of the request already in it


class NameEnd(unittest.TestCase):
    def locate(self, levels, history_ms=HISTORY):
        return wake_window.locate(levels, history_ms)

    def test_the_cut_is_in_the_pause_between_the_name_and_the_request(self):
        for name_end in (1960, 2040, 2100):
            for gap in (60, 100, 250):
                with self.subTest(name_end=name_end, gap=gap):
                    levels = history(name_end, gap_ms=gap)
                    found = self.locate(levels)
                    self.assertIsNotNone(found.end_ms, found.reason)
                    self.assertGreaterEqual(found.end_ms, name_end, "a piece of the name stays in the audio")
                    self.assertLessEqual(found.end_ms, name_end + gap, "the request's first frame is cut")

    def test_a_name_with_nothing_after_it_is_cut_right_behind_it(self):
        found = self.locate(history(2040))
        self.assertGreaterEqual(found.end_ms, 2040)
        self.assertLessEqual(found.end_ms, 2040 + 100)

    def test_a_request_that_follows_without_a_pause_is_not_cut(self):
        """No quiet frames between the name and the request: where one ends is unknown, the text-level strip decides."""
        found = self.locate(history(2040, gap_ms=0))
        self.assertIsNone(found.end_ms)
        self.assertEqual(found.reason, "no_pause")

    def test_a_dip_inside_the_first_request_word_is_not_the_end_of_the_name(self):
        """The name runs straight into the request (no pause) and the request's first word has a 60 ms closure 0.16 s before the end
        of the history: the quiet is the request's own, cutting there would delete the start of the request."""
        levels = history(2040, gap_ms=0)
        levels[108:111] = [3.0, 3.0, 3.0]
        found = self.locate(levels)
        self.assertIsNone(found.end_ms)
        self.assertEqual(found.reason, "no_pause")

    def test_a_pause_after_the_first_request_word_is_not_the_end_of_the_name(self):
        """The name ends at 1960 ms, the request's first word follows at once and ends at 2220 ms, then 80 ms of quiet: the burst is 0.64 s."""
        levels = history(1960, gap_ms=0, request_ms=260, total_ms=3920)
        levels[115:150] = burst(35, 24.0)
        self.assertIsNone(self.locate(levels).end_ms)

    def test_speech_running_through_the_whole_window_is_not_cut(self):
        levels = room(HISTORY // 20 + 20)
        levels[40:] = burst(len(levels) - 40, 30.0)
        self.assertIsNone(self.locate(levels).end_ms)

    def test_a_request_already_running_inside_the_history_is_not_cut(self):
        """The name early, the request following 0.25 s later and still going at the boundary (2026-11-10 16:26)."""
        levels = history(1240, gap_ms=220, request_ms=1500)
        self.assertIsNone(self.locate(levels).end_ms)

    def test_speech_before_the_name_with_a_short_pause_is_not_cut(self):
        """The real 2026-11-10 16:26 history: the name, a 0.22 s pause, the request going on through the boundary, and a dip of
        60 ms inside it. The dip is not the end of the name: something was said right before the burst the dip follows."""
        levels = history(1240, gap_ms=220, request_ms=1500)
        levels[101:104] = [3.0, 3.0, 3.0]
        found = self.locate(levels)
        self.assertIsNone(found.end_ms)
        self.assertEqual(found.reason, "not_alone")

    def test_a_quiet_history_has_no_name_to_cut(self):
        found = self.locate(room(HISTORY // 20 + 20))
        self.assertIsNone(found.end_ms)
        self.assertEqual(found.reason, "no_name")

    def test_the_click_at_the_start_of_the_stream_is_not_the_name(self):
        levels = room(HISTORY // 20 + 20)
        levels[0:3] = [40.0, 30.0, 20.0]
        self.assertIsNone(self.locate(levels).end_ms)

    def test_a_name_that_is_longer_than_a_word_is_not_cut(self):
        found = self.locate(history(2040, name_ms=1300))
        self.assertIsNone(found.end_ms)

    def test_the_boundary_estimate_may_be_off_by_a_tenth_of_a_second(self):
        for shift in (-100, 0, 100):
            with self.subTest(shift=shift):
                levels = history(2040, gap_ms=100)
                found = self.locate(levels, HISTORY + shift)
                self.assertIsNotNone(found.end_ms, found.reason)
                self.assertGreaterEqual(found.end_ms, 2040)
                self.assertLessEqual(found.end_ms, 2140)


class RealSessions(unittest.TestCase):
    """Eight stored sessions (gateway journal + clip store, 2026-11-10 … 11-17): the frame levels of their archived cuts, the
    stretches between cuts filled with room noise (the archive keeps cuts only), and where a cut is right. «Right» was settled by
    re-recognising each session through the live recogniser with the head cut at every 50 ms from 0.6 s before the end of the
    history to the end of it: the name is gone and the request's first word is still there inside `cut_after_history_ms`.
    No audio is kept here, only levels."""

    def test_each_stored_session_is_cut_where_it_is_right_or_left_alone(self):
        with open(os.path.join(os.path.dirname(__file__), "wake_window_sessions.json"), encoding="utf-8") as stored:
            sessions = json.load(stored)
        self.assertGreaterEqual(len(sessions), 8)
        for session in sessions:
            with self.subTest(session=session["session"], what=session["what"]):
                found = wake_window.locate(session["levels"], session["history_ms"])
                if "reason" in session:
                    self.assertIsNone(found.end_ms)
                    self.assertEqual(found.reason, session["reason"])
                    continue
                low, high = session["cut_after_history_ms"]
                self.assertIsNotNone(found.end_ms, found.reason)
                self.assertGreaterEqual(found.end_ms - session["history_ms"], low)
                self.assertLessEqual(found.end_ms - session["history_ms"], high)


class Gate(unittest.TestCase):
    """The gate holds the start of a stream until the burst is over, then hands on what follows the name."""

    def stream(self, levels):
        return pcm_of(levels)

    def play(self, gate, pcm, burst_ms, live_ms_per_chunk=200):
        """The station's delivery: the history in one go at t=0, the rest in real time. Returns (audio handed on, removed audio)."""
        out = bytearray()
        chunk = RATE * 2 * live_ms_per_chunk // 1000
        head = RATE * 2 * burst_ms // 1000
        out.extend(gate.push(pcm[:head], 0))
        elapsed = 0
        for offset in range(head, len(pcm), chunk):
            elapsed += live_ms_per_chunk
            out.extend(gate.push(pcm[offset:offset + chunk], elapsed))
        out.extend(gate.release(elapsed))
        return bytes(out), gate.removed

    def test_an_activation_stream_loses_the_audio_up_to_the_end_of_the_name(self):
        levels = history(2040, gap_ms=140, total_ms=HISTORY + 1600)
        pcm = self.stream(levels)
        gate = wake_window.Gate(RATE)
        out, removed = self.play(gate, pcm, burst_ms=HISTORY)
        self.assertGreater(len(removed), 0)
        self.assertAlmostEqual(gate.history, HISTORY, delta=20, msg="the history the gate measured, for the log")
        self.assertEqual(removed + out, pcm, "nothing is lost but the cut audio, nothing is reordered")
        cut_ms = len(removed) * 1000 // (2 * RATE)
        self.assertGreaterEqual(cut_ms, 2040)
        self.assertLessEqual(cut_ms, 2040 + 140)

    def test_a_follow_up_stream_passes_untouched(self):
        pcm = self.stream(history(2040, gap_ms=140, total_ms=HISTORY + 1600))
        gate = wake_window.Gate(RATE)
        out, removed = self.play(gate, pcm, burst_ms=200)          # real time from the first frame: no history
        self.assertEqual(out, pcm)
        self.assertIsNone(removed)

    def test_nothing_is_handed_on_before_the_burst_is_over_and_all_of_it_afterwards(self):
        pcm = self.stream(history(2040, gap_ms=140, total_ms=HISTORY + 1600))
        gate = wake_window.Gate(RATE)
        first = gate.push(pcm[:RATE * 2 * HISTORY // 1000], 0)
        self.assertEqual(first, b"")
        later = gate.push(pcm[RATE * 2 * HISTORY // 1000:][:6400], 700)
        self.assertGreater(len(later), 0)

    def test_a_sender_far_ahead_of_real_time_is_not_held_until_the_stream_ends(self):
        """More audio than any history could be, at once: it is a fast sender (a replay, a catch-up), not the station."""
        pcm = self.stream(history(2040, gap_ms=140, total_ms=6000))
        gate = wake_window.Gate(RATE)
        self.assertEqual(gate.push(pcm, 0), pcm)
        self.assertIsNone(gate.removed)

    def test_a_burst_that_holds_live_speech_behind_the_history_is_not_cut(self):
        """2026-11-20 11:18: the socket opened late and 3.65 s arrived within 0.18 s. Read as a 3.48 s «history» the window sat on the
        request's first word, its stressed vowel was taken for the name and the cut removed the whole word («включить») with the real
        name. A lead longer than any history is a burst whose end nobody knows: the audio goes on whole, the text-level strip answers."""
        pcm = self.stream(delayed_connect())
        gate = wake_window.Gate(RATE)
        out = bytearray()
        pos = 0
        for ms, at in ((1200, 120), (1200, 150), (1254, 176)):
            size = RATE * 2 * ms // 1000
            out.extend(gate.push(pcm[pos:pos + size], at))
            pos += size
        self.assertEqual(bytes(out), pcm[:pos], "nothing is held once the lead is known to be too long")
        out.extend(gate.push(pcm[pos:], 400))
        self.assertEqual(bytes(out), pcm)
        self.assertIsNone(gate.removed)
        self.assertEqual(gate.located.reason, "burst", "the gateway log says why nothing was cut")
        self.assertAlmostEqual(gate.history, 3478, delta=2)

    def test_the_same_speech_with_only_the_history_in_the_burst_is_cut_behind_the_name(self):
        """The control of the test above: the name's position is the same, the socket opened on time, so the lead is the history."""
        pcm = self.stream(delayed_connect())
        gate = wake_window.Gate(RATE)
        out, removed = self.play(gate, pcm, burst_ms=HISTORY)
        self.assertIsNotNone(removed)
        self.assertEqual(removed + out, pcm)
        cut_ms = len(removed) * 1000 // (2 * RATE)
        self.assertGreaterEqual(cut_ms, 2000)
        self.assertLessEqual(cut_ms, 2440, "the request's first word starts at 2.48 s")

    def test_a_stream_that_ends_before_the_decision_is_released_whole(self):
        pcm = self.stream(history(2040, gap_ms=140, total_ms=HISTORY))
        gate = wake_window.Gate(RATE)
        out = gate.push(pcm, 0) + gate.release(50)
        self.assertEqual(out, pcm)
        self.assertIsNone(gate.removed)

    def test_an_ambiguous_history_is_released_whole(self):
        pcm = self.stream(history(2040, gap_ms=0, total_ms=HISTORY + 1600))
        gate = wake_window.Gate(RATE)
        out, removed = self.play(gate, pcm, burst_ms=HISTORY)
        self.assertEqual(out, pcm)
        self.assertIsNone(removed)

    def test_a_disabled_gate_never_holds_or_cuts(self):
        pcm = self.stream(history(2040, gap_ms=140, total_ms=HISTORY + 1600))
        gate = wake_window.Gate(RATE, enabled=False)
        self.assertEqual(gate.push(pcm[:6400], 0), pcm[:6400])

    def test_the_same_layout_at_48_khz(self):
        levels = history(2040, gap_ms=140, total_ms=HISTORY + 1600)
        pcm = pcm_of(levels, 48000)
        gate = wake_window.Gate(48000)
        out = bytearray()
        head = 48000 * 2 * HISTORY // 1000
        out.extend(gate.push(pcm[:head], 0))
        elapsed = 0
        for offset in range(head, len(pcm), 19200):
            elapsed += 200
            out.extend(gate.push(pcm[offset:offset + 19200], elapsed))
        out.extend(gate.release(elapsed))
        self.assertEqual(gate.removed + bytes(out), pcm)
        self.assertGreaterEqual(len(gate.removed) * 1000 // (2 * 48000), 2040)


if __name__ == "__main__":
    unittest.main()
