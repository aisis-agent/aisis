"""clipfeed → Telegram routing: posts must name their forum topic, not depend on a live agent session (aisis#82)."""
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import clipfeed

SHIM = Path(__file__).resolve().parent.parent / "bin" / "clip-truth"


def completed(command, stdout="OK tg#28345\n", returncode=0, stderr=""):
    return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr=stderr)


class TgSendRouting(unittest.TestCase):
    def sent_command(self, settings, audio=None):
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            return completed(command)

        with mock.patch.object(subprocess, "run", fake_run):
            self.assertEqual(clipfeed.tg_send({**clipfeed.DEFAULTS, **settings}, "caption", audio), 28345)
        return calls[0]

    def test_topic_is_passed_even_when_the_pinned_session_is_gone(self):
        # tg silently sends to General when a --session pin names an ended session; the explicit topic is what keeps the post in place
        command = self.sent_command({"tg_session": "omp:dead-1", "tg_topic": 209201})
        self.assertEqual(command, ["tg", "--automated", "--session", "omp:dead-1", "--topic", "209201", "caption"])

    def test_topic_alone_and_with_a_file(self):
        audio = Path("clip-x.ogg")
        command = self.sent_command({"tg_topic": 209201}, audio)
        self.assertEqual(command, ["tg", "--automated", "--topic", "209201", "--file", str(audio), "caption"])

    def test_no_topic_flag_when_unset(self):
        self.assertEqual(self.sent_command({}), ["tg", "--automated", "caption"])


class TopicSetting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"AISIS_CONFIG_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("AISIS_CLIPFEED_SETTINGS", None)

    def test_default_is_unset_and_roundtrips(self):
        self.assertEqual(clipfeed.load_settings()["tg_topic"], 0)
        clipfeed.update_settings({"tg_topic": 209201})
        self.assertEqual(clipfeed.load_settings()["tg_topic"], 209201)

    def test_invalid_topics_are_refused(self):
        for bad in (-1, True, "209201", 1.5, None, 2**31):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                clipfeed.update_settings({"tg_topic": bad})

    def test_refresh_session_follows_the_callers_session_and_topic(self):
        answers = {"--session-id": "omp:live-2", "--session-topic": "209201"}
        with mock.patch.object(clipfeed, "_tg_query", lambda flag: answers[flag]):
            clipfeed.refresh_session()
        settings = clipfeed.load_settings()
        self.assertEqual((settings["tg_session"], settings["tg_topic"]), ("omp:live-2", 209201))

    def test_refresh_session_changes_nothing_outside_a_session(self):
        clipfeed.update_settings({"tg_session": "omp:keep-1", "tg_topic": 7})
        with mock.patch.object(clipfeed, "_tg_query", lambda flag: ""):
            clipfeed.refresh_session()
        settings = clipfeed.load_settings()
        self.assertEqual((settings["tg_session"], settings["tg_topic"]), ("omp:keep-1", 7))

    def test_refresh_session_keeps_the_saved_topic_when_the_topic_lookup_is_empty(self):
        clipfeed.update_settings({"tg_session": "omp:live-2", "tg_topic": 209201})
        answers = {"--session-id": "omp:live-2", "--session-topic": ""}
        with mock.patch.object(clipfeed, "_tg_query", lambda flag: answers[flag]):
            clipfeed.refresh_session()
        settings = clipfeed.load_settings()
        self.assertEqual((settings["tg_session"], settings["tg_topic"]), ("omp:live-2", 209201))

    def test_refresh_session_drops_another_sessions_topic_when_the_new_topic_is_unknown(self):
        clipfeed.update_settings({"tg_session": "omp:old-1", "tg_topic": 209201})
        answers = {"--session-id": "omp:live-2", "--session-topic": ""}
        with mock.patch.object(clipfeed, "_tg_query", lambda flag: answers[flag]):
            clipfeed.refresh_session()
        settings = clipfeed.load_settings()
        self.assertEqual((settings["tg_session"], settings["tg_topic"]), ("omp:live-2", 0))

    def test_a_failing_topic_query_is_treated_as_no_answer(self):
        clipfeed.update_settings({"tg_session": "omp:old-1", "tg_topic": 209201})
        with mock.patch.object(subprocess, "run", side_effect=[completed([], "omp:live-2\n"), subprocess.TimeoutExpired("tg", 10)]):
            clipfeed.refresh_session()
        settings = clipfeed.load_settings()
        self.assertEqual((settings["tg_session"], settings["tg_topic"]), ("omp:live-2", 0))


class ClipTruthShim(unittest.TestCase):
    def test_shim_runs_this_checkouts_clipfeed_with_the_aisis_venv_python(self):
        self.assertTrue(os.access(SHIM, os.X_OK))
        with tempfile.TemporaryDirectory() as tmp:
            python = Path(tmp) / "venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
            python.chmod(python.stat().st_mode | stat.S_IXUSR)
            link = Path(tmp) / "clip-truth"
            link.symlink_to(SHIM)  # ~/.local/bin/clip-truth is a symlink to the repo script
            out = subprocess.run([str(link), "29807", "="], capture_output=True, text=True, check=True,
                                 env={**os.environ, "AISIS_VENV": str(Path(tmp) / "venv")}).stdout.split("\n")
        script = Path(out[0])
        self.assertEqual(script.resolve(), (SHIM.parent.parent / "code" / "clipfeed.py").resolve())
        self.assertEqual(out[1:4], ["truth", "29807", "="])


if __name__ == "__main__":
    unittest.main()
