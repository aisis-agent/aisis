"""voice_skills: nameless «включи музыку» is answered before the brain (weather: test_weather_skills.py)."""
import asyncio
import tempfile
import unittest
from unittest import mock

import music_airplay
import voice_skills
from adapter import Adapter


class MusicParseTests(unittest.TestCase):
    def test_nameless_requests(self):
        for text in ("Включи музыку.", "Алиса, включи музыку", "Поставь что-нибудь", "Ну давай музыку какую-нибудь",
                     "Включи любимую музыку пожалуйста", "Запусти музыку на колонке", "включи музыку!"):
            with self.subTest(text=text):
                self.assertTrue(voice_skills.is_generic_music_request(text))

    def test_named_negated_or_device_requests_are_not_nameless(self):
        for text in ("Включи Sex Pistols", "Включи музыку Эминема", "Не включай музыку", "Включи свет",
                     "Включи телевизор", "Выключи музыку", "Какая музыка играет", "Включи", ""):
            with self.subTest(text=text):
                self.assertFalse(voice_skills.is_generic_music_request(text))


class SkillsRunnerTests(unittest.TestCase):
    def skills(self) -> voice_skills.Skills:
        return voice_skills.Skills({"PATH": "/usr/bin:/bin"})

    def test_music_replies_follow_the_engine_result(self):
        skills = self.skills()
        results = {
            "playing": ({"ok": True, "action": "already"}, "Музыка уже играет."),
            "play": ({"ok": True, "action": "play", "matched": {"name": "Hero", "artist": "Skillet"}}, "Включаю Hero, Skillet."),
            "empty": ({"ok": False, "err": "empty_library"}, voice_skills.MUSIC_FAILED),
            "down": ({"ok": False, "err": "MA недоступен"}, voice_skills.MUSIC_UNAVAILABLE),
        }
        for name, (result, spoken) in results.items():
            with self.subTest(name=name), mock.patch.object(music_airplay, "music_any", return_value=result):
                self.assertEqual(asyncio.run(skills.answer("Включи музыку.")), spoken)

    def test_everything_else_is_left_to_the_brain(self):
        self.assertIsNone(asyncio.run(self.skills().answer("Расскажи анекдот")))



class MusicAnyTests(unittest.TestCase):
    """music_any leaves what plays alone and otherwise queues random library tracks."""

    PHONE = {"connected": True, "playing": True, "peer": "192.0.2.3"}
    IDLE = {"connected": False, "playing": False, "peer": "192.0.2.3"}

    def run_any(self, *, receiver=None, ma_state="idle"):
        calls = []

        def ma_call(command, args=None, timeout=20):
            calls.append(command)
            if command == "music/tracks/library_items":
                return [{"uri": "library://track/1", "name": "Hero", "available": True,
                         "artists": [{"name": "Skillet"}]}, {"uri": "library://track/2", "name": "Two"}]
            return None

        with mock.patch.object(music_airplay, "_player_id", return_value="speaker"), \
                mock.patch.object(music_airplay, "_receiver_session", return_value=receiver), \
                mock.patch.object(music_airplay, "_ma_player_state", return_value=ma_state), \
                mock.patch.object(music_airplay, "ma_call", side_effect=ma_call), \
                mock.patch.object(music_airplay, "_play_media", side_effect=lambda pid, media: calls.append(
                    ("play", pid, media))), \
                mock.patch.object(music_airplay, "_save_last"):
            return music_airplay.music_any(), calls

    def test_playing_is_left_alone(self):
        result, calls = self.run_any(ma_state="playing")
        self.assertEqual(result, {"ok": True, "action": "already"})
        self.assertEqual(calls, [])

    def test_a_phone_streaming_over_airplay_is_not_replaced(self):
        result, calls = self.run_any(receiver=self.PHONE)
        self.assertEqual(result, {"ok": True, "action": "already"})
        self.assertEqual(calls, [])

    def test_idle_queues_library_tracks_and_names_the_first(self):
        for receiver, state in ((None, "idle"), (self.IDLE, "paused"), (self.IDLE, None)):
            with self.subTest(receiver=receiver, state=state):
                result, calls = self.run_any(receiver=receiver, ma_state=state)
                self.assertEqual(result["action"], "play")
                self.assertEqual(result["matched"]["name"], "Hero")
                self.assertEqual(result["matched"]["artist"], "Skillet")
                self.assertEqual(calls, ["music/tracks/library_items",
                                         ("play", "speaker", ["library://track/1", "library://track/2"])])

    def test_empty_library_is_an_error(self):
        with mock.patch.object(music_airplay, "_player_id", return_value="speaker"), \
                mock.patch.object(music_airplay, "_receiver_session", return_value=None), \
                mock.patch.object(music_airplay, "_ma_player_state", return_value="idle"), \
                mock.patch.object(music_airplay, "ma_call", return_value=[]), \
                mock.patch.object(music_airplay, "_save_last"):
            self.assertEqual(music_airplay.music_any(), {"ok": False, "err": "empty_library"})


class AdapterRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.adapter = Adapter({"AISIS_STATE_DB": self.tmp.name + "/state.db", "AISIS_ALLOWED_STATIONS": "a",
                                "AISIS_UTTERANCE_TRACE": "off",
                                "AISIS_MEMORY": "0", "AISIS_CHATGPT_COMMAND": '["brain"]',
                                "AISIS_WEATHER_COMMAND": '["true"]'})
        self.adapter.turn = mock.AsyncMock(return_value="brain reply")

    def reply(self, text: str) -> str:
        sent = []

        class Socket:
            closed = False

            async def send_json(self, payload):
                sent.append(payload)

        asyncio.run(self.adapter._process_turn(Socket(), {"metadata": {"stationId": "a"}, "text": text}))
        return sent[0]["data"]["text"]

    def test_skill_turns_never_reach_the_brain(self):
        with mock.patch.object(music_airplay, "music_any", return_value={"ok": True, "action": "already"}):
            self.assertEqual(self.reply("Включи музыку."), "Музыка уже играет.")
        self.assertEqual(self.reply("Какая погода завтра?"), voice_skills.WEATHER_FAILED)  # `true` prints nothing
        self.adapter.turn.assert_not_called()

    def test_other_turns_reach_the_brain(self):
        self.assertEqual(self.reply("Кто написал Войну и мир"), "brain reply")
        self.adapter.turn.assert_called_once()


if __name__ == "__main__":
    unittest.main()
