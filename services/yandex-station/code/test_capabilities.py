"""capabilities: «что ты умеешь» is built from live facts, pages through the abilities, and offers only phrases the grammar reads."""
import asyncio
import re
import unittest
from datetime import datetime
from unittest import mock

import capabilities
import intent_route
from capabilities import Capabilities, Facts

DAY = datetime(2026, 11, 22, 14, 0)
MORNING = datetime(2026, 11, 22, 8, 0)
EVENING = datetime(2026, 11, 22, 20, 0)


class Home:
    def __init__(self, light=("Гостиная", "Второй этаж"), climate=("Гостиная", "Второй этаж"), error=None, hang=False):
        self.rooms, self.error, self.hang = {"light": light, "climate": climate}, error, hang

    async def rooms_with(self, domain):
        if self.hang:
            await asyncio.sleep(60)
        if self.error:
            raise self.error
        return self.rooms[domain]


class Timers:
    def __init__(self, left=None):
        self.left = left

    def remaining(self):
        return [{"id": "t", "left": self.left, "note": None}] if self.left is not None else []


class Skills:
    def __init__(self, weather=True):
        self.weather = weather

    def weather_available(self):
        return self.weather


class Clock:
    def __init__(self, now=DAY):
        self.now, self.t = now, 0.0

    def __call__(self):
        return self.now

    def monotonic(self):
        return self.t


def build(home="default", timers="default", skills="default", clock=None, **kwargs):
    clock = clock or Clock()
    caps = Capabilities(ha=Home() if home == "default" else home, timers=Timers() if timers == "default" else timers,
                        skills=Skills() if skills == "default" else skills, now=clock, pick=lambda options: options[0],
                        monotonic=clock.monotonic, **kwargs)
    return caps, clock


class AnswerTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_first_answer_lists_what_is_configured_with_real_rooms_and_an_example(self):
        caps, _ = build()
        self.assertEqual(await caps.answer("st"),
                         "Доступно: управление светом в гостиной и на втором этаже; кондиционеры в гостиной и на втором этаже: режим и температура; "
                         "таймеры, будильники и напоминания. Например: «включи свет в гостиной».")

    async def test_an_unconfigured_ability_is_not_offered(self):
        caps, _ = build(timers=None, skills=None)
        text = " ".join([await caps.answer("st"), await caps.answer("st")])
        for gone in ("таймер", "погода", "музыка"):
            self.assertNotIn(gone, text)
        self.assertIn("время, дата и счёт", text)

    async def test_home_that_does_not_answer_is_said_once_and_its_abilities_are_left_out(self):
        caps, _ = build(home=Home(error=OSError("down")))
        text = await caps.answer("st")
        self.assertTrue(text.startswith(capabilities.HOME_DOWN), text)
        self.assertNotIn("свет", text.replace(capabilities.HOME_DOWN, ""))
        self.assertIn("таймеры", text)

    async def test_a_home_that_hangs_is_given_a_second_and_a_half_not_a_minute(self):
        caps, _ = build(home=Home(hang=True))
        with mock.patch.object(capabilities, "HOME_TIMEOUT_S", 0.01):
            text = await caps.answer("st")
        self.assertTrue(text.startswith(capabilities.HOME_DOWN))

    async def test_without_home_actions_nothing_is_said_about_the_home(self):
        caps, _ = build(home=None)
        text = await caps.answer("st")
        self.assertNotIn(capabilities.HOME_DOWN, text)
        self.assertNotIn("свет", text)

    async def test_a_home_without_lights_does_not_offer_light(self):
        caps, _ = build(home=Home(light=()))
        self.assertNotIn("свет", await caps.answer("st"))

    async def test_the_hour_orders_the_abilities(self):
        for now, expected in ((MORNING, "Доступно: погода на сегодня"), (EVENING, "Доступно: управление светом"), (DAY, "Доступно: управление светом")):
            caps, _ = build(clock=Clock(now))
            self.assertTrue((await caps.answer("st")).startswith(expected), now)

    async def test_a_running_timer_is_mentioned(self):
        caps, _ = build(timers=Timers(left=240))
        self.assertIn("(сейчас идёт таймер: осталось 4 минуты)", await caps.answer("st"))

    async def test_more_than_four_rooms_end_with_other_rooms(self):
        self.assertEqual(capabilities.rooms(("Гостиная", "Кухня", "Спальня", "Студия", "Прихожая")),
                         "в гостиной, на кухне, в спальне, в студии и в других комнатах")

    async def test_no_answer_names_an_actor(self):
        forbidden = re.compile(r"колонк|\bя\b|умею|включаю|сам[аои]\b|штатн", re.IGNORECASE)
        for hour in (3, 8, 14, 20):
            for home in (Home(), Home(error=OSError()), None):
                caps, _ = build(home=home, clock=Clock(datetime(2026, 11, 22, hour)))
                for _ in range(4):
                    text = await caps.answer("st")
                    self.assertIsNone(forbidden.search(text), text)
                for topic in ("light", "climate", "timers", "weather", "music", "volume", "tv", "home"):
                    self.assertIsNone(forbidden.search(await caps.answer("st", topic=topic) or ""), topic)


class CursorTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_same_question_again_or_a_follow_up_gives_the_next_group_then_closes(self):
        caps, _ = build()
        first = await caps.answer("st")
        second = await caps.answer("st", more=True)
        self.assertIn("Ещё доступно:", second)
        self.assertIn("музыка", second)
        self.assertIn("справка о том, что включено", await caps.answer("st", more=True))
        self.assertEqual(await caps.answer("st", more=True), capabilities.WRAP)
        self.assertEqual(await caps.answer("st"), first)       # the cursor is gone: the list starts over

    async def test_a_follow_up_with_nothing_to_follow_is_not_an_answer(self):
        caps, clock = build()
        self.assertIsNone(await caps.answer("st", more=True))
        await caps.answer("st")
        clock.t += capabilities.CURSOR_WINDOW_S + 1
        self.assertIsNone(await caps.answer("st", more=True))

    async def test_each_station_has_its_own_cursor(self):
        caps, _ = build()
        await caps.answer("a")
        self.assertIsNone(await caps.answer("b", more=True))

    async def test_any_other_line_ends_the_cursor_but_the_answer_itself_does_not(self):
        caps, _ = build()
        said = await caps.answer("st")
        caps.forget("st", said)
        self.assertIsNotNone(await caps.answer("st", more=True))
        caps.forget("st", "Сейчас 14 часов 5 минут.")
        self.assertIsNone(await caps.answer("st", more=True))


class TopicTests(unittest.IsolatedAsyncioTestCase):
    async def topic(self, topic, **kwargs):
        caps, _ = build(**kwargs)
        return await caps.answer("st", topic=topic)

    async def test_topics_with_real_rooms_and_examples(self):
        self.assertEqual(await self.topic("light"),
                         "Со светом доступно: включить и выключить в гостиной и на втором этаже, поменять цвет и яркость. Например: «включи свет в гостиной».")
        self.assertEqual(await self.topic("climate"),
                         "С кондиционерами в гостиной и на втором этаже доступно: включить, выбрать режим и температуру, узнать, какой включён. "
                         "Например: «включи кондиционер в гостиной на обогрев».")
        self.assertIn("«поставь таймер на десять минут»", await self.topic("timers"))
        self.assertIn("«какая погода завтра»", await self.topic("weather"))
        self.assertIn("«включи музыку»", await self.topic("music"))
        self.assertIn("«громкость на пять»", await self.topic("volume"))
        self.assertIn("«включи телевизор»", await self.topic("tv"))
        home = await self.topic("home")
        self.assertTrue(home.startswith("Умный дом: управление светом"), home)

    async def test_what_is_not_available_is_said_so_not_denied_as_unable(self):
        self.assertEqual(await self.topic("timers", timers=None), "Таймеры и будильники сейчас недоступны.")
        self.assertEqual(await self.topic("weather", skills=Skills(weather=False)), "Погода сейчас недоступна.")
        self.assertEqual(await self.topic("music", skills=None), "Музыка сейчас недоступна.")
        self.assertEqual(await self.topic("light", home=Home(error=OSError())), capabilities.HOME_DOWN)
        self.assertEqual(await self.topic("tv", home=Home(error=OSError())), capabilities.HOME_DOWN)
        self.assertEqual(await self.topic("light", home=Home(light=())), capabilities.NO_LIGHTS)
        self.assertEqual(await self.topic("climate", home=Home(climate=())), capabilities.NO_CLIMATE)
        self.assertEqual(await self.topic("home", home=Home(light=(), climate=())), "Управление умным домом сейчас недоступно.")
        self.assertEqual(await self.topic("home", home=None), "Управление умным домом сейчас недоступно.")   # AISIS_HA_ACTIONS=off: not «света нет»
        self.assertEqual(await self.topic("home", home=Home(error=OSError())), capabilities.HOME_DOWN)

    async def test_the_volume_topic_needs_no_home(self):
        self.assertIn("Громкость:", await self.topic("volume", home=Home(error=OSError())))


class ExamplesAreUnderstoodTests(unittest.IsolatedAsyncioTestCase):
    """An answer must never offer a phrase the stack does not read: every example is classified by the real grammar."""
    EXPECTED = {"light": "light.on", "climate": "climate.set", "timers": "timer.set", "weather": "skill.weather", "music": "music.generic",
                "clock": "time.now", "status": "home.climate_status"}

    def test_every_ability_example_is_read_into_the_intent_it_demonstrates(self):
        router = intent_route.IntentRouter(intent_route.load_grammar())
        facts = Facts(True, True, ("Гостиная", "Второй этаж"), ("Гостиная", "Второй этаж"), True, None, True, True)
        for ability in capabilities.ABILITIES:
            for where_facts in (facts, Facts(True, True, ("Второй этаж", "Кухня"), ("Кухня",), True, None, True, True),
                                Facts(True, True, ("Угол",), ("Угол",), True, None, True, True)):
                example = ability.example(where_facts)
                decision = router.classify(example)
                with self.subTest(ability=ability.key, example=example):
                    self.assertIsNotNone(decision)
                    self.assertEqual(decision.intent, self.EXPECTED[ability.key])
                    self.assertFalse(decision.fuzzy)

    def test_the_fixed_topic_examples_are_read_too(self):
        router = intent_route.IntentRouter(intent_route.load_grammar())
        for phrase in capabilities.TOPIC_EXAMPLE.values():
            decision = router.classify(phrase)
            self.assertIsNotNone(decision, phrase)
            self.assertIn(decision.tier, ("execute", "query"))

    def test_every_ability_is_covered(self):
        self.assertEqual(set(self.EXPECTED), {ability.key for ability in capabilities.ABILITIES})


if __name__ == "__main__":
    unittest.main()
