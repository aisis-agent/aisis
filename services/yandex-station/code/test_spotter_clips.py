"""Commands the speaker recognised on the device reach the voice archive and the clip feed (aisis#103)."""
import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path
from urllib.parse import quote

import bench_asr_archive
import clipfeed
import cliptruth
import dashboard
import dialogstore
import spotter_clips
import voiceclips

FFMPEG = os.environ.get("FFMPEG") or shutil.which("ffmpeg")


def ogg_opus(seconds: float, frequency: int = 440, amplitude: float = 0.2) -> bytes:
    """A real Ogg/Opus stream, the container the station uploads (a tone stands in for the voice)."""
    return subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=%d:duration=%s" % (frequency, seconds),
                           "-af", "volume=%s" % amplitude, "-ar", "16000", "-ac", "1", "-c:a", "libopus", "-f", "ogg", "pipe:1"],
                          stdout=subprocess.PIPE, check=True).stdout


@unittest.skipUnless(FFMPEG, "ffmpeg with libopus is needed to build and decode the station's Ogg/Opus")
class ArchiveSpotterClip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "dialogs.sqlite3")
        self.old_root = os.environ.get("AISIS_VOICE_ARCHIVE")
        os.environ["AISIS_VOICE_ARCHIVE"] = os.path.join(self.tmp.name, "archive")
        self.addCleanup(self.restore_root)

    def restore_root(self):
        if self.old_root is None:
            os.environ.pop("AISIS_VOICE_ARCHIVE", None)
        else:
            os.environ["AISIS_VOICE_ARCHIVE"] = self.old_root

    def row(self, clip_id):
        conn = voiceclips.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            return dict(conn.execute("SELECT * FROM voice_clips WHERE id=?", (clip_id,)).fetchone())
        finally:
            conn.close()

    def test_an_upload_becomes_a_clip_the_feed_can_post(self):
        saved = spotter_clips.archive(self.db, ogg_opus(1.2, amplitude=2.0), "включи свет", station="M00ZRW3008RAJG")
        clip = self.row(saved["clip"])
        self.assertEqual(clip["station"], "M00ZRW3008RAJG")
        self.assertEqual((clip["transcript"], clip["status"], clip["trigger"]), ("включи свет", "sent", "spotter"))
        self.assertEqual(clip["engine"], voiceclips.SPOTTER_ENGINE)
        self.assertAlmostEqual(clip["dur"], 1.2, delta=0.1)
        self.assertGreater(clip["peak"], 1000)
        self.assertTrue(voiceclips.clip_file(self.db, saved["clip"]).is_file(), "the feed finds the recording on disk")

    def test_the_caption_says_the_speaker_recognised_it_and_does_not_invent_vad_figures(self):
        saved = spotter_clips.archive(self.db, ogg_opus(1.0), "включи свет")
        clip = self.row(saved["clip"])
        clip["first_in_dialog"] = True
        caption = clipfeed.build_caption(clip, "✅ выполнено: on [light.studiia]", 1)
        self.assertIn("«включи свет»", caption)
        self.assertIn("колонкой", caption)
        self.assertNotIn("хвост тишины", caption)
        self.assertNotIn("слова активации", caption)

    def test_the_sender_time_is_used_when_plausible_and_ignored_when_not(self):
        now = 1_800_000_000.0
        self.assertEqual(spotter_clips.clip_time(str(int((now - 3) * 1000)), now), now - 3)
        self.assertEqual(spotter_clips.clip_time(str(int((now - 3600) * 1000)), now), now)
        self.assertEqual(spotter_clips.clip_time("garbage", now), now)
        self.assertEqual(spotter_clips.clip_time(None, now), now)
        self.assertEqual(spotter_clips.clip_time("9" * 400, now), now, "a number too long for a float falls back like any other bad value")

    def test_uploads_that_are_not_a_command_recording_are_refused_with_a_reason(self):
        cases = {"no_transcript": (ogg_opus(1.0), "   "), "no_audio": (b"", "включи свет"),
                 "undecodable": (b"this is not an ogg stream", "включи свет"), "short": (ogg_opus(0.05), "включи свет")}
        for reason, (audio, transcript) in cases.items():
            with self.subTest(reason):
                with self.assertRaises(spotter_clips.Rejected) as caught:
                    spotter_clips.archive(self.db, audio, transcript)
                self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(voiceclips.usage(self.db)["clips"], 0, "a refused upload leaves nothing behind")

    def test_the_decoder_takes_ogg_only_and_never_more_than_the_cap(self):
        for name, payload in {"wav": subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "sine=duration=1", "-f", "wav", "pipe:1"],
                                                    stdout=subprocess.PIPE, check=True).stdout,
                              "hls playlist": b"#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:1,\nhttp://127.0.0.1:9/x.ts\n"}.items():
            with self.subTest(name), self.assertRaises(spotter_clips.Rejected) as caught:
                spotter_clips.decode(payload)
            self.assertEqual(caught.exception.reason, "undecodable")
        pcm = spotter_clips.decode(ogg_opus(spotter_clips.MAX_S + 8))
        self.assertLessEqual(len(pcm) // 2 / spotter_clips.RATE, spotter_clips.MAX_S + 0.1, "a runaway stream is cut at the cap")

    def test_chained_ogg_streams_cannot_run_past_the_cap(self):
        chained = ogg_opus(0.01) * 3000   # each chained stream restarts the timestamps: ffmpeg's -t alone lets ~40 s through
        cap = spotter_clips.MAX_S * spotter_clips.RATE * 2
        produced = []
        real_run = subprocess.run

        def spy(*args, **kwargs):
            done = real_run(*args, **kwargs)
            produced.append(len(done.stdout))
            return done
        with mock.patch.object(spotter_clips.subprocess, "run", spy):
            pcm = spotter_clips.decode(chained)
        self.assertLessEqual(len(pcm), cap)
        self.assertLess(produced[0], cap + 4096, "ffmpeg itself stops writing, the bytes are never buffered first")

    def insert_dialog(self, dialog_id, kind, start, end, station="M00ZRW3008RAJG", text=None):
        conn = voiceclips.connect(self.db)
        conn.execute("CREATE TABLE IF NOT EXISTS dialogs(id TEXT PRIMARY KEY, station TEXT, kind TEXT NOT NULL, src TEXT, "
                     "ts_start REAL NOT NULL, ts_end REAL, status TEXT NOT NULL DEFAULT '', actions TEXT NOT NULL DEFAULT '[]')")
        actions = json.dumps([{"kind": "запрос", "text": text, "res": ""}] if text else [], ensure_ascii=False)
        conn.execute("INSERT INTO dialogs(id, station, kind, ts_start, ts_end, actions) VALUES(?,?,?,?,?,?)",
                     (dialog_id, station, kind, start, end, actions))
        conn.commit()
        conn.close()

    def test_a_spotter_clip_links_to_its_text_dialog_and_a_gateway_clip_to_its_voice_dialog(self):
        base = time.time() - 600
        self.insert_dialog("voice-dialog", "voice", base, base + 12)
        self.insert_dialog("text-dialog", "text", base + 4, base + 5, text="Включи свет.")   # starts inside the voice dialog's window, ends later in the same seconds
        spotted = spotter_clips.archive(self.db, ogg_opus(1.0), "включи свет", sent_ms=str(int((base + 4.5) * 1000)), now=base + 6)
        heard = voiceclips.save(self.db, b"\x01\x00" * 16000, 16000, {"ts": base + 4.6, "status": "sent", "transcript": "алиса, включи свет"})
        voiceclips.link_dialogs(self.db)
        self.assertEqual(self.row(spotted["clip"])["dialog_id"], "text-dialog")
        self.assertEqual(self.row(heard)["dialog_id"], "voice-dialog", "the text dialog must not steal a voice clip heard at the same time")


    def test_commands_said_in_quick_succession_each_link_to_their_own_dialog(self):
        base = time.time() - 600
        self.insert_dialog("first", "text", base, base + 0.6, text="громче")
        self.insert_dialog("second", "text", base + 4, base + 4.6, text="пауза")
        first = spotter_clips.archive(self.db, ogg_opus(1.0), "громче", sent_ms=str(int((base + 0.3) * 1000)), now=base + 2)
        second = spotter_clips.archive(self.db, ogg_opus(1.0), "пауза", sent_ms=str(int((base + 4.3) * 1000)), now=base + 6)
        voiceclips.link_dialogs(self.db)
        self.assertEqual(self.row(first["clip"])["dialog_id"], "first")
        self.assertEqual(self.row(second["clip"])["dialog_id"], "second")

    def test_a_command_the_device_ran_alone_does_not_join_a_neighbours_dialog(self):
        base = time.time() - 600
        self.insert_dialog("light", "text", base, base + 1, text="включи свет")
        alone = spotter_clips.archive(self.db, ogg_opus(1.0), "громче", sent_ms=str(int((base + 3) * 1000)), now=base + 4)
        voiceclips.link_dialogs(self.db)
        self.assertIsNone(self.row(alone["clip"])["dialog_id"])

    def test_a_clip_links_to_a_dialog_of_its_own_station_only(self):
        base = time.time() - 600
        self.insert_dialog("studio", "text", base, base + 1, station="M00ZRW3008RAJG", text="включи свет")
        self.insert_dialog("bedroom", "text", base + 0.2, base + 1.2, station="M10DST310HXNPK", text="включи свет")   # started later: would win by recency
        studio = spotter_clips.archive(self.db, ogg_opus(1.0), "включи свет", station="M00ZRW3008RAJG", sent_ms=str(int((base + 0.5) * 1000)), now=base + 2)
        unknown = spotter_clips.archive(self.db, ogg_opus(1.0), "включи свет", sent_ms=str(int((base + 0.5) * 1000)), now=base + 2)
        voiceclips.link_dialogs(self.db)
        self.assertEqual(self.row(studio["clip"])["dialog_id"], "studio")
        self.assertEqual(self.row(unknown["clip"])["dialog_id"], "bedroom", "a clip with no station still links by time alone")

    def test_an_archive_made_before_the_station_column_is_migrated_in_place(self):
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE voice_clips(id TEXT PRIMARY KEY, ts REAL NOT NULL, dialog_id TEXT, path TEXT NOT NULL, dur REAL, rate INTEGER, "
                     "bytes INTEGER, rms INTEGER, peak INTEGER, voiced_ms INTEGER, floor_rms INTEGER, contrast REAL, engine TEXT, trigger TEXT, "
                     "status TEXT NOT NULL, reason TEXT, transcript TEXT, asr_ms INTEGER, silence_ms INTEGER, final_ms INTEGER)")
        conn.execute("INSERT INTO voice_clips(id, ts, path, status, transcript) VALUES('old', 1, 'x.opus', 'sent', 'старый клип')")
        conn.commit()
        conn.close()
        saved = spotter_clips.archive(self.db, ogg_opus(1.0), "включи свет", station="M00ZRW3008RAJG")
        self.assertEqual(self.row("old")["transcript"], "старый клип")
        self.assertEqual(self.row("old")["station"], None)
        self.assertEqual(self.row(saved["clip"])["station"], "M00ZRW3008RAJG")

    def test_a_spotter_clip_with_no_dialog_says_the_speaker_ran_it(self):
        saved = spotter_clips.archive(self.db, ogg_opus(1.0), "громче")
        result = clipfeed.describe_result(self.row(saved["clip"]), None, time.time() + 45)
        self.assertIn("сама колонка", result)
        self.assertNotIn("диалог не найден", result)

    def test_the_speakers_own_recognition_stays_out_of_the_recognizer_numbers(self):
        whisper = voiceclips.save(self.db, b"\x01\x00" * 16000, 16000, {"status": "sent", "transcript": "включи свет", "engine": "whisper"})
        spotted = spotter_clips.archive(self.db, ogg_opus(1.0), "включи свет")["clip"]
        conn = cliptruth.connect(self.db)
        for clip_id, truth in ((whisper, "включи свет"), (spotted, "включи свет")):
            conn.execute("INSERT INTO clip_truth(clip_id, truth, label, source, ts) VALUES(?,?,?,?,?)", (clip_id, truth, "correct", "test", time.time()))
        conn.commit()
        conn.close()
        stats = cliptruth.stats(self.db)
        self.assertEqual(stats["judged"], 2)
        self.assertEqual(stats["speech"], 1, "only Whisper's clip counts toward the headline")
        self.assertEqual(set(stats["engines"]), {"whisper", voiceclips.SPOTTER_ENGINE}, "the speaker's engine keeps its own row")

    def test_the_benchmark_does_not_score_candidates_against_the_speakers_own_text(self):
        whisper = voiceclips.save(self.db, b"\x01\x00" * 16000, 16000, {"status": "sent", "transcript": "включи свет", "engine": "whisper"})
        spotted = spotter_clips.archive(self.db, ogg_opus(1.0), "включи свет")["clip"]
        conn = bench_asr_archive.connect(self.db)
        for clip_id in (whisper, spotted):
            conn.execute("INSERT INTO asr_bench(clip_id, engine, text, ms, dur, empty, pattern, loop, err, ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (clip_id, "candidate", "включи свет", 100, 1.0, 0, 0, 0, None, time.time()))
        conn.commit()
        conn.close()
        engine = bench_asr_archive.summary(self.db)["engines"]["candidate"]
        self.assertEqual((engine["n"], engine["live_clips"]), (2, 1))

    def test_the_dashboard_attaches_the_recording_to_the_text_dialog_of_the_command(self):
        base = time.time() - 600
        self.insert_dialog("text-dialog", "text", base, base + 1, text="включи свет")
        spotter_clips.archive(self.db, ogg_opus(1.0), "включи свет", sent_ms=str(int((base + 0.5) * 1000)), now=base + 2)
        old_path = dialogstore.DB_PATH
        dialogstore.DB_PATH = self.db
        self.addCleanup(setattr, dialogstore, "DB_PATH", old_path)
        dialogs = [{"id": "text-dialog", "kind": "text"}, {"id": "other", "kind": "router"}]
        dashboard.attach_clips(dialogs)
        self.assertEqual([c["transcript"] for c in dialogs[0]["clips"]], ["включи свет"])
        self.assertEqual(dialogs[1]["clips"], [])


@unittest.skipUnless(FFMPEG, "ffmpeg with libopus is needed to build and decode the station's Ogg/Opus")
class GatewayRoute(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"AISIS_DIALOGS_DB": os.path.join(self.tmp.name, "dialogs.sqlite3")}
        self.old_root = os.environ.get("AISIS_VOICE_ARCHIVE")
        os.environ["AISIS_VOICE_ARCHIVE"] = os.path.join(self.tmp.name, "archive")
        self.addCleanup(lambda: os.environ.pop("AISIS_VOICE_ARCHIVE", None) if self.old_root is None
                        else os.environ.__setitem__("AISIS_VOICE_ARCHIVE", self.old_root))

    def post(self, audio, headers, archive=True):
        import stt_wyoming
        from aiohttp.test_utils import TestClient, TestServer

        async def run():
            gateway = stt_wyoming.STTGateway(env=self.env, archive=archive, verifier=object())
            async with TestClient(TestServer(gateway.app())) as client:
                response = await client.post("/spotter-clip", data=audio, headers=headers)
                return response.status, await response.text()

        return asyncio.run(run())

    def headers(self, transcript="включи свет"):
        return {"content-type": "audio/ogg", "x-aisis-transcript": quote(transcript), "x-aisis-station": "M00ZRW3008RAJG",
                "x-aisis-source": "ru-RU-quasar-commands-x", "x-aisis-frames": "3", "x-aisis-ts": str(int(time.time() * 1000))}

    def test_quasar_post_lands_as_a_clip(self):
        status, body = self.post(ogg_opus(1.0), self.headers())
        self.assertEqual(status, 201, body)
        conn = voiceclips.connect(self.env["AISIS_DIALOGS_DB"])
        row = conn.execute("SELECT transcript, trigger, status FROM voice_clips").fetchall()
        conn.close()
        self.assertEqual(row, [("включи свет", "spotter", "sent")])

    def test_spotter_uploads_alone_still_apply_the_retention_window(self):
        os.environ["AISIS_VOICE_RETENTION_DAYS"] = "1"
        self.addCleanup(os.environ.pop, "AISIS_VOICE_RETENTION_DAYS", None)
        old = voiceclips.save(self.env["AISIS_DIALOGS_DB"], b"\x01\x00" * 16000, 16000,
                              {"ts": time.time() - 3 * 86400, "status": "sent", "transcript": "старый"})
        status, body = self.post(ogg_opus(1.0), self.headers())
        self.assertEqual(status, 201, body)
        conn = voiceclips.connect(self.env["AISIS_DIALOGS_DB"])
        ids = [row[0] for row in conn.execute("SELECT id FROM voice_clips")]
        conn.close()
        self.assertNotIn(old, ids)
        self.assertEqual(len(ids), 1)

    def test_a_refused_upload_is_422_with_the_reason(self):
        status, body = self.post(b"not an ogg stream", self.headers())
        self.assertEqual(status, 422)
        self.assertIn("undecodable", body)

    def test_nothing_is_archived_when_archiving_is_off(self):
        status, _ = self.post(ogg_opus(1.0), self.headers(), archive=False)
        self.assertEqual(status, 204)
        self.assertFalse(os.path.exists(self.env["AISIS_DIALOGS_DB"]))


if __name__ == "__main__":
    unittest.main()
