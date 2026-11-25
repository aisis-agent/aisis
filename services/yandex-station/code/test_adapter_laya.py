"""The adapter's /laya proxy finds the CURRENT Laya socket: the file name carries the task-cli version and changes at every upgrade.

2026-11-20 11:18 the quasar semantic shadow logged `laya http 503` for every phrase: the adapter looked for a hard-coded
laya-0.23.1.sock that no longer existed (the live one was laya-0.34.0.sock, now 0.35.1).

aisis#247: the proxy forwards the Laya protocol v2 `model` and `max_len`, turns a `language` hint or Cyrillic text into the
multilingual checkpoint, and answers a distinct status and JSON body for every failure.
"""
import asyncio
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

import laya_route
from adapter import Adapter, laya_sockets

TOKEN = "test-only-token"
AUTH = {"Authorization": "Bearer " + TOKEN}


def abandoned_socket(path):
    """A socket file nobody listens on: bound, then closed (closing does not remove the inode)."""
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.close()
    return path


class SocketListTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.base = Path(self.dir.name)

    def test_newest_version_first_and_lock_files_are_ignored(self):
        old = abandoned_socket(self.base / "laya-0.9.0.sock")
        newest = abandoned_socket(self.base / "laya-0.35.1.sock")
        (self.base / "laya-0.36.0.sock.lock").write_text("")
        self.assertEqual(laya_sockets({}, self.base), [newest, old])

    def test_explicit_env_overrides_discovery(self):
        abandoned_socket(self.base / "laya-0.35.1.sock")
        self.assertEqual(laya_sockets({"LAYA_SOCKET": "/run/x.sock"}, self.base), [Path("/run/x.sock")])

    def test_no_socket_is_an_empty_list(self):
        (self.base / "laya-0.34.0.sock.lock").write_text("")
        self.assertEqual(laya_sockets({}, self.base), [])


