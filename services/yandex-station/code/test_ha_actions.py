import copy
import json
import unittest

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


class Now:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class FakeHa:
    def __init__(self, states=None, service_status=200, template_as_text=True):
        self.states = copy.deepcopy(STATES if states is None else states)
        self.service_status = service_status
        self.template_as_text = template_as_text
        self.gets = 0
        self.template_posts = 0
        self.services: list[tuple[str, dict]] = []

    async def get(self, path):
        assert path == "/api/states"
        self.gets += 1
        return self.states

    async def post(self, path, body):
        if path == "/api/template":
            self.template_posts += 1
            area = body["template"].split("'")[1]
            ids = AREA_ENTITIES.get(area, [])
            return 200, (json.dumps(ids) if self.template_as_text else ids)
        self.services.append((path, body))
        return self.service_status, None


class Base(unittest.IsolatedAsyncioTestCase):
    def make(self, **kw):
        self.ha = FakeHa(**kw)
        self.now = Now()
        self.actions = HaActions(self.ha.get, self.ha.post, ttl=60.0, clock=self.now)
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

        a = HaActions(FakeHa().get, post)
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
        self.assertEqual((self.ha.gets, self.ha.template_posts), (1, 1))
        await a.run("ha.light_brightness", {"percent": 20, "room": "kitchen"})
        self.assertEqual((self.ha.gets, self.ha.template_posts), (1, 2))
        self.now.t += 61
        await a.run("ha.light_brightness", {"percent": 30, "room": None})
        self.assertEqual((self.ha.gets, self.ha.template_posts), (2, 3))


if __name__ == "__main__":
    unittest.main()
