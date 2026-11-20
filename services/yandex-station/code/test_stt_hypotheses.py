"""The recognition-alternatives layer in the STT gateway (second_opinion.py): the same audio goes to a second recogniser beside Whisper,
its scored NBest is published to the adapter before the final and kept with the clip.

The core is a fake (hypotheses_fakes), the adapter is a local aiohttp server, Whisper is a function. The properties pinned here:
nothing changes with the feature off; the Wyoming transcript is never touched; a raising, hanging or slow second opinion (or an
adapter that is down or slow) changes neither the reply nor its timing beyond the documented bound; only a short command with a
confusable word makes the final wait.

The case behind it (clip tg#30662): the owner said «Выключи свет»; the speaker's own spotter heard «включи свет» and the audio, decoded
afterwards by GigaAM CTC, said «выключи свет».
"""
import asyncio
import contextlib
import os
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch
from urllib.parse import quote

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

try:
    import numpy  # noqa: F401
except ImportError:
    sys.modules.setdefault("speech_frontend", types.ModuleType("speech_frontend"))   # CI has no numpy: the level front-end is switched off below

import hypotheses_fakes as fakes
import second_opinion
import spotter_clips
import stt_wyoming
import voiceclips
from test_stt_wake import LoudIsSpeech, silence, speech

RATE = 16000
CONFUSABLE = "Включи свет."
PLAIN = "Какая погода?"
TOKEN = "test-only-token"


class HangingSecond:
    """A SecondOpinion that violates its own contract (it hangs, or raises): the gateway must still be fail-open."""
    enabled = True
    wait_ms = 100

    def __init__(self, raises=None, hang_in="resolve"):
        self.raises, self.hang_in = raises, hang_in

    def health(self):
        return {"enabled": True, "state": "ready", "model": "x", "last_ms": None}

    async def warm(self):
        return None

    async def close(self):
        return None

    def begin(self, pcm, rate):
        if self.raises and self.hang_in == "begin":
            raise self.raises
        return object()

    def waits_for(self, text):
        if self.raises and self.hang_in == "waits_for":
            raise self.raises
        return True

    async def resolve(self, task, primary_text, wait_ms):
        if self.raises:
            raise self.raises
        await asyncio.Event().wait()


