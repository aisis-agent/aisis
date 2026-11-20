"""POST /asr/hypotheses: the STT gateway hands the adapter what a second recogniser thought of the audio behind a phrase.

Same authorization as /intent/decide; always 204 (kept) or 200 {stored: false, reason} (not kept): the gateway never reads the answer and
a failure here must never reach a voice turn. The evidence lives in `adapter.hypotheses` (hypothesis_store.py) for 15 s.
"""
import json
import tempfile
import time
import unittest

from aiohttp.test_utils import TestClient, TestServer

import hypotheses_fakes as fakes
import hypothesis_store
from adapter import Adapter

NBEST = {"engine": "gigaam-v3-ctc", "ms": 88, "hypotheses": [{"text": "выключи свет", "percent": 71}, {"text": "включи свет", "percent": 29}]}
BODY = {"station": None, "text": "Включи свет.", "source": "whisper", "nbest": NBEST, "ms": 91}
TOKEN = "test-only-token"
AUTH = {"Authorization": "Bearer " + TOKEN}


class EndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.env = {"AISIS_ALLOWED_STATIONS": "st1", "AISIS_STATE_DB": self.dir.name + "/adapter.sqlite3", "AISIS_MEMORY": "0",
                    "AISIS_DIALOGS_DB": self.dir.name + "/dialogs.sqlite3", "AISIS_CHATGPT_COMMAND": "[]", "AISIS_ADAPTER_TOKEN": TOKEN,
                    "AISIS_HA_ACTIONS": "off"}
        self.adapter = Adapter(self.env)
        self.adapter.hypotheses = hypothesis_store.HypothesisStore(same_text=fakes.same_text)   # the real one needs the core installed
        self.client = TestClient(TestServer(self.adapter.app()))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def post(self, body, headers=AUTH, raw=None):
        return await self.client.post("/asr/hypotheses", data=raw if raw is not None else json.dumps(body), headers={**headers, "content-type": "application/json"})

    async def test_valid_evidence_is_kept_and_answered_204(self):
        response = await self.post(BODY)
        self.assertEqual(response.status, 204)
        got = self.adapter.hypotheses.get(None, "включи свет", time.monotonic())
        self.assertEqual((got["text"], got["source"], got["nbest"], got["ms"], got["station"]), ("Включи свет.", "whisper", NBEST, 91, None))

    async def test_station_and_spotter_source_are_kept(self):
        await self.post({**BODY, "station": "st1", "source": "spotter"})
        got = self.adapter.hypotheses.get("st1", "включи свет", time.monotonic())
        self.assertEqual((got["station"], got["source"]), ("st1", "spotter"))

    async def test_wrong_token_is_unauthorized_like_the_other_routes(self):
        self.assertEqual((await self.post(BODY, headers={"Authorization": "Bearer nope"})).status, 401)
        self.assertEqual((await self.post(BODY, headers={})).status, 401)
        self.assertEqual(self.adapter.hypotheses.stats()["entries"], 0)

    async def test_bad_bodies_are_answered_200_and_not_kept(self):
        bad = {
            "not json": {"raw": b"{nope"},
            "not an object": {"raw": b"[1,2]"},
            "no text": {"body": {**BODY, "text": ""}},
            "text not a string": {"body": {**BODY, "text": 5}},
            "text too long": {"body": {**BODY, "text": "а" * 401}},
            "unknown source": {"body": {**BODY, "source": "mic"}},
            "no nbest": {"body": {k: v for k, v in BODY.items() if k != "nbest"}},
            "nbest not an object": {"body": {**BODY, "nbest": "x"}},
            "hypotheses not a list": {"body": {**BODY, "nbest": {"hypotheses": "x"}}},
            "too many hypotheses": {"body": {**BODY, "nbest": {"hypotheses": [{"text": "а"}] * 33}}},
            "station not a string": {"body": {**BODY, "station": 7}},
            "station too long": {"body": {**BODY, "station": "s" * 65}},
            "ms not a number": {"body": {**BODY, "ms": "fast"}},
            "ms is a bool": {"body": {**BODY, "ms": True}},
        }
        for name, case in bad.items():
            response = await self.post(case.get("body"), raw=case.get("raw"))
            self.assertEqual(response.status, 200, name)
            self.assertEqual((await response.json())["stored"], False, name)
        self.assertEqual(self.adapter.hypotheses.stats()["entries"], 0)

    async def test_a_large_body_is_refused_without_being_kept(self):
        huge = {**BODY, "nbest": {"engine": "x", "hypotheses": [], "pad": "я" * 100_000}}
        response = await self.post(huge)
        self.assertEqual((response.status, (await response.json())["reason"]), (200, "too_large"))
        self.assertEqual(self.adapter.hypotheses.stats()["entries"], 0)

    async def test_a_store_without_the_core_answers_200_and_keeps_nothing(self):
        def missing():
            raise ImportError("no stt_cli")
        self.adapter.hypotheses = hypothesis_store.HypothesisStore(load_same_text=missing)
        response = await self.post(BODY)
        self.assertEqual((response.status, await response.json()), (200, {"stored": False, "reason": "unavailable"}))

    async def test_healthz_reports_the_store(self):
        await self.post(BODY)
        body = await (await self.client.get("/healthz")).json()
        self.assertEqual({k: body["hypotheses"][k] for k in ("available", "entries", "puts")}, {"available": True, "entries": 1, "puts": 1})

    async def test_the_default_store_does_not_need_the_core_to_construct(self):
        adapter = Adapter(self.env)
        self.assertIsInstance(adapter.hypotheses, hypothesis_store.HypothesisStore)


if __name__ == "__main__":
    unittest.main()