class ParseRequestTests(unittest.TestCase):
    def build(self, **body):
        body.setdefault("message", "turn on the light")
        request = laya_route.parse(body)
        return laya_route.line_for(request), request.model

    def problem(self, **body):
        with self.assertRaises(laya_route.LayaProblem) as raised:
            self.build(**body)
        return raised.exception

    def test_missing_questions_default_to_an_empty_object(self):
        self.assertEqual(self.build()[0]["questions"], {})

    def test_a_request_without_a_hint_goes_to_the_multilingual_checkpoint_whatever_the_script(self):
        texts = ("включи свет", "turn on the light", "uključi svetlo u dnevnoj sobi", "Schalte das Licht im Wohnzimmer ein",
                 "vkluchi svet", "включи the light", "включи свет в living room", "12345", "👍")
        for text in texts:
            with self.subTest(text=text):
                line, model = self.build(message=text)
                self.assertEqual((line["model"], model), ("multilingual", "multilingual"))

    def test_the_line_for_laya_is_exact_and_always_names_the_model(self):
        line, _ = self.build(questions={"q": {"type": "choice"}})
        self.assertEqual(line, {"op": "predict", "message": "turn on the light", "questions": {"q": {"type": "choice"}},
                                "model": "multilingual"})

    def test_language_hint_selects_the_checkpoint(self):
        for language, expected in (("ru", "multilingual"), ("sr", "multilingual"), ("de", "multilingual"), ("zh-Hans-CN", "multilingual"),
                                   ("mul", "multilingual"), ("und", "multilingual"),
                                   ("en", "english"), ("EN", "english"), ("en-US", "english")):
            with self.subTest(language=language):
                self.assertEqual(self.build(language=language)[1], expected)

    def test_an_english_hint_on_text_with_cyrillic_is_ambiguous_and_goes_multilingual(self):
        self.assertEqual(self.build(message="включи свет", language="en")[1], "multilingual")
        self.assertEqual(self.build(message="turn on свет", language="en-GB")[1], "multilingual")

    def test_a_non_english_hint_is_followed_even_for_latin_text(self):
        self.assertEqual(self.build(language="ru")[1], "multilingual")
        self.assertEqual(self.build(message="uključi svetlo", language="sr-Latn")[1], "multilingual")

    def test_explicit_model_wins_over_language_and_script(self):
        self.assertEqual(self.build(message="включи свет", model="english", language="ru")[1], "english")
        self.assertEqual(self.build(model="multilingual", language="en")[1], "multilingual")
        self.assertEqual(self.build(model="english")[1], "english")

    def test_language_is_not_forwarded_to_laya(self):
        self.assertNotIn("language", self.build(language="ru")[0])

    def test_max_len_is_forwarded_with_the_routed_model(self):
        line, model = self.build(max_len=1024)
        self.assertEqual((line["max_len"], line["model"], model), (1024, "multilingual", "multilingual"))
        line, model = self.build(max_len=2048, language="en")
        self.assertEqual((line["max_len"], line["model"]), (2048, "english"))

    def test_no_max_len_is_sent_when_none_is_given(self):
        self.assertNotIn("max_len", self.build()[0])

    def test_max_len_bounds_are_inclusive(self):
        self.assertEqual(self.build(max_len=512)[0]["max_len"], 512)
        self.assertEqual(self.build(max_len=2048)[0]["max_len"], 2048)

    def test_bad_model_values_are_refused_before_anything_is_sent(self):
        for value in ("Multilingual", "convaiinnovations/laya-multilingual", "../x", "", None, 5, ["english"], True):
            with self.subTest(model=value):
                problem = self.problem(model=value)
                self.assertEqual((problem.status, problem.code, problem.fields["field"]), (400, "invalid_request", "model"))

    def test_bad_max_len_values_are_refused(self):
        for value in (True, False, 511, 2049, 0, -1, 1024.0, 1e3, "1024", None, float("nan"), [512]):
            with self.subTest(max_len=value):
                problem = self.problem(max_len=value)
                self.assertEqual((problem.status, problem.fields["field"]), (400, "max_len"))

    def test_bad_language_values_are_refused(self):
        for value in ("", "r", "ru_RU", "русский", "ru ", "x" * 40, 5, None, ["ru"], "ru-"):
            with self.subTest(language=value):
                self.assertEqual(self.problem(language=value).fields["field"], "language")

    def test_bad_message_and_questions_and_body_are_refused(self):
        many = {f"q{i}": {"type": "choice"} for i in range(laya_route.QUESTIONS_MAX + 1)}
        for body, field in (({"message": ""}, "message"), ({"message": "  "}, "message"), ({"message": 3}, "message"),
                            ({"message": "я" * 2001}, "message"), ({}, "message"),
                            ({"message": "x", "questions": []}, "questions"), ({"message": "x", "questions": None}, "questions"),
                            ({"message": "x", "questions": many}, "questions")):
            with self.subTest(body=body if len(str(body)) < 200 else "too many questions"):
                with self.assertRaises(laya_route.LayaProblem) as raised:
                    laya_route.parse(body)
                self.assertEqual((raised.exception.status, raised.exception.fields["field"]), (400, field))
        for body in ([], "x", None, 5):
            with self.subTest(body=body):
                with self.assertRaises(laya_route.LayaProblem) as raised:
                    laya_route.parse(body)
                self.assertEqual(raised.exception.fields["field"], "body")

    def test_a_message_of_exactly_the_limit_is_accepted(self):
        self.assertEqual(len(self.build(message="я" * 2000)[0]["message"]), 2000)

    def test_exactly_the_most_questions_are_accepted(self):
        questions = {f"q{i}": {"type": "choice"} for i in range(laya_route.QUESTIONS_MAX)}
        self.assertEqual(len(self.build(questions=questions)[0]["questions"]), laya_route.QUESTIONS_MAX)


def reply(**fields):
    return (json.dumps(fields) + "\n").encode()


