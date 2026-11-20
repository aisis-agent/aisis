"""The confusables evaluation set (confusables_corpus.py) and its measurement (bench_confusables.py): the phrase tables, the polarity
detector, the counting and the worker protocol. No recogniser, model, network or speaker is used: engines are fakes."""
import asyncio
import json
import math
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import bench_confusables as bench
import confusables_corpus as corpus

try:
    import numpy as np
except ImportError:
    np = None


def result(text, polarity, pair_set="light", whisper="", giga="", augmentation="station", voice="denis", id_=None):
    return {"id": id_ or f"{pair_set}-{polarity}-{whisper}-{giga}", "text": text, "pair_set": pair_set, "polarity": polarity, "voice": voice,
            "augmentation": augmentation, "whisper": {"text": whisper}, "giga": {"text": giga}}


class Phrases(unittest.TestCase):
    def test_every_pair_set_has_four_or_more_phrasings_of_both_members_and_the_members_differ(self):
        for pair in corpus.PAIR_SETS:
            self.assertGreaterEqual(len(pair.a), 4, pair.id)
            self.assertEqual(len(pair.a), len(pair.b), pair.id)
            self.assertFalse(set(pair.a) & set(pair.b), pair.id)

    def test_the_named_confusables_are_all_present(self):
        said = " | ".join(phrase.text for phrase in corpus.phrases())
        for needle in ("включи свет", "выключи свет", "громче", "тише", "добавь громкость", "убавь громкость", "открой штору", "закрой штору",
                       "да", "нет", "пауза", "продолжи", "позвони", "не звони"):
            self.assertIn(needle, said)
        self.assertGreaterEqual(sum(1 for pair in corpus.PAIR_SETS if pair.a[0].startswith("включи")), 6)   # room/device objects

    def test_the_manifest_is_the_full_product_balanced_per_polarity_and_per_cell(self):
        rows = corpus.manifest_rows()
        self.assertGreaterEqual(len(rows), 400)
        self.assertEqual(len({row["id"] for row in rows}), len(rows))
        self.assertEqual(sum(row["polarity"] == "a" for row in rows), sum(row["polarity"] == "b" for row in rows))
        cells = {}
        for row in rows:
            key = (row["pair_set"], row["polarity"], row["augmentation"], row["voice"])
            cells[key] = cells.get(key, 0) + 1
        self.assertEqual(set(cells.values()), {len(corpus.PAIR_SETS[0].a)})   # every pair set x polarity x augmentation x voice cell has one clip per phrasing
        for row in rows[:1]:
            for field in ("id", "text", "pair_set", "polarity", "voice", "augmentation", "path"):
                self.assertTrue(row[field])
            self.assertEqual(row["split"], "synthetic")

    def test_every_truth_phrase_reads_as_its_own_member_and_never_as_the_other(self):
        for phrase in corpus.phrases():
            self.assertEqual(corpus.heard_polarity(phrase.pair_set, phrase.text), phrase.polarity, phrase.text)
            self.assertEqual(corpus.heard_polarity(phrase.pair_set, phrase.text.capitalize() + "."), phrase.polarity, phrase.text)

    def test_polarity_reading_of_transcripts_whisper_and_gigaam_write(self):
        cases = [("light", "Выключи свет.", "b"), ("light", "Включи свет!", "a"), ("light", "включить свет", "a"), ("light", "Отключи свет", "b"),
                 ("light", "свет", "none"), ("light", "включи и выключи", "both"), ("call", "Не звони.", "b"), ("call", "Позвони маме", "a"),
                 ("call", "не позвони", "b"), ("call", "мне звони", "a"), ("yes-no", "Да, конечно", "a"), ("yes-no", "Нет.", "b"), ("yes-no", "давай", "none"),
                 ("volume", "Сделай потише", "b"), ("volume", "Сделай погромче", "a"), ("pause", "Продолжи", "b"), ("pause", "Продолжим воспроизведение.", "b"), ("pause", "Продолжение следует.", "none"),   # Whisper's caption on silence is no command ("pause", "Поставь на паузу", "a"),
                 ("curtain", "Закрой шторы", "b"), ("volume-step", "Прибавь громкость", "a"), ("volume-step", "убавь", "b")]
        for pair_set, text, wanted in cases:
            self.assertEqual(corpus.heard_polarity(pair_set, text), wanted, text)

    def test_normalisation_ignores_case_punctuation_and_yo(self):
        self.assertTrue(corpus.same_text("Включи свет.", "включи  свет"))
        self.assertTrue(corpus.same_text("Всё!", "все"))
        self.assertFalse(corpus.same_text("включи свет", "выключи свет"))

    def test_verdict_marks_the_opposite_member_as_a_flip(self):
        row = result("включи свет", "a")
        self.assertEqual(corpus.verdict(row, "выключи свет"), {"exact": False, "heard": "b", "flip": True})
        self.assertEqual(corpus.verdict(row, "Включи свет."), {"exact": True, "heard": "a", "flip": False})
        self.assertEqual(corpus.verdict(row, "включи лампу"), {"exact": False, "heard": "a", "flip": False})


