"""Exercise the actual authenticated Adapter handlers without runtime startup."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import announcement_cache
from adapter import Adapter
from test_announcement_cache import audio_fixture, populate


class AdapterAnnouncementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name)
        self.folder_patch = patch.object(announcement_cache, "DEFAULT_FOLDER", self.path)
        self.folder_patch.start()
        self.addCleanup(self.folder_patch.stop)
        self.adapter = Adapter.__new__(Adapter)
        self.adapter.env = {"AISIS_ADAPTER_TOKEN": "test-only-token"}
        self.adapter.tts_limit = asyncio.Semaphore(1)
        self.adapter.fish_key = Mock(return_value="test-only-key")
        self.adapter._fish_audio = AsyncMock(side_effect=AssertionError("unexpected synthesis"))
        self.adapter._alice_audio = AsyncMock(side_effect=AssertionError("unexpected synthesis"))
        self.settings_patch = patch("adapter.tts_settings.load", return_value={"tts_voice": "bracho"})
        self.settings = self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)
        self.adapter.start = AsyncMock()
        self.adapter.close = AsyncMock()
        app = self.adapter.app()
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.headers = {"Authorization": "Bearer test-only-token"}

    async def test_valid_marker_returns_cached_opus_without_any_synthesis(self):
        key = "a" * 64
        audio = audio_fixture("cached", duration=0.5)
        (self.path / (key + ".ogg")).write_bytes(audio)
        (self.path / (key + ".json")).write_text(json.dumps({
            "voice": announcement_cache.VOICE_ID, "duration": 0.5,
            "sha256": hashlib.sha256(audio).hexdigest()}))
        response = await self.client.post("/v1/audio/speech", headers=self.headers,
            json={"input": announcement_cache.PREFIX + key + "]"})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "audio/ogg")
        self.assertEqual(await response.read(), audio)
        self.adapter._fish_audio.assert_not_awaited()
        self.adapter._alice_audio.assert_not_awaited()

    async def test_missing_and_malformed_markers_never_fall_back(self):
        for marker in ["[ha-remote:../../secret]", "[ha-remote:" + "b" * 64 + "]",
                " [ha-remote:" + "b" * 64 + "]", "\n[ha-remote:bad]", "\ufeff[ha-remote:bad]",
                "\u00a0[ha-remote:bad]", "\u2028[ha-remote:bad]", "\u3000[ha-remote:bad]",
                "\u200b[ha-remote:bad]", "\u2060[ha-remote:bad]", "[HA-REMOTE:bad]",
                "[ha-remote:" + "b" * 64 + "]\n"]:
            with self.subTest(marker=marker):
                response = await self.client.post("/v1/audio/speech", headers=self.headers,
                    json={"input": marker})
                self.assertEqual(response.status, 503)
                await response.read()
        self.adapter._fish_audio.assert_not_awaited()
        self.adapter._alice_audio.assert_not_awaited()

    async def test_speech_and_status_keep_authorization(self):
        speech = await self.client.post("/v1/audio/speech", json={"input": "[ha-remote:bad]"})
        status = await self.client.get("/v1/remote-announcements/status")
        self.assertEqual(speech.status, 401)
        self.assertEqual(status.status, 401)
        await speech.read()
        await status.read()

    async def test_malformed_manifest_is_not_ready_instead_of_http_500(self):
        (self.path / "manifest.json").write_text(json.dumps({
            "voice_id": announcement_cache.VOICE_ID, "clips": sorted(announcement_cache.TARGETS)}))
        response = await self.client.get("/v1/remote-announcements/status", headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertFalse((await response.json())["ready"])

    async def test_corrupt_generation_history_is_503_without_synthesis(self):
        generation = self.path / "generation-fixture"
        generation.mkdir()
        populate(announcement_cache, generation)
        (self.path / "current").symlink_to(generation.name)
        (generation / "previous").write_bytes(b"\xff")
        response = await self.client.post("/v1/audio/speech", headers=self.headers,
            json={"input": announcement_cache.PREFIX + "f" * 64 + "]"})
        self.assertEqual(response.status, 503)
        await response.read()
        self.adapter._fish_audio.assert_not_awaited()
        self.adapter._alice_audio.assert_not_awaited()

    async def test_normal_speech_still_uses_selected_live_provider(self):
        self.adapter._fish_audio.side_effect = None
        self.adapter._fish_audio.return_value = b"synthesized"
        self.adapter._encode_speech = AsyncMock(return_value=b"OggS encoded fixture")
        response = await self.client.post("/v1/audio/speech", headers=self.headers,
            json={"input": "ordinary speech"})
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), b"OggS encoded fixture")
        self.adapter._fish_audio.assert_awaited_once_with("ordinary speech")
        self.adapter._alice_audio.assert_not_awaited()

    async def test_explicit_alice_pin_still_prevents_provider_fallback(self):
        self.adapter._alice_audio.side_effect = web.HTTPBadGateway()
        with patch("adapter.tts_settings.alice_engine", return_value="fixture"):
            response = await self.client.post("/v1/audio/speech",
                headers={**self.headers, "X-AISIS-Voice": "alice"}, json={"input": "ordinary speech"})
        self.assertEqual(response.status, 502)
        await response.read()
        self.adapter._fish_audio.assert_not_awaited()

    async def test_unpinned_alice_retains_existing_bracho_fallback(self):
        self.settings.return_value = {"tts_voice": "alice"}
        self.adapter._alice_audio.side_effect = web.HTTPBadGateway()
        self.adapter._fish_audio.side_effect = None
        self.adapter._fish_audio.return_value = b"synthesized"
        self.adapter._encode_speech = AsyncMock(return_value=b"OggS encoded fixture")
        with patch("adapter.tts_settings.alice_engine", return_value="fixture"):
            response = await self.client.post("/v1/audio/speech", headers=self.headers,
                json={"input": "ordinary speech"})
        self.assertEqual(response.status, 200)
        await response.read()
        self.adapter._fish_audio.assert_awaited_once_with("ordinary speech")


class AdapterConfigurationTests(unittest.TestCase):
    def test_timeout_defaults_preserved_in_actual_constructor(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"AISIS_CONFIG_DIR": folder}):
            env = {"AISIS_ALLOWED_STATIONS": "fixture", "AISIS_MEMORY": "0",
                "AISIS_STATE_DB": folder + "/state.sqlite3"}
            adapter = Adapter(env)
            self.addCleanup(adapter.store.db.close)
            self.assertIsNone(adapter.chatgpt_timeout)
            adapter_pm = Adapter({**env, "AISIS_PM_URL": "http://example.invalid"})
            self.addCleanup(adapter_pm.store.db.close)
            self.assertEqual(adapter_pm.chatgpt_timeout, 250.0)

    def test_invalid_timeout_budgets_still_fail_before_opening_state(self):
        for value in ["nan", "inf", "-1", "bad", "250"]:
            with self.subTest(value=value), patch("adapter.Store") as store:
                with self.assertRaises(ValueError):
                    Adapter({"AISIS_ALLOWED_STATIONS": "fixture", "AISIS_PM_URL": "http://example.invalid",
                        "AISIS_PM_REPLY_TIMEOUT": value})
                store.assert_not_called()


if __name__ == "__main__":
    unittest.main()
