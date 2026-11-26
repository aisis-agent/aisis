"""TV pause (#276): the pause is the speaker's own, it reaches Home Assistant only through the shared client, and it touches only the playing
media players of the room the activated speaker stands in.

Everything here runs against fakes: `FakeHa` (the transport interface of TvPause) and `ha_fakes.FakeHomeAssistant` behind the real
`ha_client.HaClient` (the real transport). No test calls a service of a real Home Assistant or a real device.
"""
from __future__ import annotations

import errno
import fcntl
import inspect
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import ha_client
import ha_fakes
import tvpause
from tvpause import Player, TvPause

STATION = "m00zrw3008rajg"
SPEAKER = f"media_player.aisis_speaker_{STATION}"
OTHER_STATION = "m10dst310hxnpk"
OTHER_SPEAKER = f"media_player.aisis_speaker_{OTHER_STATION}"
LIVING, STUDIO, BEDROOM = "Гостиная", "Студия", "Спальня"
ENTITY = "media_player.televizor"
PROJECTOR = "media_player.projector"
STUDIO_TV = "media_player.studio_tv"
BEDROOM_TV = "media_player.bedroom_tv"


class FakeHa:
    """The transport interface of TvPause: `players()` (one live read of every media player), `state(entity)`, `call(service, entity)`."""

    def __init__(self):
        self.up = True
        self.rows: dict[str, list] = {}       # entity -> [state, area, device, queue, device area]
        self.calls: list[str] = []            # services called, in order
        self.targets: list[tuple[str, str]] = []
        self.add(SPEAKER, "idle", LIVING, "dev-speaker")
        self.add(ENTITY, "playing", LIVING, "dev-tv")

    def add(self, entity, state, area, device=None, queue=None, device_area=None):
        self.rows[entity] = [state, area, device, queue, area if device_area is None else device_area]

    def move(self, entity, area):
        """The entity and its device are in another area."""
        self.rows[entity][1] = self.rows[entity][4] = area

    def set(self, entity, state):
        self.rows[entity][0] = state

    def state_of(self, entity):
        return self.rows[entity][0]

    @property
    def tv(self):
        return self.rows[ENTITY][0]

    def players(self):
        if not self.up:
            return None
        return [Player(entity, state, area, device, queue, device_area) for entity, (state, area, device, queue, device_area) in self.rows.items()]

    def state(self, entity):
        if not self.up or entity not in self.rows:
            return None
        return self.rows[entity][0]

    def call(self, service, entity):
        self.calls.append(service)
        self.targets.append((service, entity))
        self.rows[entity][0] = "paused" if service == "media_pause" else "playing"
        return True


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def settings(**changes):
    return {"enabled": True, "resume": True, "exclude": [], "resume_delay_s": 1.5, "max_hold_s": 300, **changes}


class TvCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runs = 0
        self.fresh()

    def fresh(self):
        """A new pause on a new fake Home Assistant and a new state file (a test that needs several)."""
        self.runs += 1
        self.ha, self.clock = FakeHa(), Clock()
        self.cfg = settings()
        tv = TvPause(ha=self.ha, settings=lambda: self.cfg, state_file=Path(self.tmp.name) / f"state{self.runs}.json", dry_run=False, clock=self.clock)
        self.tv = tv
        self.addCleanup(lambda: [timer.cancel() for timer in tv.timers.values()])

    def kinds(self):
        return [event["kind"] for event in self.tv.status()["events"]]

    def last_detail(self):
        return self.tv.status()["events"][-1]["detail"]


