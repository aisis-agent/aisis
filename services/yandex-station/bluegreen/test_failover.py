"""aisis-failover's `asr` component: Wyoming is restarted only on the gateway's explicit 503, never because the gateway itself
is down, does not know the route, or skipped the probe for live traffic (found in review: a dead gateway made the watchdog
restart a healthy recognizer that Home Assistant shares). A probe without a verdict neither counts nor clears a failure."""
import http.server
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import failover


def serve(status):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.send_header("content-length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def watchdog(state_dir):
    return failover.Watchdog(env="stage", interval=1.5, threshold=3, component_threshold=6, http_timeout=1.2,
                             components=True, alert=False, state_dir=Path(state_dir))


class AsrComponentTest(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.state_dir = scratch.name

    asr = next(comp for comp in failover.COMPONENTS if comp.name == "asr")

    def verdict(self, status):
        """probe_component's verdict for the asr component against a server answering `status` (None: nothing listens)."""
        if status is None:
            comp = failover.Component("asr", "http", "http://127.0.0.1:1/healthz/asr", fails_on=503)
            return watchdog(self.state_dir).probe_component(comp)[0]
        server = serve(status)
        try:
            comp = failover.Component("asr", "http", f"http://127.0.0.1:{server.server_port}/healthz/asr", fails_on=503)
            return watchdog(self.state_dir).probe_component(comp)[0]
        finally:
            server.shutdown()
            server.server_close()

    def test_the_component_restarts_the_recognizer_not_the_gateway_and_is_probed_off_the_tick(self):
        self.assertEqual(self.asr.unit, "wyoming-whisper.service")
        self.assertEqual(self.asr.fails_on, 503)
        self.assertTrue(self.asr.background)

    def test_only_the_gateways_503_fails_the_probe(self):
        self.assertIs(self.verdict(200), True)
        self.assertIs(self.verdict(503), False)

    def test_a_dead_gateway_a_missing_route_or_a_skipped_probe_is_no_verdict(self):
        for status, why in ((None, "connection refused"), (404, "gateway predates /healthz/asr"),
                            (501, "no recognizer configured"), (202, "live utterances in flight"),
                            (504, "recognizer slow, refused or unreachable")):
            self.assertIsNone(self.verdict(status), why)

    def test_two_503s_restart_the_recognizer_and_a_verdictless_probe_in_between_does_not_reset_the_count(self):
        dog = watchdog(self.state_dir)
        answers = iter([(False, "http 503"), (None, "http 202"), (False, "http 503")])
        restarts = []

        def tick():
            """One background probe cycle for the asr component, as tick() runs it (probe on the first tick of every 40)."""
            with ThreadPoolExecutor(max_workers=2) as pool, \
                    mock.patch.object(failover, "COMPONENTS", (self.asr,)), \
                    mock.patch.object(dog, "probe_component", side_effect=lambda comp: next(answers)), \
                    mock.patch.object(dog, "restart_unit", side_effect=lambda unit, key, cooldown: restarts.append((unit, cooldown))), \
                    mock.patch.object(dog, "maintenance", return_value=False), \
                    mock.patch.object(failover.bgctl, "active_color", return_value="blue"), \
                    mock.patch.object(failover.bgctl, "health", return_value={"ok": True}), \
                    mock.patch.object(dog, "_probe_switch", return_value=(True, "ok")), \
                    mock.patch.object(failover.speaker_clock, "probe", return_value=(True, "ok")):
                dog.ticks = 0
                dog.tick(pool)                                   # submits the background probe
                for _ in range(200):
                    if dog.pending["asr"].done():
                        break
                    time.sleep(0.01)
                dog.tick(pool)                                   # reads the finished probe

        tick()
        self.assertEqual(restarts, [])                           # one 503 is not enough
        tick()
        self.assertEqual(dog.trackers["asr"].fails, 1)           # no verdict: neither counted nor cleared
        self.assertEqual(restarts, [])
        tick()
        self.assertEqual(restarts, [("wyoming-whisper.service", 600.0)])  # the second 503; a restart cannot fix a full disk, so not every minute

    def test_a_slow_asr_probe_does_not_hold_up_the_tick(self):
        """background=True is why: the quasar failover decision of a tick must not wait for a hung recognizer's probe."""
        dog = watchdog(self.state_dir)
        with ThreadPoolExecutor(max_workers=2) as pool, \
                mock.patch.object(failover, "COMPONENTS", (self.asr,)), \
                mock.patch.object(dog, "probe_component", side_effect=lambda comp: (time.sleep(1.5), (True, "http 200"))[1]), \
                mock.patch.object(dog, "maintenance", return_value=False), \
                mock.patch.object(failover.bgctl, "active_color", return_value="blue"), \
                mock.patch.object(failover.bgctl, "health", return_value={"ok": True}), \
                mock.patch.object(dog, "_probe_switch", return_value=(True, "ok")), \
                mock.patch.object(failover.speaker_clock, "probe", return_value=(True, "ok")):
            started = time.monotonic()
            dog.tick(pool)
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertIn("asr", dog.pending)               # still running; its verdict is read by a later tick


if __name__ == "__main__":
    unittest.main()
