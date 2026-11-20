import asyncio
import copy
import json
import re
import unittest
from unittest import mock

import ha_actions
from ha_actions import HaActions


def climate(entity_id, state="cool", **attrs):
    base = {"hvac_modes": ["off", "heat", "cool", "dry", "fan_only", "auto"], "min_temp": 16, "max_temp": 30,
            "current_temperature": 24, "temperature": 24}
    base.update(attrs)
    return {"entity_id": entity_id, "state": state, "attributes": base}


def light(entity_id, modes, state="off"):
    return {"entity_id": entity_id, "state": state, "attributes": {"supported_color_modes": modes}}


STATES = [
    climate("climate.kondei_1"),
    climate("climate.kondei_2", state="off", current_temperature=20, temperature=22),
    light("light.living_lamp", ["hs", "color_temp"]),
    light("light.living_strip", ["color_temp"]),
    light("light.living_plug", ["onoff"]),
    light("light.up_lamp", ["rgb"]),
    light("light.kitchen_lamp", ["xy", "color_temp"]),
    light("light.bedroom_lamp", ["color_temp"]),
    light("light.studio_plug", ["onoff"]),
    {"entity_id": "sensor.noise", "state": "1", "attributes": {}},
]
AREA_ENTITIES = {
    "Гостиная": ["climate.kondei_1", "light.living_lamp", "light.living_strip", "light.living_plug", "sensor.noise"],
    "Второй этаж": ["climate.kondei_2", "light.up_lamp"],
    "Кухня": ["light.kitchen_lamp"],
    "Спальня": ["light.bedroom_lamp"],
    "Студия": ["light.studio_plug"],
    "Прихожая": [],
}
# One player per station, named after its device id, in the HA area of the room the speaker stands in (the quasar layer's
# speaker-room fixture): the AISIS player of the Studio speaker wins over the Yandex integration's one in another area.
LIVING, STUDIO, BEDROOM, SECOND_FLOOR, CORNER = "M0011HM00Z86HG", "M00ZRW3008RAJG", "M10DST310HXNPK", "M0FLOOR2", "M0CORNER0"
PLAYER_AREAS = {
    "media_player.yandex_station_m0011hm00z86hg": "Гостиная",
    "media_player.aisis_speaker_m00zrw3008rajg": "Студия",
    "media_player.yandex_station_m00zrw3008rajg": "Кухня",
    "media_player.yandex_station_m10dst310hxnpk": "Спальня",
    "media_player.yandex_station_m0floor2": "Второй этаж",
    "media_player.yandex_station_m0corner0": "Угол",
}


def registry_tsv():
    """Tab-separated entity id, friendly name, area: for every entity that has a device (the registry template's answer)."""
    areas = dict(PLAYER_AREAS)
    for area, ids in AREA_ENTITIES.items():
        areas.update({entity_id: area for entity_id in ids})
    return "".join(f"{entity_id}\t{entity_id}\t{area}\n" for entity_id, area in areas.items())


class Now:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class FakeHa:
    def __init__(self, states=None, service_status=200, template_as_text=True, outage=False, apply=True, lag=0, unreadable=False, stale=False):
        self.states = copy.deepcopy(STATES if states is None else states)
        self.service_status = service_status
        self.template_as_text = template_as_text
        self.outage = outage            # hacli answers every template with its cached registry TSV (status 200)
        self.apply = apply              # the unit takes a command Home Assistant accepted (False: accepted, the device does not change)
        self.lag = lag                  # reads of one entity that still show its old state after a command
        self.unreadable = unreadable    # reading one entity fails
        self.stale = stale              # reading one entity answers hacli's old cache (X-Aisis-Cache: stale, marked `_stale` by the adapter)
        self.hidden: dict[str, list] = {}
        self.gets = 0
        self.entity_gets = 0
        self.template_posts = 0
        self.templates: list[str] = []
        self.services: list[tuple[str, dict]] = []

    async def get(self, path):
        if path.startswith("/api/states/"):
            self.entity_gets += 1
            if self.unreadable:
                raise OSError("unreadable")
            entity_id = path.removeprefix("/api/states/")
            hidden = self.hidden.get(entity_id)
            if hidden and hidden[0] > 0:
                hidden[0] -= 1
                return copy.deepcopy(hidden[1])
            seen = copy.deepcopy(next(s for s in self.states if s["entity_id"] == entity_id))
            return {**seen, "_stale": True} if self.stale else seen
        assert path == "/api/states"
        self.gets += 1
        return copy.deepcopy(self.states)      # a JSON answer, not the fake's own dicts: what the executor caches must not change the device

    def _apply(self, path, body):
        entity = next(s for s in self.states if s["entity_id"] == body["entity_id"])
        if self.lag:
            self.hidden.setdefault(body["entity_id"], [self.lag, copy.deepcopy(entity)])
        if path.endswith("climate/set_hvac_mode"):
            entity["state"] = body["hvac_mode"]
        elif path.endswith("climate/set_temperature"):
            entity["attributes"]["temperature"] = body["temperature"]
            if "hvac_mode" in body:
                entity["state"] = body["hvac_mode"]

    async def post(self, path, body):
        if path == "/api/template":
            self.template_posts += 1
            if self.outage or "area_entities" not in body["template"]:
                return 200, registry_tsv()
            self.templates.append(body["template"])
            area = json.loads(re.fullmatch(r"\{\{ area_entities\((.*)\) \| tojson \}\}", body["template"], re.DOTALL).group(1))
            ids = AREA_ENTITIES.get(area, [])
            return 200, (json.dumps(ids) if self.template_as_text else ids)
        self.services.append((path, body))
        if self.service_status == 200 and self.apply and path.startswith("/api/services/climate/"):
            self._apply(path, body)
        return self.service_status, None


