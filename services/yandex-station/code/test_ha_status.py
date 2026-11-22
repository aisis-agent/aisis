"""HaActions read-only answers: `ha.climate_status` and `rooms_with` (spec 150). Nothing here may ever call a service."""
import unittest
from datetime import datetime, timedelta, timezone

import home_status
from ha_actions import HaActions
from test_ha_actions import FakeHa, climate, light

NOW = datetime(2026, 11, 22, 12, 30, tzinfo=timezone.utc)


def stamped(entity, **ago):
    entity["last_changed"] = (NOW - timedelta(**ago)).isoformat()
    return entity


def states(upstairs="heat", living="off", **upstairs_ago):
    return [
        stamped(climate("climate.kondei_2", state=upstairs, temperature=30, current_temperature=None), **(upstairs_ago or {"minutes": 20})),
        stamped(climate("climate.kondei_1", state=living, temperature=30, current_temperature=19.8), hours=2),
        light("light.living_lamp", ["hs"], state="on"),
        light("light.up_lamp", ["rgb"]),
        light("light.k1se_85b6_light", ["onoff"], state="on"),
        {"entity_id": "sensor.noise", "state": "1", "attributes": {}},
    ]


def actions(fake, **kwargs):
    return HaActions(fake.get, fake.post, wall=lambda: NOW, pick=lambda options: options[0], **kwargs)


class ClimateStatusTests(unittest.IsolatedAsyncioTestCase):
    async def status(self, fake=None, args=None):
        fake = fake or FakeHa(states())
        reply = await actions(fake).run("ha.climate_status", args or {})
        self.assertEqual(fake.services, [], "a status question never calls a service")
        return reply

    async def test_the_owner_example(self):
        self.assertEqual(await self.status(),
                         "Сейчас включён кондиционер на втором этаже: обогрев, 30 градусов, уже около 20 минут. Кондиционер в гостиной выключен.")

    async def test_both_on(self):
        fake = FakeHa(states(living="heat"))
        text = await self.status(fake)
        self.assertIn("на втором этаже", text)
        self.assertIn("в гостиной", text)
        self.assertIn("около 20 минут", text)
        self.assertIn("около 2 часов", text)

    async def test_a_room_narrows_the_answer(self):
        self.assertEqual(await self.status(args={"room": "upstairs"}),
                         "Сейчас включён кондиционер на втором этаже: обогрев, 30 градусов, уже около 20 минут.")
        self.assertEqual(await self.status(args={"room": "living"}), "Кондиционер в гостиной выключен уже около 2 часов.")

    async def test_a_room_without_a_unit(self):
        self.assertEqual(await self.status(args={"room": "kitchen"}), "В комнате «Кухня» нет кондиционера.")

    async def test_an_unknown_room(self):
        self.assertEqual(await self.status(args={"room": "attic"}), "Не знаю такую комнату.")

    async def test_aspects(self):
        self.assertEqual(await self.status(args={"aspect": "temp", "room": "living"}),
                         "Кондиционер в гостиной выключен, в комнате 19,8 градуса.")
        self.assertEqual(await self.status(args={"aspect": "mode", "room": "upstairs"}),
                         "Кондиционер на втором этаже работает в режиме «обогрев».")
        self.assertEqual(await self.status(args={"aspect": "since", "room": "upstairs"}), "Кондиционер на втором этаже включён около 20 минут.")

    async def test_the_state_is_read_fresh_not_from_the_adapters_cache(self):
        fake = FakeHa(states())
        ha = actions(fake)
        self.assertIn("Кондиционер в гостиной выключен", await ha.run("ha.climate_status", {}))
        for entity in fake.states:
            if entity["entity_id"] == "climate.kondei_1":
                entity["state"] = "cool"
        self.assertIn("охлаждение", await ha.run("ha.climate_status", {}))
        self.assertEqual(fake.gets, 2)

    async def test_home_assistant_unreachable_says_so_in_one_line(self):
        class Down(FakeHa):
            async def get(self, path):
                raise OSError("down")

        self.assertEqual(await self.status(Down(states())), home_status.STATUS_UNAVAILABLE)

    async def test_an_unreadable_answer_says_so_in_one_line(self):
        class Garbage(FakeHa):
            async def get(self, path):
                return {"message": "401"}

        self.assertEqual(await self.status(Garbage(states())), home_status.STATUS_UNAVAILABLE)

    async def test_a_missing_or_garbled_last_changed_is_left_out_of_the_sentence(self):
        for bad in (None, "yesterday", "2026-11-22T12:00:00"):
            with self.subTest(last_changed=bad):
                items = states()
                items[0]["last_changed"] = bad
                items[1]["state"] = "off"
                text = await self.status(FakeHa(items), {"room": "upstairs"})
                self.assertEqual(text, "Сейчас включён кондиционер на втором этаже: обогрев, 30 градусов.")

    async def test_no_climate_entity_at_all(self):
        self.assertEqual(await self.status(FakeHa([light("light.living_lamp", ["hs"])])), home_status.NO_UNITS)

    async def test_an_entity_without_a_state_is_not_read_and_never_reported_as_on(self):
        items = states(living="heat")
        del items[1]["state"]
        text = await self.status(FakeHa(items), {})
        self.assertIn("Кондиционер в гостиной сейчас не отвечает.", text)
        self.assertNotIn("Кондиционер в гостиной включён", text)

    async def test_a_unit_with_no_known_room_is_listed_last(self):
        items = states(living="heat")
        items.append(stamped(climate("climate.kondei_x", state="heat", temperature=21, current_temperature=None), minutes=5))
        text = await self.status(FakeHa(items), {})
        self.assertTrue(text.endswith("Сейчас включён кондиционер: обогрев, 21 градус, уже около 5 минут."), text)

    async def test_a_dead_unit_is_not_reported_as_off(self):
        items = states(upstairs="unavailable")
        text = await self.status(FakeHa(items), {})
        self.assertIn("Кондиционер на втором этаже сейчас не отвечает.", text)
        self.assertIn("Кондиционер в гостиной выключен", text)

    async def test_the_trace_names_the_room_and_the_unit_count(self):
        import utterance_trace
        trace = utterance_trace.start("turn", "какой кондиционер включён", "st")
        await self.status(args={"room": "upstairs"})
        self.assertEqual((trace.data.get("status_units"), trace.data.get("room")), (1, "Второй этаж"))


class RoomsWithTests(unittest.IsolatedAsyncioTestCase):
    async def test_rooms_with_lights_skip_equipment_leds_and_dead_entities(self):
        fake = FakeHa(states())
        ha = actions(fake)
        found = await ha.rooms_with("light")
        self.assertEqual(found, ("Гостиная", "Второй этаж"))   # the printer's LED is not «свет»; the home's own order
        self.assertEqual(await ha.rooms_with("climate"), ("Гостиная", "Второй этаж"))
        self.assertEqual(fake.services, [])

    async def test_an_unavailable_light_is_not_a_room_with_light(self):
        items = states()
        items[3]["state"] = "unavailable"
        self.assertEqual(await actions(FakeHa(items)).rooms_with("light"), ("Гостиная",))

    async def test_a_failing_read_raises_for_the_caller_to_leave_the_home_out(self):
        class Down(FakeHa):
            async def get(self, path):
                raise OSError("down")

        with self.assertRaises(OSError):
            await actions(Down(states())).rooms_with("light")


if __name__ == "__main__":
    unittest.main()
