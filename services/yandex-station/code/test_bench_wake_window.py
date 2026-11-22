"""The back-test's own arithmetic (bench_wake_window.py): the journal parser, the splice geometry and the leak/loss verdict.
The recognizer is never called here."""
import os
import shutil
import tempfile
import unittest

import bench_wake_window as bench
import wake_window

JOURNAL = """\
2026-11-17T16:03:27.828598+01:00 host python[1]: INFO:aisis.stt:stt_session_open
2026-11-17T16:03:27.846968+01:00 host python[1]: INFO:aisis.stt:stt_init rate=48000
2026-11-17T16:03:27.888899+01:00 host python[1]: INFO:aisis.stt:stt_finalize reason=vad pcm=86400B speech_frames=4 peak=26 rms=4 received=86400B frames=45 voiced_ms=80 floor=4.3 contrast=1.1
2026-11-17T16:03:28.238668+01:00 host python[1]: INFO:aisis.stt:stt_finalize reason=vad pcm=119040B speech_frames=17 peak=222 rms=14 received=261120B frames=136 voiced_ms=260 floor=9.2 contrast=3.8
2026-11-17T16:03:28.850396+01:00 host python[1]: INFO:aisis.stt:stt_session_close bytes=324480B frames=169
2026-11-17T16:03:33.010172+01:00 host python[1]: INFO:aisis.stt:stt_session_open
2026-11-17T16:03:33.020000+01:00 host python[1]: INFO:aisis.stt:stt_init rate=48000
2026-11-17T16:03:37.486665+01:00 host python[1]: INFO:aisis.stt:stt_session_close bytes=430080B frames=224
"""


class Journal(unittest.TestCase):
    def test_the_real_16_03_pair_is_one_activation_session_with_a_2_32_s_history_and_one_follow_up_without(self):
        first, second = bench.journal_sessions(JOURNAL)
        self.assertAlmostEqual(first["history"], 2.36, delta=0.01)        # 3.38 s of audio in 1.02 s
        self.assertAlmostEqual(second["history"], 0.0, delta=0.01)        # 4.48 s of audio in 4.48 s
        self.assertEqual([round(cut["start"], 2) for cut in first["cuts"]], [0.0, 1.48])
        self.assertEqual(first["rate"], 48000)


class Verdict(unittest.TestCase):
    def test_the_name_in_front_of_the_request_is_a_leak(self):
        self.assertEqual(bench.outcome("Алиса, включи свет", "Включи свет."), "leak")

    def test_the_request_alone_is_ok_whatever_the_case_ending(self):
        self.assertEqual(bench.outcome("Выключи света в спальне", "Выключи свет на спальне"), "ok")

    def test_a_request_without_its_first_word_is_lost(self):
        self.assertEqual(bench.outcome("Сейчас погода.", "Какая сейчас погода?"), "lost")

    def test_a_word_the_request_has_itself_in_front_of_its_first_word_is_not_the_name(self):
        self.assertEqual(bench.outcome("Нет, два плюс семь", "Нет, два плюс семь."), "ok")

    def test_a_missing_first_word_is_lost_even_when_the_second_shares_its_beginning(self):
        self.assertEqual(bench.outcome("Показания датчика", "Покажи показания датчика"), "lost")

    def test_the_name_in_front_of_a_one_letter_first_word_is_a_leak(self):
        self.assertEqual(bench.outcome("Алиса, а какая погода", "А какая погода"), "leak")

    def test_nothing_heard_is_empty(self):
        self.assertEqual(bench.outcome("", "Включи свет"), "empty")