class HoldTests(TvCase):
    """The timer, grace and lock rules that existed before the room scoping (#75), unchanged."""

    def test_second_activation_while_ha_down_rearms_hold_timer(self):
        self.tv.activation()
        self.assertEqual(self.ha.calls, ["media_pause"])
        first = self.clock.now
        self.clock.now += 50
        self.ha.up = False
        self.tv.activation()
        self.assertNotEqual(self.tv.timers["hold"].args, (first,))
        self.assertEqual(self.tv.timers["hold"].args, (self.clock.now,))
        # the rearmed timer must actually resume the pause we still hold
        self.ha.up = True
        self.tv._hold_expired(self.clock.now)
        self.assertEqual(self.ha.calls, ["media_pause", "media_play"])
        self.assertEqual(self.tv.status()["paused"], {})

    def test_stale_hold_timer_is_a_noop_after_newer_activation(self):
        self.tv.activation()
        old = self.clock.now
        self.clock.now += 10
        self.tv.activation()
        self.tv._hold_expired(old)
        self.assertEqual(self.ha.calls, ["media_pause"])
        self.assertIn(ENTITY, self.tv.status()["paused"])

    def test_activation_during_grace_cancels_resume_atomically(self):
        self.tv.activation()
        ended = self.clock.now + 1
        self.clock.now += 2
        self.tv.activation()                      # next turn starts inside the grace window
        self.tv._resume_if_idle(ended)
        self.assertEqual(self.ha.calls, ["media_pause"])
        self.assertIn(ENTITY, self.tv.status()["paused"])

    def test_resume_guard_is_evaluated_under_the_state_lock(self):
        self.tv.activation()
        seen = []

        def guard(st):
            seen.append(ENTITY in st["paused"])
            return False

        self.tv.resume("dialog ended", unless=guard)
        self.assertEqual(seen, [True])
        self.assertEqual(self.ha.calls, ["media_pause", "media_play"])

    def assert_state_lock_held(self):
        fd = os.open(str(self.tv.state_file) + ".lock", os.O_RDWR)
        try:
            with self.assertRaises(OSError) as info:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertIn(info.exception.errno, (errno.EWOULDBLOCK, errno.EAGAIN))
        finally:
            os.close(fd)

    def test_resume_callbacks_check_and_release_under_one_lock(self):
        for callback in ("hold", "idle"):
            with self.subTest(callback=callback):
                self.fresh()
                self.tv.activation()
                checked = []
                original = self.tv.resume

                def spy(reason, unless=None, original=original):
                    def guard(st):
                        self.assert_state_lock_held()
                        checked.append(True)
                        return unless(st)
                    original(reason, unless=guard)

                self.tv.resume = spy
                ha_state = self.ha.state

                def locked_state(entity, ha_state=ha_state):
                    self.assert_state_lock_held()      # the release happens under the same lock as the guard
                    return ha_state(entity)

                self.ha.state = locked_state
                if callback == "hold":
                    self.tv._hold_expired(self.clock.now)
                else:
                    self.tv._resume_if_idle(self.clock.now)
                self.assertEqual(checked, [True])
                self.assertEqual(self.ha.calls, ["media_pause", "media_play"])

    def test_older_activation_cannot_replace_newer_hold_timer(self):
        self.tv.activation()                      # pause held, so an HA-down activation still arms a hold
        ticks = iter(range(2000, 2100))
        self.tv.clock = lambda: float(next(ticks))
        newer_done = threading.Event()
        older_arming = threading.Event()
        original_arm = self.tv._arm
        armed = []
        errors = []

        def arm(name, delay, fn, *args):
            if not armed:                        # the older activation, about to arm its timer
                armed.append(args)
                older_arming.set()
                newer_done.wait(0.5)             # with the lock held it times out; unlocked, the newer one finishes here
            original_arm(name, delay, fn, *args)

        def run(fn):
            try:
                fn()
            except BaseException as error:       # surfaced by the assertion below
                errors.append(error)

        self.tv._arm = arm
        self.ha.up = False
        older = threading.Thread(target=run, args=(self.tv.activation,))
        older.start()
        self.assertTrue(older_arming.wait(5), "older activation never reached the hold timer")
        self.ha.up = True

        def newer():
            self.tv.activation()
            newer_done.set()

        newer_thread = threading.Thread(target=run, args=(newer,))
        newer_thread.start()
        older.join(5)
        newer_thread.join(5)
        self.assertFalse(older.is_alive() or newer_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.tv.timers["hold"].args, (self.tv.status()["last_activation"],))


