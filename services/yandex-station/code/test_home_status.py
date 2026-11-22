"""home_status: the rounding of «since when» and the impersonal climate sentences (spec 150, §4.2)."""
import re
import unittest
from datetime import datetime, timedelta, timezone

import home_status
from home_status import Unit, climate_text, since_text

NOW = datetime(2026, 11, 22, 12, 30, tzinfo=timezone.utc)


def ago(**delta):
    return NOW - timedelta(**delta)


def first(options):
    return options[0]


class SinceTextTests(unittest.TestCase):
    def check(self, expected, since):
        self.assertEqual(since_text(NOW, since), expected)

    def test_minutes_are_rounded_not_counted(self):
        for minutes, spoken in ((0, "меньше минуты"), (1, "около минуты"), (2, "пару минут"), (3, "около 3 минут"), (9, "около 9 минут"),
                                (10, "около 10 минут"), (12, "около 10 минут"), (13, "около 15 минут"), (22, "около 20 минут"),
                                (52, "около 50 минут"), (53, "около часа"), (54, "около часа")):
            with self.subTest(minutes=minutes):
                self.check(spoken, ago(minutes=minutes))

    def test_the_hour_boundaries(self):
        for minutes, spoken in ((55, "около часа"), (69, "около часа"), (70, "больше часа"), (119, "больше часа"), (120, "около 2 часов"),
                                (150, "около 3 часов"), (151, "с утра")):
            with self.subTest(minutes=minutes):
                self.check(spoken, ago(minutes=minutes))

    def test_morning_wording_needs_a_morning_switch_and_an_afternoon_question(self):
        self.check("с утра", NOW.replace(hour=9, minute=0))              # 3.5 h ago, switched before 10, asked after 12
        self.check("с утра", NOW.replace(hour=7, minute=30))
        self.check("около 2 часов", NOW - timedelta(hours=2))            # switched at 10:30: not the morning
        later = NOW.replace(hour=11, minute=30)
        self.assertEqual(since_text(later, later.replace(hour=7, minute=45)), "около 4 часов")      # asked before 12: hours, not «с утра»
        self.assertEqual(since_text(NOW.replace(hour=20), NOW.replace(hour=11)), "несколько часов")  # switched after 10: not the morning

    def test_days(self):
        self.check("со вчерашнего вечера", datetime(2026, 11, 21, 20, 0, tzinfo=timezone.utc))
        self.check("со вчерашнего дня", datetime(2026, 11, 21, 13, 0, tzinfo=timezone.utc))
        self.check("больше суток", datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc))
        self.check("несколько дней", datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc))

    def test_unknown_and_future_times_are_left_out(self):
        self.assertIsNone(since_text(NOW, None))
        self.assertIsNone(since_text(NOW, NOW + timedelta(minutes=5)))

    def test_a_time_in_another_zone_counts_in_the_zone_of_now(self):
        berlin = timezone(timedelta(hours=1))
        now = datetime(2026, 11, 22, 13, 30, tzinfo=berlin)
        self.assertEqual(since_text(now, datetime(2026, 11, 22, 9, 0, tzinfo=timezone.utc)), "около 4 часов")   # 10:00 in Berlin: not before ten
        self.assertEqual(since_text(now, datetime(2026, 11, 22, 8, 30, tzinfo=timezone.utc)), "с утра")        # 09:30 in Berlin