class Stage(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "dialogs.sqlite3")
        previous = os.environ.get("AISIS_VOICE_ARCHIVE")
        os.environ["AISIS_VOICE_ARCHIVE"] = os.path.join(self.tmp.name, "archive")
        self.addCleanup(lambda: os.environ.pop("AISIS_VOICE_ARCHIVE", None) if previous is None
                        else os.environ.__setitem__("AISIS_VOICE_ARCHIVE", previous))
        self.posts = []          # what the adapter received: {at, auth, body}
        self.adapter_delay = 0.0      # after the evidence is stored, before the adapter answers
        self.adapter_pre_delay = 0.0  # before the adapter has read the request: until then nothing has "arrived"
        self.adapter_status = 204
        adapter = web.Application()
        adapter.router.add_post("/asr/hypotheses", self.adapter_hypotheses)
        self.adapter = TestServer(adapter)
        await self.adapter.start_server()
        self.adapter_url = str(self.adapter.make_url("")).rstrip("/")
        self.addAsyncCleanup(self.adapter.close)

    async def adapter_hypotheses(self, request):
        if self.adapter_pre_delay:
            await asyncio.sleep(self.adapter_pre_delay)
        self.posts.append({"at": time.monotonic(), "auth": request.headers.get("Authorization"), "body": await request.json()})
        if self.adapter_delay:
            await asyncio.sleep(self.adapter_delay)
        return web.Response(status=self.adapter_status)

    def env(self, **extra):
        env = {"AISIS_WYOMING_URI": "tcp://127.0.0.1:1", "AISIS_DIALOGS_DB": self.db, "AISIS_STT_AB_PERCENT": "0", "AISIS_STT_DSP": "0",
               "AISIS_STT_WAKE_WINDOW": "0", "AISIS_ADAPTER_URL": self.adapter_url, "AISIS_ADAPTER_TOKEN": TOKEN}
        env.update(extra)
        return env

    def second(self, script=None, **env):
        """A real SecondOpinion over the fake core."""
        return second_opinion.SecondOpinion({"AISIS_HYP_ENABLED": "1", "AISIS_HYP_PYTHON": "/engine/python", **env},
                                            load_core=lambda: fakes.core(script))

    async def talk(self, client, script=None, headers=None):
        """One websocket session through `client`: the audio, the finish, the one final. (final or None, when it arrived)."""
        async with client.ws_connect("/", headers=headers) as ws:
            await ws.send_json({"sample_rate": RATE})
            data = script if script is not None else speech(1500) + silence(900)
            for offset in range(0, len(data), 6400):
                await ws.send_bytes(data[offset:offset + 6400])
            await asyncio.sleep(0.15)
            await ws.send_json({"type": "finish"})
            try:   # a session has exactly one final
                return await asyncio.wait_for(ws.receive_json(), 3.0), time.monotonic()
            except (asyncio.TimeoutError, TypeError):
                return None, None

    async def play(self, heard, second=None, env=None, script=None, headers=None, whisper_delay=0.0, speaker=None):
        """One utterance through a gateway websocket. Returns a dict: messages [(message, received_at)], whisper_pcm, whisper_at, gateway,
        events (publish/send order), posts_when_final_left (the texts the adapter had RECEIVED when the final started to leave).
        `speaker` is the verdict an enforcing speaker check returns."""
        answers = list(heard)
        out = {"messages": [], "whisper_pcm": [], "whisper_at": None, "events": [], "posts_when_final_left": None}

        async def recognizer(uri, pcm, rate, language, **extras):
            out["whisper_pcm"].append(pcm)
            if whisper_delay:
                await asyncio.sleep(whisper_delay)
            out["whisper_at"] = time.monotonic()
            return answers.pop(0)

        verifier = types.SimpleNamespace(score=lambda pcm, rate: speaker) if speaker else object()
        gateway = stt_wyoming.STTGateway(env=env if env is not None else self.env(), vad_factory=LoudIsSpeech, recognizer=recognizer,
                                         verifier=verifier, second_opinion=second)
        out["gateway"] = gateway
        publish, send_json = gateway.publish_hypotheses, web.WebSocketResponse.send_json

        def spy_publish(*args, **kwargs):
            out["events"].append("publish")
            return publish(*args, **kwargs)

        async def spy_send(ws, data, *args, **kwargs):
            out["events"].append("send")
            if out["posts_when_final_left"] is None:
                out["posts_when_final_left"] = [post["body"]["text"] for post in self.posts]
            return await send_json(ws, data, *args, **kwargs)

        with contextlib.ExitStack() as patches:
            patches.enter_context(patch.object(gateway, "publish_hypotheses", spy_publish))
            patches.enter_context(patch.object(web.WebSocketResponse, "send_json", spy_send))
            if speaker:
                patches.enter_context(patch.object(gateway, "speaker_settings", lambda: {"mode": "enforce"}))
                patches.enter_context(patch.object(gateway, "speaker_record", lambda *args: None))
            async with TestClient(TestServer(gateway.app())) as client:
                reply, received_at = await self.talk(client, script, headers)
                if reply is not None:
                    out["messages"].append((reply, received_at))
                await self.settle(gateway)
        return out

    async def settle(self, gateway, seconds=3.0):
        """Let the background work (late opinions, the loopback POSTs, the archive writes) finish."""
        end = time.monotonic() + seconds
        while time.monotonic() < end and (gateway.speaker_background or gateway.hypothesis_background):
            await asyncio.sleep(0.02)

    def replies(self, out):
        return [message for message, _ in out["messages"]]

    def clip_rows(self):
        conn = voiceclips.connect(self.db)
        try:
            return conn.execute("SELECT id, status, transcript FROM voice_clips ORDER BY ts, id").fetchall()
        finally:
            conn.close()