class Splice(unittest.TestCase):
    def clip(self, ms, level):
        """A voiced stretch with a six-frame lead and tail of room, as voiced_span hands clips to the splice."""
        import array
        frame = bench.FRAME
        samples = array.array("h")
        for _ in range(6):
            samples.extend([0] * frame)
        for _ in range(ms // 20):
            samples.extend(int(level) if (n // 40) % 2 == 0 else -int(level) for n in range(frame))
        for _ in range(6):
            samples.extend([0] * frame)
        return samples.tobytes()

    def test_the_cut_of_a_spliced_stream_lies_between_the_end_of_the_name_and_the_start_of_the_request(self):
        for before in bench.NAME_ENDS_BEFORE_MS:
            for pause in (100, 150, 250, 400):
                with self.subTest(before=before, pause=pause):
                    name, request = self.clip(380, 30)[:-6 * bench.FRAME * 2], self.clip(700, 25)   # the name ends at its last voiced frame, as run_stress trims it
                    pcm, name_end, request_start = bench.splice(name, request, 2330, before, pause, 1.0)
                    self.assertEqual(name_end, 2330 - before)
                    self.assertEqual(request_start, name_end + pause)
                    found = bench.cut_of(pcm, 2330)
                    self.assertIsNotNone(found.end_ms, found.reason)
                    self.assertGreaterEqual(found.end_ms, name_end)
                    self.assertLessEqual(found.end_ms, request_start + 20)

    def test_the_request_lead_does_not_widen_the_pause(self):
        """The clip keeps six frames of room in front of its voiced part: the voiced onset, not the clip's start, is `pause_ms` behind the name."""
        import array
        for pause in (60, 100, 150):
            with self.subTest(pause=pause):
                name, request = self.clip(380, 30)[:-6 * bench.FRAME * 2], self.clip(700, 25)
                pcm, name_end, request_start = bench.splice(name, request, 2330, 300, pause, 0.0, request_lead=6)
                samples = array.array("h")
                samples.frombytes(pcm)
                first = next(i for i in range(name_end * bench.RATE // 1000 + bench.FRAME, len(samples)) if abs(samples[i]) > 20)
                self.assertAlmostEqual(first * 1000 / bench.RATE - name_end, pause, delta=1)

    def test_the_stream_is_long_enough_to_close_the_last_cut(self):
        pcm, _, request_start = bench.splice(self.clip(380, 30)[:-6 * bench.FRAME * 2], self.clip(700, 25), 2330, 300, 150, 1.0)
        self.assertGreaterEqual(len(pcm) // (2 * bench.RATE) * 1000, 2330 + wake_window.LOOKAHEAD_MS)

    def test_live_audio_inside_the_burst_makes_a_lead_longer_than_the_history_and_nothing_is_cut(self):
        """The 2026-11-20 11:18 layout: the burst holds the history and 1.16 s of the request. The gate sees a 3.5 s lead and keeps the stream whole."""
        pcm, _, _ = bench.splice(self.clip(380, 30)[:-6 * bench.FRAME * 2], self.clip(700, 25), 2330, 300, 150, 1.0)
        self.assertIsNotNone(bench.cut_of(pcm, 2330).end_ms, "the same stream with only the history in the burst is cut behind the name")
        found = bench.cut_of(pcm, 2330, extra_ms=1158)
        self.assertIsNone(found.end_ms)
        self.assertEqual(found.reason, "burst")

    def test_the_delivery_gives_the_gate_the_lead_it_was_asked_for(self):
        pcm, _, _ = bench.splice(self.clip(380, 30)[:-6 * bench.FRAME * 2], self.clip(700, 25), 2330, 300, 150, 1.0)
        gate = bench.deliver(pcm, 2330)
        self.assertAlmostEqual(gate.history, 2330, delta=5)
        self.assertTrue(gate.decided)


class Position(unittest.TestCase):
    """`position`: the stored gateway journal replayed through the place rule (wake_window.name_by_position): which VAD cuts of the activation
    streams the window left alone are taken for the name. The recognizer is never called. Real lines of 2026-11-22 11:48 (the owner's
    «Алиса» clip tg#31613 and the request behind it; the session after it, whose name the window cut; a follow-up turn)."""

    JOURNAL = """\
2026-11-22T11:48:24.863913+01:00 host python[1]: INFO:aisis.stt:stt_session_open
2026-11-22T11:48:24.873058+01:00 host python[1]: INFO:aisis.stt:stt_init rate=48000
2026-11-22T11:48:25.498104+01:00 host python[1]: INFO:aisis.stt:stt_wake_window history_ms=2329 skipped=not_alone
2026-11-22T11:48:25.515223+01:00 host python[1]: INFO:aisis.stt:stt_finalize reason=vad pcm=122880B speech_frames=15 peak=79 rms=5 received=284160B frames=148 voiced_ms=300 floor=1.1 contrast=14.1
2026-11-22T11:48:28.274681+01:00 host python[1]: INFO:aisis.stt:stt_finalize reason=vad pcm=232320B speech_frames=79 peak=79 rms=6 received=552960B frames=288 voiced_ms=1280 floor=0.9 contrast=9.4
2026-11-22T11:48:28.603773+01:00 host python[1]: INFO:aisis.stt:stt_session_close bytes=583680B frames=304
2026-11-22T11:48:33.766560+01:00 host python[1]: INFO:aisis.stt:stt_session_open
2026-11-22T11:48:33.789963+01:00 host python[1]: INFO:aisis.stt:stt_init rate=48000
2026-11-22T11:48:34.413103+01:00 host python[1]: INFO:aisis.stt:stt_wake_window history_ms=2287 cut_ms=2020 voiced_ms=180
2026-11-22T11:48:36.754593+01:00 host python[1]: INFO:aisis.stt:stt_finalize reason=vad pcm=172800B speech_frames=36 peak=56 rms=3 received=508800B frames=265 voiced_ms=720 floor=0.9 contrast=8.9
2026-11-22T11:48:37.053352+01:00 host python[1]: INFO:aisis.stt:stt_session_close bytes=537600B frames=280
2026-11-22T11:48:44.415404+01:00 host python[1]: INFO:aisis.stt:stt_session_open
2026-11-22T11:48:44.423994+01:00 host python[1]: INFO:aisis.stt:stt_init rate=48000
2026-11-22T11:48:49.412978+01:00 host python[1]: INFO:aisis.stt:stt_finalize reason=finish pcm=264960B speech_frames=74 peak=55 rms=6 received=478080B frames=249 voiced_ms=1400 floor=1.5 contrast=7.3
2026-11-22T11:48:49.766849+01:00 host python[1]: INFO:aisis.stt:stt_session_close bytes=478080B frames=249
"""

    def rows(self, **kwargs):
        return bench.position_rows(bench.journal_sessions(self.JOURNAL), **kwargs)

    def test_the_journal_keeps_what_the_window_decided_and_the_speech_of_each_cut(self):
        skipped, cut, follow_up = bench.journal_sessions(self.JOURNAL)
        self.assertEqual((skipped["window"], skipped["window_history_ms"]), ("not_alone", 2329))
        self.assertEqual((cut["window"], cut["window_history_ms"]), ("cut", 2287))
        self.assertIsNone(follow_up.get("window"))
        self.assertEqual([c["voiced"] for c in skipped["cuts"]], [300, 1280])

    def test_the_name_the_window_skipped_is_taken_by_its_place_and_the_request_behind_it_is_not(self):
        rows = self.rows()
        self.assertEqual([(row["window"], row["end_ms"], row["voiced_ms"], row["name"]) for row in rows],
                         [("not_alone", 2160, 300, True), ("not_alone", 4960, 1280, False)])

    def test_a_stream_the_window_cut_and_a_stream_without_history_have_no_rows(self):
        self.assertEqual({row["opened"] for row in self.rows()}, {bench.journal_sessions(self.JOURNAL)[0]["open"]})

    def test_a_stream_from_before_the_window_existed_is_judged_by_its_measured_lead(self):
        journal = "\n".join(line for line in self.JOURNAL.splitlines() if "stt_wake_window" not in line)
        sessions = bench.journal_sessions(journal)
        rows = bench.position_rows(sessions)
        self.assertEqual([(row["window"], row["name"]) for row in rows], [("-", True), ("-", False), ("-", False)])
        self.assertEqual(rows[0]["history_ms"], round(1000 * sessions[0]["history"]))

    def test_a_replayed_stretch_of_the_journal_can_be_left_out(self):
        opened = bench.journal_sessions(self.JOURNAL)[0]["open"]
        self.assertEqual(self.rows(exclude=[(opened - 1, opened + 1)]), [])


class Arithmetic(unittest.TestCase):
    """The recognizer-free verdict of a cut against the splice's true layout (name ends at 2000 ms, request starts at 2200 ms)."""

    def verdict(self, end_ms, reason=""):
        return bench.geometry(wake_window.Located(end_ms, reason), name_end_ms=2000, request_start_ms=2200)

    def test_a_cut_in_the_pause_is_clean(self):
        self.assertEqual(self.verdict(2040), "clean")

    def test_a_cut_that_takes_a_voiced_frame_of_the_request_is_a_loss(self):
        self.assertEqual(self.verdict(2220), "loss")

    def test_a_cut_just_in_front_of_the_request_is_tight(self):
        self.assertEqual(self.verdict(2180), "tight")
        self.assertEqual(self.verdict(2200), "tight", "the first voiced frame survives, its soft onset is at risk")

    def test_a_cut_well_inside_the_name_is_a_leak(self):
        self.assertEqual(self.verdict(1900), "leak")
        self.assertEqual(self.verdict(1970), "clean", "30 ms of the name's decay is under the room noise")

    def test_no_cut_says_why(self):
        self.assertEqual(self.verdict(None, "burst"), "not_cut:burst")


class RemovedPrefix(unittest.TestCase):
    """Which stored clip is the head the gateway cut off a replayed session (a name the recogniser heard alone is a stream utterance)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        for name in ("cut.opus", "heard.opus"):
            open(os.path.join(self.dir, name), "wb").close()
        self.addCleanup(shutil.rmtree, self.dir)

    def row(self, ident, ts, path, reason):
        return (ident, ts, path, 1.0, 100, 4.0, "activation", reason)

    def test_only_the_clip_the_cut_itself_stored_is_the_removed_head(self):
        clips = [self.row("heard", 101.0, "heard.opus", "wake"), self.row("cut", 100.4, "cut.opus", "wake_window")]
        self.assertEqual(bench.removed_prefix(clips, 100.0, self.dir)[0], "cut")

    def test_a_name_heard_alone_is_not_a_removed_head(self):
        self.assertIsNone(bench.removed_prefix([self.row("heard", 101.0, "heard.opus", "wake")], 100.0, self.dir))

    def test_a_removed_head_of_another_session_is_not_taken(self):
        self.assertIsNone(bench.removed_prefix([self.row("cut", 120.0, "cut.opus", "wake_window")], 100.0, self.dir))


if __name__ == "__main__":
    unittest.main()