class LivingRoomActions(HaActions):
    """HaActions as the commands of the living-room speaker reach it (an explicit `station` still wins)."""

    async def run(self, executor, args, station=LIVING):
        return await super().run(executor, args, station)


class Base(unittest.IsolatedAsyncioTestCase):
    def make(self, **kw):
        self.ha = FakeHa(**kw)
        self.now = Now()
        self.slept: list[float] = []
        self.actions = LivingRoomActions(self.ha.get, self.ha.post, ttl=60.0, clock=self.now, sleep=self.sleep)
        return self.actions

    async def sleep(self, seconds):
        self.slept.append(seconds)

    def svc(self):
        return [(p.removeprefix("/api/services/"), b) for p, b in self.ha.services]


class Climate(Base):
    async def test_mode_and_temperature_default_room(self):
        a = self.make()
        reply = await a.run("ha.climate", {"room": None, "mode": "heat", "temperature": 30})
        self.assertEqual(reply, "Обогрев, 30 градусов.")
        self.assertEqual(self.svc(), [
            ("climate/set_hvac_mode", {"entity_id": "climate.kondei_1", "hvac_mode": "heat"}),
            ("climate/set_temperature", {"entity_id": "climate.kondei_1", "temperature": 30}),
        ])

    async def test_mode_only(self):
        a = self.make()
        reply = await a.run("ha.climate", {"room": "living", "mode": "fan", "temperature": None})
        self.assertEqual(reply, "Включаю вентиляцию.")
        self.assertEqual(self.svc(), [("climate/set_hvac_mode", {"entity_id": "climate.kondei_1", "hvac_mode": "fan_only"})])

    async def test_temperature_only_running_unit_keeps_mode(self):
        a = self.make()
        reply = await a.run("ha.climate", {"room": "living", "mode": None, "temperature": 22})
        self.assertEqual(reply, "Ставлю 22 градуса.")
        self.assertEqual(self.svc(), [("climate/set_temperature", {"entity_id": "climate.kondei_1", "temperature": 22})])

    async def test_temperature_only_on_off_unit_picks_mode_by_sign(self):
        a = self.make()
        await a.run("ha.climate", {"room": "upstairs", "mode": None, "temperature": 26})
        self.assertEqual(self.svc()[0], ("climate/set_hvac_mode", {"entity_id": "climate.kondei_2", "hvac_mode": "heat"}))
        self.assertEqual(self.svc()[1][0], "climate/set_temperature")
        a = self.make()
        await a.run("ha.climate", {"room": "upstairs", "mode": None, "temperature": 18})
        self.assertEqual(self.svc()[0][1]["hvac_mode"], "cool")

    async def test_off_unit_without_preferred_mode_uses_first_non_off(self):
        a = self.make(states=[climate("climate.kondei_1", state="off", hvac_modes=["off", "auto", "dry"])])
        await a.run("ha.climate", {"room": "living", "mode": None, "temperature": 26})
        self.assertEqual(self.svc()[0][1]["hvac_mode"], "auto")

    async def test_clamping(self):
        a = self.make()
        reply = await a.run("ha.climate", {"room": "living", "mode": "heat", "temperature": 99})
        self.assertEqual(reply, "Обогрев, 30 градусов.")
        self.assertEqual(self.svc()[1][1]["temperature"], 30)
        a = self.make()
        reply = await a.run("ha.climate", {"room": "living", "mode": None, "temperature": 5})
        self.assertEqual(reply, "Ставлю 16 градусов.")

    async def test_fractional_temperature(self):
        a = self.make()
        reply = await a.run("ha.climate", {"room": "living", "mode": None, "temperature": 22.5})
        self.assertEqual(reply, "Ставлю 22,5 градуса.")

    async def test_unsupported_mode(self):
        a = self.make(states=[climate("climate.kondei_1", hvac_modes=["off", "cool"])])
        reply = await a.run("ha.climate", {"room": "living", "mode": "heat", "temperature": None})
        self.assertEqual(reply, ha_actions.REPLY_MODE_UNSUPPORTED)
        self.assertEqual(self.ha.services, [])

    async def test_no_climate_never_falls_back_to_other_room(self):
        a = self.make()
        reply = await a.run("ha.climate", {"room": "kitchen", "mode": "cool", "temperature": 20})
        self.assertEqual(reply, "В комнате «Кухня» нет кондиционера.")
        self.assertEqual(self.ha.services, [])

    async def test_all_climates_in_area(self):
        extra = climate("climate.second")
        states = copy.deepcopy(STATES) + [extra]
        AREA_ENTITIES["Гостиная"].append("climate.second")
        self.addCleanup(AREA_ENTITIES["Гостиная"].remove, "climate.second")
        a = self.make(states=states)
        await a.run("ha.climate", {"room": "living", "mode": "cool", "temperature": 20})
        targets = [b["entity_id"] for _, b in self.svc() if _ == "climate/set_temperature"]
        self.assertEqual(targets, ["climate.kondei_1", "climate.second"])

    async def test_unavailable_climate(self):
        a = self.make(states=[climate("climate.kondei_1", state="unavailable")])
        reply = await a.run("ha.climate", {"room": "living", "mode": "cool", "temperature": None})
        self.assertEqual(reply, ha_actions.REPLY_CLIMATE_UNAVAILABLE)

    async def test_unknown_mode_is_not_understood(self):
        a = self.make()
        self.assertEqual(await a.run("ha.climate", {"room": None, "mode": "boil", "temperature": None}), ha_actions.REPLY_UNKNOWN_COMMAND)
        self.assertEqual(self.ha.services, [])


