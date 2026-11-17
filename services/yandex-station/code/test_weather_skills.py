"""voice_skills weather: parsing, the qualifier hint and the weather-cli runner (stdlib only, so CI runs it)."""
import asyncio
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import voice_skills


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
            # What the quasar fast-intent layer used to answer from the HA entity itself.
            "Алиса, какая сейчас погода?": "now",
            "Сколько градусов": "now",
            "Сколько сейчас градусов": "now",
            "Какая температура": "now",
            "Скажи погоду": "now",
            "Какая погода на улице": "now",
            "Что за окном": "now",
            # Questions the quasar layer used to answer from the entity and now hands over (review of the overlay).
            "Расскажи о погоде": "now",
            "Какая температура на балконе": "now",
            "Какая погода и сколько градусов": "now",
            "Погода и температура на улице": "now",
            "Сколько градусов за окном": "now",
            "Какой ветер на улице": "now",
            "На улице дождь": "umbrella",
            "Там дождь?": "umbrella",
            "Будет ли дождь": "umbrella",
            "Какая погода на этой неделе": "week",
            "Какая погода завтра в Белграде": "tomorrow",
            "Какая температура в Белграде": "now",
            "Погода в Белграде": "now",
            "Какая погода в эти выходные": "week",
            "Какая погода в это воскресенье": "week",
            "Что на дворе": "now",
            "Какая погода в ближайшие дни": "week",
            "Будет ли гроза": "umbrella",
            "Идёт ли снег": "umbrella",
        }
        for day in ("понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"):
            cases[f"Какая погода в {day}"] = "week"
        for text, mode in cases.items():
            with self.subTest(text=text):
                self.assertEqual(voice_skills.parse_weather_ask(text), mode)

    def test_commands_other_places_and_noise_are_not_weather(self):
        for text in ("Какая температура в Лиссабоне", "Сколько градусов в Лионе", "Сколько градусов в Днепре",
                     "Сколько градусов в Грозном", "Какая температура в теплице", "Какая температура в лифте",
                     "Какая температура в Ливнах", "Алиса погоди", "Какой градусник", "Где ветеринар", "Какая температура в Минске",
                     "Что с ветераном", "Какой завтрак", "Погоди-ка",
                     # No hour-of-day mode in weather-cli and one day per answer: not a parse (a hint is spoken instead).
                     "Какая погода будет вечером", "Какая погода сегодня вечером", "Какая погода ночью",
                     "Какая температура утром", "Какая погода завтра днём", "Какая погода сегодня и завтра",
                     "Включи погоду на телевизоре", "Какая погода в Париже", "Включи свет", "Какая температура в комнате", "Сколько градусов в духовке", "Какая температура у процессора",
                     "Что за шум", "Что за зонт", "Что за одежда",
                     "Включи музыку", "Расскажи прогноз курса доллара", "Почему идёт дождь", "Какая погода в выходные в Париже",
                     "Какая погода в среду в Париже", "Какая средняя температура в комнате", "Где купить зонт",
                     "Как надеть галстук", "Почему погода меняется", "Как починить зонт", "Как одеться на свадьбу",
                     "Как погода влияет на здоровье", "Расскажи о погоде на Марсе", "Расскажи про дождь в японской поэзии и про то как он влияет на настроение людей", ""):
            with self.subTest(text=text):
                self.assertIsNone(voice_skills.parse_weather_ask(text))


class OverlayHandoverTests(unittest.TestCase):
    """The quasar overlay diverts these questions to the adapter: every one must have an answer here."""

    TABLE = Path(__file__).resolve().parent.parent / "overlays" / "weather-via-cli" / "diverted_phrases.json"

    def test_every_handoff_entry_gets_an_answer_from_the_adapter_with_any_lead_in(self):
        table = json.loads(self.TABLE.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(table["phrases"]), 100)
        for text in table["phrases"]:
            for lead in ("", *table["lead_ins"]):
                spoken = f"{lead} {text}".strip()
                with self.subTest(text=spoken):
                    self.assertTrue(voice_skills.parse_weather_ask(spoken) is not None
                                    or voice_skills.is_unsupported_weather_qualifier(spoken))

    def test_the_lead_ins_are_the_adapters_own(self):
        table = json.loads(self.TABLE.read_text(encoding="utf-8"))
        self.assertEqual(table["lead_ins"], sorted(voice_skills._WEATHER_LEAD_IN))

    def test_questions_reviewers_found_are_in_the_table_or_keep_the_entity_answer(self):
        table = json.loads(self.TABLE.read_text(encoding="utf-8"))["phrases"]
        for text in ("расскажи о погоде", "какая температура на балконе", "какая погода и сколько градусов",
                     "какая сейчас температура воздуха на улице", "сколько градусов цельсия на улице"):
            normalized = " ".join(t for t in voice_skills._tokens(text) if t not in voice_skills._WEATHER_LEAD_IN)
            with self.subTest(text=text):
                self.assertIn(normalized, table)
                self.assertIsNotNone(voice_skills.parse_weather_ask(text))

    def test_the_forms_the_quasar_layer_diverted_but_the_parser_missed(self):
        for text, mode in (("Какая уличная температура", "now"), ("Какова погода", "now"), ("Покажи погоду", "now"),
                           ("Какова погода завтра", "tomorrow"), ("Проверь погоду", "now")):
            with self.subTest(text=text):
                self.assertEqual(voice_skills.parse_weather_ask(text), mode)


