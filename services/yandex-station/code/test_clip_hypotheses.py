"""voiceclips: the recognition alternatives of an archived clip (table clip_hypotheses in dialogs.sqlite3), for diagnostics."""
import os
import sqlite3
import tempfile
import unittest

import voiceclips

NBEST = {"engine": "gigaam-v3-ctc", "ms": 88, "hypotheses": [{"text": "выключи свет", "percent": 71}, {"text": "включи свет", "percent": 29}]}


class ClipHypotheses(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "dialogs.sqlite3")
        previous = os.environ.get("AISIS_VOICE_ARCHIVE")
        os.environ["AISIS_VOICE_ARCHIVE"] = os.path.join(self.tmp.name, "archive")
        self.addCleanup(lambda: os.environ.pop("AISIS_VOICE_ARCHIVE", None) if previous is None
                        else os.environ.__setitem__("AISIS_VOICE_ARCHIVE", previous))

    def clip(self, ts=1_800_000_000.0):
        return voiceclips.save(self.db, b"\x01\x00" * 1600, 16000, {"ts": ts, "status": "sent", "transcript": "включи свет"})

    def test_schema_is_created_idempotently_with_the_documented_columns(self):
        voiceclips.connect(self.db).close()
        voiceclips.connect(self.db).close()
        conn = sqlite3.connect(self.db)
        try:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(clip_hypotheses)")]
            key = [row[1] for row in conn.execute("PRAGMA table_info(clip_hypotheses)") if row[5]]
        finally:
            conn.close()
        self.assertEqual(columns, ["clip_id", "ts", "source", "primary_text", "nbest", "ms"])
        self.assertEqual(key, ["clip_id"])

    def test_round_trip(self):
        clip_id = self.clip()
        self.assertTrue(voiceclips.save_hypotheses(self.db, clip_id, "whisper", "Включи свет.", NBEST, 88, ts=1_800_000_001.0))
        self.assertEqual(voiceclips.hypotheses_of(self.db, clip_id),
                         {"clip_id": clip_id, "ts": 1_800_000_001.0, "source": "whisper", "primary_text": "Включи свет.", "nbest": NBEST, "ms": 88})

    def test_unknown_clip_has_none(self):
        self.assertIsNone(voiceclips.hypotheses_of(self.db, "20260101T000000-abcdef"))

    def test_a_second_write_for_the_clip_replaces_the_first(self):
        clip_id = self.clip()
        voiceclips.save_hypotheses(self.db, clip_id, "whisper", "а", {"engine": "x"}, 1)
        voiceclips.save_hypotheses(self.db, clip_id, "spotter", "б", NBEST, 2)
        row = voiceclips.hypotheses_of(self.db, clip_id)
        self.assertEqual((row["source"], row["primary_text"], row["ms"]), ("spotter", "б", 2))

    def test_the_clip_feed_still_sees_one_row_per_clip(self):
        clip_id = self.clip()
        voiceclips.save_hypotheses(self.db, clip_id, "whisper", "включи свет", NBEST, 1)
        conn = voiceclips.connect(self.db)
        try:
            self.assertEqual(len(voiceclips._rows(conn, "1=1", ())), 1)
        finally:
            conn.close()

    def test_pruning_a_clip_drops_its_hypotheses(self):
        old, fresh = self.clip(ts=1_000_000_000.0), self.clip(ts=1_800_000_000.0)
        for clip_id in (old, fresh):
            voiceclips.save_hypotheses(self.db, clip_id, "whisper", "включи свет", NBEST, 1)
        self.assertEqual(voiceclips.prune(self.db, now=1_800_000_000.0 + 3600), 1)
        self.assertIsNone(voiceclips.hypotheses_of(self.db, old))
        self.assertIsNotNone(voiceclips.hypotheses_of(self.db, fresh))

    def test_bad_input_never_raises(self):
        self.assertFalse(voiceclips.save_hypotheses(os.path.join(self.tmp.name, "no", "such", "dir", "x.sqlite3"), "id", "whisper", "т", NBEST, 1))
        self.assertFalse(voiceclips.save_hypotheses(self.db, "id", "whisper", "т", object(), 1))
        self.assertIsNone(voiceclips.hypotheses_of(os.path.join(self.tmp.name, "no", "such", "dir", "x.sqlite3"), "id"))


if __name__ == "__main__":
    unittest.main()