class ClimateDelta(Base):
    async def test_default_one_degree_up(self):
        a = self.make()
        reply = await a.run("ha.climate_delta", {"room": None, "direction": 1, "delta": None})
        self.assertEqual(reply, "Делаю теплее, 25 градусов.")
        self.assertEqual(self.svc(), [("climate/set_temperature", {"entity_id": "climate.kondei_1", "temperature": 25})])

    async def test_down_with_amount_and_unit(self):
        a = self.make()
        for unit in ("", "градуса"):
            self.ha.services.clear()
            reply = await a.run("ha.climate_delta",
                                {"room": "living", "direction": -1, "delta": {"amount": 3, "unit": unit}})
            self.assertEqual(reply, "Делаю прохладнее, 21 градус.")
            self.ha.states[0]["attributes"]["temperature"] = 24
            a._states = None

    async def test_repeated_commands_chain_from_new_setpoint(self):
        a = self.make()
        await a.run("ha.climate_delta", {"room": None, "direction": 1, "delta": None})
        reply = await a.run("ha.climate_delta", {"room": None, "direction": 1, "delta": None})
        self.assertEqual(reply, "Делаю теплее, 26 градусов.")

    async def test_clamped_at_limit(self):
        a = self.make(states=[climate("climate.kondei_1", temperature=30)])
        reply = await a.run("ha.climate_delta", {"room": None, "direction": 1, "delta": {"amount": 5, "unit": ""}})
        self.assertEqual(reply, "Делаю теплее, 30 градусов.")

    async def test_off_unit_wakes_by_direction(self):
        a = self.make()
        await a.run("ha.climate_delta", {"room": "upstairs", "direction": 1, "delta": None})
        self.assertEqual(self.svc()[0], ("climate/set_hvac_mode", {"entity_id": "climate.kondei_2", "hvac_mode": "heat"}))
        self.assertEqual(self.svc()[1][1]["temperature"], 23)

    async def test_missing_setpoint_falls_back_to_current_then_says_unknown(self):
        a = self.make(states=[climate("climate.kondei_1", temperature=None, current_temperature=21)])
        self.assertEqual(await a.run("ha.climate_delta", {"room": None, "direction": 1, "delta": None}),
                         "Делаю теплее, 22 градуса.")
        a = self.make(states=[climate("climate.kondei_1", temperature=None, current_temperature=None)])
        self.assertEqual(await a.run("ha.climate_delta", {"room": None, "direction": 1, "delta": None}),
                         ha_actions.REPLY_NO_CURRENT_TEMP)

    async def test_foreign_unit_rejected(self):
        a = self.make()
        reply = await a.run("ha.climate_delta", {"room": None, "direction": 1, "delta": {"amount": 2, "unit": "процент"}})
        self.assertEqual(reply, ha_actions.REPLY_BAD_DELTA)
        self.assertEqual(self.ha.services, [])


