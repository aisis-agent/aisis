"""The clarifier (#307): which actions a phrase nobody read may have meant, how the question is worded, what is said when there is none,
and the dialog that carries the answer. The home is a fake `Home`; nothing here reaches Home Assistant."""
import asyncio
import unittest
from datetime import datetime

import clarify
import intent_route
from clarify import Entity, HaCall
from test_correction import Clock, FakeCore

NOON = datetime(2026, 11, 26, 12, 0).astimezone()
NIGHT = datetime(2026, 11, 26, 23, 30).astimezone()


class FakeHome:
    def __init__(self, *entities, fail=False):
        self.items = list(entities)
        self.calls = []
        self.fail = fail

    async def entities(self):
        if self.fail:
            raise ConnectionError("ha-cli is down")
        return tuple(self.items)

    async def act(self, call: HaCall) -> str:
        self.calls.append((call.domain, call.service, call.entity_id))
        return "Готово."


TV = Entity("media_player.televizor", "Телевизор", "playing")
LAMP = Entity("light.ultra", "Ультра лампа", "on")
AC = Entity("climate.upstairs", "Кондиционер наверху", "off")


def clarifier(*entities, now=NOON, **kwargs):
    grammar = intent_route.load_grammar()
    router = intent_route.IntentRouter(grammar, now=lambda: now, home=FakeHome(*entities, **kwargs))
    return router.clarifier


def options(clar, text, floor=clarify.ASK_AT):
    return asyncio.run(clar.options("st", text, floor=floor))


