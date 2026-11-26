"""The installer of the Home Assistant TV auto-pause (ha_autopause.py): the versioned automation file is rendered from a Spec, created through the
config API on the shared client, and a second run changes nothing. An in-memory Home Assistant stands in for the API (the real one is exercised by
ha_autopause_proof.py on a stand-in entity)."""
import copy
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_autopause  # noqa: E402
import ha_client  # noqa: E402


class MemoryHa:
    """The part of Home Assistant the installer touches: helpers, automation configs, a few service calls. Every mutation is journalled."""

    def __init__(self):
        self.helpers = {}        # entity id -> state
        self.automations = {}    # config id -> config
        self.journal = []
        self.down = None

    def _check(self):
        if self.down:
            raise self.down

    def helper_ids(self, kind):
        self._check()
        return {entity for entity in self.helpers if entity.startswith(kind + ".")}

    def create_helper(self, kind, name):
        self._check()
        entity = kind + "." + re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        self.helpers[entity] = "off"
        self.journal.append(("create_helper", entity))
        return entity

    def delete_helper(self, entity):
        self._check()
        self.helpers.pop(entity, None)
        self.journal.append(("delete_helper", entity))

    def set_helper(self, entity, on):
        self._check()
        self.helpers[entity] = "on" if on else "off"
        self.journal.append(("set_helper", entity, on))

    def get_automation(self, config_id):
        self._check()
        return copy.deepcopy(self.automations.get(config_id))

    def put_automation(self, config_id, config):
        self._check()
        self.automations[config_id] = copy.deepcopy(config)
        self.journal.append(("put_automation", config_id))

    def delete_automation(self, config_id):
        self._check()
        self.automations.pop(config_id, None)
        self.journal.append(("delete_automation", config_id))

    def reload_automations(self):
        self._check()
        self.journal.append(("reload",))


def walk(node):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk(item)
    else:
        yield node