class AirConditionerOff(Base):
    """An air conditioner that is off and is asked for a mode, a setpoint or just «включи» is switched on (the owner, tg#31285: if it is
    off, it has to be turned on), and the answer says what the unit reports afterwards, not what was asked."""

    def unit(self, **attrs):
        """kondei_1 in the living room: off, 20 degrees in the room, setpoint 22."""
        base = {"current_temperature": 20, "temperature": 22}
        base.update(attrs)
        return [climate("climate.kondei_1", state="off", **base)]

    async def say(self, args, **kw):
        a = self.make(states=self.unit(**kw.pop("attrs", {})), **kw)
        return await a.run("ha.climate", {"room": "living", "mode": None, "temperature": None, **args})

    async def test_a_mode_phrase_switches_an_off_unit_on_in_that_mode(self):
        reply = await self.say({"mode": "heat"})
        self.assertEqual(self.svc(), [("climate/set_hvac_mode", {"entity_id": "climate.kondei_1", "hvac_mode": "heat"})])
        self.assertEqual(reply, "Кондиционер включён на обогрев, 22 градуса.")

    async def test_a_setpoint_phrase_switches_an_off_unit_on_then_sets_it(self):
        reply = await self.say({"temperature": 26})
        self.assertEqual(self.svc(), [("climate/set_hvac_mode", {"entity_id": "climate.kondei_1", "hvac_mode": "heat"}),
                                      ("climate/set_temperature", {"entity_id": "climate.kondei_1", "temperature": 26})])
        self.assertEqual(reply, "Кондиционер включён на обогрев, 26 градусов.")
        self.assertEqual(await self.say({"temperature": 18}), "Кондиционер включён на охлаждение, 18 градусов.")
        self.assertEqual(self.svc()[0][1]["hvac_mode"], "cool")

    async def test_a_setpoint_near_the_room_temperature_starts_the_unit_in_the_mode_it_ran_last(self):
        for last in ("heat", "cool"):
            reply = await self.say({"temperature": 23}, attrs={"current_temperature": 23, "last_hvac_mode": last})
            self.assertEqual(self.svc()[0], ("climate/set_hvac_mode", {"entity_id": "climate.kondei_1", "hvac_mode": last}), last)
            self.assertEqual(reply, "Кондиционер включён на %s, 23 градуса." % {"heat": "обогрев", "cool": "охлаждение"}[last])

    async def test_a_unit_that_knows_neither_the_room_temperature_nor_its_last_mode_starts_automatic(self):
        reply = await self.say({"temperature": 25}, attrs={"current_temperature": None})
        self.assertEqual(self.svc()[0], ("climate/set_hvac_mode", {"entity_id": "climate.kondei_1", "hvac_mode": "auto"}))
        self.assertEqual(reply, "Кондиционер включён в автоматическом режиме, 25 градусов.")

    async def test_a_delta_phrase_switches_an_off_unit_on(self):
        a = self.make(states=self.unit())
        reply = await a.run("ha.climate_delta", {"room": "living", "direction": 1, "delta": None})
        self.assertEqual(self.svc(), [("climate/set_hvac_mode", {"entity_id": "climate.kondei_1", "hvac_mode": "heat"}),
                                      ("climate/set_temperature", {"entity_id": "climate.kondei_1", "temperature": 23})])
        self.assertEqual(reply, "Кондиционер включён на обогрев, 23 градуса.")

    async def test_a_plain_include_switches_an_off_unit_on_in_its_last_mode(self):
        reply = await self.say({}, attrs={"last_hvac_mode": "cool"})
        self.assertEqual(self.svc(), [("climate/set_hvac_mode", {"entity_id": "climate.kondei_1", "hvac_mode": "cool"})])
        self.assertEqual(reply, "Кондиционер включён на охлаждение, 22 градуса.")

    async def test_a_plain_include_of_a_unit_without_a_last_mode_starts_automatic(self):
        reply = await self.say({})
        self.assertEqual(self.svc(), [("climate/set_hvac_mode", {"entity_id": "climate.kondei_1", "hvac_mode": "auto"})])
        self.assertEqual(reply, "Кондиционер включён в автоматическом режиме, 22 градуса.")

    async def test_a_plain_include_of_a_running_unit_calls_nothing(self):
        a = self.make()
        reply = await a.run("ha.climate", {"room": "living", "mode": None, "temperature": None})
        self.assertEqual(self.ha.services, [])
        self.assertEqual(reply, "Кондиционер уже включён: охлаждение, 24 градуса.")

    async def test_a_stale_cached_state_does_not_hide_an_off_unit(self):
        """The list of states is cached for a minute; the unit was switched off meanwhile: «включи» must read the unit, not the list."""
        self.make(states=self.unit(last_hvac_mode="heat"))
        stale = copy.deepcopy(self.unit(last_hvac_mode="heat"))
        stale[0]["state"] = "cool"

        async def get(path):
            return copy.deepcopy(stale) if path == "/api/states" else await self.ha.get(path)

        a = LivingRoomActions(get, self.ha.post, ttl=60.0, clock=self.now, sleep=self.sleep)
        self.assertEqual(await a.run("ha.climate", {"room": "living", "mode": None, "temperature": None}),
                         "Кондиционер включён на обогрев, 22 градуса.")

    async def test_a_unit_that_turned_unavailable_is_not_commanded(self):
        self.make(states=[climate("climate.kondei_1", state="unavailable")])
        listed = copy.deepcopy(self.unit())

        async def get(path):
            return copy.deepcopy(listed) if path == "/api/states" else await self.ha.get(path)

        a = LivingRoomActions(get, self.ha.post, ttl=60.0, clock=self.now, sleep=self.sleep)
        self.assertEqual(await a.run("ha.climate", {"room": "living", "mode": "heat", "temperature": None}),
                         ha_actions.REPLY_CLIMATE_UNAVAILABLE)
        self.assertEqual(self.ha.services, [])

    async def test_a_state_that_shows_up_late_is_waited_for(self):
        reply = await self.say({"mode": "heat"}, lag=2)
        self.assertEqual(reply, "Кондиционер включён на обогрев, 22 градуса.")
        self.assertEqual(len(self.slept), 2)

    async def test_a_unit_that_does_not_change_is_not_reported_as_switched_on(self):
        reply = await self.say({"mode": "heat"}, apply=False)
        self.assertEqual(reply, ha_actions.REPLY_NOT_CONFIRMED)
        self.assertEqual(len(self.svc()), 1, "the command was sent")

    async def test_an_unreadable_state_is_not_reported_as_done(self):
        self.assertEqual(await self.say({"mode": "heat"}, unreadable=True), ha_actions.REPLY_UNVERIFIED)

    async def test_a_queued_command_is_not_verified(self):
        reply = await self.say({"mode": "heat"}, service_status=202)
        self.assertEqual(reply, ha_actions.REPLY_QUEUED)
        self.assertEqual(self.ha.entity_gets, 1, "only the read before the command")

    async def test_a_running_unit_asked_for_a_mode_keeps_the_old_wording_when_the_state_shows_it(self):
        a = self.make()
        self.assertEqual(await a.run("ha.climate", {"room": "living", "mode": "heat", "temperature": 30}), "Обогрев, 30 градусов.")

    async def test_consecutive_deltas_queued_during_an_outage_add_up(self):
        """hacli serves its old cached setpoint while Home Assistant is down: the fresh read must not undo the queued 25 before the next «теплее»."""
        a = self.make(service_status=202, stale=True)
        for _ in range(2):
            self.assertEqual(await a.run("ha.climate_delta", {"room": "living", "direction": 1, "delta": None}), ha_actions.REPLY_QUEUED)
        self.assertEqual([body["temperature"] for path, body in self.ha.services if path.endswith("set_temperature")], [25, 26])

    async def test_hacli_serving_its_old_cache_does_not_authorise_already_on(self):
        """The old cache says the unit runs while it was switched off: that is no reading, «включи» sends the command and verifies nothing."""
        self.make(states=self.unit(last_hvac_mode="heat"), stale=True)
        cached = copy.deepcopy(self.unit(last_hvac_mode="heat"))
        cached[0]["state"] = "cool"

        async def old_cache(path):
            return {**cached[0], "_stale": True} if path.startswith("/api/states/") else cached

        a = LivingRoomActions(old_cache, self.ha.post, ttl=60.0, clock=self.now, sleep=self.sleep)
        self.assertEqual(await a.run("ha.climate", {"room": "living", "mode": None, "temperature": None}), ha_actions.REPLY_UNVERIFIED)
        self.assertEqual(self.svc(), [("climate/set_hvac_mode", {"entity_id": "climate.kondei_1", "hvac_mode": "heat"})])

    async def test_a_setpoint_wakes_a_unit_whose_state_could_not_be_read(self):
        a = self.make(states=[climate("climate.kondei_1", state="cool", temperature=22, current_temperature=20, last_hvac_mode="heat")], unreadable=True)
        await a.run("ha.climate", {"room": "living", "mode": None, "temperature": 26})
        self.assertEqual([name for name, _ in self.svc()], ["climate/set_hvac_mode", "climate/set_temperature"])
        self.ha.services.clear()
        a = self.make(states=[climate("climate.kondei_1", state="cool", temperature=22, current_temperature=20, last_hvac_mode="heat")], unreadable=True)
        await a.run("ha.climate_delta", {"room": "living", "direction": 1, "delta": None})
        self.assertEqual([name for name, _ in self.svc()], ["climate/set_hvac_mode", "climate/set_temperature"])

    async def test_a_setpoint_set_after_the_outage_is_not_overwritten_by_the_queued_one(self):
        """Queued 25 during an outage, Home Assistant recovers, someone sets 22: «теплее» reads the live 22."""
        a = self.make(service_status=202, stale=True)
        await a.run("ha.climate_delta", {"room": "living", "direction": 1, "delta": None})
        self.ha.services.clear()
        self.ha.stale, self.ha.service_status = False, 200
        self.ha.states[0]["attributes"]["temperature"] = 22
        await a.run("ha.climate_delta", {"room": "living", "direction": 1, "delta": None})
        self.assertEqual([body["temperature"] for path, body in self.ha.services if path.endswith("set_temperature")], [23])

    async def test_overlapping_deltas_add_up_when_the_device_reports_late(self):
        async def yielding(seconds):
            await asyncio.sleep(0)

        self.make(lag=2)
        a = LivingRoomActions(self.ha.get, self.ha.post, ttl=60.0, clock=self.now, sleep=yielding)
        replies = await asyncio.gather(*[a.run("ha.climate_delta", {"room": "living", "direction": 1, "delta": None}) for _ in range(2)])
        self.assertEqual([body["temperature"] for path, body in self.ha.services if path.endswith("set_temperature")], [25, 26])
        self.assertTrue(all("градус" in r for r in replies), replies)

    async def test_a_state_that_could_not_be_read_does_not_authorise_already_on(self):
        """The cache says the unit runs, the read fails: «включи» sends the command and says only what could be checked."""
        reply = await self.say({}, unreadable=True)
        self.assertEqual(len(self.svc()), 1, "the command is sent: a guess is not a reason to do nothing")
        self.assertEqual(reply, ha_actions.REPLY_UNVERIFIED)

    async def test_a_missing_setpoint_does_not_confirm_a_setpoint(self):
        """A running unit in fan mode reports no setpoint; an accepted command that changed nothing must not be announced as «ставлю 23»."""
        a = self.make(states=[climate("climate.kondei_1", state="fan_only", temperature=None)], apply=False)
        self.assertEqual(await a.run("ha.climate", {"room": "living", "mode": None, "temperature": 23}), ha_actions.REPLY_NOT_CONFIRMED)



