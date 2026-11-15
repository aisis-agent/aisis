"""voice_skills: weather questions and nameless «включи музыку» are answered before the brain."""
import asyncio
import os
import stat
import tempfile
import unittest
from unittest import mock

import music_airplay
import voice_skills
from adapter import Adapter


class WeatherParseTests(unittest.TestCase):
    def test_questions_map_to_modes(self):
        cases = {
            "Какая погода?": "now",
            "Алиса, какая погода": "now",
            "Погода завтра": "tomorrow",
            "Какая погода будет завтра?": "tomorrow",
            "Какая погода на неделю": "week",
            "Что на улице?": "now",
            "Нужен ли зонт": "umbrella",
            "Будет ли завтра дождь": "tomorrow",
            "Как одеться": "dress",
            "Сколько градусов на улице": "now",
            "Какая температура завтра": "tomorrow",
            "Холодно ли на улице": "now",
            "Какая погода в субботу": "week",
            "Прогноз на завтра": "tomorrow",
            "Прогноз погоды": "now",
            "Расскажи прогноз": "now",
        }
        for day in ("понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"):
            cases[f"Какая погода в {day}"] = "week"
        for text, mode in cases.items():
            with self.subTest(text=text):
                self.assertEqual(voice_skills.parse_weather_ask(text), mode)

    def test_commands_other_places_and_noise_are_not_weather(self):
        for text in ("Включи погоду на телевизоре", "Какая погода в Париже", "Включи свет", "Какая температура в комнате",
                     "Включи музыку", "Расскажи прогноз курса доллара", "Почему идёт дождь", "Какая погода в выходные в Париже",
                     "Какая погода в среду в Париже", "Какая средняя температура в комнате", "Где купить зонт",
                     "Как надеть галстук", "Почему погода меняется", "Как починить зонт", "Как одеться на свадьбу",
                     "Как погода влияет на здоровье", "Расскажи о погоде на Марсе", "Расскажи про дождь в японской поэзии и про то как он влияет на настроение людей", ""):
            with self.subTest(text=text):
                self.assertIsNone(voice_skills.parse_weather_ask(text))


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
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def fake_weather(self, body: str) -> str:
        path = os.path.join(self.tmp.name, "weather")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, stat.S_IRWXU)
        return path

    def skills(self, weather_body: str | None) -> voice_skills.Skills:
        env = {"PATH": "/usr/bin:/bin"}
        if weather_body is not None:
            env["AISIS_WEATHER_COMMAND"] = '["%s"]' % self.fake_weather(weather_body)
        return voice_skills.Skills(env)

    def test_weather_runs_the_cli_with_the_mode_and_the_ha_cli_proxy(self):
        skills = self.skills('echo "$@ $WEATHER_HA_URL"')
        reply = asyncio.run(skills.answer("Какая погода завтра"))
        self.assertEqual(reply, "say --tomorrow http://127.0.0.1:8795")

    def test_weather_failure_is_an_honest_sentence(self):
        self.assertEqual(asyncio.run(self.skills("exit 3").answer("какая погода")), voice_skills.WEATHER_FAILED)
        self.assertEqual(asyncio.run(self.skills("true").answer("какая погода")), voice_skills.WEATHER_FAILED)

    def test_weather_timeout_kills_the_cli(self):
        skills = self.skills("sleep 30")
        with mock.patch.object(voice_skills, "WEATHER_TIMEOUT_S", 0.2):
            self.assertEqual(asyncio.run(skills.answer("какая погода")), voice_skills.WEATHER_FAILED)

    def test_missing_cli_is_an_honest_sentence(self):
        skills = voice_skills.Skills({"PATH": "/nonexistent"})
        with mock.patch.object(voice_skills.shutil, "which", return_value=None):
            self.assertEqual(asyncio.run(skills.answer("какая погода")), voice_skills.WEATHER_FAILED)

    def test_cli_installed_after_start_is_found(self):
        skills = voice_skills.Skills({"PATH": "/nonexistent"})
        with mock.patch.object(voice_skills.shutil, "which", return_value=self.fake_weather('echo "ok $@"')):
            self.assertEqual(asyncio.run(skills.answer("какая погода")), "ok say --now")

    def test_unlaunchable_command_is_an_honest_sentence(self):
        missing = voice_skills.Skills({"AISIS_WEATHER_COMMAND": '["/nonexistent/weather"]'})
        self.assertEqual(asyncio.run(missing.answer("какая погода")), voice_skills.WEATHER_FAILED)
        plain = os.path.join(self.tmp.name, "plain")
        open(plain, "w").close()  # exists, not executable
        denied = voice_skills.Skills({"AISIS_WEATHER_COMMAND": '["%s"]' % plain})
        self.assertEqual(asyncio.run(denied.answer("какая погода")), voice_skills.WEATHER_FAILED)

    def test_bad_command_setting_is_rejected(self):
        with self.assertRaises(ValueError):
            voice_skills.Skills({"AISIS_WEATHER_COMMAND": '"weather"'})

    def test_music_replies_follow_the_engine_result(self):
        skills = self.skills(None)
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
        self.assertIsNone(asyncio.run(self.skills(None).answer("Расскажи анекдот")))


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