class Disabled(Stage):
    async def test_nothing_changes_when_the_feature_is_off(self):
        loaded = []
        second = second_opinion.SecondOpinion({}, load_core=lambda: loaded.append(1))
        off = await self.play([CONFUSABLE], second=second)
        baseline = await self.play([CONFUSABLE], second=None)
        self.assertEqual(self.replies(off), [{"text": CONFUSABLE, "end_of_utt": True}])
        self.assertEqual(self.replies(off), self.replies(baseline))
        self.assertEqual((self.posts, loaded, off["events"]), ([], [], ["send"]))
        self.assertIsNone(off["gateway"].hypothesis_session)   # no client session was even made
        self.assertEqual(self.clip_rows()[0][1:], ("sent", CONFUSABLE))
        conn = voiceclips.connect(self.db)
        try:
            self.assertEqual(conn.execute("SELECT count(*) FROM clip_hypotheses").fetchone()[0], 0)
        finally:
            conn.close()


class Published(Stage):
    async def test_the_evidence_is_published_before_the_final_and_the_transcript_is_untouched(self):
        out = await self.play([CONFUSABLE], second=self.second())
        self.assertEqual(self.replies(out), [{"text": CONFUSABLE, "end_of_utt": True}])
        self.assertEqual(out["events"], ["publish", "send"])
        self.assertEqual(len(self.posts), 1)
        body = self.posts[0]["body"]
        self.assertEqual((body["station"], body["text"], body["source"]), (None, CONFUSABLE, "whisper"))
        self.assertEqual([h["text"] for h in body["nbest"]["hypotheses"]], [CONFUSABLE, "выключи свет"])
        self.assertTrue(70 <= body["ms"] < 400, body["ms"])
        self.assertEqual(self.posts[0]["auth"], "Bearer " + TOKEN)

    async def test_the_second_recogniser_gets_the_same_pcm_as_whisper(self):
        second = self.second()
        out = await self.play([CONFUSABLE], second=second)
        (whisper_pcm,) = out["whisper_pcm"]
        (worker,) = fakes.FakeWorker.instances
        self.assertEqual(worker.decodes, [(len(whisper_pcm), RATE)])

    async def test_the_station_comes_from_the_websocket_header_when_there_is_one(self):
        await self.play([CONFUSABLE], second=self.second(), headers={"x-aisis-station": "st1"})
        self.assertEqual(self.posts[0]["body"]["station"], "st1")

    async def test_without_a_token_no_authorization_header_is_sent(self):
        env = self.env()
        del env["AISIS_ADAPTER_TOKEN"]
        await self.play([CONFUSABLE], second=self.second(), env=env)
        self.assertIsNone(self.posts[0]["auth"])

    async def test_the_opinion_is_kept_with_the_clip(self):
        await self.play([CONFUSABLE], second=self.second())
        ((clip_id, status, transcript),) = self.clip_rows()
        stored = voiceclips.hypotheses_of(self.db, clip_id)
        self.assertEqual((status, transcript, stored["source"], stored["primary_text"]), ("sent", CONFUSABLE, "whisper", CONFUSABLE))
        self.assertEqual(stored["nbest"], self.posts[0]["body"]["nbest"])

    async def test_an_utterance_with_the_activation_name_in_the_text_is_not_scored(self):
        """The audio holds the name the transcript lacks: the opinion would be about a different text. Nothing is published."""
        await self.play(["Алиса, включи свет."], second=self.second())
        self.assertEqual(self.posts, [])

    async def test_the_adapter_has_received_the_evidence_when_the_final_leaves(self):
        """Receipt, not call order: the adapter is slow to read the request, and the final must still have waited for it."""
        self.adapter_pre_delay = 0.05
        out = await self.play([CONFUSABLE], second=self.second())
        self.assertEqual(out["posts_when_final_left"], [CONFUSABLE])
        self.assertLess(self.posts[0]["at"], out["messages"][0][1])


