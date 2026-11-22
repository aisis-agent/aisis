"""The information intents of spec 150: the phrase fixture, what must NOT be read as them, and the router's executors."""
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import intent_route
from ha_actions import HaActions
from test_ha_actions import FakeHa
from test_ha_status import states

FIXTURE = Path(__file__).parent / "intent_grammar" / "grammar" / "info-ru.jsonl"
INFO = ("assistant.", "home.climate_status", "home.climate_bare")
NOW = datetime(2026, 11, 22, 12, 30, tzinfo=timezone.utc)

# Phrases next to the new intents that keep their own readings.
OTHERS = {
    "включи кондиционер": "climate.set", "включи кондиционер на втором этаже": "climate.set", "включи кондиционер на обогрев": "climate.set",
    "кондиционер 23": "climate.set", "поставь кондиционер на 24": "climate.set", "кондиционер на втором этаже на обогрев": "climate.set",
    "сделай теплее": "climate.warmer", "не включай кондиционер": "negation.noop", "выключи кондиционер": "refuse.unknown_device",
    "какая температура на улице": "skill.weather", "какой свет сейчас включен": "state.report", "что включено": "state.report",
    "скажи время": "time.now", "какая погода": "skill.weather", "помогите": "refuse.emergency", "на помощь": "refuse.emergency",
    "как дела": "smalltalk.how_are_you", "что нового": "smalltalk.how_are_you", "включи свет": "light.on", "ещё раз": "dialog.repeat",
    "ещё громче": "volume.up",
}
# Requests and chatter that go to the brain, not to an information answer.
BRAIN = ["можешь включить свет", "можешь поставить таймер", "что ты умеешь готовить", "расскажи анекдот", "помоги мне найти песню",
         "включён ли свет", "что это значит", "какие у тебя планы", "как тебя зовут", "кто ты", "что у тебя нового"]


def decide(router, text):
    return router.classify(text)


class FixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.router = intent_route.IntentRouter(intent_route.load_grammar())
        cls.rows = [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_the_fixture_is_big_and_has_every_intent(self):
        self.assertGreaterEqual(len(self.rows), 100)
        self.assertEqual({row["intent"] for row in self.rows},
                         {"assistant.capabilities", "assistant.capabilities_topic", "assistant.capabilities_more", "home.climate_status", "home.climate_bare"})
        self.assertTrue(any(row["kind"] == "real" for row in self.rows))
        self.assertEqual(len({row["text"] for row in self.rows}), len(self.rows))

    def test_every_phrase_reads_as_its_intent_with_its_slots(self):
        for row in self.rows:
            decision = decide(self.router, row["text"])
            with self.subTest(text=row["text"]):
                self.assertIsNotNone(decision)
                self.assertEqual(decision.intent, row["intent"])
                self.assertFalse(decision.fuzzy)
                for slot, value in row.get("slots", {}).items():
                    self.assertEqual(decision.slots.get(slot), value)

    def test_the_wake_word_forms_and_a_leading_reaction_change_nothing(self):
        for lead in ("Алиса, ", "Яндекс, ", "Малис, ", "Тоже неплохо. ", "Ладно, а ", "Слушай, а ", "Ну и ", "Хорошо. "):
            decision = decide(self.router, lead + "что ты умеешь?")
            with self.subTest(lead=lead):
                self.assertEqual(decision.intent, "assistant.capabilities")

    def test_neighbours_keep_their_readings(self):
        for text, intent in OTHERS.items():
            decision = decide(self.router, text)
            with self.subTest(text=text):
                self.assertEqual(decision.intent if decision else None, intent)

    def test_chatter_and_requests_are_not_information_answers(self):
        for text in BRAIN:
            decision = decide(self.router, text)
            with self.subTest(text=text):
                self.assertFalse(decision is not None and decision.intent.startswith(INFO), decision)

    def test_the_status_questions_are_asked_before_the_quasar_layer(self):
        for text in ("какой кондиционер включён", "кондиционер на втором этаже", "что с кондиционером"):
            decision = self.router.classify(text, "pre")
            with self.subTest(text=text):
                self.assertIsNotNone(decision)
                self.assertTrue(decision.intent.startswith("home.climate"))
        self.assertIsNone(self.router.classify("что ты умеешь", "pre"))      # the container has no reading of it: the turn path answers


def router(ha=True, **kwargs):
    fake = FakeHa(states())
    actions = HaActions(fake.get, fake.post, wall=lambda: NOW, pick=lambda options: options[0]) if ha else None
    kwargs.setdefault("now", lambda: NOW)
    kwargs.setdefault("pick", lambda options: options[0])
    return intent_route.IntentRouter(intent_route.load_grammar(), ha=actions, **kwargs), fake


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_real_clip_is_answered_without_the_brain(self):
        r, _ = router()
        route = await r.route("st", "Тоже неплохо. А что ты умеешь?")
        self.assertEqual((route.kind, route.intent, route.source, route.listen), ("reply", "assistant.capabilities", "grammar", False))
        self.assertTrue(route.text.startswith("Доступно:"), route.text)
        self.assertNotIn("колонк", route.text)

    async def test_the_misheard_state_question_is_a_state_answer(self):
        r, fake = router()
        route = await r.route("st", "Какой кондиционер включил?", phase="pre", via_quasar=True)
        self.assertEqual((route.kind, route.intent), ("reply", "home.climate_status"))
        self.assertTrue(route.text.startswith("Сейчас включён кондиционер на втором этаже: обогрев, 30 градусов, уже около 20 минут"), route.text)
        self.assertEqual(fake.services, [])

    async def test_a_room_in_the_phrase_narrows_the_answer(self):
        r, _ = router()
        route = await r.route("st", "включён ли кондиционер в гостиной", phase="pre", via_quasar=True)
        self.assertIn("в гостиной", route.text)
        self.assertNotIn("втором", route.text)

    async def test_follow_up_continues_only_an_answer_just_given(self):
        r, _ = router()
        self.assertEqual((await r.route("st", "а ещё")).kind, "none")           # nothing to follow: the old path
        first = await r.route("st", "что ты умеешь")
        r.note_reply("st", first.text)
        more = await r.route("st", "а ещё")
        self.assertEqual(more.kind, "reply")
        self.assertTrue(more.text.startswith("Ещё доступно:"), more.text)
        r.note_reply("st", "Сейчас 14 часов.")
        self.assertEqual((await r.route("st", "что ещё")).kind, "none")         # another line was said in between

    async def test_topic_question(self):
        r, _ = router()
        route = await r.route("st", "что ты умеешь со светом")
        self.assertTrue(route.text.startswith("Со светом доступно:"), route.text)

    async def test_the_bare_noun_phrase_is_shadow_first(self):
        r, fake = router()
        shadow = await r.route("st", "кондиционер на втором этаже", phase="pre", via_quasar=True)
        self.assertEqual(shadow.kind, "none")        # the quasar layer's question stands
        self.assertEqual(fake.gets, 0)               # nothing was read either
        on, _ = router(status_bare="on")
        route = await on.route("st", "кондиционер на втором этаже", phase="pre", via_quasar=True)
        self.assertEqual((route.kind, route.intent), ("reply", "home.climate_bare"))
        self.assertIn("на втором этаже", route.text)

    async def test_the_bare_decision_is_traced_with_its_reason(self):
        import utterance_trace
        r, _ = router()
        trace = utterance_trace.start("hook", "кондиционер на втором этаже")
        await r.route("quasar", "кондиционер на втором этаже", phase="pre", via_quasar=True)
        self.assertEqual(trace.data["intent"], "home.climate_bare")
        self.assertIn("AISIS_STATUS_BARE", trace.data["declined"])

    async def test_without_home_actions_the_status_is_the_old_paths(self):
        r, _ = router(ha=False)
        self.assertEqual((await r.route("st", "какой кондиционер включён", phase="pre", via_quasar=True)).kind, "none")

    async def test_the_capabilities_answer_still_works_without_home_actions(self):
        r, _ = router(ha=False)
        route = await r.route("st", "что ты умеешь")
        self.assertEqual(route.kind, "reply")
        self.assertNotIn("свет", route.text)

    def test_a_decision_of_the_status_intent_is_adapter_executable(self):
        r, _ = router()
        status = r.classify("какой кондиционер включён", "pre")
        self.assertTrue(r.executable(status, via_quasar=True))
        self.assertIn("ha.climate_status", intent_route.ADAPTER_EXECUTORS)
        self.assertIn("skill.capabilities", intent_route.ADAPTER_EXECUTORS)

    async def test_nothing_in_the_status_path_can_start_a_unit(self):
        r, fake = router()
        for text in ("какой кондиционер включён", "в каком режиме кондиционер", "что с кондиционером", "включён ли кондиционер"):
            await r.route("st", text, phase="pre", via_quasar=True)
        self.assertEqual(fake.services, [])

    async def test_an_old_cache_is_not_served_as_a_fresh_state(self):
        r, fake = router()
        await r.route("st", "какой кондиционер включён", phase="pre", via_quasar=True)
        for entity in fake.states:
            if entity["entity_id"] == "climate.kondei_2":
                entity["state"] = "off"
                entity["last_changed"] = (NOW - timedelta(minutes=1)).isoformat()
        route = await r.route("st", "какой кондиционер включён", phase="pre", via_quasar=True)
        self.assertNotIn("втором этаже: обогрев", route.text)
        self.assertEqual(route.text, "Сейчас все кондиционеры выключены.")


if __name__ == "__main__":
    unittest.main()