class Lights(Base):
    async def test_colour_named_only_on_colour_lamps(self):
        a = self.make()
        reply = await a.run("ha.light_color", {"color": "red", "room": None})
        self.assertEqual(reply, "Красный свет.")
        self.assertEqual(self.svc(), [("light/turn_on", {"entity_id": "light.living_lamp", "color_name": "red"})])

    async def test_warm_cold_white(self):
        a = self.make()
        await a.run("ha.light_color", {"color": "warm", "room": "living"})
        await a.run("ha.light_color", {"color": "cold", "room": "living"})
        await a.run("ha.light_color", {"color": "white", "room": "living"})
        self.assertEqual(self.svc(), [
            ("light/turn_on", {"entity_id": "light.living_lamp", "color_temp_kelvin": 2700}),
            ("light/turn_on", {"entity_id": "light.living_strip", "color_temp_kelvin": 2700}),
            ("light/turn_on", {"entity_id": "light.living_lamp", "color_temp_kelvin": 6500}),
            ("light/turn_on", {"entity_id": "light.living_strip", "color_temp_kelvin": 6500}),
            ("light/turn_on", {"entity_id": "light.living_lamp", "color_temp_kelvin": 4000}),
            ("light/turn_on", {"entity_id": "light.living_strip", "color_temp_kelvin": 4000}),
        ])

    async def test_white_on_colour_only_lamp_uses_name(self):
        a = self.make()
        await a.run("ha.light_color", {"color": "white", "room": "upstairs"})
        self.assertEqual(self.svc(), [("light/turn_on", {"entity_id": "light.up_lamp", "color_name": "white"})])

    async def test_no_colour_support(self):
        a = self.make()
        self.assertEqual(await a.run("ha.light_color", {"color": "red", "room": "bedroom"}), "Эта лампа не меняет цвет.")
        self.assertEqual(await a.run("ha.light_color", {"color": "blue", "room": "studio"}), "Эта лампа не меняет цвет.")
        self.assertEqual(self.ha.services, [])

    async def test_brightness(self):
        a = self.make()
        reply = await a.run("ha.light_brightness", {"percent": 50, "room": "living"})
        self.assertEqual(reply, "Яркость 50 процентов.")
        self.assertEqual(self.svc(), [
            ("light/turn_on", {"entity_id": "light.living_lamp", "brightness_pct": 50}),
            ("light/turn_on", {"entity_id": "light.living_strip", "brightness_pct": 50}),
        ])
        self.assertEqual(await a.run("ha.light_brightness", {"percent": 1, "room": "living"}), "Яркость 1 процент.")
        self.assertEqual(await a.run("ha.light_brightness", {"percent": 22, "room": "living"}), "Яркость 22 процента.")

    async def test_brightness_onoff_only(self):
        a = self.make()
        self.assertEqual(await a.run("ha.light_brightness", {"percent": 30, "room": "studio"}),
                         "Эта лампа не регулируется по яркости.")

    async def test_brightness_range_checked(self):
        a = self.make()
        for bad in (0, 101, None):
            self.assertEqual(await a.run("ha.light_brightness", {"percent": bad, "room": None}),
                             ha_actions.REPLY_UNKNOWN_COMMAND)

    async def test_no_light_in_area(self):
        a = self.make()
        self.assertEqual(await a.run("ha.light_color", {"color": "red", "room": "hall"}), "В комнате «Прихожая» нет света.")