class Waiting(Stage):
    async def test_a_confusable_command_waits_for_a_slow_opinion_within_the_bound(self):
        second = self.second(lambda worker: setattr(worker, "delay", 0.15), AISIS_HYP_WAIT_MS="600")
        out = await self.play([CONFUSABLE], second=second)
        self.assertEqual(out["events"], ["publish", "send"])   # the evidence was there when the final left
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.replies(out), [{"text": CONFUSABLE, "end_of_utt": True}])

    async def test_a_slower_opinion_costs_the_final_at_most_the_bound_and_arrives_later(self):
        second = self.second(lambda worker: setattr(worker, "delay", 1.5), AISIS_HYP_WAIT_MS="200")
        out = await self.play([CONFUSABLE], second=second)
        (reply, received) = out["messages"][0]
        self.assertEqual(reply, {"text": CONFUSABLE, "end_of_utt": True})
        self.assertLess(received - out["whisper_at"], 0.2 + 0.5)   # the bound plus scheduling slack
        self.assertEqual(out["events"], ["send", "publish"])           # the final did not wait for it
        self.assertEqual(len(self.posts), 1)                           # ...and the evidence still arrived, late
        self.assertGreater(self.posts[0]["at"], received)
        ((clip_id, _, _),) = self.clip_rows()
        self.assertIsNotNone(voiceclips.hypotheses_of(self.db, clip_id))

    async def test_other_utterances_send_the_final_at_once_and_record_the_opinion_when_it_arrives(self):
        second = self.second(lambda worker: setattr(worker, "delay", 1.2), AISIS_HYP_WAIT_MS="2000")
        out = await self.play([PLAIN], second=second)
        (reply, received) = out["messages"][0]
        self.assertEqual(reply, {"text": PLAIN, "end_of_utt": True})
        self.assertLess(received - out["whisper_at"], 0.7)
        self.assertEqual(out["events"], ["send", "publish"])
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0]["body"]["text"], PLAIN)

    async def test_a_long_command_does_not_wait_either(self):
        second = self.second(lambda worker: setattr(worker, "delay", 1.2), AISIS_HYP_WAIT_MS="2000")
        out = await self.play(["Включи свет и расскажи мне пожалуйста про погоду на завтра."], second=second)
        self.assertLess(out["messages"][0][1] - out["whisper_at"], 0.7)