class PlaceNamesThatLookLikeWeatherTests(unittest.TestCase):
    def test_a_town_that_starts_like_a_weather_word_is_not_the_home_forecast(self):
        for text in ("Какая температура в Снегирях", "Какой снег в Снегирях", "Сколько градусов в Снегирёве",
                     "Будет ли дождь в Дождевке", "Какая погода во Львове", "Какая температура в Москве"):
            with self.subTest(text=text):
                self.assertIsNone(voice_skills.parse_weather_ask(text))

    def test_a_window_sill_question_is_not_weather(self):
        for text in ("Что на окне", "Что в окне", "Какая температура на окне"):
            with self.subTest(text=text):
                self.assertIsNone(voice_skills.parse_weather_ask(text))

    def test_thanks_after_the_question_do_not_change_the_answer(self):
        for text, mode in (("Какая погода? Спасибо.", "now"), ("Спасибо, будет ли завтра дождь", "tomorrow")):
            with self.subTest(text=text):
                self.assertEqual(voice_skills.parse_weather_ask(text), mode)

    def test_a_demonstrative_before_a_place_is_not_a_day(self):
        for text in ("Какая погода в этой Дождевке", "Какая температура в этом Снегирёве"):
            with self.subTest(text=text):
                self.assertIsNone(voice_skills.parse_weather_ask(text))

    def test_a_filler_word_inside_the_phrase_does_not_change_the_answer(self):
        for text in ("Какая погода во, ну, дворе?", "Какая температура в, ну, Белграде"):
            with self.subTest(text=text):
                self.assertEqual(voice_skills.parse_weather_ask(text), "now")

    def test_the_home_city_a_day_and_a_demonstrative_after_in_are_still_weather(self):
        for text, mode in (("Какая температура в Белграде", "now"), ("Какая погода в субботу", "week"),
                           ("Какая погода в эти выходные", "week"), ("Будет ли снег", "umbrella"),
                           ("Какой снег на улице", "umbrella"), ("Будет ли дождь в пятницу", "week"),
                           ("Какая температура во дворе", "now"), ("Как одеться в дождь", "dress"),
                           ("Нужен ли зонт в дождь", "umbrella"), ("Как одеться в снегу", "dress"),
                           ("Как одеться в холодную погоду", "dress"), ("Нужен ли зонт в дождливую погоду", "umbrella"),
                           ("Сколько градусов тепла на улице", "now"), ("Как одеться в тёплую погоду", "dress"),
                           ("Как одеться в холодную дождливую погоду", "dress"),
                           ("Нужен ли зонт в тёплую дождливую погоду", "umbrella"),
                           ("В какой одежде выйти на улицу", "dress"), ("В чём одеться", "dress"),
                           ("Как одеться в холодный день", "dress"), ("Нужен ли зонт в дождливый день", "umbrella"),
                           ("Будет ли ливневый дождь", "umbrella"), ("Будет ли грозовой дождь", "umbrella"),
                           ("Какая текущая погода", "now"), ("Какая же погода на улице", "now"),
                           ("Какая погода на следующий день", "tomorrow"), ("Какая погода на следующую неделю", "week")):
            with self.subTest(text=text):
                self.assertEqual(voice_skills.parse_weather_ask(text), mode)


class WeatherQualifierHintTests(unittest.TestCase):
    def test_two_days_joined_by_and_are_not_answered_as_one(self):
        for text in ("Какая погода сегодня и на следующий день", "Какая погода сегодня и завтра",
                     "Какая погода завтра и на неделю"):
            with self.subTest(text=text):
                self.assertIsNone(voice_skills.parse_weather_ask(text))
                self.assertTrue(voice_skills.is_unsupported_weather_qualifier(text))

    def test_weather_question_with_an_unsupported_qualifier_is_caught(self):
        for text in ("Какая погода будет вечером", "Какая погода сегодня вечером", "Какая погода ночью",
                     "Какая погода завтра днём", "Какая погода сегодня и завтра", "Будет ли дождь вечером",
                     "Нужен ли зонт утром", "Будет ли снег вечером", "Будет ли гроза ночью", "Будет ли град вечером", "Что на улице ночью", "Алиса, прогноз на завтра и на неделю"):
            with self.subTest(text=text):
                self.assertTrue(voice_skills.is_unsupported_weather_qualifier(text))

    def test_other_turns_are_not_caught(self):
        for text in ("Какая температура утром", "Сколько градусов вечером", "Какая погода завтра", "Включи музыку",
                     "Какая погода в Париже вечером", "Почему идёт дождь вечером", "Расскажи о погоде на Марсе",
                     "Как погода влияет на здоровье", "Мама и папа", "Какая температура в комнате вечером", ""):
            with self.subTest(text=text):
                self.assertFalse(voice_skills.is_unsupported_weather_qualifier(text))

    def test_the_hint_is_spoken_not_the_brain(self):
        skills = voice_skills.Skills({"PATH": "/usr/bin:/bin"})
        self.assertEqual(asyncio.run(skills.answer("Какая погода ночью")), voice_skills.WEATHER_QUALIFIER_HINT)
        self.assertIsNone(asyncio.run(skills.answer("Какая температура утром")))