class Failures(Base):
    async def test_unknown_room_and_executor(self):
        a = self.make()
        self.assertEqual(await a.run("ha.climate", {"room": "garage", "mode": "heat", "temperature": None}),
                         ha_actions.REPLY_UNKNOWN_ROOM)
        self.assertEqual(await a.run("ha.nope", {}), ha_actions.REPLY_UNKNOWN_COMMAND)
        self.assertEqual(self.ha.gets, 0)

    async def test_202_queued(self):
        a = self.make(service_status=202)
        reply = await a.run("ha.light_brightness", {"percent": 40, "room": None})
        self.assertEqual(reply, "Дом перезагружается, команду выполню, когда он вернётся.")

    async def test_400_and_500(self):
        for status in (400, 500):
            a = self.make(service_status=status)
            reply = await a.run("ha.light_color", {"color": "red", "room": None})
            self.assertEqual(reply, "Не удалось выполнить команду умного дома.")

    async def test_transport_errors_never_raise(self):
        async def boom(*_):
            raise OSError("down")

        a = HaActions(boom, boom)
        self.assertEqual(await a.run("ha.climate", {"room": None, "mode": "heat", "temperature": 25}),
                         ha_actions.REPLY_FAILED)

    async def test_states_not_a_list(self):
        a = self.make(states={"oops": 1})
        self.assertEqual(await a.run("ha.climate", {"room": None, "mode": "heat", "temperature": 25}),
                         ha_actions.REPLY_FAILED)

    async def test_template_status_failure(self):
        async def post(path, body):
            return 500, None

        a = LivingRoomActions(FakeHa().get, post)
        self.assertEqual(await a.run("ha.climate", {"room": None, "mode": "heat", "temperature": 25}),
                         ha_actions.REPLY_FAILED)

    async def test_template_list_body_accepted(self):
        a = self.make(template_as_text=False)
        self.assertEqual(await a.run("ha.light_color", {"color": "red", "room": None}), "Красный свет.")