class RoomTests(TvCase):
    """Which players an activation pauses: playing, in the room of the activated speaker, never the speaker itself."""

    def home(self):
        """The studio speaker with a studio TV, the living-room TV and projector, a bedroom TV: everything playing."""
        self.ha.rows.clear()
        self.ha.add(SPEAKER, "idle", STUDIO, "dev-speaker")
        self.ha.add(STUDIO_TV, "playing", STUDIO, "dev-studio-tv")
        self.ha.add(ENTITY, "playing", LIVING, "dev-tv")
        self.ha.add(PROJECTOR, "playing", LIVING, "dev-projector")
        self.ha.add(BEDROOM_TV, "playing", BEDROOM, "dev-bedroom-tv")

    def test_only_the_players_of_the_speakers_room_are_paused(self):
        self.home()
        self.tv.activation(STATION)
        self.assertEqual(self.ha.targets, [("media_pause", STUDIO_TV)])
        for untouched in (ENTITY, PROJECTOR, BEDROOM_TV):
            self.assertEqual(self.ha.state_of(untouched), "playing", untouched)
        self.assertEqual(list(self.tv.status()["paused"]), [STUDIO_TV])

    def test_a_projector_and_a_tv_of_the_room_are_both_paused(self):
        self.home()
        self.ha.move(SPEAKER, LIVING)
        self.tv.activation(STATION)
        self.assertCountEqual(self.ha.targets, [("media_pause", ENTITY), ("media_pause", PROJECTOR)])
        self.assertEqual(self.ha.state_of(STUDIO_TV), "playing")
        self.assertEqual(self.ha.state_of(BEDROOM_TV), "playing")

    def test_a_player_of_another_room_is_never_touched(self):
        self.ha.move(SPEAKER, BEDROOM)        # the speaker is in the bedroom, the only playing player is the living-room TV
        self.tv.activation(STATION)
        self.assertEqual(self.ha.calls, [])
        self.assertEqual(self.tv.status()["paused"], {})

    def test_the_speakers_own_players_are_never_paused(self):
        self.ha.rows.clear()
        self.ha.add(SPEAKER, "playing", STUDIO, "dev-speaker")
        self.ha.add(f"media_player.yandex_station_{STATION}", "playing", STUDIO, "dev-yandex")
        self.ha.add("media_player.second_face_of_the_speaker", "playing", STUDIO, "dev-speaker")        # the same HA device
        self.ha.add(f"media_player.no_device_{STATION}", "playing", STUDIO, None)                       # an entity of the speaker without a device
        self.ha.add("media_player.music_assistant_face", "playing", STUDIO, "dev-ma", queue=SPEAKER)     # Music Assistant's player of the speaker
        self.ha.add(STUDIO_TV, "playing", STUDIO, "dev-studio-tv")
        self.tv.activation(STATION)
        self.assertEqual(self.ha.targets, [("media_pause", STUDIO_TV)])

    def test_only_a_playing_player_is_paused(self):
        for state in ("idle", "paused", "off", "standby", "unavailable", "unknown", "buffering", "on"):
            with self.subTest(state=state):
                self.fresh()
                self.ha.set(ENTITY, state)
                self.tv.activation()
                self.assertEqual(self.ha.calls, [])
                self.assertEqual(self.tv.status()["paused"], {})

    def test_a_player_without_an_area_is_not_in_any_room(self):
        self.ha.add("media_player.ubuntu_pc", "playing", None)
        self.tv.activation()
        self.assertEqual(self.ha.targets, [("media_pause", ENTITY)])

    def test_the_exclude_list_keeps_a_player_out(self):
        self.cfg = settings(exclude=[PROJECTOR])
        self.ha.add(PROJECTOR, "playing", LIVING, "dev-projector")
        self.tv.activation()
        self.assertEqual(self.ha.targets, [("media_pause", ENTITY)])

    def test_the_station_the_websocket_names_decides_the_room(self):
        self.home()
        self.ha.add(OTHER_SPEAKER, "idle", BEDROOM, "dev-speaker-2")
        self.tv.activation(OTHER_STATION)
        self.assertEqual(self.ha.targets, [("media_pause", BEDROOM_TV)])

    def test_the_station_id_is_matched_without_case(self):
        self.home()
        self.tv.activation(STATION.upper())
        self.assertEqual(self.ha.targets, [("media_pause", STUDIO_TV)])

    def test_an_id_that_only_ends_an_ordinary_player_is_no_station(self):
        self.home()
        self.tv.activation("tv")                          # media_player.studio_tv ends in _tv
        self.assertEqual(self.ha.calls, [])
        self.assertIn("not in Home Assistant", self.last_detail())

    def test_a_station_that_has_only_the_yandex_integrations_player_is_a_station(self):
        self.home()
        self.ha.add(f"media_player.yandex_station_{OTHER_STATION}", "paused", BEDROOM, "dev-yandex-2")
        self.tv.activation(OTHER_STATION)
        self.assertEqual(self.ha.targets, [("media_pause", BEDROOM_TV)])

    def test_the_speakers_room_is_the_area_of_its_device_not_of_its_player_entity(self):
        self.home()
        self.ha.rows[SPEAKER][1] = LIVING                 # only the player entity is set to another area; its device stays in the studio
        self.tv.activation(STATION)
        self.assertEqual(self.ha.targets, [("media_pause", STUDIO_TV)])

    def test_two_speakers_and_no_station_means_no_room(self):
        self.home()
        self.ha.add(OTHER_SPEAKER, "idle", BEDROOM, "dev-speaker-2")
        self.tv.activation()
        self.assertEqual(self.ha.calls, [])
        self.assertEqual(self.kinds(), ["skip"])
        self.assertIn("2 speakers", self.last_detail())

    def test_a_station_home_assistant_does_not_know_means_no_room(self):
        self.home()
        self.tv.activation("m99unknown")
        self.assertEqual(self.ha.calls, [])
        self.assertIn("m99unknown", self.last_detail())

    def test_a_speaker_without_an_area_means_no_room(self):
        self.home()
        self.ha.move(SPEAKER, None)
        self.tv.activation(STATION)
        self.assertEqual(self.ha.calls, [])
        self.assertIn("no area", self.last_detail())

    def test_no_speaker_at_all_means_no_room(self):
        del self.ha.rows[SPEAKER]
        self.tv.activation()
        self.assertEqual(self.ha.calls, [])
        self.assertEqual(self.kinds(), ["skip"])
        self.assertIn("0 speakers", self.last_detail())

    def test_an_unreachable_home_assistant_does_nothing(self):
        self.ha.up = False
        self.tv.activation()
        self.assertEqual(self.ha.calls, [])
        self.assertEqual(self.kinds(), ["skip"])
        self.assertIn("unreachable", self.last_detail())

    def test_disabled_asks_home_assistant_nothing(self):
        self.cfg = settings(enabled=False)
        self.ha.players = mock.Mock(side_effect=AssertionError("a disabled pause must not read Home Assistant"))
        self.tv.activation()
        self.assertEqual(self.ha.calls, [])

    def test_dry_run_reads_but_never_calls(self):
        self.tv.dry_run = True
        self.tv.activation()
        self.assertEqual(self.ha.calls, [])
        self.assertEqual(self.kinds(), ["dry-run"])

    def test_a_pause_of_another_room_that_is_still_held_survives_a_new_activation(self):
        self.home()
        self.ha.add(OTHER_SPEAKER, "idle", BEDROOM, "dev-speaker-2")
        self.tv.activation(STATION)                        # studio TV paused
        self.tv.activation(OTHER_STATION)                  # bedroom: its TV is paused too, the studio one stays ours
        self.assertCountEqual(self.tv.status()["paused"], [STUDIO_TV, BEDROOM_TV])
        self.assertEqual(self.ha.state_of(ENTITY), "playing")

    def test_a_held_player_somebody_else_changed_is_forgotten(self):
        self.tv.activation()
        self.ha.set(ENTITY, "off")                         # the owner switched the TV off
        self.tv.activation()
        self.assertEqual(self.tv.status()["paused"], {})
        self.assertIn("forget", self.kinds())


