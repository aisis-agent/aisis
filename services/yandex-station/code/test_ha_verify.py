"""Per-domain expectations of a service call: what the device state must say once the command worked."""
import json
import unittest

import ha_verify


def state(entity_id, value, **attrs):
    return {"entity_id": entity_id, "state": value, "attributes": attrs}


def verdict(domain, service, body, states, before=None):
    expected = ha_verify.expectation(domain, service, body, before)
    assert expected is not None, (domain, service, body)
    return expected.check(states)


class OnOff(unittest.TestCase):
    def test_turn_off_matches_only_off(self):
        body = {"entity_id": "light.studio"}
        self.assertEqual(verdict("light", "turn_off", body, {"light.studio": state("light.studio", "off")}).result, "ok")
        late = verdict("light", "turn_off", body, {"light.studio": state("light.studio", "on")})
        self.assertEqual((late.result, late.reason, late.wrong), ("failed", "state_mismatch", {"light.studio": "on"}))

    def test_turn_on_and_brightness(self):
        body = {"entity_id": "light.lamp", "brightness_pct": 40}
        self.assertEqual(verdict("light", "turn_on", body, {"light.lamp": state("light.lamp", "on", brightness=102)}).result, "ok")
        self.assertEqual(verdict("light", "turn_on", body, {"light.lamp": state("light.lamp", "on", brightness=102 + 5)}).result, "ok")
        wrong = verdict("light", "turn_on", body, {"light.lamp": state("light.lamp", "on", brightness=255)})
        self.assertEqual((wrong.result, wrong.wrong), ("failed", {"light.lamp": "on 100%"}))

    def test_brightness_zero_means_off(self):
        body = {"entity_id": "light.lamp", "brightness_pct": 0}
        self.assertEqual(verdict("light", "turn_on", body, {"light.lamp": state("light.lamp", "off")}).result, "ok")

    def test_colour_temperature_tolerates_mired_rounding(self):
        body = {"entity_id": "light.lamp", "color_temp_kelvin": 2700}
        self.assertEqual(verdict("light", "turn_on", body, {"light.lamp": state("light.lamp", "on", color_temp_kelvin=2688)}).result, "ok")
        self.assertEqual(verdict("light", "turn_on", body, {"light.lamp": state("light.lamp", "on", color_temp_kelvin=6500)}).result, "failed")

    def test_named_colour_only_has_to_be_on(self):
        body = {"entity_id": "light.lamp", "color_name": "red"}
        self.assertEqual(verdict("light", "turn_on", body, {"light.lamp": state("light.lamp", "on")}).result, "ok")

    def test_switch_and_toggle(self):
        self.assertEqual(verdict("switch", "turn_on", {"entity_id": "switch.plug"}, {"switch.plug": state("switch.plug", "on")}).result, "ok")
        body = {"entity_id": "switch.plug"}
        before = {"switch.plug": state("switch.plug", "on")}
        self.assertEqual(verdict("switch", "toggle", body, {"switch.plug": state("switch.plug", "off")}, before).result, "ok")
        self.assertEqual(verdict("switch", "toggle", body, {"switch.plug": state("switch.plug", "on")}, before).result, "failed")
        self.assertIsNone(ha_verify.expectation("switch", "toggle", body, {}))   # unknown start: nothing to compare with

    def test_homeassistant_domain_uses_each_entitys_own_rule(self):
        body = {"entity_id": ["light.a", "switch.b"]}
        states = {"light.a": state("light.a", "off"), "switch.b": state("switch.b", "off")}
        self.assertEqual(verdict("homeassistant", "turn_off", body, states).result, "ok")

    def test_every_entity_must_match(self):
        body = {"entity_id": ["light.a", "light.b"]}
        states = {"light.a": state("light.a", "off"), "light.b": state("light.b", "on")}
        result = verdict("light", "turn_off", body, states)
        self.assertEqual((result.result, result.wrong, result.after), ("failed", {"light.b": "on"}, {"light.a": "off", "light.b": "on"}))

    def test_unavailable_device_is_its_own_reason(self):
        result = verdict("light", "turn_off", {"entity_id": "light.a"}, {"light.a": state("light.a", "unavailable")})
        self.assertEqual((result.result, result.reason), ("failed", "device_unavailable"))

    def test_unreadable_state_is_pending_not_failed(self):
        result = verdict("light", "turn_off", {"entity_id": ["light.a", "light.b"]}, {"light.a": state("light.a", "off"), "light.b": None})
        self.assertEqual((result.result, result.reason), ("pending", "state_unreadable"))

    def test_a_lamp_that_cannot_report_the_attribute_is_not_held_to_it(self):
        kelvin = {"entity_id": "light.rgb", "color_temp_kelvin": 2700}
        colour_only = state("light.rgb", "on", supported_color_modes=["rgb"], color_mode="rgb")
        result = verdict("light", "turn_on", kelvin, {"light.rgb": colour_only})
        self.assertEqual((result.result, result.unchecked), ("ok", ("color_temp_kelvin",)))   # on is confirmed, the colour is reported as not checkable
        dim = {"entity_id": "light.plug", "brightness_pct": 50}
        on_off = verdict("light", "turn_on", dim, {"light.plug": state("light.plug", "on", supported_color_modes=["onoff"])})
        self.assertEqual((on_off.result, on_off.unchecked), ("ok", ("brightness",)))      # on is confirmed, the level never can be
        both = verdict("light", "turn_on", {**dim, "color_temp_kelvin": 2700},
                       {"light.plug": state("light.plug", "on", supported_color_modes=["onoff"])})
        self.assertEqual((both.result, both.unchecked), ("ok", ("brightness", "color_temp_kelvin")))

    def test_the_wanted_temperature_is_the_end_of_the_range_of_the_lamp(self):
        cold = {"entity_id": "light.lamp", "color_temp_kelvin": 6500}
        lamp = {"supported_color_modes": ["color_temp"], "min_color_temp_kelvin": 2200, "max_color_temp_kelvin": 4000}
        self.assertEqual(verdict("light", "turn_on", cold, {"light.lamp": state("light.lamp", "on", color_temp_kelvin=4000, **lamp)}).result, "ok")
        self.assertEqual(verdict("light", "turn_on", cold, {"light.lamp": state("light.lamp", "on", color_temp_kelvin=2700, **lamp)}).result, "failed")