class Cache(Base):
    async def test_ttl(self):
        a = self.make()
        await a.run("ha.light_brightness", {"percent": 10, "room": None})
        await a.run("ha.light_brightness", {"percent": 20, "room": None})
        self.assertEqual((self.ha.gets, self.ha.template_posts), (1, 2))      # the room of the speaker, then the area's entities
        await a.run("ha.light_brightness", {"percent": 20, "room": "kitchen"})
        self.assertEqual((self.ha.gets, self.ha.template_posts), (1, 3))
        self.now.t += 61
        await a.run("ha.light_brightness", {"percent": 30, "room": None})
        self.assertEqual((self.ha.gets, self.ha.template_posts), (2, 5))


class SpeakerRoom(Base):
    """A command that names no room is about the room of the speaker that heard it (Home Assistant's area of the station's player)."""

    async def heard_by(self, station, executor, args):
        return await self.actions.run(executor, args, station)

    async def test_colour_and_brightness_act_in_the_speakers_room_not_the_living_room(self):
        self.make()
        self.assertEqual(await self.heard_by(BEDROOM, "ha.light_color", {"color": "warm", "room": None}), "Тёплый свет.")
        self.assertEqual(self.svc(), [("light/turn_on", {"entity_id": "light.bedroom_lamp", "color_temp_kelvin": 2700})])
        self.ha.services.clear()
        self.assertEqual(await self.heard_by(SECOND_FLOOR, "ha.light_color", {"color": "red", "room": None}), "Красный свет.")
        self.assertEqual(self.svc(), [("light/turn_on", {"entity_id": "light.up_lamp", "color_name": "red"})])
        self.ha.services.clear()
        self.assertEqual(await self.heard_by(STUDIO, "ha.light_brightness", {"percent": 50, "room": None}),
                         "Эта лампа не регулируется по яркости.")          # the Studio lamp, not the living-room ones
        self.assertEqual(self.ha.services, [])

    async def test_the_climate_of_the_speakers_room_and_its_absence(self):
        self.make()
        self.assertEqual(await self.heard_by(SECOND_FLOOR, "ha.climate_delta", {"room": None, "direction": 1, "delta": None}),
                         "Кондиционер включён на обогрев, 23 градуса.")      # kondei_2 is off: the command switches it on
        self.assertEqual([body["entity_id"] for _, body in self.svc()], ["climate.kondei_2", "climate.kondei_2"])
        self.ha.services.clear()
        self.assertEqual(await self.heard_by(BEDROOM, "ha.climate_delta", {"room": None, "direction": 1, "delta": None}),
                         "В комнате «Спальня» нет кондиционера.")
        self.assertEqual(self.ha.services, [])

    async def test_the_own_player_of_the_speaker_decides_its_room(self):
        self.make()
        # the Studio speaker also has a Yandex-integration player in the kitchen: the AISIS player (Студия) wins
        self.assertEqual(await self.heard_by(STUDIO, "ha.light_color", {"color": "red", "room": None}), "Эта лампа не меняет цвет.")
        self.assertEqual(await self.heard_by(CORNER, "ha.light_color", {"color": "red", "room": None}), "В комнате «Угол» нет света.")
        self.assertEqual(self.ha.services, [])

    async def test_a_room_that_was_said_beats_the_speakers_room(self):
        self.make()
        await self.heard_by(BEDROOM, "ha.light_color", {"color": "warm", "room": "living"})
        self.assertEqual([body["entity_id"] for _, body in self.svc()], ["light.living_lamp", "light.living_strip"])

    async def test_a_speaker_whose_room_is_unknown_is_asked_never_guessed(self):
        self.make()
        for station in (None, "", "quasar", "M0UNKNOWN0"):
            reply = await self.heard_by(station, "ha.light_color", {"color": "red", "room": None})
            self.assertEqual(reply, ha_actions.REPLY_WHICH_ROOM, station)
        self.assertEqual(self.ha.services, [])
        self.assertEqual(self.ha.gets, 0)