class ResumeTests(TvCase):
    def test_resume_only_what_we_paused(self):
        self.ha.add(PROJECTOR, "paused", LIVING, "dev-projector")       # paused by the owner before the dialog
        self.tv.activation()
        self.assertEqual(self.ha.targets, [("media_pause", ENTITY)])
        self.tv.resume("dialog ended")
        self.assertEqual(self.ha.targets, [("media_pause", ENTITY), ("media_play", ENTITY)])
        self.assertEqual(self.ha.state_of(PROJECTOR), "paused")

    def test_resume_leaves_a_player_that_is_not_paused_any_more(self):
        self.tv.activation()
        self.ha.set(ENTITY, "playing")                     # the owner pressed play
        self.tv.resume("dialog ended")
        self.assertEqual(self.ha.calls, ["media_pause"])
        self.assertIn("skip-resume", self.kinds())

    def test_resume_switch_off_resumes_nothing(self):
        self.cfg = settings(resume=False)
        self.tv.activation()
        self.tv.resume("dialog ended")
        self.assertEqual(self.ha.calls, ["media_pause"])

    def test_a_dialog_that_acted_on_a_media_player_is_never_undone(self):
        self.tv.activation()
        self.tv.observe(1001.0, "Fast intent executed: pause [media_player.televizor]")
        self.tv.resume("dialog ended")
        self.assertEqual(self.ha.calls, ["media_pause"])
        self.assertIn("user-action", self.kinds())

    def test_a_dialog_that_acted_on_another_media_player_is_not_undone_either(self):
        self.tv.activation()
        self.tv.observe(1001.0, "Fast intent executed: stop [media_player.ue48j5500_ue48j5500]")
        self.tv.resume("dialog ended")
        self.assertEqual(self.ha.calls, ["media_pause"])

    def test_a_dialog_about_a_light_does_not_stop_the_resume(self):
        self.tv.activation()
        self.tv.observe(1001.0, "Fast intent executed: turn on [light.living_lamp]")
        self.tv.resume("dialog ended")
        self.assertEqual(self.ha.calls, ["media_pause", "media_play"])

    def test_nothing_held_means_an_executed_intent_is_not_noted(self):
        self.tv.observe(1001.0, "Fast intent executed: pause [media_player.televizor]")
        self.assertEqual(self.tv.status()["events"], [])

    def test_the_dialog_end_resumes_after_the_grace_and_only_for_its_own_dialog(self):
        self.tv.activation()
        did = "11111111-1111-1111-1111-111111111111"
        self.tv.observe(self.clock.now + 0.5, f"Dialog {did} started")
        with mock.patch.object(self.tv, "_arm") as arm:
            self.tv.observe(self.clock.now + 2, f"Dialog {did} ended: serverFinished")
        arm.assert_called_once()
        self.assertEqual(arm.call_args.args[0:2], ("resume", 1.5))
        later = "22222222-2222-2222-2222-222222222222"
        self.tv.observe(self.clock.now + 60, f"Dialog {later} started")          # a text command long after the activation
        with mock.patch.object(self.tv, "_arm") as arm:
            self.tv.observe(self.clock.now + 61, f"Dialog {later} ended: serverFinished")
        arm.assert_not_called()


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "tv_pause.json"
        patch = mock.patch.dict(os.environ, {"AISIS_TVPAUSE_SETTINGS": str(self.path)})
        patch.start()
        self.addCleanup(patch.stop)

    def write(self, body):
        self.path.write_text(json.dumps(body))

    def test_the_owners_file_with_the_old_entities_key_keeps_enabled_and_resume(self):
        self.write({"enabled": False, "resume": False, "entities": ["media_player.televizor"], "resume_delay_s": 1.5, "max_hold_s": 300})
        loaded = tvpause.load_settings()
        self.assertIs(loaded["enabled"], False)
        self.assertIs(loaded["resume"], False)
        self.assertNotIn("entities", loaded)
        self.assertEqual(loaded["exclude"], [])

    def test_saving_rewrites_the_file_without_the_old_key_and_without_losing_the_switches(self):
        self.write({"enabled": False, "resume": False, "entities": ["media_player.televizor"], "resume_delay_s": 1.5, "max_hold_s": 300})
        tvpause.update_settings({"resume": True})
        stored = json.loads(self.path.read_text())
        self.assertEqual((stored["enabled"], stored["resume"]), (False, True))
        self.assertNotIn("entities", stored)
        self.assertEqual(stored["exclude"], [])

    def test_one_invalid_key_does_not_reset_the_others(self):
        self.write({"enabled": False, "resume": False, "exclude": "media_player.x", "max_hold_s": 5})
        loaded = tvpause.load_settings()
        self.assertEqual((loaded["enabled"], loaded["resume"]), (False, False))
        self.assertEqual((loaded["exclude"], loaded["max_hold_s"]), ([], 300))

    def test_exclude_is_a_list_of_media_player_ids(self):
        self.assertEqual(tvpause.update_settings({"exclude": ["media_player.a", "media_player.a", "media_player.b"]})["exclude"],
                         ["media_player.a", "media_player.b"])
        for bad in ("media_player.a", ["light.a"], [5], ["media_player.A b"]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                tvpause.update_settings({"exclude": bad})

    def test_the_old_entities_key_is_not_accepted_any_more(self):
        tvpause.update_settings({"entities": ["media_player.televizor"]})
        self.assertNotIn("entities", json.loads(self.path.read_text()))

    def test_no_file_gives_the_defaults(self):
        self.assertEqual(tvpause.load_settings(), {"enabled": True, "resume": True, "exclude": [], "resume_delay_s": 1.5, "max_hold_s": 300})


class HomeAssistantTransportTests(unittest.TestCase):
    """The real transport: tvpause.HaPlayers on the real ha_client.HaClient against a fake Home Assistant server."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ha = ha_fakes.FakeHomeAssistant([ha_fakes.entity(entity, state) for entity, state in (
            (SPEAKER, "idle"), (STUDIO_TV, "playing"), (ENTITY, "playing"), (PROJECTOR, "playing"), (BEDROOM_TV, "paused"))]
            + [ha_fakes.entity("media_player.ma_face", "idle", active_queue=SPEAKER),
               ha_fakes.entity("media_player.weird", "idle", active_queue={"not": "a name"})])
        self.addCleanup(self.ha.close)
        self.ha.areas.update({SPEAKER: LIVING, STUDIO_TV: STUDIO, ENTITY: LIVING, PROJECTOR: LIVING, BEDROOM_TV: BEDROOM, "media_player.weird": STUDIO})
        self.ha.devices.update({SPEAKER: "dev-speaker", STUDIO_TV: "dev-studio-tv"})
        self.ha.device_areas.update({SPEAKER: STUDIO})       # the speaker's player entity says living room, its device says studio
        self.ha.long_lived.add("test-token")
        self.store = ha_client.Store(Path(self.tmp.name) / "connection.json")
        self.store.save(ha_client.Connection(self.ha.url, "long_lived", "test-token"))
        self.client = ha_client.HaClient(self.store, discover_fn=lambda **_: [])
        self.transport = tvpause.HaPlayers(self.client)

    def test_players_are_read_with_state_area_and_device_in_one_call(self):
        players = {player.entity: player for player in self.transport.players()}
        self.assertEqual(players[STUDIO_TV], Player(STUDIO_TV, "playing", STUDIO, "dev-studio-tv", None, STUDIO))
        self.assertEqual(players[ENTITY], Player(ENTITY, "playing", LIVING, None))
        self.assertEqual(players["media_player.ma_face"], Player("media_player.ma_face", "idle", None, None, SPEAKER))
        self.assertEqual((players[SPEAKER].area, players[SPEAKER].device_area), (LIVING, STUDIO))
        self.assertEqual(len(players), 7)
        templates = [path for method, path, _ in self.ha.requests if method == "POST"]
        self.assertEqual(templates, ["/api/template"])

    def test_an_attribute_that_is_not_a_name_is_nothing_and_never_breaks_the_room(self):
        players = self.transport.players()
        self.assertIsNone({player.entity: player for player in players}["media_player.weird"].queue)
        room = tvpause.discover(players, STATION, [])
        self.assertEqual(room.area, STUDIO)
        self.assertIn("media_player.weird", [player.entity for player in room.players])

    def test_a_state_and_a_service_call_use_the_standard_api(self):
        self.assertEqual(self.transport.state(ENTITY), "playing")
        self.assertIsNone(self.transport.state("media_player.nobody"))
        self.assertTrue(self.transport.call("media_pause", ENTITY))
        self.assertEqual(self.ha.calls(), [("/api/services/media_player/media_pause", {"entity_id": ENTITY})])
        self.assertEqual(self.transport.state(ENTITY), "paused")

    def test_a_refused_service_call_is_not_a_success(self):
        self.ha.script("media_player.media_pause", status=500)
        self.assertFalse(self.transport.call("media_pause", ENTITY))

    def test_an_unreachable_home_assistant_is_nothing_not_an_exception(self):
        self.ha.stop_listening()
        self.assertIsNone(self.transport.players())
        self.assertIsNone(self.transport.state(ENTITY))
        self.assertFalse(self.transport.call("media_pause", ENTITY))

    def test_not_connected_is_nothing_not_an_exception(self):
        self.store.clear()
        self.assertIsNone(self.transport.players())
        self.assertIsNone(self.transport.state(ENTITY))
        self.assertFalse(self.transport.call("media_pause", ENTITY))

    def test_an_unreadable_answer_is_nothing(self):
        with mock.patch.object(self.ha, "render", return_value="not json"):
            self.assertIsNone(self.transport.players())

    def test_the_whole_turn_through_the_real_client_pauses_the_rooms_player_and_resumes_it(self):
        tv = TvPause(ha=self.transport, settings=lambda: settings(), state_file=Path(self.tmp.name) / "state.json", dry_run=False)
        self.addCleanup(lambda: [timer.cancel() for timer in tv.timers.values()])
        tv.activation(STATION)
        self.assertEqual(self.ha.calls(), [("/api/services/media_player/media_pause", {"entity_id": STUDIO_TV})])
        for untouched in (ENTITY, PROJECTOR):
            self.assertEqual(self.ha.states[untouched]["state"], "playing")
        tv.resume("dialog ended")
        self.assertEqual(self.ha.calls()[-1], ("/api/services/media_player/media_play", {"entity_id": STUDIO_TV}))
        self.assertEqual(self.ha.states[STUDIO_TV]["state"], "playing")

    def test_the_module_reaches_home_assistant_only_through_the_shared_client(self):
        source = inspect.getsource(tvpause)
        for forbidden in ("8795", "AISIS_HACLI_URL", "urllib.request", "Authorization", "http://"):
            self.assertNotIn(forbidden, source)
        self.assertIsNone(re.search(r"\bHaCli\b|\bimport hacli\b", source), "the ha-cli proxy is not used")


class DashboardCardTests(unittest.TestCase):
    """The dashboard card keeps its schema (entities, live, paused, events, ...) so the UI that is already built keeps working."""

    UI_KEYS = {"enabled", "resume", "entities", "resume_delay_s", "max_hold_s", "live", "paused", "events", "dry_run"}

    def setUp(self):
        import dashboard
        self.dashboard = dashboard
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.dict(os.environ, {"AISIS_TVPAUSE_SETTINGS": str(Path(self.tmp.name) / "tv_pause.json")})
        patch.start()
        self.addCleanup(patch.stop)
        self.ha = FakeHa()
        self.ha.rows.clear()
        self.ha.add(SPEAKER, "idle", STUDIO, "dev-speaker")
        self.ha.add(STUDIO_TV, "playing", STUDIO, "dev-studio-tv")
        self.ha.add(ENTITY, "playing", LIVING, "dev-tv")
        tv = TvPause(ha=self.ha, state_file=Path(self.tmp.name) / "state.json", dry_run=False)
        patch = mock.patch.object(tvpause, "_default", tv)
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_card_lists_the_players_of_the_speakers_room_with_their_live_state(self):
        card = self.dashboard.tv_state()
        self.assertTrue(self.UI_KEYS <= set(card), self.UI_KEYS - set(card))
        self.assertEqual(card["entities"], [STUDIO_TV])
        self.assertEqual(card["live"], {STUDIO_TV: "playing"})
        self.assertEqual(card["room"], STUDIO)

    def test_the_card_without_a_known_room_lists_nothing_and_says_why(self):
        self.ha.up = False
        card = self.dashboard.tv_state()
        self.assertEqual((card["entities"], card["live"], card["room"]), ([], {}, None))
        self.assertIn("unreachable", card["why"])

    def test_the_switches_still_save(self):
        reply = self.dashboard.tv_save({"enabled": False, "resume": False})
        self.assertTrue(reply["ok"])
        self.assertEqual((reply["enabled"], reply["resume"]), (False, False))


if __name__ == "__main__":
    unittest.main()