class FailOpen(Stage):
    async def baseline(self):
        return self.replies(await self.play([CONFUSABLE], second=None))

    async def assert_same_reply_within(self, second, bound):
        out = await self.play([CONFUSABLE], second=second)
        self.assertEqual(self.replies(out), await self.baseline())
        self.assertLess(out["messages"][0][1] - out["whisper_at"], bound)
        self.assertEqual(self.posts, [])
        return out

    async def test_a_hanging_second_opinion_delays_the_final_by_the_bound_at_most(self):
        await self.assert_same_reply_within(HangingSecond(), HangingSecond.wait_ms / 1000 + 0.6)

    async def test_a_raising_resolve_changes_nothing(self):
        await self.assert_same_reply_within(HangingSecond(raises=RuntimeError("boom")), 0.7)

    async def test_a_raising_begin_changes_nothing(self):
        await self.assert_same_reply_within(HangingSecond(raises=RuntimeError("boom"), hang_in="begin"), 0.7)

    async def test_a_raising_waits_for_changes_nothing(self):
        await self.assert_same_reply_within(HangingSecond(raises=RuntimeError("boom"), hang_in="waits_for"), 0.7)

    async def test_a_worker_that_cannot_start_changes_nothing(self):
        second = self.second(lambda worker: setattr(worker, "fail_start", OSError("no engine")))
        out = await self.play([CONFUSABLE], second=second)
        self.assertEqual(self.replies(out), await self.baseline())
        self.assertEqual(self.posts, [])
        self.assertEqual(second.health()["state"], "down:OSError")

    async def test_an_adapter_that_is_down_changes_nothing_and_the_status_is_recorded(self):
        await self.adapter.close()
        with self.assertLogs("aisis.stt", level="INFO") as logs:
            out = await self.play([CONFUSABLE], second=self.second())
        self.assertEqual(self.replies(out), [{"text": CONFUSABLE, "end_of_utt": True}])
        self.assertLess(out["messages"][0][1] - out["whisper_at"], 0.8)
        self.assertTrue(any("stt_hyp publish status=error:" in line for line in logs.output), logs.output)

    async def test_an_adapter_that_hangs_holds_the_final_back_by_the_publish_bound_at_most(self):
        self.adapter_delay = 5.0
        began = time.monotonic()
        with self.assertLogs("aisis.stt", level="INFO") as logs:
            out = await self.play([CONFUSABLE], second=self.second())
        self.assertEqual(self.replies(out), [{"text": CONFUSABLE, "end_of_utt": True}])
        held = out["messages"][0][1] - out["whisper_at"]
        self.assertGreater(held, stt_wyoming.HYP_PUBLISH_TIMEOUT_S * 0.8)   # it waited for the adapter...
        self.assertLess(held, 0.8)                                          # ...but only for the bound
        self.assertTrue(any("stt_hyp publish status=timeout" in line for line in logs.output), logs.output)
        self.assertLess(time.monotonic() - began, 4.0)   # settle() returned: the POST did not hold on for the adapter's five seconds

    async def test_an_adapter_that_refuses_the_post_changes_nothing_and_the_status_is_recorded(self):
        self.adapter_status = 401
        with self.assertLogs("aisis.stt", level="INFO") as logs:
            out = await self.play([CONFUSABLE], second=self.second())
        self.assertEqual(self.replies(out), [{"text": CONFUSABLE, "end_of_utt": True}])
        self.assertTrue(any("stt_hyp publish status=http:401" in line for line in logs.output), logs.output)

    async def test_a_zero_wait_budget_never_holds_the_final_for_the_adapter(self):
        self.adapter_pre_delay = 0.12   # within the POST's own 150 ms bound: the evidence arrives, but not before the final has left
        out = await self.play([CONFUSABLE], second=self.second(AISIS_HYP_WAIT_MS="0"), whisper_delay=0.3)
        (reply, received) = out["messages"][0]
        self.assertEqual(reply, {"text": CONFUSABLE, "end_of_utt": True})
        self.assertEqual(out["events"], ["publish", "send"])   # the opinion was ready: it was handed over before the final, not waited for
        self.assertEqual(out["posts_when_final_left"], [])
        self.assertEqual(len(self.posts), 1)                   # and it still arrived, after the final
        self.assertGreater(self.posts[0]["at"], received)


class Rejected(Stage):
    async def test_a_distrusted_transcript_is_not_published_but_its_opinion_is_kept(self):
        out = await self.play(["Продолжение следует..."], second=self.second())
        self.assertEqual(self.replies(out), [{"text": "", "end_of_utt": True}])   # dropped mid-stream, then the empty end-of-stream final
        self.assertEqual(self.posts, [])
        ((clip_id, status, _),) = self.clip_rows()
        stored = voiceclips.hypotheses_of(self.db, clip_id)
        self.assertEqual((status, stored["primary_text"]), ("blacklist", "Продолжение следует..."))