class CheckReplyTests(unittest.TestCase):
    def problem(self, raw, model="english"):
        with self.assertRaises(laya_route.LayaProblem) as raised:
            laya_route.check_reply(raw, model)
        return raised.exception

    def test_a_success_is_passed_through_unchanged_with_its_model_and_truncation_metadata(self):
        raw = reply(ok=True, result={"answers": {}}, meta={"api_version": 2, "model": "multilingual", "checkpoint": "c",
                                                           "max_len": 1024, "truncated": True, "input_tokens": 3000})
        self.assertEqual(laya_route.check_reply(raw, "multilingual"), raw.decode())

    def test_an_older_server_that_ignores_the_model_is_caught(self):
        for raw in (reply(ok=True, result={}), reply(ok=True, result={}, meta="x"),
                    reply(ok=True, result={}, meta={"model": "english"})):
            with self.subTest(raw=raw):
                problem = self.problem(raw, "multilingual")
                self.assertEqual((problem.status, problem.code), (502, "laya_model_not_confirmed"))

    def test_a_model_still_loading_is_503_with_its_reason(self):
        problem = self.problem(reply(ok=False, error="model_unavailable", model="multilingual", reason="loading",
                                     checkpoint="c", detail="LoadInProgress"), "multilingual")
        self.assertEqual((problem.status, problem.code), (503, "laya_model_unavailable"))
        self.assertEqual(problem.fields, {"model": "multilingual", "reason": "loading"})

    def test_a_model_that_failed_to_load_is_503_with_its_reason(self):
        problem = self.problem(reply(ok=False, error="model_unavailable", model="multilingual", reason="load_failed"), "multilingual")
        self.assertEqual((problem.status, problem.fields["reason"]), (503, "load_failed"))

    def test_laya_refusing_the_request_is_400_with_its_field(self):
        problem = self.problem(reply(ok=False, error="invalid_request", field="max_len", detail="max_len must be an integer"), "english")
        self.assertEqual((problem.status, problem.code, problem.fields["field"]), (400, "invalid_request", "max_len"))

    def test_a_server_without_a_model_registry_is_a_bad_upstream(self):
        self.assertEqual(self.problem(reply(ok=False, error="unsupported"), "english").code, "laya_route_unsupported")

    def test_a_legacy_exception_name_is_a_bad_upstream_with_that_name(self):
        problem = self.problem(reply(ok=False, error="ValueError"))
        self.assertEqual((problem.status, problem.code, problem.fields), (502, "laya_upstream_error", {"upstream": "ValueError"}))

    def test_an_error_text_that_is_not_a_plain_word_is_not_echoed(self):
        problem = self.problem(reply(ok=False, error="/home/ultra/secret path"))
        self.assertEqual((problem.code, problem.fields), ("laya_upstream_error", {}))

    def test_empty_unparsable_and_misshapen_replies_are_502(self):
        for raw, code in ((b"", "laya_empty_response"), (b"  \n", "laya_empty_response"), (b"not json\n", "laya_bad_response"),
                          (b"[1]\n", "laya_bad_response"), (reply(result={}), "laya_bad_response"),
                          (reply(ok="yes"), "laya_bad_response"), (b"\xff\n", "laya_bad_response"),
                          ('{"ok": true, "result": {}}'.encode("utf-16"), "laya_bad_response"),
                          ('{"ok": true, "result": {}}'.encode("utf-16-le"), "laya_bad_response"),
                          (b'{"ok": true, "result": ' + b"[" * 200_000 + b"]" * 200_000 + b"}\n", "laya_bad_response")):  # under 1 MiB
            with self.subTest(raw=raw):
                problem = self.problem(raw)
                self.assertEqual((problem.status, problem.code), (502, code))


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.base = Path(self.dir.name)
        self.requests = []

    async def client(self, **extra):
        env = {"AISIS_ALLOWED_STATIONS": "st1", "AISIS_STATE_DB": str(self.base / "adapter.sqlite3"), "AISIS_MEMORY": "0",
               "AISIS_CHATGPT_COMMAND": "[]", "AISIS_ADAPTER_TOKEN": TOKEN, "AISIS_HA_ACTIONS": "off",
               "AISIS_DIALOGS_DB": str(self.base / "dialogs.sqlite3"), "XDG_STATE_HOME": str(self.base / "journal-state"), **extra}
        client = TestClient(TestServer(Adapter(env).app()))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    async def serve(self, path, answer=None, deaf=False):
        """A fake Laya server: `answer(request)` is the raw reply (None: close without a reply, "hang": never answer);
        `deaf` never even reads the request, so a large one stays in the client's buffer."""
        async def handle(reader, writer):
            if deaf:
                await asyncio.sleep(30)
                return
            request = json.loads(await reader.readline())
            self.requests.append(request)
            raw = answer(request) if answer else echo(request)
            if raw == "hang":
                await asyncio.sleep(30)
            elif raw is not None:
                writer.write(raw)
                await writer.drain()
            writer.close()

        server = await asyncio.start_unix_server(handle, str(path))
        self.addCleanup(server.close)
        return server

    async def live_client(self, answer=None, deaf=False, **extra):
        sock = self.base / "laya-0.35.1.sock"
        await self.serve(sock, answer, deaf)
        return await self.client(LAYA_SOCKET=str(sock), **extra)

    async def ask(self, client, **body):
        body.setdefault("message", "включи свет")
        return await client.post("/laya", json=body, headers=AUTH)

    async def test_proxy_answers_through_the_explicit_socket(self):
        client = await self.live_client()
        response = await self.ask(client)
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["echo"], "включи свет")

    async def test_a_live_older_server_wins_over_an_abandoned_newer_socket(self):
        client = await self.client(XDG_STATE_HOME=str(self.base / "state"))
        state = self.base / "state" / "task-cli" / "classify"
        state.mkdir(parents=True)
        abandoned_socket(state / "laya-0.36.0.sock")
        await self.serve(state / "laya-0.35.1.sock")
        response = await self.ask(client)
        self.assertEqual(response.status, 200)

    async def test_the_request_is_unauthorized_without_the_token(self):
        client = await self.live_client()
        response = await client.post("/laya", json={"message": "x"})
        self.assertEqual(response.status, 401)
        self.assertEqual(self.requests, [])

    async def test_a_russian_phrase_reaches_laya_on_the_multilingual_model(self):
        client = await self.live_client()
        response = await self.ask(client, questions={"domain": {"type": "choice"}})
        body = await response.json()
        self.assertEqual(response.status, 200)
        self.assertEqual(self.requests, [{"op": "predict", "message": "включи свет", "questions": {"domain": {"type": "choice"}},
                                          "model": "multilingual"}])
        self.assertEqual(body["meta"]["model"], "multilingual")

    async def test_model_and_max_len_are_forwarded_exactly(self):
        client = await self.live_client()
        response = await self.ask(client, message="turn on the light", model="multilingual", max_len=2048, language="en")
        self.assertEqual(response.status, 200)
        self.assertEqual(self.requests, [{"op": "predict", "message": "turn on the light", "questions": {},
                                          "model": "multilingual", "max_len": 2048}])

    async def test_a_language_hint_picks_the_model_and_is_not_forwarded(self):
        client = await self.live_client()
        await self.ask(client, message="turn on the light", language="ru-RU")
        await self.ask(client, message="turn on the light", language="en")
        self.assertEqual([r["model"] for r in self.requests], ["multilingual", "english"])
        self.assertTrue(all("language" not in r for r in self.requests))

    async def test_latin_text_without_a_hint_is_not_sent_to_the_english_checkpoint(self):
        client = await self.live_client()
        response = await self.ask(client, message="uključi svetlo u dnevnoj sobi")
        self.assertEqual(response.status, 200)
        self.assertEqual(self.requests, [{"op": "predict", "message": "uključi svetlo u dnevnoj sobi", "questions": {},
                                          "model": "multilingual"}])

    async def test_the_reply_meta_with_the_model_and_truncation_reaches_the_caller(self):
        meta = {"api_version": 2, "model": "multilingual", "checkpoint": "convaiinnovations/laya-multilingual", "max_len": 512,
                "truncated": True, "input_tokens": 900, "state_tokens": 10, "state_tokens_dropped": 4}
        client = await self.live_client(lambda request: reply(ok=True, result={"answers": {}}, meta=meta))
        body = await (await self.ask(client, max_len=512)).json()
        self.assertEqual(body["meta"], meta)

    async def test_an_invalid_field_is_400_json_and_nothing_reaches_laya(self):
        client = await self.live_client()
        for body, field in (({"model": "spanish"}, "model"), ({"max_len": 100}, "max_len"), ({"language": "ru_RU"}, "language"),
                            ({"questions": "x"}, "questions"), ({"message": ""}, "message")):
            with self.subTest(body=body):
                response = await self.ask(client, **body)
                data = await response.json()
                self.assertEqual((response.status, data["ok"], data["error"], data["field"]), (400, False, "invalid_request", field))
        self.assertEqual(self.requests, [])

    async def test_a_body_that_is_not_json_is_400(self):
        client = await self.live_client()
        response = await client.post("/laya", data=b"{nope", headers=AUTH)
        self.assertEqual((response.status, (await response.json())["field"]), (400, "body"))

    async def test_only_abandoned_sockets_is_503_socket_refused(self):
        state = self.base / "state" / "task-cli" / "classify"
        state.mkdir(parents=True)
        abandoned_socket(state / "laya-0.1.0.sock")
        client = await self.client(XDG_STATE_HOME=str(self.base / "state"))
        response = await self.ask(client)
        self.assertEqual((response.status, (await response.json())["error"]), (503, "laya_socket_refused"))

    async def test_no_socket_is_503_socket_missing(self):
        client = await self.client(XDG_STATE_HOME=str(self.base / "empty"))
        response = await self.ask(client)
        self.assertEqual((response.status, (await response.json())["error"]), (503, "laya_socket_missing"))

    async def test_a_missing_explicit_socket_is_503_socket_missing(self):
        client = await self.client(LAYA_SOCKET=str(self.base / "gone.sock"))
        response = await self.ask(client)
        self.assertEqual((response.status, (await response.json())["error"]), (503, "laya_socket_missing"))

    async def test_a_server_that_never_answers_is_504_timeout(self):
        client = await self.live_client(lambda request: "hang")
        with mock.patch.object(laya_route, "EXCHANGE_TIMEOUT_S", 0.2):
            response = await self.ask(client)
        self.assertEqual((response.status, (await response.json())["error"]), (504, "laya_timeout"))

    async def test_a_server_that_stops_reading_a_request_that_grows_when_serialized_is_still_504_timeout(self):
        client = await self.live_client(deaf=True)
        connect = asyncio.open_unix_connection

        async def small_send_buffer(*args, **kwargs):
            reader, writer = await connect(*args, **kwargs)
            writer.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)  # the kernel buffer must not swallow it
            return reader, writer

        # 62.5 KB on the wire (under the adapter's body limit) but ~250 KB once re-serialized
        body = '{"message": "turn on the light", "questions": {"x": [' + "1e15," * 12500 + '0]}}'
        with mock.patch.object(laya_route, "EXCHANGE_TIMEOUT_S", 0.3), mock.patch("asyncio.open_unix_connection", small_send_buffer):
            started = asyncio.get_running_loop().time()
            response = await asyncio.wait_for(client.post("/laya", data=body, headers={**AUTH, "Content-Type": "application/json"}), 5)
        self.assertEqual((response.status, (await response.json())["error"]), (504, "laya_timeout"))
        self.assertLess(asyncio.get_running_loop().time() - started, 3)

    async def test_a_server_that_closes_without_a_reply_is_502_empty_response(self):
        client = await self.live_client(lambda request: None)
        response = await self.ask(client)
        self.assertEqual((response.status, (await response.json())["error"]), (502, "laya_empty_response"))

    async def test_a_reply_that_is_not_json_is_502_bad_response(self):
        client = await self.live_client(lambda request: b"<html>\n")
        response = await self.ask(client)
        self.assertEqual((response.status, (await response.json())["error"]), (502, "laya_bad_response"))

    async def test_a_reply_line_longer_than_the_default_stream_limit_is_still_read(self):
        big = {"ok": True, "result": {"pad": "x" * 200_000}, "meta": {"model": "multilingual"}}
        client = await self.live_client(lambda request: json.dumps(big).encode() + b"\n")
        response = await self.ask(client)
        self.assertEqual(response.status, 200)

    async def test_an_older_server_that_ignores_the_model_is_502_not_an_english_answer(self):
        client = await self.live_client(lambda request: reply(ok=True, result={"answers": {}}))
        response = await self.ask(client)
        self.assertEqual((response.status, (await response.json())["error"]), (502, "laya_model_not_confirmed"))

    async def test_a_model_still_loading_is_503_with_retry_after(self):
        client = await self.live_client(lambda request: reply(
            ok=False, error="model_unavailable", model="multilingual", checkpoint="c", reason="loading", detail="LoadInProgress"))
        response = await self.ask(client)
        data = await response.json()
        self.assertEqual((response.status, data["error"], data["reason"]), (503, "laya_model_unavailable", "loading"))
        self.assertEqual(response.headers["Retry-After"], "1")

    async def test_a_model_that_failed_to_load_is_503_without_retry_after(self):
        client = await self.live_client(lambda request: reply(
            ok=False, error="model_unavailable", model="multilingual", reason="dependency_missing"))
        response = await self.ask(client)
        self.assertEqual((response.status, (await response.json())["reason"]), (503, "dependency_missing"))
        self.assertNotIn("Retry-After", response.headers)

    async def test_laya_refusing_a_request_is_400_with_its_field(self):
        client = await self.live_client(lambda request: reply(ok=False, error="invalid_request", field="questions", detail="d"))
        response = await self.ask(client)
        self.assertEqual((response.status, (await response.json())["field"]), (400, "questions"))

    async def test_a_legacy_exception_reply_is_502_upstream_error(self):
        client = await self.live_client(lambda request: reply(ok=False, error="ValueError"))
        response = await self.ask(client, message="turn on the light")
        data = await response.json()
        self.assertEqual((response.status, data["error"], data["upstream"]), (502, "laya_upstream_error", "ValueError"))