class OptionTests(unittest.TestCase):
    def test_the_recorded_incident_names_the_playing_tv(self):
        found = options(clarifier(TV, LAMP, AC), "Ставьте их на паузу.")
        self.assertEqual([(o.ask, o.call.service, o.call.entity_id) for o in found], [("поставить телевизор на паузу", "media_pause", TV.entity_id)])
        self.assertEqual(clarify.Clarifier.question(found), "Поставить телевизор на паузу?")

    def test_a_synonym_of_the_grammars_own_lexicon_names_the_same_device(self):
        found = options(clarifier(TV), "поставь телик на паузу")
        self.assertEqual([o.call.entity_id for o in found], [TV.entity_id])

    def test_the_state_decides_the_verb(self):
        paused = Entity(TV.entity_id, "Телевизор", "paused")
        self.assertEqual([o.ask for o in options(clarifier(paused), "продолжите просмотр телика")], ["продолжить телевизор"])
        self.assertEqual(options(clarifier(paused), "Ставьте их на паузу."), [])      # already paused: pausing it is not an option

    def test_a_device_the_home_does_not_have_is_never_named(self):
        self.assertFalse([o for o in options(clarifier(LAMP, AC), "Ставьте их на паузу.") if o.call is not None])

    def test_a_device_that_is_off_or_dead_offers_nothing_to_pause(self):
        for state in ("off", "unavailable", "unknown"):
            found = options(clarifier(Entity(TV.entity_id, "Телевизор", state)), "Ставьте их на паузу.")
            self.assertFalse([o for o in found if o.call is not None and o.call.service == "media_pause"], state)

    def test_a_lamp_needs_its_name_heard_and_is_offered_the_opposite_of_its_state(self):
        clar = clarifier(LAMP, TV)
        self.assertEqual([o.ask for o in options(clar, "выключи ульта лампу")], ["выключить ультра лампу"])
        self.assertEqual(options(clar, "выключи"), [])               # no name, many devices: nothing to guess

    def test_a_phrase_that_is_not_a_command_gets_no_question_before_the_brain(self):
        clar = clarifier(TV, LAMP, AC)
        for text in ("расскажи про паузу в фильме", "какая столица у Австралии", "сколько стоит телевизор", "мне скучно"):
            self.assertEqual(options(clar, text), [], text)

    def test_two_players_make_two_options_at_most(self):
        radio = Entity("media_player.radio", "Радио", "playing")
        found = options(clarifier(TV, radio, LAMP), "поставь на паузу")
        self.assertEqual(sorted(o.ask for o in found), ["поставить радио на паузу", "поставить телевизор на паузу"])
        self.assertEqual(clarify.Clarifier.question(found).count(" или "), 1)

    def test_home_assistant_down_leaves_the_grammars_phrases(self):
        found = options(clarifier(TV, fail=True), "который час", floor=clarify.FALLBACK_AT)
        self.assertEqual([o.source for o in found], ["grammar"])
        self.assertIsNone(found[0].call)

    def test_an_option_must_be_something_this_turn_runs(self):
        found = options(clarifier(), "громкость пять", floor=0.0)
        self.assertNotIn("speaker.volume", [o.detail for o in found])      # the quasar layer's, a «да» here could not run it

    def test_a_value_is_never_guessed(self):
        found = options(clarifier(), "поставь таймер на семь минут", floor=0.0)
        self.assertFalse([o for o in found if o.detail.startswith("timer.")])

    def test_a_light_off_is_a_little_likelier_at_night(self):
        day = clarifier(LAMP, now=NOON)
        night = clarifier(LAMP, now=NIGHT)
        self.assertGreater(night._prior(options(night, "выключи лампу", floor=0)[0], "st"),
                           day._prior(options(day, "выключи лампу", floor=0)[0], "st"))

    def test_a_phrase_near_a_recent_command_is_a_little_likelier(self):
        clar = clarifier(TV)
        pause = options(clar, "поставь на паузу")[0]
        before = clar._prior(pause, "st")
        clar.remember("st", "поставь телевизор на паузу")
        self.assertGreater(clar._prior(pause, "st"), before)

    def test_another_rooms_device_is_never_offered(self):
        studio = Entity("light.studio", "Свет в студии", "on")
        clar = clarifier(studio)
        self.assertEqual(options(clar, "свет в спальне выключи", floor=clarify.FALLBACK_AT), [])
        self.assertEqual([o.ask for o in options(clar, "выключи свет в студии", floor=clarify.FALLBACK_AT)], ["выключить свет в студии"])

    def test_a_phrase_without_a_verb_is_asked_as_what_was_meant_and_never_second(self):
        found = options(clarifier(TV), "помоги мне", floor=clarify.ASK_AT)
        self.assertTrue(all(o.ask.startswith(clarify.MEANT) for o in found))
        both = options(clarifier(TV), "поставь на паузу помоги", floor=0.3)
        self.assertFalse(len(both) == 2 and both[1].ask.startswith(clarify.MEANT))

    def test_a_player_with_a_technical_name_is_still_the_one_that_plays(self):
        """The real catalog (2026-11-26): media_player.televizor is «Chromecast — пульт», a name nobody says aloud."""
        states = [{"entity_id": "media_player.televizor", "state": "playing", "attributes": {"friendly_name": "Chromecast — пульт"}},
                  {"entity_id": "light.ultra", "state": "on", "attributes": {"friendly_name": "Chromecast лампа"}},
                  {"entity_id": "media_player.station", "state": "paused", "attributes": {"friendly_name": "Станция Мини Гостиная"}}]
        clar = clarifier(*clarify.entities_from(states))
        found = options(clar, "Ставьте их на паузу.")
        self.assertEqual([(o.ask, o.call.entity_id) for o in found], [("поставить на паузу", "media_player.televizor")])
        self.assertEqual(clarify.Clarifier.question(found), "Поставить на паузу?")
        self.assertEqual(clarify.HaHome._said(found[0].call), "Воспроизведение на паузе.")
        self.assertEqual(options(clar, "выключи"), [])      # a device nobody can name is never switched off by a guess

    def test_a_question_that_holds_a_pinned_phrase_goes_to_the_brain(self):
        clar = clarifier(TV)
        for text in ("который час в токио", "сколько времени до нового года"):
            self.assertEqual(options(clar, text), [], text)

    def test_a_long_phrase_before_the_brain_gets_none(self):
        self.assertEqual(options(clarifier(TV), "поставь на паузу я говорю тебе вот прямо сейчас немедленно"), [])

    def test_a_socket_and_a_tv_power_are_never_offered(self):
        kettle = Entity("switch.kettle_plug_local", "Чайник", "off")
        tv_off = Entity("media_player.projector", "Проектор", "off")
        clar = clarifier(kettle, tv_off)
        self.assertEqual(options(clar, "включи чайнек", floor=clarify.FALLBACK_AT), [])
        self.assertEqual(options(clar, "включи проектор", floor=clarify.FALLBACK_AT), [])

    def test_a_players_device_class_names_it_when_its_own_name_is_technical(self):
        states = [{"entity_id": "media_player.televizor", "state": "playing",
                   "attributes": {"friendly_name": "Chromecast — пульт", "device_class": "tv"}}]
        found = options(clarifier(*clarify.entities_from(states)), "Ставьте их на паузу.")
        self.assertEqual(clarify.Clarifier.question(found), "Поставить телевизор на паузу?")
        self.assertEqual(clarify.HaHome._said(found[0].call), "Телевизор на паузе.")


class WordingTests(unittest.TestCase):
    def test_a_feminine_name_is_a_direct_object(self):
        self.assertEqual(clarify.accusative("лампа в студии"), "лампу в студии")
        self.assertEqual(clarify.accusative("телевизор"), "телевизор")
        self.assertEqual(clarify.accusative("свеча"), "свечу")

    def test_the_imperative_becomes_the_question_infinitive(self):
        self.assertEqual(clarify.infinitive("поставь телевизор на паузу"), "поставить телевизор на паузу")
        self.assertEqual(clarify.infinitive("который час"), "который час")
        self.assertEqual(clarify.ask_form("который час"), "вы имели в виду «который час»")
        self.assertEqual(clarify.ask_form("выключи свет"), "выключить свет")

    def test_nothing_read_says_what_was_heard_and_what_can_be_asked_for_never_to_repeat(self):
        clar = clarifier(TV, LAMP, AC)
        asyncio.run(clar.refresh())
        self.assertEqual(clar.describe("мумба юмба."), "Прозвучало «мумба юмба», но что сделать, не ясно: свет, кондиционер или телевизор?")
        self.assertEqual(clar.describe(""), "Ничего не расслышал. Что сделать: свет, кондиционер или телевизор?")
        self.assertEqual(clar.describe("мумба", denied=True), "Тогда что сделать: свет, кондиционер или телевизор?")
        for line in (clar.describe("мумба"), clar.describe(""), clar.describe("x", True)):
            self.assertNotIn("повтор", line.lower())

    def test_without_a_catalog_the_line_still_says_what_was_heard(self):
        bare = clarifier(fail=True)
        asyncio.run(bare.refresh())
        self.assertEqual(bare.describe("мумба"), "Прозвучало «мумба», но что сделать, не ясно.")


