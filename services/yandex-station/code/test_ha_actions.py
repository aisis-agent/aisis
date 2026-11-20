import copy
import json
import re
import unittest
from unittest import mock

import ha_actions
import utterance_trace
from ha_actions import HaActions, HaReply


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
    def __init__(self, states=None, service_status=200, template_as_text=True, outage=False, service_reply=None):
        self.states = copy.deepcopy(STATES if states is None else states)
        self.service_status = service_status
        self.template_as_text = template_as_text
        self.outage = outage            # hacli answers every template with its cached registry TSV (status 200)
        self.service_reply = service_reply   # what hacli answers a service call (HaReply with its verdict); default: the plain status
        self.gets = 0
        self.template_posts = 0
        self.templates: list[str] = []
        self.services: list[tuple[str, dict]] = []

    async def get(self, path):
        assert path == "/api/states"
        self.gets += 1
        return self.states

    async def post(self, path, body):
        if path == "/api/template":
            self.template_posts += 1
            if self.outage or "area_entities" not in body["template"]:
                return HaReply(200, registry_tsv())
            self.templates.append(body["template"])
            area = json.loads(re.fullmatch(r"\{\{ area_entities\((.*)\) \| tojson \}\}", body["template"], re.DOTALL).group(1))
            ids = AREA_ENTITIES.get(area, [])
            return HaReply(200, json.dumps(ids) if self.template_as_text else ids)
        self.services.append((path, body))
        return self.service_reply or HaReply(self.service_status)


class LivingRoomActions(HaActions):
    """HaActions as the commands of the living-room speaker reach it (an explicit `station` still wins)."""

    async def run(self, executor, args, station=LIVING):
        return await super().run(executor, args, station)


class Base(unittest.IsolatedAsyncioTestCase):
    def make(self, **kw):
        self.ha = FakeHa(**kw)
        self.now = Now()
        self.actions = LivingRoomActions(self.ha.get, self.ha.post, ttl=60.0, clock=self.now)
        return self.actions

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

    async def test_empty_command(self):
        a = self.make()
        self.assertEqual(await a.run("ha.climate", {"room": None, "mode": None, "temperature": None}),
                         ha_actions.REPLY_UNKNOWN_COMMAND)


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

    async def test_202_without_evidence_is_neutral(self):
        a = self.make(service_status=202)
        reply = await a.run("ha.light_brightness", {"percent": 40, "room": None})
        self.assertEqual(reply, ha_actions.REPLY_QUEUED_UNKNOWN)
        self.assertNotIn("перезагруж", reply)

    async def test_400_and_500(self):
        for status, expected in ((400, ha_actions.REPLY_REJECTED), (500, ha_actions.REPLY_FAILED)):
            a = self.make(service_status=status)
            reply = await a.run("ha.light_color", {"color": "red", "room": None})
            self.assertEqual(reply, expected)

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
            return HaReply(500)

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
                         "Делаю теплее, 23 градуса.")
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
        a = self.make(service_reply=HaReply(202, None, None, "a1", "refused", "refused"))
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
        a = self.make(service_reply=HaReply(202, None, None, "a1", "refused", "refused"), outage=True)      # every template answers with the tab-separated registry, status 200
        reply = await a.run("ha.light_brightness", {"percent": 50, "room": None})
        self.assertEqual(reply, ha_actions.REPLY_QUEUED)
        self.assertEqual([body["entity_id"] for _, body in self.svc()], ["light.living_lamp", "light.living_strip"])

    async def test_a_failure_after_a_queued_call_is_still_a_failure(self):
        a = self.make()
        statuses = iter([202, 500])

        async def post(path, body):
            if path == "/api/template":
                return await self.ha.post(path, body)
            return HaReply(next(statuses))

        a = LivingRoomActions(self.ha.get, post, ttl=60.0, clock=self.now)
        self.assertEqual(await a.run("ha.light_brightness", {"percent": 50, "room": "living"}),
                         f"{ha_actions.REPLY_FAILED} {ha_actions.REPLY_QUEUED_UNKNOWN}")      # the queued lamp still changes after recovery: say so

    async def test_a_failure_before_a_queued_call_still_reports_the_queued_work(self):
        self.make()
        statuses = iter([500, 202])

        async def post(path, body):
            if path == "/api/template":
                return await self.ha.post(path, body)
            return HaReply(next(statuses))

        a = LivingRoomActions(self.ha.get, post, ttl=60.0, clock=self.now)
        self.assertEqual(await a.run("ha.light_brightness", {"percent": 50, "room": "living"}),
                         f"{ha_actions.REPLY_FAILED} {ha_actions.REPLY_QUEUED_UNKNOWN}")


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