class ClimateTextTests(unittest.TestCase):
    UPSTAIRS = Unit("на втором этаже", "heat", 30.0, None, ago(minutes=20))
    LIVING = Unit("в гостиной", "heat", 30.0, 19.8, ago(minutes=80))

    def text(self, units, **kwargs):
        kwargs.setdefault("pick", first)
        return climate_text(units, NOW, **kwargs)

    def test_the_owner_example_one_unit_on(self):
        self.assertEqual(self.text([self.UPSTAIRS]), "Сейчас включён кондиционер на втором этаже: обогрев, 30 градусов, уже около 20 минут.")

    def test_variants_say_the_same_facts(self):
        seen = set()
        for index in range(3):
            text = self.text([self.UPSTAIRS], pick=lambda options, i=index: options[i])
            seen.add(text)
            for fact in ("на втором этаже", "обогрев", "30 градусов", "около 20 минут"):
                self.assertIn(fact, text)
        self.assertEqual(len(seen), 3)

    def test_two_units_alike_are_one_sentence(self):
        alike = Unit("в гостиной", "heat", 30.0, 19.8, ago(minutes=20))
        self.assertEqual(self.text([self.UPSTAIRS, alike]),
                         "Включены оба кондиционера: на втором этаже и в гостиной, обогрев, 30 градусов, уже около 20 минут.")

    def test_two_units_different_one_sentence_each(self):
        text = self.text([self.UPSTAIRS, self.LIVING])
        self.assertIn("на втором этаже", text)
        self.assertIn("около 20 минут", text)
        self.assertIn("больше часа", text)
        self.assertNotIn("оба", text)

    def test_one_on_one_off(self):
        off = Unit("в гостиной", "off", 22.0, 20.0, ago(hours=3))
        self.assertEqual(self.text([self.UPSTAIRS, off]),
                         "Сейчас включён кондиционер на втором этаже: обогрев, 30 градусов, уже около 20 минут. Кондиционер в гостиной выключен.")

    def test_all_off(self):
        a, b = Unit("на втором этаже", "off"), Unit("в гостиной", "off")
        self.assertEqual(self.text([a, b]), "Сейчас все кондиционеры выключены.")
        self.assertEqual(self.text([Unit("на кухне", "off", since=ago(hours=1, minutes=20))]), "Кондиционер на кухне выключен уже больше часа.")

    def test_a_unit_that_does_not_answer(self):
        self.assertIn("Кондиционер на втором этаже сейчас не отвечает.", self.text([Unit("на втором этаже", "unavailable"), self.LIVING]))
        self.assertEqual(self.text([Unit("на втором этаже", "unavailable")]), home_status.STATUS_UNAVAILABLE)

    def test_every_mode_home_assistant_reports_is_spoken_in_words(self):
        """HA reports auto as heat_cool on some units (ha_actions.STATE_MODES): the raw token is never spoken."""
        unit = Unit("на втором этаже", "heat_cool", 24.0, None, ago(minutes=20))
        self.assertEqual(self.text([unit], aspect="mode"), "Кондиционер на втором этаже работает в режиме «автоматический режим».")
        self.assertEqual(self.text([unit]), "Сейчас включён кондиционер на втором этаже: автоматический режим, 24 градуса, уже около 20 минут.")
        odd = Unit("на втором этаже", "something_new")
        self.assertEqual(self.text([odd], aspect="mode"), "Кондиционер на втором этаже включён, режим определить не удаётся.")
        self.assertNotIn("something_new", self.text([odd]))

    def test_all_off_is_not_said_while_a_unit_does_not_answer(self):
        text = self.text([Unit("на втором этаже", "off"), Unit("в гостиной", "off"), Unit("на кухне", "unavailable")])
        self.assertNotIn("все кондиционеры", text.lower())
        self.assertIn("Кондиционер на кухне сейчас не отвечает.", text)
        self.assertIn("Кондиционер на втором этаже выключен.", text)

    def test_missing_facts_are_left_out_not_invented(self):
        bare = Unit(None, "cool")
        self.assertEqual(self.text([bare]), "Сейчас включён кондиционер: охлаждение.")
        self.assertEqual(self.text([Unit("в гостиной", "heat")]), "Сейчас включён кондиционер в гостиной: обогрев.")
        self.assertEqual(self.text([Unit("в гостиной", "on")]), "Сейчас включён кондиционер в гостиной.")
        self.assertEqual(self.text([]), home_status.NO_UNITS)

    def test_aspects(self):
        self.assertEqual(self.text([self.LIVING], aspect="temp"), "Кондиционер в гостиной выставлен на 30 градусов, сейчас в комнате 19,8 градуса.")
        self.assertEqual(self.text([self.UPSTAIRS], aspect="temp"), "Кондиционер на втором этаже выставлен на 30 градусов.")
        self.assertEqual(self.text([self.UPSTAIRS], aspect="mode"), "Кондиционер на втором этаже работает в режиме «обогрев».")
        self.assertEqual(self.text([self.UPSTAIRS], aspect="since"), "Кондиционер на втором этаже включён около 20 минут.")
        self.assertEqual(self.text([Unit("на кухне", "off", since=ago(hours=1, minutes=20))], aspect="mode"), "Кондиционер на кухне сейчас выключен.")
        self.assertEqual(self.text([Unit("на кухне", "off", current=21.0)], aspect="temp"), "Кондиционер на кухне выключен, в комнате 21 градус.")

    def test_degrees_and_lists(self):
        self.assertEqual([home_status.degrees(v) for v in (1, 2, 5, 21, 30, 19.8)],
                         ["1 градус", "2 градуса", "5 градусов", "21 градус", "30 градусов", "19,8 градуса"])
        self.assertEqual(home_status.and_list(["A", "B", "C"]), "A, B и C")
        self.assertEqual(home_status.locative("Угол"), "в комнате «Угол»")

    def test_no_sentence_names_an_actor_or_a_denial(self):
        forbidden = re.compile(r"колонк|\bя\b|не включал|не умею|включу|включаю|сделаю", re.IGNORECASE)
        units = [self.UPSTAIRS, self.LIVING, Unit("на кухне", "off"), Unit("в студии", "unavailable"), Unit(None, "cool"), Unit("в гостиной", "off", since=ago(days=3))]
        for aspect in (None, "mode", "temp", "since"):
            for subset in (units[:1], units[:2], units[2:3], units[:3], units[3:], units):
                for index in range(3):
                    text = climate_text(subset, NOW, aspect=aspect, pick=lambda options, i=index: options[i % len(options)])
                    self.assertIsNone(forbidden.search(text), text)


if __name__ == "__main__":
    unittest.main()