class Foreign(Stage):
    """With speaker enforcement on, a foreign voice gets the empty `foreign_voice` final: nothing about its words may reach the adapter."""
    FOREIGN = {"score": 0.12, "best": None, "verdict": "foreign", "ms": 1.0, "dur": 1.0, "model": "test"}
    OWNER = {**FOREIGN, "score": 0.9, "verdict": "owner"}
    REJECTED = {"text": "", "end_of_utt": True, "rejected": "foreign_voice", "score": 0.12}

    async def test_a_slow_opinion_about_a_foreign_voice_is_kept_with_the_clip_but_never_published(self):
        second = self.second(lambda worker: setattr(worker, "delay", 0.6), AISIS_HYP_WAIT_MS="150")
        out = await self.play([CONFUSABLE], second=second, speaker=self.FOREIGN)
        self.assertEqual(self.replies(out), [self.REJECTED])
        self.assertEqual(self.posts, [])
        ((clip_id, _, _),) = self.clip_rows()
        self.assertIsNotNone(voiceclips.hypotheses_of(self.db, clip_id))

    async def test_a_fast_opinion_about_a_foreign_voice_is_not_published_either(self):
        out = await self.play([CONFUSABLE], second=self.second(), speaker=self.FOREIGN)
        self.assertEqual((self.replies(out), self.posts), ([self.REJECTED], []))
        ((clip_id, _, _),) = self.clip_rows()
        self.assertIsNotNone(voiceclips.hypotheses_of(self.db, clip_id))

    async def test_the_final_of_a_foreign_voice_does_not_wait_for_the_opinion(self):
        second = self.second(lambda worker: setattr(worker, "delay", 1.0), AISIS_HYP_WAIT_MS="2000")
        out = await self.play([CONFUSABLE], second=second, speaker=self.FOREIGN)
        self.assertLess(out["messages"][0][1] - out["whisper_at"], 0.7)

    async def test_the_owner_is_still_published_late_with_enforcement_on(self):
        second = self.second(lambda worker: setattr(worker, "delay", 0.6), AISIS_HYP_WAIT_MS="150")
        out = await self.play([CONFUSABLE], second=second, speaker=self.OWNER)
        self.assertEqual(self.replies(out), [{"text": CONFUSABLE, "end_of_utt": True}])
        self.assertEqual([post["body"]["text"] for post in self.posts], [CONFUSABLE])


class Stalled(Stage):
    async def test_turns_against_a_worker_that_never_starts_keep_the_outstanding_work_bounded(self):
        cap = second_opinion.MAX_OUTSTANDING
        second = self.second(lambda worker: setattr(worker, "hang_start", True), AISIS_HYP_WAIT_MS="50")

        async def recognizer(uri, pcm, rate, language, **extras):
            return CONFUSABLE

        gateway = stt_wyoming.STTGateway(env=self.env(), vad_factory=LoudIsSpeech, recognizer=recognizer, verifier=object(), second_opinion=second)
        with patch.object(second_opinion, "TASK_TTL_S", 2.0), self.assertLogs("aisis.stt", level="INFO") as logs:
            async with TestClient(TestServer(gateway.app())) as client:
                finals = await asyncio.gather(*(self.talk(client) for _ in range(cap + 4)))
                self.assertEqual([final for final, _ in finals], [{"text": CONFUSABLE, "end_of_utt": True}] * (cap + 4))
                self.assertLessEqual(len(second._tasks), cap)
                await asyncio.sleep(2.3)   # past the lifetime of the decodes that were accepted
                self.assertEqual(second._tasks, set())
        self.assertTrue(any("stt_hyp late status=busy" in line or "stt_hyp status=busy" in line for line in logs.output), logs.output)
        self.assertEqual(self.posts, [])


class Health(Stage):
    async def test_healthz_asr_reports_the_second_opinion(self):
        async def recognizer(uri, pcm, rate, language, **extras):
            return ""
        gateway = stt_wyoming.STTGateway(env=self.env(), recognizer=recognizer, second_opinion=self.second())
        async with TestClient(TestServer(gateway.app())) as client:
            response = await client.get("/healthz/asr")
            body = await response.json()
            plain = await (await client.get("/healthz")).json()
        self.assertEqual(response.status, 200)
        self.assertIn(body["second_opinion"].pop("state"), ("idle", "starting", "ready"))   # the worker warms up at startup
        self.assertEqual(body["second_opinion"], {"enabled": True, "model": "gigaam-v3-ctc", "last_ms": None})
        self.assertEqual(plain["second_opinion"]["enabled"], True)

    async def test_off_by_default_says_so(self):
        async def recognizer(uri, pcm, rate, language, **extras):
            return ""
        gateway = stt_wyoming.STTGateway(env=self.env(), recognizer=recognizer)
        async with TestClient(TestServer(gateway.app())) as client:
            body = await (await client.get("/healthz/asr")).json()
        self.assertEqual(body["second_opinion"]["enabled"], False)
        self.assertEqual(body["second_opinion"]["state"], "disabled")


