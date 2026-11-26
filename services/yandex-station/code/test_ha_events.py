"""The two events that drive the Home Assistant auto-pause automation (`aisis_listening`, `aisis_dialog_ended`): they reach Home Assistant's
`/api/events/<type>` through the shared client, a failure is logged and never raises or delays the voice turn, and while the legacy tvpause is
enabled neither is sent (exactly one of the two can act). Nothing here talks to a real Home Assistant."""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_client  # noqa: E402
import ha_events  # noqa: E402
import stt_wyoming  # noqa: E402

DIALOG = "0a1b2c3d-0000-4000-8000-0123456789ab"


class EventServer:
    """A Home Assistant that only records event posts; `hang` never answers (a wedged instance)."""

    def __init__(self):
        self.posts = []
        self.hang = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.posts.append((self.path, self.headers.get("Authorization"), json.loads(body or b"null")))
                if outer.hang.is_set():
                    time.sleep(5)
                    return
                raw = json.dumps({"message": "ok"}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.block_on_close = False
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FireTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.ha = EventServer()
        self.addCleanup(self.ha.close)
        self.store = ha_client.Store(Path(self.dir.name) / "connection.json")
        self.store.save(ha_client.Connection(self.ha.url, "long_lived", "tok-1"))
        self.client = ha_client.HaClient(self.store)

    def test_the_event_is_posted_with_its_data_and_the_clients_credential(self):
        self.assertTrue(ha_events.fire("aisis_listening", {"source": "stt"}, client=self.client))
        self.assertEqual(self.ha.posts, [("/api/events/aisis_listening", "Bearer tok-1", {"source": "stt"})])

    def test_an_unreachable_home_assistant_is_logged_and_never_raises(self):
        self.ha.close()
        with self.assertLogs("aisis.ha_events", "WARNING") as logs:
            self.assertFalse(ha_events.fire("aisis_listening", client=self.client))
        self.assertIn("aisis_listening", logs.output[0])

    def test_a_wedged_home_assistant_costs_at_most_the_short_timeout(self):
        self.ha.hang.set()
        started = time.monotonic()
        with self.assertLogs("aisis.ha_events", "WARNING"):
            self.assertFalse(ha_events.fire("aisis_listening", client=self.client, timeout=0.3))
        self.assertLess(time.monotonic() - started, 1.5)

    def test_not_connected_is_logged_and_never_raises(self):
        empty = ha_client.HaClient(ha_client.Store(Path(self.dir.name) / "none.json"))
        with self.assertLogs("aisis.ha_events", "WARNING"):
            self.assertFalse(ha_events.fire("aisis_listening", client=empty))

    def test_default_timeout_is_short(self):
        self.assertLessEqual(ha_events.EVENT_TIMEOUT_S, 1.0)


class Sink:
    def __init__(self):
        self.sent = []
        self.seen = threading.Event()

    def __call__(self, event, data=None):
        self.sent.append((event, data))
        self.seen.set()
        return True


class Legacy:
    """tvpause.TvPause stand-in: its settings say whether it acts."""

    def __init__(self, enabled):
        self.enabled = enabled
        self.activations = 0
        self.settings = lambda: {"enabled": enabled}

    def activation(self):
        self.activations += 1


class ActivationTests(unittest.TestCase):
    def activate(self, tv, sink):
        async def run():
            gateway = stt_wyoming.STTGateway(env={}, archive=False, tv=tv, fire_event=sink)
            gateway.tv_activation()
            while gateway.speaker_background:
                await asyncio.sleep(0.01)
        asyncio.run(run())

    def test_listening_is_fired_with_its_time_when_the_legacy_pause_is_disabled(self):
        tv, sink = Legacy(False), Sink()
        before = time.time()
        self.activate(tv, sink)
        [(event, data)] = sink.sent
        self.assertEqual(event, "aisis_listening")
        self.assertTrue(before <= data["at"] <= time.time())
        self.assertEqual(tv.activations, 0)

    def test_listening_is_not_fired_while_the_legacy_pause_is_enabled(self):
        tv, sink = Legacy(True), Sink()
        self.activate(tv, sink)
        self.assertEqual(sink.sent, [])
        self.assertEqual(tv.activations, 1)

    def test_a_failing_event_call_does_not_hold_the_turn(self):
        def slow(event, data=None):
            time.sleep(0.5)
            return False

        async def run():
            gateway = stt_wyoming.STTGateway(env={}, archive=False, tv=Legacy(False), fire_event=slow)
            started = time.monotonic()
            gateway.tv_activation()
            return time.monotonic() - started
        self.assertLess(asyncio.run(run()), 0.2)


class DialogEndTests(unittest.TestCase):
    def follower(self, enabled):
        sink = Sink()
        return ha_events.DialogEndFollower(sink, lambda: enabled), sink

    def test_a_dialog_end_fires_the_event_with_the_dialog_id_and_both_times(self):
        follower, sink = self.follower(False)
        follower.observe(99.0, f"Dialog {DIALOG} started")
        follower.observe(100.0, f"Dialog {DIALOG} ended")
        self.assertTrue(sink.seen.wait(2))
        self.assertEqual(sink.sent, [("aisis_dialog_ended", {"dialog_id": DIALOG, "started_at": 99.0, "ended_at": 100.0})])

    def test_an_end_whose_start_was_not_seen_still_fires_without_a_start(self):
        follower, sink = self.follower(False)
        follower.observe(100.0, f"Dialog {DIALOG} ended")
        self.assertTrue(sink.seen.wait(2))
        self.assertEqual(sink.sent, [("aisis_dialog_ended", {"dialog_id": DIALOG, "ended_at": 100.0})])

    def test_other_log_lines_and_dialog_starts_fire_nothing(self):
        follower, sink = self.follower(False)
        follower.observe(100.0, f"Dialog {DIALOG} started")
        follower.observe(100.1, "Fast intent executed: x [media_player.televizor]")
        time.sleep(0.1)
        self.assertEqual(sink.sent, [])

    def test_nothing_is_fired_while_the_legacy_pause_is_enabled(self):
        follower, sink = self.follower(True)
        follower.observe(99.0, f"Dialog {DIALOG} started")
        follower.observe(100.0, f"Dialog {DIALOG} ended")
        time.sleep(0.1)
        self.assertEqual(sink.sent, [])

    def test_starts_in_flight_are_bounded(self):
        follower, sink = self.follower(False)
        for index in range(ha_events.STARTS_KEPT + 20):
            follower.observe(float(index), f"Dialog {index:036x} started")
        self.assertLessEqual(len(follower._started), ha_events.STARTS_KEPT)


if __name__ == "__main__":
    unittest.main()