class OtherDomains(unittest.TestCase):
    def test_climate_mode_and_setpoint(self):
        body = {"entity_id": "climate.ac", "hvac_mode": "cool"}
        self.assertEqual(verdict("climate", "set_hvac_mode", body, {"climate.ac": state("climate.ac", "cool", temperature=24)}).result, "ok")
        self.assertEqual(verdict("climate", "set_hvac_mode", body, {"climate.ac": state("climate.ac", "off")}).result, "failed")
        both = {"entity_id": "climate.ac", "temperature": 22, "hvac_mode": "heat"}
        self.assertEqual(verdict("climate", "set_temperature", both, {"climate.ac": state("climate.ac", "heat", temperature=22)}).result, "ok")
        self.assertEqual(verdict("climate", "set_temperature", both, {"climate.ac": state("climate.ac", "cool", temperature=22)}).result, "failed")
        self.assertEqual(verdict("climate", "set_temperature", {"entity_id": "climate.ac", "temperature": 24.5},
                                 {"climate.ac": state("climate.ac", "cool", temperature=24)}).result, "ok")   # a one-degree step rounds

    def test_media_volume_mute_and_playback(self):
        player = "media_player.station"
        self.assertEqual(verdict("media_player", "volume_set", {"entity_id": player, "volume_level": 0.3},
                                 {player: state(player, "playing", volume_level=0.3)}).result, "ok")
        self.assertEqual(verdict("media_player", "volume_set", {"entity_id": player, "volume_level": 0.3},
                                 {player: state(player, "playing", volume_level=0.5)}).result, "failed")
        self.assertEqual(verdict("media_player", "volume_mute", {"entity_id": player, "is_volume_muted": True},
                                 {player: state(player, "playing", is_volume_muted=True)}).result, "ok")
        self.assertEqual(verdict("media_player", "media_pause", {"entity_id": player}, {player: state(player, "paused")}).result, "ok")
        self.assertEqual(verdict("media_player", "media_pause", {"entity_id": player}, {player: state(player, "idle")}).result, "ok")
        self.assertEqual(verdict("media_player", "media_pause", {"entity_id": player}, {player: state(player, "playing")}).result, "failed")

    def test_cover_in_motion_counts_as_obeyed(self):
        body = {"entity_id": "cover.blind"}
        self.assertEqual(verdict("cover", "close_cover", body, {"cover.blind": state("cover.blind", "closing")}).result, "ok")
        self.assertEqual(verdict("cover", "close_cover", body, {"cover.blind": state("cover.blind", "open")}).result, "failed")
        pos = {"entity_id": "cover.blind", "position": 50}
        self.assertEqual(verdict("cover", "set_cover_position", pos, {"cover.blind": state("cover.blind", "open", current_position=50)}).result, "ok")

    def test_number_select_lock(self):
        self.assertEqual(verdict("number", "set_value", {"entity_id": "number.x", "value": 3}, {"number.x": state("number.x", "3.0")}).result, "ok")
        self.assertEqual(verdict("select", "select_option", {"entity_id": "select.x", "option": "eco"}, {"select.x": state("select.x", "eco")}).result, "ok")
        self.assertEqual(verdict("lock", "lock", {"entity_id": "lock.door"}, {"lock.door": state("lock.door", "locked")}).result, "ok")