class QualityProxyTests(unittest.IsolatedAsyncioTestCase):
    """aisis#250: the same endpoint with the quality envelope: controls, fallback, journal records and the coverage KPI."""

    asyncSetUp = ProxyTests.asyncSetUp
    serve = ProxyTests.serve
    ask = ProxyTests.ask
    LABELS = ("light", "volume", "timer", "climate", "math", "weather_time", "smalltalk", "question")
    QUESTIONS = {"domain": {"type": "choice", "instructions": "Classify", "criteria": {label: "about " + label for label in LABELS}}}

    async def client(self, **extra):
        env = {"AISIS_ALLOWED_STATIONS": "st1", "AISIS_STATE_DB": str(self.base / "adapter.sqlite3"), "AISIS_MEMORY": "0",
               "AISIS_CHATGPT_COMMAND": "[]", "AISIS_ADAPTER_TOKEN": TOKEN, "AISIS_HA_ACTIONS": "off",
               "AISIS_DIALOGS_DB": str(self.base / "dialogs.sqlite3"), "XDG_STATE_HOME": str(self.base / "state"), **extra}
        self.adapter = Adapter(env)
        client = TestClient(TestServer(self.adapter.app()))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    async def live(self, answer=None, **extra):
        sock = self.base / "laya-0.35.1.sock"
        await self.serve(sock, answer or self.classifier())
        return await self.client(LAYA_SOCKET=str(sock), **extra)

    def classifier(self, main="light", checkpoint="convaiinnovations/laya-multilingual"):
        """A fake Laya that answers a control phrase with its expected label and every other message with `main`."""
        import laya_quality
        expected = {case.text: case.expected for case in laya_quality.VOICE_COMMAND_DOMAIN.controls}

        def answer(request):
            choice = expected.get(request["message"], main)
            return reply(ok=True, result={"answers": {"domain": {"choice": choice, "answer_confidence": 0.9}}},
                         meta={"api_version": 2, "model": request["model"], "checkpoint": checkpoint, "max_len": 1024, "truncated": False})
        return answer

    def journal(self):
        folder = self.base / "state" / "agent-tools" / "classifier-quality" / "journal"
        records = [json.loads(path.read_bytes()) for path in sorted(folder.glob("*.json"))] if folder.exists() else []
        return {record["phase"]: record for record in records}

    async def health(self, client):
        return (await (await client.get("/healthz")).json())["laya"]

    async def test_a_registered_request_is_answered_with_controls_and_journaled(self):
        client = await self.live()
        response = await self.ask(client, questions=self.QUESTIONS, language="ru")
        body = await response.json()
        self.assertEqual(response.status, 200)
        self.assertEqual(body["result"]["answers"]["domain"]["choice"], "light")
        self.assertEqual(body["meta"]["checkpoint"], "convaiinnovations/laya-multilingual")
        quality = body["quality"]
        self.assertEqual((quality["outcome"], quality["controls"], quality["tier"], quality["recorded"]), ("accepted", "passed", "laya", True))
        self.assertEqual(len(self.requests), 3)
        self.assertEqual({r["model"] for r in self.requests}, {"multilingual"})
        records = self.journal()
        self.assertEqual({k: v["request_id"] for k, v in records.items()}, {"request": quality["request_id"], "proposed": quality["request_id"]})
        self.assertEqual(records["request"]["answer"]["outcome"], "accepted")

    async def test_the_coverage_kpi_is_in_the_existing_health_output(self):
        client = await self.live()
        self.assertEqual(await self.health(client), {"quality": {}, "journal": {}, "incidents": {}})
        await self.ask(client, questions=self.QUESTIONS, language="ru")
        await self.ask(client, questions={"other": {"type": "choice", "criteria": {"a": "x"}}})
        health = await self.health(client)
        self.assertEqual(health["quality"]["voice-command-domain"]["controls_coverage"], 1.0)
        self.assertEqual(health["quality"]["unregistered"]["controls_coverage"], 0.0)
        self.assertEqual(health["journal"]["written"], 3)  # request + proposed for the registered one, the request alone for the excluded one
        self.assertNotIn("threshold", json.dumps(health))

    async def test_an_outage_is_the_distinct_status_with_an_explicit_unknown_and_a_critical_incident(self):
        client = await self.client(LAYA_SOCKET=str(self.base / "gone.sock"))
        response = await self.ask(client, questions=self.QUESTIONS, language="ru")
        body = await response.json()
        self.assertEqual((response.status, body["error"], body["quality"]["outcome"]), (503, "laya_socket_missing", "unknown"))
        self.assertNotIn("result", body)
        records = self.journal()
        self.assertEqual(records["request"]["answer"]["outcome"], "unknown")
        self.assertEqual(records["proposed"]["candidate_action"], "unknown")
        events = list((self.base / "state" / "agent-tools" / "classifier-quality" / "events").glob("*-c-*.json"))
        self.assertTrue(events)

    async def test_a_degraded_model_that_fails_a_control_is_unknown_never_a_default_answer(self):
        client = await self.live(lambda request: reply(ok=True, result={"answers": {"domain": {"choice": "light"}}},
                                                       meta={"model": request["model"], "checkpoint": "c", "truncated": False}))
        statuses = set()
        for _ in range(12):  # an always-"light" model fails the control of the other polarity, whichever pair a request draws
            response = await self.ask(client, questions=self.QUESTIONS, language="ru")
            statuses.add((response.status, (await response.json())["quality"]["outcome"]))
        self.assertEqual(statuses, {(502, "unknown")})

    async def test_an_invalid_request_is_400_and_leaves_no_record(self):
        client = await self.live()
        for body in ({"fallback": "everything"}, {"model": "spanish"}, {"message": ""}):
            response = await self.ask(client, questions=self.QUESTIONS, **body)
            self.assertEqual(response.status, 400)
        self.assertEqual(self.journal(), {})
        self.assertEqual(self.requests, [])

    async def test_fallback_none_is_accepted_and_keeps_the_chain_to_laya(self):
        client = await self.live()
        response = await self.ask(client, questions=self.QUESTIONS, language="ru", fallback="none")
        self.assertEqual(response.status, 200)

    async def test_a_caller_that_gives_up_still_gets_its_request_journaled(self):
        client = await self.live()
        gate = asyncio.Event()
        real_exchange = self.adapter._laya_exchange

        async def delayed(line):
            await gate.wait()
            return await real_exchange(line)

        self.adapter._laya_exchange = delayed
        task = asyncio.ensure_future(self.ask(client, questions=self.QUESTIONS, language="ru"))
        await asyncio.sleep(0.1)
        task.cancel()  # the quasar shadow's own 5 s timeout
        with self.assertRaises(asyncio.CancelledError):
            await task
        gate.set()
        await asyncio.wait_for(asyncio.gather(*self.adapter.laya_pending), 5)
        self.assertEqual(self.journal()["request"]["answer"]["outcome"], "accepted")

    async def test_the_cloud_tiers_are_off_until_the_owner_switches_them_on(self):
        client = await self.live()
        self.assertEqual(self.adapter._laya_jev_missing(), "disabled:aisis_laya_fallback")
        self.assertEqual(self.adapter._laya_haiku_missing(), "disabled:aisis_laya_fallback")
        await self.ask(client, questions=self.QUESTIONS, language="ru")
        events = self.base / "state" / "agent-tools" / "classifier-quality" / "events"
        self.assertFalse(events.exists() and list(events.glob("*.json")))  # switched off is not "missing"

    async def test_the_later_tiers_say_what_they_lack_by_name(self):
        client = await self.live(AISIS_LAYA_FALLBACK="jev,haiku")
        self.assertEqual(self.adapter._laya_jev_missing(), "missing_env:vercel_jev_key")
        self.assertEqual(self.adapter._laya_haiku_missing(), "missing_config:claude_primary_module")
        await self.ask(client, questions=self.QUESTIONS, language="ru")
        reasons = {json.loads(p.read_bytes())["reason_code"] for p in (self.base / "state" / "agent-tools" / "classifier-quality" / "events").glob("*.json")}
        self.assertEqual(reasons, {"missing_env:vercel_jev_key", "missing_config:claude_primary_module"})

    async def test_jev_answers_when_laya_cannot_and_is_recorded_as_jev(self):
        import jev
        client = await self.live(lambda request: None, VERCEL_JEV_KEY="test-only-key", AISIS_LAYA_FALLBACK="jev")

        async def post(url, headers, body):
            import laya_quality
            expected = {case.text: case.expected for case in laya_quality.VOICE_COMMAND_DOMAIN.controls}
            return 200, {"answers": [{"name": "domain", "choice": expected.get(body["input"], "volume"), "confidence": 0.9}]}

        self.adapter.laya_jev = jev.JevClient(post, "test-only-key", timeout=1.0)
        response = await self.ask(client, questions=self.QUESTIONS, language="ru")
        body = await response.json()
        self.assertEqual((response.status, body["quality"]["tier"], body["quality"]["provider"], body["quality"]["fallback"]),
                         (200, "jev", "jev", True))
        self.assertEqual(body["result"]["answers"]["domain"]["choice"], "volume")
        self.assertEqual(self.journal()["request"]["model"]["provider"], "jev")

    async def test_the_source_identity_is_computed_at_startup_not_on_the_first_request(self):
        await self.client()
        self.assertEqual(sorted(self.adapter.laya_source), ["code_hash", "commit", "release"])

    async def test_shutdown_waits_a_bounded_time_for_requests_in_flight_then_cancels_them(self):
        import laya_quality
        await self.client()
        stuck = asyncio.ensure_future(asyncio.sleep(3600))
        self.adapter.laya_pending.add(stuck)
        with mock.patch.object(laya_quality, "SHUTDOWN_WAIT_S", 0.05):
            await asyncio.wait_for(self.adapter._close_background(), 5)
        self.assertTrue(stuck.cancelled())

    async def test_shutdown_lets_a_request_that_finishes_in_time_complete(self):
        await self.client()
        quick = asyncio.ensure_future(asyncio.sleep(0.01, result="done"))
        self.adapter.laya_pending.add(quick)
        await self.adapter._close_background()
        self.assertEqual(quick.result(), "done")


def echo(request):
    """The default fake Laya answer: a routed request gets the `meta` a v2 server adds."""
    reply_fields = {"ok": True, "echo": request["message"]}
    if "model" in request:
        reply_fields["meta"] = {"api_version": 2, "model": request["model"], "max_len": request.get("max_len", 512)}
    return reply(**reply_fields)


if __name__ == "__main__":
    unittest.main()