class Outage(Base):
    async def test_the_whole_command_is_queued_not_only_its_first_call(self):
        a = self.make(service_status=202)
        reply = await a.run("ha.climate", {"room": "living", "mode": "heat", "temperature": 30})
        self.assertEqual(reply, ha_actions.REPLY_QUEUED)
        self.assertEqual(self.svc(), [
            ("climate/set_hvac_mode", {"entity_id": "climate.kondei_1", "hvac_mode": "heat"}),
            ("climate/set_temperature", {"entity_id": "climate.kondei_1", "temperature": 30}),
        ])
        self.ha.services.clear()
        self.assertEqual(await a.run("ha.light_brightness", {"percent": 50, "room": "living"}), ha_actions.REPLY_QUEUED)
        self.assertEqual([body["entity_id"] for _, body in self.svc()], ["light.living_lamp", "light.living_strip"])

    async def test_the_cached_registry_hacli_serves_during_an_outage_still_reaches_the_queue(self):
        a = self.make(service_status=202, outage=True)      # every template answers with the tab-separated registry, status 200
        reply = await a.run("ha.light_brightness", {"percent": 50, "room": None})
        self.assertEqual(reply, ha_actions.REPLY_QUEUED)
        self.assertEqual([body["entity_id"] for _, body in self.svc()], ["light.living_lamp", "light.living_strip"])

    async def test_a_failure_after_a_queued_call_is_still_a_failure(self):
        a = self.make()
        statuses = iter([202, 500])

        async def post(path, body):
            if path == "/api/template":
                return await self.ha.post(path, body)
            return next(statuses), None

        a = LivingRoomActions(self.ha.get, post, ttl=60.0, clock=self.now)
        self.assertEqual(await a.run("ha.light_brightness", {"percent": 50, "room": "living"}), ha_actions.REPLY_FAILED)


class EquipmentLights(Base):
    JUNK = [light("light.k1se_85b6_light", ["brightness"]), light("light.snapmaker_u1_led_cavity_led", ["brightness"]),
            light("light.studio_dim", ["brightness"])]

    def studio(self, ids):
        patcher = mock.patch.dict(AREA_ENTITIES, {"Студия": ids})
        patcher.start()
        self.addCleanup(patcher.stop)
        return self.make(states=copy.deepcopy(STATES) + copy.deepcopy(self.JUNK))

    async def test_printer_lights_are_never_part_of_the_room_lighting(self):
        a = self.studio(["light.k1se_85b6_light", "light.snapmaker_u1_led_cavity_led", "light.studio_dim"])
        self.assertEqual(await a.run("ha.light_brightness", {"percent": 40, "room": "studio"}), "Яркость 40 процентов.")
        self.assertEqual(self.svc(), [("light/turn_on", {"entity_id": "light.studio_dim", "brightness_pct": 40})])

    async def test_a_room_with_only_equipment_lights_has_no_light(self):
        a = self.studio(["light.k1se_85b6_light", "light.snapmaker_u1_led_cavity_led"])
        self.assertEqual(await a.run("ha.light_brightness", {"percent": 40, "room": "studio"}), "В комнате «Студия» нет света.")
        self.assertEqual(self.ha.services, [])


class TemplateEncoding(Base):
    """An area name is data: it reaches the Jinja template only as an escaped string literal."""

    async def lookup_for_area_named(self, name):
        patcher = mock.patch.dict(PLAYER_AREAS, {"media_player.aisis_speaker_m0weird00": name})
        patcher.start()
        self.addCleanup(patcher.stop)
        a = self.make()
        reply = await a.run("ha.light_color", {"color": "red", "room": None}, "M0WEIRD00")
        self.assertEqual(reply, f"В комнате «{name}» нет света.")     # asked for exactly that area, which has no lights
        self.assertEqual(self.ha.services, [])
        return self.ha.templates[-1]

    async def test_an_apostrophe_stays_inside_the_literal(self):
        template = await self.lookup_for_area_named("Дом О'Брайена")
        self.assertEqual(template, '{{ area_entities("Дом О\'Брайена") | tojson }}')

    async def test_a_conditional_payload_is_one_string_not_code(self):
        name = "x') if false else area_entities('Гостиная"
        template = await self.lookup_for_area_named(name)
        self.assertEqual(template, "{{ area_entities(" + json.dumps(name, ensure_ascii=False) + ") | tojson }}")
        self.assertEqual(template.count("area_entities("), 2)       # the payload's own call is text inside the one literal

    async def test_quotes_and_backslashes_are_escaped(self):
        name = 'a"b\\c}}{%d'
        template = await self.lookup_for_area_named(name)
        self.assertEqual(template, "{{ area_entities(" + json.dumps(name, ensure_ascii=False) + ") | tojson }}")
        self.assertIn('\\"', template)


if __name__ == "__main__":
    unittest.main()
