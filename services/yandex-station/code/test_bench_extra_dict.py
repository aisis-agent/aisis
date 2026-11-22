"""bench_extra_dict.py (ticket #268): the replay counts corrected and wrongly changed clips by the owner's verdicts, the mining lists wake-word head
words with who covers them, and the database is only ever opened read-only. Synthetic clips: no stored transcript of the owner is a fixture."""
import contextlib
import io
import os
import sqlite3
import tempfile
import unittest

import bench_extra_dict as bench
import cliptruth
import extra_dict
import voiceclips

DICTIONARY = extra_dict.load()


def clip(clip_id, text, status="sent", trigger="vad", label=None, truth=None, ts=0.0):
    return {"id": clip_id, "ts": ts, "status": status, "trigger": trigger, "transcript": text, "label": label, "truth": truth}


class Replay(unittest.TestCase):
    def test_counts_corrected_and_routing_changes(self):
        report = bench.replay([clip("a", "Олеса, включи свет."), clip("b", "Олеся."), clip("c", "Включи свет."), clip("d", "Алиса, включи свет.")], DICTIONARY)
        self.assertEqual((report["n_clips"], report["n_corrected"], report["n_routing_changed"], report["n_not_at_head"]), (4, 2, 2, 0))
        self.assertEqual(report["kinds"], {"wake": 2})
        self.assertEqual([item["id"] for item in report["corrected"]], ["a", "b"])

    def test_a_name_in_a_sentence_is_not_a_correction(self):
        report = bench.replay([clip("a", "Мы с Алексой ходили в кино."), clip("b", "Привет, Олеса"), clip("c", "Алекса пошёл домой")], DICTIONARY)
        self.assertEqual((report["n_corrected"], report["n_changed_wrongly"]), (0, 0))

    def test_a_change_the_owner_called_correct_or_noise_counts_as_wrong(self):
        report = bench.replay([clip("a", "Олеса, включи свет.", label="correct", truth="Олеса, включи свет."),
                               clip("b", "Олеся.", label="noise", truth="")], DICTIONARY)
        self.assertEqual((report["n_corrected"], report["n_changed_wrongly"], report["n_moved_toward_truth"]), (2, 2, 0))

    def test_a_change_toward_the_owners_text_counts_as_toward_and_the_rest_does_not(self):
        report = bench.replay([clip("a", "Олеса, включи свет.", label="corrected", truth="Алиса, включи свет."),
                               clip("b", "Олеса, включи свет.", label="corrected", truth="Алиса, выключи свет.")], DICTIONARY)
        self.assertEqual((report["n_moved_toward_truth"], report["n_changed_with_verdict_not_toward_truth"], report["n_changed_wrongly"]), (1, 1, 0))

    def test_a_clip_an_entry_was_written_from_is_marked_as_its_own_evidence(self):
        report = bench.replay([clip("20261109T010646-564b98", "Алиса, урок из 5.", label="corrected", truth="Алиса, громкость 5."),
                               clip("other", "урок из 5")], DICTIONARY)
        self.assertEqual((report["n_corrected"], report["n_entry_source_clips"]), (2, 1))

    def test_the_report_prints_counts_and_claims_no_accuracy(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            bench.print_replay(bench.replay([clip("a", "Олеса, включи свет.")], DICTIONARY))
        text = out.getvalue()
        self.assertIn("corrected by the dictionary: 1", text)
        self.assertIn("not an accuracy figure", text)
        self.assertNotIn("%", text)


class Mine(unittest.TestCase):
    def test_head_words_that_sound_like_the_name_are_listed_with_their_cover(self):
        rows = {row["word"]: row for row in bench.mine([clip("a", "Тариса."), clip("b", "Толиса включи свет"), clip("c", "Олеса, стоп", trigger="spotter"),
                                                        clip("d", "Олеса.", label="noise"), clip("e", "Алиса, привет"), clip("f", "Включи свет")], DICTIONARY)}
        self.assertEqual(set(rows), {"тариса", "толиса", "олеса"})
        self.assertEqual(rows["олеса"]["n"], 2)
        self.assertEqual(rows["олеса"]["whole_segment"], 1)
        self.assertEqual(rows["олеса"]["covered_by"], "dictionary")
        self.assertEqual(rows["олеса"]["triggers"], {"vad": 1, "spotter": 1})
        self.assertEqual(rows["олеса"]["verdicts"], {"none": 1, "noise": 1})
        self.assertEqual(rows["тариса"]["covered_by"], "lexicon")

    def test_an_unknown_mishearing_is_reported_as_covered_by_nobody(self):
        rows = bench.mine([clip("a", "Лица в ключике")], DICTIONARY)
        self.assertEqual([(row["word"], row["covered_by"]) for row in rows], [("лица", "nobody")])


class Database(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "dialogs.sqlite3")
        conn = voiceclips.connect(self.db)
        for statement in cliptruth.SCHEMA:
            conn.execute(statement)
        rows = [("c1", 1.0, "sent", "vad", "Олеса, включи свет."), ("c2", 2.0, "sent", "vad", "Включи свет."), ("c3", 3.0, "activation", "vad", "Олеся."),
                ("c4", 4.0, "blacklist", "vad", "Олеса, стоп"), ("c5", 5.0, "gate", "vad", None), ("c6", 6.0, "sent", "vad", "")]
        for clip_id, ts, status, trigger, transcript in rows:
            conn.execute("INSERT INTO voice_clips(id, ts, path, status, trigger, transcript) VALUES (?,?,?,?,?,?)", (clip_id, ts, clip_id + ".opus", status, trigger, transcript))
        conn.execute("INSERT INTO clip_truth(clip_id, truth, label, source, ts) VALUES ('c1', 'Алиса, включи свет.', 'corrected', 'tg', 1.0)")
        conn.commit()
        conn.close()

    def test_only_sent_and_activation_transcripts_are_read_newest_first_with_the_verdict(self):
        conn = bench.open_readonly(self.db)
        try:
            clips = bench.stored(conn, 100)
        finally:
            conn.close()
        self.assertEqual([item["id"] for item in clips], ["c3", "c2", "c1"])
        self.assertEqual([item["label"] for item in clips], [None, None, "corrected"])

    def test_the_limit_bounds_the_read(self):
        conn = bench.open_readonly(self.db)
        try:
            self.assertEqual([item["id"] for item in bench.stored(conn, 2)], ["c3", "c2"])
        finally:
            conn.close()

    def test_the_connection_cannot_write(self):
        conn = bench.open_readonly(self.db)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM voice_clips")
        finally:
            conn.close()

    def test_main_replays_the_database_and_rejects_a_limit_out_of_range(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(bench.main(["replay", "--db", self.db]), 0)
        self.assertIn("clips replayed (newest first, bounded): 3", out.getvalue())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            bench.main(["replay", "--db", self.db, "--limit", "0"])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            bench.main(["replay", "--db", self.db, "--limit", str(bench.MAX_LIMIT + 1)])

    def test_a_missing_database_is_an_error_not_an_empty_report(self):
        with self.assertRaises(sqlite3.OperationalError):
            bench.main(["replay", "--db", os.path.join(self.tmp.name, "missing.sqlite3")])


if __name__ == "__main__":
    unittest.main()