class NothingToCheck(unittest.TestCase):
    def test_calls_without_a_state_effect_have_no_expectation(self):
        for domain, service, body in (
                ("tts", "speak", {"entity_id": "tts.cloud", "message": "hi"}),
                ("script", "turn_on", {"entity_id": "script.x"}),
                ("scene", "turn_on", {"entity_id": "scene.x"}),
                ("light", "turn_off", {"area_id": "studio"}),
                ("light", "turn_off", {"device_id": "abc"}),
                ("light", "turn_off", {}),
                ("light", "turn_off", None),
                ("climate", "set_temperature", {"entity_id": "climate.ac"}),
                ("light", "turn_off", {"entity_id": [f"light.l{i}" for i in range(ha_verify.MAX_ENTITIES + 1)]}),
                ("media_player", "media_next_track", {"entity_id": "media_player.x"}),
                ("light", "turn_off", {"entity_id": "switch.plug"}),      # an entity of another domain is not this service's to judge
                ("homeassistant", "turn_off", {"entity_id": "script.x"}),
        ):
            with self.subTest(domain=domain, service=service, body=body):
                self.assertIsNone(ha_verify.expectation(domain, service, body))

    def test_comma_separated_entities_are_split(self):
        expected = ha_verify.expectation("light", "turn_off", {"entity_id": "light.a, light.b"})
        self.assertEqual(expected.entities, ("light.a", "light.b"))


class WholeDomain(unittest.TestCase):
    def test_all_lights_is_checked_through_a_template(self):
        expected = ha_verify.expectation("light", "turn_off", {"entity_id": "all"})
        self.assertEqual((expected.whole_domain, expected.whole_state), ("light", "off"))
        self.assertIn("states.light", ha_verify.whole_domain_template("light", "off"))
        self.assertEqual(ha_verify.whole_domain_verdict("[]").result, "ok")
        left = ha_verify.whole_domain_verdict(json.dumps(["light.a", "light.b"]))
        self.assertEqual((left.result, left.wrong), ("failed", {"light.a": "differs", "light.b": "differs"}))
        self.assertEqual(ha_verify.whole_domain_verdict("garbage").result, "pending")

    def test_all_of_other_domains_has_no_expectation(self):
        self.assertIsNone(ha_verify.expectation("climate", "turn_off", {"entity_id": "all"}))
        self.assertIsNone(ha_verify.expectation("light", "toggle", {"entity_id": "all"}))


class StateText(unittest.TestCase):
    def test_compact_state_lines(self):
        self.assertEqual(ha_verify.state_text(state("light.a", "on", brightness=128, color_temp_kelvin=2700)), "on 50% 2700K")
        self.assertEqual(ha_verify.state_text(state("climate.ac", "cool", temperature=24.0)), "cool 24°")
        self.assertEqual(ha_verify.state_text(state("media_player.m", "playing", volume_level=0.3, is_volume_muted=True)),
                         "playing vol 0.30 muted")
        self.assertEqual(ha_verify.state_text(None), "?")


class EntityIds(unittest.TestCase):
    def test_every_listed_entity_is_returned_however_many_but_only_few_are_polled(self):
        many = [f"light.l{i}" for i in range(ha_verify.MAX_ENTITIES + 1)]
        self.assertEqual(ha_verify.entity_ids({"entity_id": many}), tuple(many))
        self.assertIsNone(ha_verify.targets({"entity_id": many}))
        self.assertIsNone(ha_verify.entity_ids({"entity_id": "all"}))
        self.assertIsNone(ha_verify.entity_ids({"area_id": "kitchen", "entity_id": "light.a"}))


if __name__ == "__main__":
    unittest.main()