class DialogTests(unittest.IsolatedAsyncioTestCase):
    async def router(self, *entities):
        home = FakeHome(*entities)
        router = intent_route.IntentRouter(intent_route.load_grammar(), home=home, correction_core=FakeCore(), now=lambda: NOON)
        router.monotonic = Clock()
        return router, home

    async def test_a_yes_makes_the_one_service_call_and_says_what_was_observed(self):
        router, home = await self.router(TV)
        asked = await router.unheard("st", "Ставьте их на паузу.")
        self.assertEqual((asked.text, asked.listen, asked.context), ("Поставить телевизор на паузу?", True, True))
        self.assertEqual(home.calls, [])
        done = await router.route("st", "да")
        self.assertEqual((done.kind, done.text), ("reply", "Готово."))
        self.assertEqual(home.calls, [("media_player", "media_pause", TV.entity_id)])

    async def test_a_no_runs_nothing_and_names_the_kinds_of_action(self):
        router, home = await self.router(TV, LAMP)
        await router.unheard("st", "Ставьте их на паузу.")
        said = await router.route("st", "нет")
        self.assertEqual(home.calls, [])
        self.assertIn("свет", said.text)
        self.assertNotIn("повтор", said.text.lower())

    async def test_two_options_are_picked_by_the_device_name(self):
        radio = Entity("media_player.radio", "Радио", "playing")
        router, home = await self.router(TV, radio)
        asked = await router.unheard("st", "поставь на паузу")
        self.assertEqual(asked.text.count(" или "), 1)
        await router.route("st", "радио")
        self.assertEqual(home.calls, [("media_player", "media_pause", radio.entity_id)])

    async def test_a_cancel_ends_it_and_nothing_runs(self):
        router, home = await self.router(TV)
        await router.unheard("st", "Ставьте их на паузу.")
        await router.route("st", "отмена")
        self.assertEqual(home.calls, [])

    async def test_an_emergency_phrase_gets_no_question(self):
        router, home = await self.router(TV)
        said = await router.unheard("st", "вызови скорую")
        self.assertFalse(said.context)                 # no dialog opened over it
        self.assertEqual(home.calls, [])
        self.assertFalse(router.correction and router.correction.dialogs)

    async def test_a_phrase_just_answered_gets_the_line_not_a_second_question(self):
        router, _ = await self.router(TV)
        await router.unheard("st", "Ставьте их на паузу.")
        await router.route("st", "отмена")
        again = await router.unheard("st", "Ставьте их на паузу.")           # the cooldown of the dialog about this phrase
        self.assertFalse(again.context)
        self.assertNotIn("повтор", again.text.lower())

    async def test_the_clarify_switch_off_keeps_only_the_line(self):
        router, _ = await self.router(TV)
        router.clarify_mode = "off"
        said = await router.unheard("st", "Ставьте их на паузу.")
        self.assertEqual(said.text.startswith("Прозвучало «Ставьте их на паузу»"), True)
        self.assertFalse(said.context)

    async def test_a_keep_reply_to_a_clarification_is_a_no_not_a_request_to_repeat(self):
        router, home = await self.router(TV)
        await router.unheard("st", "Ставьте их на паузу.")
        said = await router.route("st", "я так и сказал")
        self.assertEqual(home.calls, [])
        self.assertNotIn("повтор", said.text.lower())
        self.assertNotIn("Скажите ещё раз", said.text)

    async def test_two_open_microphone_lines_in_a_row_are_rate_limited_on_the_noise_paths(self):
        router, _ = await self.router(TV)
        first = await router.unheard("st", "Ставьте их на паузу.", cooldown=True)
        self.assertTrue(first.listen)
        self.assertIsNone(await router.unheard("st", "Ставьте их на паузу.", cooldown=True))   # a minute: the brain takes it

    async def test_a_two_word_device_is_picked_by_its_name_in_any_hash_order(self):
        studio = Entity("light.studio", "Лампа в студии", "on")
        kitchen = Entity("light.kitchen", "Лампа на кухне", "on")
        router, home = await self.router(studio, kitchen)
        asked = await router.unheard("st", "выключи лампу")
        self.assertEqual(asked.text.count(" или "), 1)
        await router.route("st", "лампу в студии")
        self.assertEqual(home.calls, [("light", "turn_off", studio.entity_id)])


if __name__ == "__main__":
    unittest.main()