@unittest.skipIf(np is None and not os.environ.get("CI"), "numpy missing (CI installs it, so a missing numpy there fails instead of skipping)")
class Augmentation(unittest.TestCase):
    def speech(self):
        t = np.arange(16000) / 16000
        return np.sin(2 * np.pi * 220 * t) * np.sin(np.pi * t) ** 2 * 5000

    def test_the_station_level_puts_the_speech_peak_at_the_real_clips_91(self):
        pcm = corpus.condition(self.speech(), corpus.AUGMENTATION_BY_NAME["station"], seed=1)
        self.assertEqual(pcm.dtype, np.dtype("<i2"))
        self.assertAlmostEqual(int(np.abs(pcm).max()), 91, delta=6)    # the 1 LSB hiss rides on the peak

    def test_noise_conditions_hit_their_speech_to_noise_ratio(self):
        speech = self.speech()
        gain = 91 / np.abs(speech).max()
        for name, snr in (("snr20", 20), ("snr10", 10), ("snr5", 5)):
            aug = corpus.AUGMENTATION_BY_NAME[name]
            mixed = corpus.condition(speech, aug, seed=3).astype("float64")
            noise = mixed - speech * gain
            measured = 20 * math.log10(corpus.voiced_rms(speech * gain) / np.sqrt(np.mean(noise ** 2)))
            self.assertAlmostEqual(measured, snr, delta=2.0, msg=name)

    def test_a_clip_is_reproducible_from_its_id(self):
        aug = corpus.AUGMENTATION_BY_NAME["snr10"]
        self.assertTrue(np.array_equal(corpus.condition(self.speech(), aug, seed=7), corpus.condition(self.speech(), aug, seed=7)))
        self.assertFalse(np.array_equal(corpus.condition(self.speech(), aug, seed=7), corpus.condition(self.speech(), aug, seed=8)))

    def test_pink_noise_is_unit_rms_and_falls_with_frequency(self):
        noise = corpus.pink_noise(16000, seed=1)
        self.assertAlmostEqual(float(np.sqrt(np.mean(noise ** 2))), 1.0, places=6)
        spectrum = np.abs(np.fft.rfft(noise))
        self.assertGreater(spectrum[10:100].mean(), 2 * spectrum[4000:7000].mean())

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg missing")
    def test_the_telephone_band_removes_the_low_and_the_high_edge_and_keeps_the_middle(self):
        t = np.arange(22050) / 22050
        level = {}
        for hz in (80, 1000, 7000):
            out = corpus.resample_and_filter(np.sin(2 * np.pi * hz * t).astype("float32"), 22050, (corpus.BAND,))
            level[hz] = float(np.sqrt(np.mean(out[2000:-2000] ** 2)))
        self.assertGreater(level[1000], 0.6)
        self.assertLess(level[80], level[1000] / 5)
        self.assertLess(level[7000], level[1000] / 5)

    def test_every_augmentation_is_declared_once_with_a_valid_level(self):
        names = [aug.name for aug in corpus.AUGMENTATIONS]
        self.assertEqual(len(set(names)), len(names))
        for aug in corpus.AUGMENTATIONS:
            self.assertTrue(0 < aug.peak < 32768)
        self.assertEqual({aug.snr_db for aug in corpus.AUGMENTATIONS if aug.name.startswith("snr")}, {20.0, 10.0, 5.0})