class WeatherRunnerTests(unittest.TestCase):
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

    def test_missing_cli_and_failed_run_are_named_in_the_log(self):
        with self.assertLogs(voice_skills.LOG, "WARNING") as logged:
            skills = voice_skills.Skills({"PATH": "/nonexistent"})
            with mock.patch.object(voice_skills.shutil, "which", return_value=None):
                asyncio.run(skills.answer("какая погода"))
        self.assertIn("reason=weather_cli_not_found", logged.output[0])
        self.assertIn("install-weather-cli.sh", logged.output[0])
        failing = self.skills('echo "weather: home-assistant: down" >&2; exit 1')
        with self.assertLogs(voice_skills.LOG, "WARNING") as logged:
            asyncio.run(failing.answer("какая погода"))
        self.assertIn("reason=exit_1", logged.output[0])

    def test_weather_timeout_kills_the_cli(self):
        skills = self.skills("sleep 30")
        with mock.patch.object(voice_skills, "WEATHER_TIMEOUT_S", 0.2):
            self.assertEqual(asyncio.run(skills.answer("какая погода")), voice_skills.WEATHER_FAILED)

    def test_timeout_survives_a_process_group_that_is_already_gone(self):
        real_killpg = os.killpg

        def killpg_then_vanish(pgid, sig):
            real_killpg(pgid, sig)
            raise ProcessLookupError

        skills = self.skills("sleep 30")
        with mock.patch.object(voice_skills, "WEATHER_TIMEOUT_S", 0.2), \
                mock.patch.object(voice_skills.os, "killpg", side_effect=killpg_then_vanish):
            self.assertEqual(asyncio.run(skills.answer("какая погода")), voice_skills.WEATHER_FAILED)

    def test_cli_gets_a_minimal_environment_without_secrets(self):
        env = {"PATH": "/usr/bin:/bin", "HOME": self.tmp.name, "LANG": "C.UTF-8",
               "WEATHER_LAT": "1.5", "AISIS_WEATHER_COMMAND": '["%s"]' % self.fake_weather("env")}
        secrets = {"ANTHROPIC_API_KEY": "sk-x", "HA_TOKEN": "t1", "HA_URL": "http://ha", "AISIS_CLAUDE_KEY": "k",
                   "MA_TOKEN": "t2", "GITHUB_TOKEN": "t3"}
        skills = voice_skills.Skills({**env, **secrets})
        reply = asyncio.run(skills.answer("какая погода"))
        child = dict(line.split("=", 1) for line in reply.splitlines() if "=" in line)
        for name in ("ANTHROPIC_API_KEY", "HA_TOKEN", "HA_URL", "AISIS_CLAUDE_KEY", "MA_TOKEN", "GITHUB_TOKEN",
                     "AISIS_WEATHER_COMMAND"):
            self.assertNotIn(name, child)
        self.assertEqual(child["HOME"], self.tmp.name)
        self.assertEqual(child["LANG"], "C.UTF-8")
        self.assertEqual(child["WEATHER_LAT"], "1.5")
        self.assertEqual(child["WEATHER_HA_URL"], "http://127.0.0.1:8795")
        self.assertEqual(child["WEATHER_HA_TOKEN"], "via-ha-cli")

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

    def test_a_nul_in_the_command_is_rejected_at_start(self):
        with self.assertRaises(ValueError):
            voice_skills.Skills({"AISIS_WEATHER_COMMAND": '["/bin/true\\u0000ignored"]'})

    def test_the_child_environment_is_one_function_shared_with_the_installer_check(self):
        env = voice_skills.weather_child_env({"PATH": "/usr/bin", "HOME": "/h", "WEATHER_LAT": "1", "HA_TOKEN": "x"},
                                             "http://ha-cli")
        self.assertEqual(env["WEATHER_LAT"], "1")
        self.assertEqual(env["WEATHER_HA_URL"], "http://ha-cli")
        self.assertEqual(env["WEATHER_HA_TOKEN"], "via-ha-cli")
        self.assertNotIn("HA_TOKEN", env)
        self.assertTrue(env["PATH"].startswith("/usr/bin"))



if __name__ == "__main__":
    unittest.main()