class Lifecycle(Stage):
    async def test_the_worker_is_warmed_at_startup_and_closed_at_cleanup(self):
        second = self.second()
        gateway = stt_wyoming.STTGateway(env=self.env(), second_opinion=second)
        async with TestClient(TestServer(gateway.app())):
            for _ in range(50):
                if fakes.FakeWorker.instances and fakes.FakeWorker.instances[0].started:
                    break
                await asyncio.sleep(0.02)
            self.assertTrue(fakes.FakeWorker.instances[0].started)
        self.assertTrue(fakes.FakeWorker.instances[0].closed)

    async def test_nothing_is_started_when_off(self):
        loaded = []
        gateway = stt_wyoming.STTGateway(env=self.env(), second_opinion=second_opinion.SecondOpinion({}, load_core=lambda: loaded.append(1)))
        async with TestClient(TestServer(gateway.app())):
            await asyncio.sleep(0.05)
        self.assertEqual(loaded, [])


class Spotter(Stage):
    PCM = speech(800)

    async def upload(self, gateway, station="st1"):
        async with TestClient(TestServer(gateway.app())) as client:
            began = time.monotonic()
            with patch.object(spotter_clips, "decode", lambda audio: self.PCM):
                response = await client.post("/spotter-clip", data=b"OggS-test", headers={
                    "x-aisis-transcript": quote("включи свет"), "x-aisis-station": station, "x-aisis-source": "spotter", "x-aisis-ts": str(int(time.time() * 1000))})
            answered = time.monotonic() - began
            body = await response.json()
            await self.settle(gateway)
        return response.status, body, answered

    async def test_the_spotters_text_is_scored_published_and_kept(self):
        gateway = stt_wyoming.STTGateway(env=self.env(), second_opinion=self.second())
        status, saved, _ = await self.upload(gateway)
        self.assertEqual(status, 201)
        self.assertEqual(len(self.posts), 1)
        body = self.posts[0]["body"]
        self.assertEqual((body["station"], body["text"], body["source"]), ("st1", "включи свет", "spotter"))
        self.assertEqual([h["text"] for h in body["nbest"]["hypotheses"]], ["включи свет", "выключи свет"])
        stored = voiceclips.hypotheses_of(self.db, saved["clip"])
        self.assertEqual((stored["source"], stored["primary_text"], stored["nbest"]), ("spotter", "включи свет", body["nbest"]))
        (worker,) = fakes.FakeWorker.instances
        self.assertEqual(worker.decodes[0][1], RATE)

    async def test_the_upload_is_answered_without_waiting_for_the_opinion(self):
        gateway = stt_wyoming.STTGateway(env=self.env(), second_opinion=self.second(lambda worker: setattr(worker, "delay", 1.2)))
        status, _, answered = await self.upload(gateway)
        self.assertEqual(status, 201)
        self.assertLess(answered, 0.8)
        self.assertEqual(len(self.posts), 1)   # it still arrived afterwards

    async def test_a_failing_second_opinion_leaves_the_upload_as_it_was(self):
        gateway = stt_wyoming.STTGateway(env=self.env(), second_opinion=HangingSecond(raises=RuntimeError("boom"), hang_in="begin"))
        status, saved, _ = await self.upload(gateway)
        self.assertEqual((status, sorted(saved)), (201, ["clip", "dur"]))
        self.assertEqual(self.posts, [])

    async def test_off_by_default_nothing_is_published(self):
        gateway = stt_wyoming.STTGateway(env=self.env())
        status, saved, _ = await self.upload(gateway)
        self.assertEqual((status, sorted(saved)), (201, ["clip", "dur"]))
        self.assertEqual(self.posts, [])
        self.assertIsNone(voiceclips.hypotheses_of(self.db, saved["clip"]))


if __name__ == "__main__":
    unittest.main()
