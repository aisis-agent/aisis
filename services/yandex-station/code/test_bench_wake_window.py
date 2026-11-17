"""The back-test's own arithmetic (bench_wake_window.py): the journal parser, the splice geometry and the leak/loss verdict.
The recognizer is never called here."""
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


if __name__ == "__main__":
    unittest.main()