class Counting(unittest.TestCase):
    def rows(self):
        return [
            result("включи свет", "a", whisper="Включи свет.", giga="включи свет"),            # both exact
            result("включи свет", "a", whisper="Выключи свет.", giga="включи свет"),           # Whisper flips, texts differ
            result("включи свет", "a", whisper="включи свет", giga="выключи свет"),            # GigaAM flips, texts differ
            result("включи свет", "a", whisper="Выключи свет.", giga="выключи свет"),          # both flip, texts agree
            result("громче", "a", pair_set="volume", whisper="", giga="громче"),               # Whisper none
            result("нет", "b", pair_set="yes-no", whisper="Нет.", giga="да"),                  # GigaAM flips
        ]

    def test_classify_distinguishes_exact_right_flip_none(self):
        rows = self.rows()
        self.assertEqual([bench.classify(row, "whisper") for row in rows], ["exact", "flip", "exact", "flip", "none", "exact"])
        self.assertEqual([bench.classify(row, "giga") for row in rows], ["exact", "exact", "flip", "flip", "exact", "flip"])
        self.assertEqual(bench.classify(result("включи свет", "a", whisper="включи лампу"), "whisper"), "right")
        self.assertEqual(bench.classify({**rows[0], "whisper": {"text": None, "error": "timeout"}}, "whisper"), "error")

    def test_cells_carry_n_and_outcome_counts_per_engine(self):
        groups = bench.grouped(self.rows(), "pair_set", tuple(pair.id for pair in corpus.PAIR_SETS))
        self.assertEqual(list(groups), ["light", "volume", "yes-no"])
        self.assertEqual(groups["light"]["n"], 4)
        self.assertEqual(groups["light"]["whisper"], {"exact": 2, "flip": 2})
        self.assertEqual(groups["light"]["giga"], {"exact": 2, "flip": 2})
        self.assertEqual(groups["volume"]["whisper"], {"none": 1})

    def test_agreement_matrix_counts_disagreement_against_polarity_errors(self):
        matrix = bench.agreement(self.rows())["matrix"]
        self.assertEqual(dict(matrix), {(True, False, False): 1, (False, True, False): 1, (False, False, True): 2, (True, True, True): 1, (False, False, False): 1})

    def test_polarity_agreement_ignores_endings_that_text_agreement_counts_as_disagreement(self):
        rows = [result("продолжи", "b", pair_set="pause", whisper="Продолжим.", giga="продолжи"),        # texts differ, same member
                result("продолжи", "b", pair_set="pause", whisper="Пауза.", giga="продолжи"),            # the engines say opposite members
                result("продолжи", "b", pair_set="pause", whisper="Продолжение следует.", giga="продолжи")]   # one says neither
        self.assertEqual(dict(bench.agreement(rows, "text")["matrix"]), {(False, False, False): 2, (False, True, False): 1})
        self.assertEqual(dict(bench.agreement(rows, "polarity")["matrix"]), {(True, False, False): 1, (False, True, False): 1, (False, False, False): 1})
        self.assertEqual(bench.agreement(rows, "polarity")["level"], "polarity")

    def test_summary_reports_n_in_every_cell_and_the_synthetic_split(self):
        summary, text = bench.summarize(self.rows())
        self.assertEqual(summary["split"], "synthetic")
        self.assertEqual(summary["n"], 6)
        self.assertEqual(summary["pair_set"]["light"]["n"], 4)
        self.assertIn("| light | 4 |", text)
        self.assertIn("| station | 6 |", text)
        self.assertIn("synthetic only", text)
        self.assertIn("texts differ", text)
        self.assertIn("Polarity readings differ: where Whisper flips (n=2, polarity readings differ in 50.0%); where GigaAM flips (n=3, polarity readings differ in 66.7%); "
                      "where both flip (n=1, polarity readings differ in 0.0%); where neither flips (n=2, polarity readings differ in 50.0%).", text)
        json.dumps(summary)

    def test_an_engine_error_is_its_own_column_and_never_a_right_answer(self):
        rows = [result("включи свет", "a", whisper="включи свет", giga="включи свет")]
        rows[0]["whisper"] = {"text": None, "error": "timeout"}
        stats = bench.cell(rows)
        self.assertEqual(stats["whisper"], {"error": 1})
        known = bench.agreement(rows)
        self.assertEqual((known["matrix"], known["n"], known["unknown"]), ({}, 0, 1))   # a failed engine is no disagreement


class Policy(unittest.TestCase):
    def test_policy_eval_counts_questions_per_whisper_outcome_and_invents_no_threshold(self):
        rows = [result("включи свет", "a", whisper="Выключи свет", giga="включи свет"), result("включи свет", "a", whisper="включи свет", giga="включи свет"),
                result("включи свет", "a", whisper="включи свет", giga="выключи свет"), result("громче", "a", pair_set="volume", whisper="", giga="громче")]
        rows.append({**rows[0], "whisper": {"text": None}})
        asked_on_disagreement = lambda view: "ask" if not corpus.same_text(view["primary"], view["alt"]) else "none"
        outcome = bench.policy_eval(rows, asked_on_disagreement)
        self.assertEqual(outcome["skipped"], 1)
        self.assertEqual(outcome["by_whisper_outcome"]["flip"], {"n": 1, "asked": 1})
        self.assertEqual(outcome["by_whisper_outcome"]["exact"], {"n": 2, "asked": 1})
        self.assertEqual(outcome["by_whisper_outcome"]["all"], {"n": 4, "asked": 3})

    def test_decide_may_return_an_object_with_a_kind(self):
        class Decision:
            kind = "none"
        outcome = bench.policy_eval([result("включи свет", "a", whisper="включи свет", giga="включи свет")], lambda view: Decision())
        self.assertEqual(outcome["by_whisper_outcome"]["all"], {"n": 1, "asked": 0})


FAKE_WORKER = """#!{python}
import json, sys
print(json.dumps({{"ready": True}}), flush=True)
stdin = sys.stdin.buffer
while True:
    header = stdin.readline()
    if not header:
        break
    pcm = stdin.read(int(header))
    print(json.dumps({{"text": "выключи свет" if len(pcm) % 2 == 0 else "?", "ms": 1, "min_prob": 0.5, "bytes": len(pcm)}}), flush=True)
"""