class RenderTests(unittest.TestCase):
    def test_nothing_of_the_template_is_left_and_numbers_stay_numbers(self):
        plan = ha_autopause.render(ha_autopause.Spec())
        main = next(a for a in plan.automations if a["id"] == "tv_autopause_main")
        text = json.dumps(plan.automations)
        self.assertNotIn("$", text)
        wait = main["actions"][1]["parallel"][0]["sequence"][0]["repeat"]["sequence"][0]
        self.assertNotIn("this.", wait["timeout"])       # `this` is the state from before the trigger: the deadline is fixed when the run starts
        self.assertIn("hold_until", wait["timeout"])
        variables = main["actions"][0]["variables"]
        self.assertIn("+ 300", variables["hold_until"])
        self.assertEqual(wait["wait_for_trigger"], [{"trigger": "event", "event_type": "aisis_dialog_ended"}])
        self.assertEqual(main["actions"][2]["then"][0]["delay"], {"milliseconds": 1500})

    def test_entities_follow_the_spec(self):
        spec = ha_autopause.Spec(slug="ap_x", player="input_select.ap_x_tv", player_domain="input_select", pause_script="script.ap_x_pause",
                                 play_script="script.ap_x_play", watch=("input_select.ap_x_tv", "script.ap_x_pause", "script.ap_x_play"),
                                 listening_event="ap_x_listening", ended_event="ap_x_ended")
        plan = ha_autopause.render(spec)
        self.assertEqual({a["id"] for a in plan.automations}, {"ap_x_main", "ap_x_owner"})
        self.assertEqual([h.entity for h in plan.helpers], ["input_boolean.ap_x_enabled", "input_boolean.ap_x_resume", "input_boolean.ap_x_we_paused"])
        text = json.dumps(plan.automations)
        self.assertIn("script.ap_x_pause", text)
        self.assertNotIn("tv_autopause", text)
        self.assertNotIn("televizor", text)

    def test_the_default_spec_targets_the_tvs_player_and_its_scripts(self):
        text = json.dumps(ha_autopause.render(ha_autopause.Spec()).automations)
        for entity in ("media_player.televizor", "script.tv_pause", "script.tv_play"):
            self.assertIn(entity, text)

    def test_the_main_automation_arms_the_hold_first_and_only_starts_the_pause_and_the_resume(self):
        main = next(a for a in ha_autopause.render(ha_autopause.Spec()).automations if a["id"].endswith("_main"))
        self.assertEqual(main["mode"], "restart")      # the restart is what cancels a pending resume on a new activation
        pause = main["actions"][1]["parallel"][1]["sequence"][0]["then"]
        self.assertEqual([step["action"] for step in pause], ["input_boolean.turn_off", "input_boolean.turn_on", "script.turn_on"])   # the hold (re-armed) before the pause, the pause not awaited
        self.assertEqual(pause[2]["target"], {"entity_id": "script.tv_pause"})
        resume = main["actions"][2]["then"][2]["then"][1]["then"]
        self.assertEqual([(step["action"], step["target"]) for step in resume], [("script.turn_on", {"entity_id": "script.tv_play"})])

    def test_the_wait_for_the_dialog_end_exists_before_any_helper_or_script_call(self):
        """A fast dialog may end while the helper and script calls are still running; Home Assistant does not replay an event to a later waiter,
        so the waiter runs in parallel with them from the first action of the run (nothing precedes it but the variables)."""
        main = next(a for a in ha_autopause.render(ha_autopause.Spec()).automations if a["id"].endswith("_main"))
        self.assertEqual(list(main["actions"][0]), ["variables"])
        branches = main["actions"][1]["parallel"]
        self.assertIn("repeat", branches[0]["sequence"][0])
        self.assertEqual(branches[0]["sequence"][0]["repeat"]["sequence"][0]["wait_for_trigger"][0]["event_type"], "aisis_dialog_ended")
        self.assertIn("input_boolean.turn_on", json.dumps(branches[1]))

    def test_the_end_of_a_previous_dialog_is_not_this_turns_end(self):
        main = next(a for a in ha_autopause.render(ha_autopause.Spec()).automations if a["id"].endswith("_main"))
        until = main["actions"][1]["parallel"][0]["sequence"][0]["repeat"]["until"][0]["value_template"]
        self.assertIn("abs <= 3", until)               # a dialog that started well before this activation is a previous one
        self.assertIn("ended_at", until)               # and one that ended before it is too

    def test_a_slow_pause_is_awaited_before_the_hold_is_released(self):
        main = next(a for a in ha_autopause.render(ha_autopause.Spec()).automations if a["id"].endswith("_main"))
        after = main["actions"][2]["then"]
        self.assertEqual(after[0], {"delay": {"milliseconds": 1500}})
        self.assertIn("is_state('media_player.televizor', 'paused')", after[1]["wait_template"])
        self.assertEqual((after[1]["timeout"], after[1]["continue_on_timeout"]), (30, True))

    def test_nothing_of_ours_runs_the_pause_the_automation_only_calls_services(self):
        forbidden = ("python_script.", "shell_command.", "rest_command.", "pyscript.", "command_line.")
        for config in ha_autopause.render(ha_autopause.Spec()).automations:
            for text in walk(config):
                self.assertFalse(isinstance(text, str) and text.startswith(forbidden), text)

    def test_the_helper_switches_are_what_the_automations_check(self):
        text = json.dumps(ha_autopause.render(ha_autopause.Spec()).automations)
        for entity in ("input_boolean.tv_autopause_enabled", "input_boolean.tv_autopause_resume", "input_boolean.tv_autopause_we_paused"):
            self.assertIn(entity, text)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.ha = MemoryHa()
        self.spec = ha_autopause.Spec()

    def test_a_fresh_install_creates_the_helpers_and_both_automations_and_stays_off(self):
        ha_autopause.install(self.ha, self.spec)
        self.assertEqual(set(self.ha.helpers), {"input_boolean.tv_autopause_enabled", "input_boolean.tv_autopause_resume", "input_boolean.tv_autopause_we_paused"})
        self.assertEqual(set(self.ha.automations), {"tv_autopause_main", "tv_autopause_owner"})
        self.assertEqual(self.ha.helpers["input_boolean.tv_autopause_enabled"], "off")
        self.assertEqual(self.ha.helpers["input_boolean.tv_autopause_resume"], "on")
        self.assertEqual(self.ha.helpers["input_boolean.tv_autopause_we_paused"], "off")
        self.assertEqual(self.ha.journal.count(("reload",)), 1)

    def test_a_second_install_changes_nothing(self):
        ha_autopause.install(self.ha, self.spec)
        self.ha.journal.clear()
        report = ha_autopause.install(self.ha, self.spec)
        self.assertEqual(self.ha.journal, [])
        self.assertEqual((report.created, report.written), ([], []))

    def test_an_owners_switch_is_never_reset_by_a_reinstall(self):
        ha_autopause.install(self.ha, self.spec)
        self.ha.helpers["input_boolean.tv_autopause_enabled"] = "on"
        self.ha.helpers["input_boolean.tv_autopause_resume"] = "off"
        ha_autopause.install(self.ha, self.spec)
        self.assertEqual(self.ha.helpers["input_boolean.tv_autopause_enabled"], "on")
        self.assertEqual(self.ha.helpers["input_boolean.tv_autopause_resume"], "off")

    def test_a_drifted_automation_is_rewritten_alone_and_reloaded_once(self):
        ha_autopause.install(self.ha, self.spec)
        self.ha.automations["tv_autopause_owner"]["mode"] = "single"
        self.ha.journal.clear()
        report = ha_autopause.install(self.ha, self.spec)
        self.assertEqual(self.ha.journal, [("put_automation", "tv_autopause_owner"), ("reload",)])
        self.assertEqual(report.written, ["tv_autopause_owner"])

    def test_a_missing_helper_is_created_alone(self):
        ha_autopause.install(self.ha, self.spec)
        del self.ha.helpers["input_boolean.tv_autopause_we_paused"]
        self.ha.journal.clear()
        ha_autopause.install(self.ha, self.spec)
        self.assertEqual(self.ha.journal, [("create_helper", "input_boolean.tv_autopause_we_paused")])

    def test_a_helper_that_got_another_id_is_removed_again_and_is_an_error(self):
        real = self.ha.create_helper

        def misnamed(kind, name):
            real(kind, name)
            self.ha.helpers["input_boolean.something_else"] = self.ha.helpers.pop(f"input_boolean.{name.lower().replace(' ', '_')}")
            return "input_boolean.something_else"
        self.ha.create_helper = misnamed
        with self.assertRaises(ha_autopause.InstallError):
            ha_autopause.install(self.ha, self.spec)
        self.assertNotIn("input_boolean.something_else", self.ha.helpers)

    def test_a_helper_whose_first_state_could_not_be_set_is_removed_so_a_rerun_makes_it_right(self):
        real = self.ha.set_helper
        self.ha.set_helper = lambda entity, on: (_ for _ in ()).throw(ha_client.Unreachable("timeout"))
        with self.assertRaises(ha_client.HaError):
            ha_autopause.install(self.ha, self.spec)
        self.assertNotIn("input_boolean.tv_autopause_resume", self.ha.helpers)
        self.ha.set_helper = real
        ha_autopause.install(self.ha, self.spec)
        self.assertEqual(self.ha.helpers["input_boolean.tv_autopause_resume"], "on")

    def test_a_helper_whose_create_answer_was_lost_is_removed_too(self):
        real = self.ha.create_helper

        def made_then_lost(kind, name):
            real(kind, name)
            raise ha_client.Unreachable("timeout")
        self.ha.create_helper = made_then_lost
        with self.assertRaises(ha_client.HaError):
            ha_autopause.install(self.ha, self.spec)
        self.assertEqual(self.ha.helpers, {})
        self.ha.create_helper = real
        ha_autopause.install(self.ha, self.spec)
        self.assertEqual(self.ha.helpers["input_boolean.tv_autopause_resume"], "on")

    def test_the_helpers_are_listed_once_per_kind(self):
        listed = []
        real = self.ha.helper_ids
        self.ha.helper_ids = lambda kind: listed.append(kind) or real(kind)
        ha_autopause.install(self.ha, self.spec)
        self.assertEqual(listed, ["input_boolean"])

    def test_home_assistant_being_down_is_reported_not_swallowed(self):
        self.ha.down = ha_client.Unreachable("refused")
        with self.assertRaises(ha_client.HaError):
            ha_autopause.install(self.ha, self.spec)


class SwitchTests(unittest.TestCase):
    def setUp(self):
        self.ha = MemoryHa()
        self.spec = ha_autopause.Spec()
        ha_autopause.install(self.ha, self.spec)
        self.ha.journal.clear()

    def test_enable_and_disable_touch_only_the_master_switch(self):
        ha_autopause.set_enabled(self.ha, self.spec, True)
        self.assertEqual(self.ha.journal, [("set_helper", "input_boolean.tv_autopause_enabled", True)])
        ha_autopause.set_enabled(self.ha, self.spec, False)
        self.assertEqual(self.ha.helpers["input_boolean.tv_autopause_enabled"], "off")

    def test_enable_before_install_is_an_error(self):
        with self.assertRaises(ha_autopause.InstallError):
            ha_autopause.set_enabled(MemoryHa(), self.spec, True)

    def test_uninstall_removes_everything_and_twice_is_fine(self):
        ha_autopause.uninstall(self.ha, self.spec)
        self.assertEqual((self.ha.helpers, self.ha.automations), ({}, {}))
        self.ha.journal.clear()
        ha_autopause.uninstall(self.ha, self.spec)
        self.assertEqual(self.ha.journal, [])


if __name__ == "__main__":
    unittest.main()