def verdict(status=200, result="ok", reason="state_matched", ha="200", down=None, after=None):
    """The answer of ha-cli to a service call, with its verdict on the device state (hacli.py)."""
    return HaReply(status, None, {"result": result, "reason": reason, "ms": 840, "attempts": 3, "after": after or {"light.living_lamp": "on 40%"}},
                   "a1b2c3d4", ha, down)


class Verification(Base):
    """The reply is chosen by what ha-cli OBSERVED on the device, and says no cause that was not observed (2026-11-19 21:21: «перезагружается»)."""

    async def run_traced(self, reply, args=None):
        a = self.make(service_reply=reply)
        trace = utterance_trace.start("turn", "яркость сорок")
        try:
            text = await a.run("ha.light_brightness", args or {"percent": 40, "room": None})
        finally:
            record = utterance_trace.end(trace)
        return text, record

    async def test_a_verified_command_is_confirmed_and_the_trace_keeps_the_verdict(self):
        text, record = await self.run_traced(verdict())
        self.assertEqual(text, "Яркость 40 процентов.")
        call = record["calls"][0]
        self.assertEqual((call["verify"]["result"], call["verify"]["ms"], call["verify"]["after"]), ("ok", 840, {"light.living_lamp": "on 40%"}))
        self.assertEqual((call["aid"], call["ha"], call["ok"]), ("a1b2c3d4", "200", True))
        self.assertEqual(record["reply_why"], "ha_verified")

    async def test_the_device_did_not_obey_is_said_so(self):
        text, record = await self.run_traced(verdict(502, "failed", "state_mismatch"))
        self.assertEqual(text, ha_actions.REPLY_NOT_CONFIRMED)
        self.assertEqual((record["reply_why"], record["calls"][0]["ok"]), ("ha_not_confirmed", False))

    async def test_an_unavailable_device_is_named_as_observed(self):
        text, record = await self.run_traced(verdict(502, "failed", "device_unavailable"))
        self.assertEqual((text, record["reply_why"]), (ha_actions.REPLY_DEVICE_UNAVAILABLE, "ha_device_unavailable"))

    async def test_no_answer_is_neutral_and_names_no_cause(self):
        text, record = await self.run_traced(verdict(504, "unknown", "ha_no_answer", ha="timeout"))
        self.assertEqual((text, record["reply_why"]), (ha_actions.REPLY_UNVERIFIED, "ha_unverified"))
        for word in ("перезагруж", "выключен", "офлайн", "недоступ"):
            self.assertNotIn(word, text)

    async def test_an_observed_outage_is_named_with_its_evidence(self):
        text, record = await self.run_traced(HaReply(202, None, {"result": "unknown", "reason": "ha_down_observed", "ms": 260}, "a1", "refused", "refused"))
        self.assertEqual((text, record["reply_why"]), (ha_actions.REPLY_QUEUED, "ha_down_observed"))
        self.assertEqual((record["calls"][0]["down"], record["calls"][0]["queued"]), ("refused", True))
        self.assertNotIn("перезагруж", text)
        starting, _ = await self.run_traced(HaReply(202, None, None, "a1", "503", "starting"))
        self.assertEqual(starting, ha_actions.REPLY_QUEUED_STARTING)

    async def test_a_rejection_by_home_assistant_is_said_so(self):
        text, record = await self.run_traced(HaReply(400, None, {"result": "skipped", "reason": "rejected"}, "a1", "400"))
        self.assertEqual((text, record["reply_why"]), (ha_actions.REPLY_REJECTED, "ha_rejected"))

    async def test_an_acknowledgement_without_a_verdict_is_not_called_verified(self):
        text, record = await self.run_traced(HaReply(200))
        self.assertEqual((text, record["reply_why"]), ("Яркость 40 процентов.", "ha_acknowledged"))

    async def test_a_partially_verified_call_is_not_counted_fully_verified(self):
        reply = verdict()
        reply.verify["unchecked"] = ["color_temp_kelvin"]
        text, record = await self.run_traced(reply)
        self.assertEqual(record["reply_why"], "ha_partially_verified")
        self.assertEqual(record["calls"][0]["verify"]["unchecked"], ["color_temp_kelvin"])

    async def test_the_unchecked_header_survives_parsing(self):
        parsed = HaReply.from_response(200, None, {"X-Aisis-Verify": "ok", "X-Aisis-Verify-Reason": "state_matched",
                                                   "X-Aisis-Verify-Unchecked": "color_temp_kelvin,rgb_color"})
        self.assertEqual(parsed.verify["unchecked"], ["color_temp_kelvin", "rgb_color"])
        self.assertNotIn("unchecked", HaReply.from_response(200, None, {"X-Aisis-Verify": "ok"}).verify)

    async def test_ha_cli_unreachable_is_neutral(self):
        async def post(path, body):
            if path.startswith("/api/services/"):
                raise OSError("ha-cli is not there")
            return await self.ha.post(path, body)

        self.ha = FakeHa()
        a = LivingRoomActions(self.ha.get, post)
        trace = utterance_trace.start("turn", "x")
        try:
            text = await a.run("ha.light_brightness", {"percent": 40, "room": None})
        finally:
            record = utterance_trace.end(trace)
        self.assertEqual((text, record["reply_why"]), (ha_actions.REPLY_UNVERIFIED, "ha_unreachable"))
        self.assertEqual(record["calls"][0]["error"], "OSError")

    async def test_every_operation_of_a_command_is_verified_and_the_first_failure_stops_it(self):
        a = self.make(service_reply=verdict(502, "failed", "state_mismatch"))
        text = await a.run("ha.climate", {"room": "upstairs", "mode": "heat", "temperature": 28})
        self.assertEqual(text, ha_actions.REPLY_NOT_CONFIRMED)
        self.assertEqual(len(self.ha.services), 1)            # the setpoint is not sent after a mode that did not take

    async def test_a_lamp_that_is_not_confirmed_does_not_stop_the_other_lamps(self):
        replies = [verdict(502, "failed", "state_mismatch"), verdict(200, "ok", "state_matched")]

        class Sequence(FakeHa):
            async def post(self, path, body):
                if path.startswith("/api/services/"):
                    self.services.append((path, body))
                    return replies.pop(0)
                return await super().post(path, body)

        self.now = Now()
        self.ha = Sequence()
        a = LivingRoomActions(self.ha.get, self.ha.post, ttl=60.0, clock=self.now)
        text = await a.run("ha.light_color", {"color": "warm", "room": "living"})
        self.assertEqual(text, ha_actions.REPLY_NOT_CONFIRMED)
        self.assertEqual([body["entity_id"] for _, body in self.svc()], ["light.living_lamp", "light.living_strip"])

    async def test_a_reply_that_called_no_service_is_not_traced_as_acknowledged(self):
        a = self.make()
        with mock.patch.object(utterance_trace, "note") as note:
            await a.run("ha.light_brightness", {"percent": 50, "room": "studio"})   # the studio has only an on/off plug
        self.assertEqual(self.svc(), [])
        self.assertIn(mock.call(reply_why="not_executed"), note.call_args_list)


class HeadersToReply(unittest.TestCase):
    def test_ha_cli_headers_become_the_verdict(self):
        reply = HaReply.from_response(200, [], {"X-Aisis-Action": "ab12", "X-Aisis-Verify": "ok", "X-Aisis-Verify-Reason": "state_matched",
                                                "X-Aisis-Verify-Ms": "842", "X-Aisis-Verify-Attempts": "3",
                                                "X-Aisis-Verified-State": "light.studiia=off,light.kitchen=on 40%", "X-Aisis-Ha-Status": "timeout"})
        self.assertEqual(reply.verify, {"result": "ok", "reason": "state_matched", "ms": 842, "attempts": 3,
                                        "after": {"light.studiia": "off", "light.kitchen": "on 40%"}})
        self.assertEqual((reply.action, reply.ha, reply.down), ("ab12", "timeout", None))

    def test_a_proxy_without_a_verdict(self):
        self.assertEqual(HaReply.from_response(200, None, {}).verify, None)

    def test_a_queued_call_carries_its_evidence(self):
        self.assertEqual(HaReply.from_response(202, None, {"X-Aisis-Down": "refused", "X-Aisis-Queue": "1"}).down, "refused")


if __name__ == "__main__":
    unittest.main()