class Worker(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(bench, "recognizer_extras", lambda: {})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.python = self.dir / "fake-python"
        self.python.write_text(FAKE_WORKER.format(python=sys.executable))
        self.python.chmod(self.python.stat().st_mode | stat.S_IEXEC)

    def test_pcm_crosses_the_pipe_length_prefixed_and_answers_come_back_one_per_request(self):
        # The fake interpreter ignores the script argument: it speaks the same protocol as giga_worker.
        worker = bench.Giga(self.python)
        try:
            self.assertEqual(worker.decode(b"\x01\x02" * 1000)["bytes"], 2000)
            self.assertEqual(worker.decode(b"")["text"], "выключи свет")
            self.assertEqual(worker.decode(b"\n" * 7)["text"], "?")      # a PCM that contains newlines does not break the framing
        finally:
            worker.close()

    def test_a_worker_that_never_comes_up_is_an_error_not_a_hang(self):
        dead = self.dir / "dead"
        dead.write_text("#!/bin/sh\nexit 1\n")
        dead.chmod(dead.stat().st_mode | stat.S_IEXEC)
        with self.assertRaises(RuntimeError):
            bench.Giga(dead)

    def test_run_writes_one_line_per_clip_and_resumes_without_repeating_a_clip(self):
        rows = [{"id": f"c{i}", "text": "включи свет", "pair_set": "light", "polarity": "a", "voice": "denis", "augmentation": "station", "path": f"clips/c{i}.wav"}
                for i in range(3)]
        (self.dir / "manifest.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
        asked = []

        async def whisper(uri, pcm, extras=None):
            asked.append(uri)
            return {"text": "Включи свет.", "ms": 5}

        with mock.patch.object(bench, "whisper_text", whisper), mock.patch.object(bench, "read_clip", lambda path: b"x"), \
                mock.patch.object(bench, "recognizer_pcm", lambda wave, trailing_ms: b"\x00\x00" * 160), mock.patch("sys.stdout"):
            self.assertEqual(bench.run(self.dir, "tcp://fake:1", self.python, limit=2, pause=0), 0)
            self.assertEqual([r["id"] for r in bench.read_jsonl(self.dir / "results.jsonl")], ["c0", "c1"])
            self.assertEqual(bench.run(self.dir, "tcp://fake:1", self.python, limit=None, pause=0), 0)
        done = bench.read_jsonl(self.dir / "results.jsonl")
        self.assertEqual([r["id"] for r in done], ["c0", "c1", "c2"])
        self.assertEqual(asked, ["tcp://fake:1"] * 3)
        with mock.patch.object(bench, "Giga", side_effect=AssertionError("no model for no work")), mock.patch("sys.stdout"):
            self.assertEqual(bench.run(self.dir, "tcp://fake:1", self.python, limit=None, pause=0), 0)
        self.assertEqual(done[0]["whisper"]["text"], "Включи свет.")
        self.assertEqual(done[0]["giga"]["text"], "выключи свет")


@unittest.skipIf(np is None and not os.environ.get("CI"), "numpy missing")
class LiveInput(unittest.TestCase):
    def test_closing_silence_is_cut_to_the_gateways_tail_even_when_it_is_noisy(self):
        import stt_wyoming
        rate = bench.RATE
        rng = np.random.default_rng(1)
        speech = 0.3 * np.sin(np.arange(rate // 2) * 0.2)
        tail = 0.03 * rng.standard_normal(rate * 6 // 10)   # noise on the closing silence, a tenth of the speech
        wave = np.concatenate([np.zeros(rate // 5), speech, tail]).astype("float32")
        fake = types.ModuleType("bench_asr_archive")
        fake.lift = lambda wave, ms: wave
        with mock.patch.dict(sys.modules, {"bench_asr_archive": fake}):
            pcm = bench.recognizer_pcm(wave, round(corpus.TAIL_S * 1000))
        self.assertEqual(len(pcm), (len(wave) - (rate * 6 // 10 - stt_wyoming.TAIL_MS * rate // 1000)) * 2)

    def test_the_cut_comes_before_the_level_front_end_so_a_discarded_transient_changes_nothing(self):
        rate = bench.RATE
        speech = 0.01 * np.sin(np.arange(rate // 2) * 0.2)   # quiet: the front-end lifts it
        calm = np.concatenate([np.zeros(rate // 5), speech, np.zeros(rate * 6 // 10)]).astype("float32")
        bang = calm.copy()
        bang[-rate // 4: -rate // 4 + 200] = 0.9   # a loud transient in the part the gateway discards
        trailing = round(corpus.TAIL_S * 1000)
        self.assertEqual(bench.recognizer_pcm(bang, trailing), bench.recognizer_pcm(calm, trailing))

    def test_a_rebuild_waits_for_a_running_benchmark_instead_of_changing_its_clips(self):
        import fcntl
        out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, out)
        fake = types.ModuleType("piper")
        fake.PiperVoice = object
        finished = []
        with (out / "results.jsonl.lock").open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)   # a benchmark is running
            import threading

            def rebuild():
                with mock.patch.dict(sys.modules, {"piper": fake}):
                    corpus.build(out, Path("/nonexistent"), limit=0)
                finished.append(True)

            worker = threading.Thread(target=rebuild)
            worker.start()
            worker.join(0.5)
            self.assertEqual(finished, [])   # blocked on the benchmark's lock
        worker.join(10)
        self.assertEqual(finished, [True])

    def test_a_database_path_with_a_fragment_character_is_opened_read_only_as_given(self):
        odd = Path(tempfile.mkdtemp()) / "dialogs#backup?x.sqlite3"
        self.addCleanup(shutil.rmtree, odd.parent)
        conn = sqlite3.connect(odd)
        conn.execute("CREATE TABLE voice_clips(id TEXT PRIMARY KEY, ts REAL, path TEXT, trigger TEXT, voiced_ms INTEGER, transcript TEXT, status TEXT)")
        conn.commit()
        conn.close()
        self.assertEqual(bench.real_rows(odd, odd.parent, None), [])
        self.assertEqual(sorted(path.name for path in odd.parent.iterdir()), [odd.name])   # nothing was created beside it

    def test_a_missing_manifest_is_an_error_and_an_empty_one_is_nothing_to_do(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        with self.assertRaises(FileNotFoundError):
            bench.run(tmp, "tcp://x:1", "py", None, 0)
        (tmp / "manifest.jsonl").write_text("")
        with mock.patch("sys.stdout"):
            self.assertEqual(bench.run(tmp, "tcp://x:1", "py", None, 0), 0)

    def test_a_rebuild_that_fails_half_way_leaves_no_manifest_beside_changed_clips(self):
        out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, out)
        (out / "manifest.jsonl").write_text('{"id": "old"}\n')
        fake = types.ModuleType("piper")
        fake.PiperVoice = types.SimpleNamespace(load=lambda path: object())
        rows = [{"voice": "denis", "text": "да", "speed": 1.0, "augmentation": "station", "id": f"r{i}", "path": f"clips/r{i}.wav"} for i in range(2)]
        writes = []

        def write_wav(path, pcm):
            writes.append(path)
            if len(writes) == 2:
                raise OSError("disk full")

        models = out / "m" / "vits-piper-ru_RU-denis-medium"
        models.mkdir(parents=True)
        (models / "a.onnx").write_bytes(b"")
        with mock.patch.dict(sys.modules, {"piper": fake}), mock.patch.object(corpus, "manifest_rows", lambda: rows), \
                mock.patch.object(corpus, "synthesize", lambda voice, text, speed: (np.ones(2400), 24000)), mock.patch.object(corpus, "write_wav", write_wav):
            with self.assertRaises(OSError):
                corpus.build(out, out / "m")
        self.assertFalse((out / "manifest.jsonl").exists())

    def test_a_corpus_with_measurements_is_not_rebuilt_over_them(self):
        out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, out)
        (out / "results.jsonl").write_text("{}\n")
        with self.assertRaises(FileExistsError):
            corpus.build(out, Path("/nonexistent"), limit=0)

    def test_a_zero_limit_build_creates_the_output_directory(self):
        out = Path(tempfile.mkdtemp()) / "new" / "dir"
        self.addCleanup(shutil.rmtree, out.parent.parent)
        fake = types.ModuleType("piper")
        fake.PiperVoice = object
        with mock.patch.dict(sys.modules, {"piper": fake}):
            self.assertEqual(corpus.build(out, Path("/nonexistent"), limit=0), [])
        self.assertTrue((out / "manifest.jsonl").exists())


class Rendering(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def test_pipes_and_line_breaks_in_a_correction_cannot_split_a_table_row(self):
        row = {"id": "c", "trigger": "vad", "transcript": "a|b", "truth": "включи\nсвет | да", "whisper": {"text": "x"}, "giga": {"text": "y"}}
        table = [line for line in bench.render_real([row]).splitlines() if line.startswith("| c ")]
        self.assertEqual(len(table), 2)
        for line in table:
            self.assertEqual(line.replace("\\|", "").count("|"), 5)

    def test_a_failed_second_write_leaves_no_stale_report_and_the_old_rows(self):
        results, report = self.dir / "r.jsonl", self.dir / "r.md"
        results.write_text("old\n")
        report.write_text("OLD REPORT")
        real = bench.stage_private
        calls = []

        def flaky(path, text):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("ENOSPC")
            return real(path, text)

        with mock.patch.object(bench, "stage_private", flaky), self.assertRaises(OSError):
            bench.publish_pair(results, "new\n", report, "NEW")
        self.assertEqual(results.read_text(), "old\n")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir() if p.name.startswith("r.")), ["r.jsonl", "r.md"])

    def test_a_backslash_before_a_pipe_does_not_unescape_it(self):
        self.assertEqual(bench.cell_text("a\\|b"), "a\\\\\\|b")

    def test_clips_an_engine_failed_on_are_left_out_of_the_disagreement_tables(self):
        failed = result("включи свет", "a", whisper="включи свет", giga="включи свет")
        failed["whisper"] = {"text": None, "error": "t"}
        lines = bench.render_disagreement_by("Disagreement by pair set", {"light": [failed]})
        self.assertIn("| light | 0 |", "\n".join(lines))


class Stalls(unittest.TestCase):
    def test_a_worker_that_ignores_eof_and_sigterm_is_still_ended(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        stubborn = tmp / "stubborn"
        stubborn.write_text("#!/bin/sh\ntrap '' TERM\necho '{\"ready\": true}'\nwhile true; do sleep 1; done\n")
        stubborn.chmod(stubborn.stat().st_mode | stat.S_IEXEC)
        worker = bench.Giga(stubborn)
        started = time.monotonic()
        worker.proc.wait = lambda timeout=None, _wait=worker.proc.wait: _wait(timeout=0.3 if timeout else None)
        worker.close()
        self.assertLess(time.monotonic() - started, 30)
        self.assertIsNotNone(worker.proc.poll())

    def test_a_live_but_silent_worker_is_ended_at_its_deadline(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        silent = tmp / "silent"
        silent.write_text("#!/bin/sh\nexec sleep 30\n")
        silent.chmod(silent.stat().st_mode | stat.S_IEXEC)
        with mock.patch.object(bench.Giga, "START_SECONDS", 0.5):
            started = time.monotonic()
            with self.assertRaises(RuntimeError):
                bench.Giga(silent)
        self.assertLess(time.monotonic() - started, 10)


class Settings(unittest.TestCase):
    def test_the_profile_the_gateway_sends_is_sent_by_the_benchmark(self):
        with mock.patch.dict(os.environ, {"AISIS_STT_PROFILE": "home"}, clear=False):
            os.environ.pop("AISIS_STT_DOMAIN_CONTEXT", None)
            self.assertEqual(bench.recognizer_extras(), {"profile": "home"})
        seen = {}

        async def transcribe(uri, pcm, rate, language, timeout=30, profile=None, context=None):
            seen.update(profile=profile)
            return "ну"

        fake = types.ModuleType("stt_wyoming")
        fake.transcribe = transcribe
        with mock.patch.dict(sys.modules, {"stt_wyoming": fake}):
            asyncio.run(bench.whisper_text("tcp://x:1", b"", {"profile": "home"}))
        self.assertEqual(seen, {"profile": "home"})

    def test_the_domain_catalog_is_loaded_before_the_extras_are_taken(self):
        import voice_vocab
        loaded = voice_vocab.Catalog(entities=("Ночник в спальне",))
        with mock.patch.dict(os.environ, {"AISIS_STT_DOMAIN_CONTEXT": "1"}), mock.patch.object(voice_vocab, "fetch_catalog", lambda *args: loaded):
            extras = bench.recognizer_extras()
        self.assertIn("ночник", extras["context"]["prompt"].lower())

    def test_a_zero_corpus_limit_builds_nothing(self):
        fake = types.ModuleType("piper")
        fake.PiperVoice = object
        with mock.patch.dict(sys.modules, {"piper": fake}), mock.patch.object(corpus, "manifest_rows", lambda: [{"voice": "x"}]):
            self.assertEqual(corpus.build(Path(tempfile.mkdtemp()), Path("/nonexistent"), limit=0), [])


class WhisperRequest(unittest.TestCase):
    def test_a_recogniser_failure_is_a_result_with_the_error_not_an_exception(self):
        async def broken(*args, **kwargs):
            raise RuntimeError("wyoming_asr_error")

        fake = types.ModuleType("stt_wyoming")
        fake.transcribe = broken
        with mock.patch.dict(sys.modules, {"stt_wyoming": fake}):
            got = asyncio.run(bench.whisper_text("tcp://x:1", b""))
        self.assertIsNone(got["text"])
        self.assertIn("wyoming_asr_error", got["error"])


class RealClips(unittest.TestCase):
    def test_only_sent_clips_with_audio_are_taken_and_the_owner_truth_is_joined(self):
        base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, base)
        db = base / "dialogs.sqlite3"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE voice_clips(id TEXT PRIMARY KEY, ts REAL, path TEXT, trigger TEXT, voiced_ms INTEGER, transcript TEXT, status TEXT)")
        conn.execute("CREATE TABLE clip_truth(clip_id TEXT PRIMARY KEY, truth TEXT, label TEXT)")
        conn.executemany("INSERT INTO voice_clips VALUES(?,?,?,?,?,?,?)", [
            ("a", 1, "d/a.opus", "vad", 500, "Включи свет", "sent"), ("b", 2, "d/b.opus", "vad", 500, "шум", "gate"),
            ("c", 3, "d/c.opus", "spotter", 500, "выключи свет", "sent"), ("d", 4, "d/missing.opus", "vad", 500, "x", "sent")])
        conn.execute("INSERT INTO clip_truth VALUES('a','Алиса, выключи свет','corrected')")
        conn.commit()
        conn.close()
        (base / "d").mkdir()
        for name in ("a", "b", "c"):
            (base / "d" / f"{name}.opus").write_bytes(b"x")
        rows = bench.real_rows(db, base, None)
        self.assertEqual([row["id"] for row in rows], ["a", "c"])
        self.assertEqual(rows[0]["truth"], "Алиса, выключи свет")
        self.assertIsNone(rows[1]["truth"])
        self.assertEqual(len(bench.real_rows(db, base, 1)), 1)

    def test_the_real_report_lists_disagreements_and_states_it_claims_no_accuracy(self):
        rows = [{"id": "a", "trigger": "vad", "transcript": "Включи свет", "truth": "выключи свет", "whisper": {"text": "Включи свет."}, "giga": {"text": "выключи свет"}},
                {"id": "b", "trigger": "spotter", "transcript": "громче", "truth": None, "whisper": {"text": "Громче!"}, "giga": {"text": "громче"}},
                {"id": "c", "trigger": "vad", "transcript": "x", "truth": None, "whisper": {"text": None, "error": "t"}, "giga": {"text": "ну"}}]
        text = bench.render_real(rows)
        self.assertIn("n=2 clips with both texts (3 stored, 1 of them recognised by the speaker's own spotter", text)
        self.assertIn("| a | Включи свет | Включи свет. | выключи свет |", text)
        self.assertNotIn("| b |", text.split("## Clips with an owner truth")[0])
        self.assertIn("(n=1: too few for any rate)", text)
        self.assertIn("Not an accuracy", text)


class Findings(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(bench, "recognizer_extras", lambda: {})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def archive(self, with_truth):
        db = self.dir / "dialogs.sqlite3"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE voice_clips(id TEXT PRIMARY KEY, ts REAL, path TEXT, trigger TEXT, voiced_ms INTEGER, transcript TEXT, status TEXT)")
        conn.execute("INSERT INTO voice_clips VALUES('a',1,'a.opus','vad',500,'ну','sent')")
        if with_truth:
            conn.execute("CREATE TABLE clip_truth(clip_id TEXT PRIMARY KEY, truth TEXT, label TEXT)")
            conn.execute("INSERT INTO clip_truth VALUES('a','','noise')")
        conn.commit()
        conn.close()
        (self.dir / "a.opus").write_bytes(b"x")
        return db

    def run_real(self, db, out):
        fake = types.ModuleType("bench_asr_archive")
        fake.decode = lambda path: None
        fake.lift = lambda wave, ms: mock.MagicMock()

        class FakeGiga:
            def __init__(self, python):
                pass

            def decode(self, pcm):
                return {"text": "ну"}

            def close(self):
                pass

        async def whisper(uri, pcm, extras=None):
            return {"text": "ну"}

        with mock.patch.dict(sys.modules, {"bench_asr_archive": fake}), mock.patch.object(bench, "Giga", FakeGiga), \
                mock.patch.object(bench, "whisper_text", whisper), mock.patch("sys.stdout"):
            return bench.run_real(db, self.dir, "tcp://x:1", "py", out, None)

    # (no numpy needed: the lifted waveform is a stand-in, the engines are fakes)
    def test_real_exports_are_owner_only_even_over_an_existing_world_readable_file_and_create_the_output_dir(self):
        out = self.dir / "absent" / "real.jsonl"
        out.parent.mkdir()
        out.write_text("old")
        out.chmod(0o644)
        out.with_suffix(".md").write_text("old")
        out.with_suffix(".md").chmod(0o644)
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        self.assertEqual(self.run_real(self.archive(True), out), 0)
        for path in (out, out.with_suffix(".md")):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        fresh = self.dir / "fresh" / "real.jsonl"
        self.run_real(self.dir / "dialogs.sqlite3", fresh)
        self.assertEqual(stat.S_IMODE(fresh.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(fresh.with_suffix(".md").stat().st_mode), 0o600)

    def test_an_archive_without_the_clip_truth_table_gives_unlabelled_clips_and_stays_unmodified(self):
        db = self.archive(False)
        before = db.read_bytes()
        rows = bench.real_rows(db, self.dir, None)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["truth"])
        self.assertEqual(db.read_bytes(), before)

    def test_an_explicit_noise_verdict_is_listed_even_when_both_engines_agree(self):
        rows = [{"id": "n", "trigger": "vad", "transcript": "ну", "truth": "", "label": "noise", "whisper": {"text": "ну"}, "giga": {"text": "ну"}},
                {"id": "u", "trigger": "vad", "transcript": "ну", "truth": None, "whisper": {"text": "ну"}, "giga": {"text": "ну"}}]
        text = bench.render_real(rows)
        self.assertIn("(n=1:", text)
        self.assertIn("| n | (noise: the owner heard no speech) | ну | ну |", text)
        self.assertNotIn("| u |", text)

    def test_private_export_ignores_a_planted_temp_name_and_symlink(self):
        victim = self.dir / "victim"
        victim.write_text("keep")
        out = self.dir / "real.jsonl"
        (self.dir / "real.jsonl.tmp").symlink_to(victim)
        bench.write_private(out, "x")
        self.assertEqual(victim.read_text(), "keep")
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)

    def test_a_failure_on_a_later_clip_keeps_the_finished_results(self):
        db = self.archive(False)
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO voice_clips VALUES('b',2,'b.opus',  'vad',500,'ну','sent')")
        conn.commit()
        conn.close()
        (self.dir / "b.opus").write_bytes(b"x")
        out = self.dir / "real.jsonl"
        calls = []

        def decode(path):
            calls.append(path)
            if len(calls) == 2:
                raise RuntimeError("pruned")

        fake = types.ModuleType("bench_asr_archive")
        fake.decode = decode
        fake.lift = lambda wave, ms: mock.MagicMock()

        class FakeGiga:
            def __init__(self, python):
                pass

            def decode(self, pcm):
                return {"text": "ну"}

            def close(self):
                pass

        async def whisper(uri, pcm, extras=None):
            return {"text": "ну"}

        with mock.patch.dict(sys.modules, {"bench_asr_archive": fake}), mock.patch.object(bench, "Giga", FakeGiga), \
                mock.patch.object(bench, "whisper_text", whisper), mock.patch("sys.stdout"):
            with self.assertRaises(RuntimeError):
                bench.run_real(db, self.dir, "tcp://x:1", "py", out, None)
        self.assertEqual([row["id"] for row in bench.read_jsonl(out)], ["a"])

    def test_results_are_saved_even_when_the_worker_cleanup_fails(self):
        out = self.dir / "real.jsonl"
        fake = types.ModuleType("bench_asr_archive")
        fake.decode = lambda path: None
        fake.lift = lambda wave, ms: mock.MagicMock()

        class FakeGiga:
            def __init__(self, python):
                pass

            def decode(self, pcm):
                return {"text": "ну"}

            def close(self):
                raise BrokenPipeError("gone")

        async def whisper(uri, pcm, extras=None):
            return {"text": "ну"}

        with mock.patch.dict(sys.modules, {"bench_asr_archive": fake}), mock.patch.object(bench, "Giga", FakeGiga), \
                mock.patch.object(bench, "whisper_text", whisper), mock.patch("sys.stdout"):
            with self.assertRaises(BrokenPipeError):
                bench.run_real(self.archive(False), self.dir, "tcp://x:1", "py", out, None)
        self.assertEqual([row["id"] for row in bench.read_jsonl(out)], ["a"])

    def test_a_partial_save_refreshes_the_report_beside_the_results(self):
        out = self.dir / "real.jsonl"
        out.with_suffix(".md").write_text("OLD REPORT")
        db = self.archive(False)
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO voice_clips VALUES('b',2,'b.opus','vad',500,'ну','sent')")
        conn.commit()
        conn.close()
        (self.dir / "b.opus").write_bytes(b"x")
        calls = []

        def decode(path):
            calls.append(path)
            if len(calls) == 2:
                raise RuntimeError("pruned")

        fake = types.ModuleType("bench_asr_archive")
        fake.decode = decode
        fake.lift = lambda wave, ms: mock.MagicMock()

        class FakeGiga:
            def __init__(self, python):
                pass

            def decode(self, pcm):
                return {"text": "ну"}

            def close(self):
                pass

        async def whisper(uri, pcm, extras=None):
            return {"text": "ну"}

        with mock.patch.dict(sys.modules, {"bench_asr_archive": fake}), mock.patch.object(bench, "Giga", FakeGiga), \
                mock.patch.object(bench, "whisper_text", whisper), mock.patch("sys.stdout"):
            with self.assertRaises(RuntimeError):
                bench.run_real(db, self.dir, "tcp://x:1", "py", out, None)
        self.assertNotIn("OLD REPORT", out.with_suffix(".md").read_text())

    def test_the_report_never_overwrites_the_results_file(self):
        out = self.dir / "report.md"
        self.run_real(self.archive(False), out)
        self.assertEqual(bench.read_jsonl(out)[0]["id"], "a")
        self.assertTrue((self.dir / "report.md.md").exists())

    def test_wrong_shaped_json_replies_are_engine_errors(self):
        for error in (AttributeError("list"), KeyError("type"), TypeError("none")):
            async def broken(*args, error=error, **kwargs):
                raise error

            fake = types.ModuleType("stt_wyoming")
            fake.transcribe = broken
            with mock.patch.dict(sys.modules, {"stt_wyoming": fake}):
                got = asyncio.run(bench.whisper_text("tcp://x:1", b""))
            self.assertIsNone(got["text"])

    def test_a_malformed_or_truncated_recogniser_reply_is_an_engine_error_for_that_clip(self):
        for error in (json.JSONDecodeError("bad", "x", 0), asyncio.IncompleteReadError(b"ab", 10)):
            async def broken(*args, error=error, **kwargs):
                raise error

            fake = types.ModuleType("stt_wyoming")
            fake.transcribe = broken
            with mock.patch.dict(sys.modules, {"stt_wyoming": fake}):
                got = asyncio.run(bench.whisper_text("tcp://x:1", b""))
            self.assertIsNone(got["text"])
            self.assertIn(type(error).__name__, got["error"])

    def test_an_interrupted_final_line_is_dropped_and_truncated_but_a_middle_one_is_an_error(self):
        path = self.dir / "results.jsonl"
        path.write_text('{"id": "a"}\n{"id": "b"}\n{"id": "c", "wh')
        self.assertEqual([r["id"] for r in bench.read_results(path)], ["a", "b"])
        self.assertIn("wh", path.read_text())   # reading alone leaves the file alone
        self.assertEqual([r["id"] for r in bench.read_results(path, repair=True)], ["a", "b"])
        self.assertEqual(path.read_text(), '{"id": "a"}\n{"id": "b"}\n')
        path.write_text('{"id": "a"}\nnot json\n{"id": "c"}\n')
        with self.assertRaises(ValueError):
            bench.read_results(path)

    def test_a_final_record_without_its_newline_gets_one_before_the_next_append(self):
        path = self.dir / "results.jsonl"
        path.write_text('{"id": "a"}')
        self.assertEqual([r["id"] for r in bench.read_results(path, repair=True)], ["a"])
        with path.open("a") as out:
            out.write('{"id": "b"}\n')
        self.assertEqual([r["id"] for r in bench.read_results(path)], ["a", "b"])

    def test_summary_counts_a_repeated_clip_id_once_keeping_the_first(self):
        first = result("включи свет", "a", whisper="включи свет", giga="включи свет", id_="x")
        again = result("включи свет", "a", whisper="выключи свет", giga="выключи свет", id_="x")
        summary, _ = bench.summarize([first, again])
        self.assertEqual(summary["n"], 1)
        self.assertEqual(summary["overall"]["whisper"]["exact"], 1)


class Cli(unittest.TestCase):
    def test_summary_mode_writes_the_tables_next_to_the_results(self):
        base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, base)
        (base / "results.jsonl").write_text(json.dumps(result("включи свет", "a", whisper="Включи свет.", giga="включи свет")) + "\n")
        with mock.patch("sys.stdout"):
            self.assertEqual(bench.main(["summary", "--corpus", str(base)]), 0)
        self.assertEqual(json.loads((base / "summary.json").read_text())["n"], 1)
        self.assertIn("| light | 1 |", (base / "summary.md").read_text())

    def test_the_gateway_is_never_the_default_recogniser_address(self):
        self.assertNotIn("31121", bench.DEFAULT_URI)
        self.assertNotIn("31121", os.environ.get("AISIS_WYOMING_URI", ""))


if __name__ == "__main__":
    unittest.main()
