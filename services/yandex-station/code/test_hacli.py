"""aisis-ha-cli answers a service call inside the quasar container's 3 s fetch timeout even when Home Assistant hangs, and says
nothing about WHY Home Assistant is silent.

2026-11-15 21:33: HA did not answer, ha-cli waited its 5 s probe timeout before queueing (202), quasar had aborted at 3 s and told the
owner the command failed; ha-cli then replayed it and the command ran. 2026-11-19 21:21: a service call that took longer than ha-cli's
2 s was treated as «Home Assistant is down», queued and announced as a reboot although the light had already gone off. A hung Home
Assistant now gets an honest «no answer» (504) inside the budget, never a queue entry and never a cause (test_hacli_verify.py has the
verification scenarios).
"""
import http.client
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import unittest.mock
from http.server import ThreadingHTTPServer

_TMP = tempfile.TemporaryDirectory()
os.environ.setdefault("AISIS_HACLI_HA_URL", "http://127.0.0.1:1")
os.environ["AISIS_HACLI_CACHE"] = os.path.join(_TMP.name, "cache.json")
os.environ["AISIS_HACLI_OUTAGES"] = os.path.join(_TMP.name, "outages.json")
os.environ["AISIS_HACLI_JOURNAL"] = os.path.join(_TMP.name, "journal.jsonl")

import hacli  # noqa: E402  (reads its config from the environment at import)

QUASAR_FETCH_TIMEOUT_S = 3.0  # FETCH_TIMEOUT_MS in the quasar container's ha-intents.ts


class HangingHomeAssistant:
    """Accepts connections and never answers: what a wedged or overloaded HA looks like to a client."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.05)
        self.held = []
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._accept)
        self.thread.start()

    def _accept(self):
        while not self.stopping.is_set():
            try:
                self.held.append(self.sock.accept()[0])
            except socket.timeout:
                continue
            except OSError:
                return

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.sock.getsockname()[1]

    def close(self):
        self.stopping.set()
        self.thread.join()
        self.sock.close()
        for conn in self.held:
            conn.close()


class ServiceCallWhileHaHangs(unittest.TestCase):
    def setUp(self):
        ha = HangingHomeAssistant()
        self.addCleanup(ha.close)
        patch_url = unittest.mock.patch.object(hacli, "HA_URL", ha.url)
        patch_url.start()
        self.addCleanup(patch_url.stop)
        patch_avail = unittest.mock.patch.object(hacli, "_avail", hacli.Availability())
        patch_avail.start()
        self.addCleanup(patch_avail.stop)
        self.addCleanup(hacli._queue.clear)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), hacli.Handler)
        self.server.daemon_threads = True
        thread = threading.Thread(target=self.server.serve_forever)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def post(self, path, body):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=QUASAR_FETCH_TIMEOUT_S)
        started = time.monotonic()
        try:
            conn.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
            response = conn.getresponse()
            return response.status, json.loads(response.read()), {k.lower(): v for k, v in response.getheaders()}, time.monotonic() - started
        finally:
            conn.close()

    def test_hung_home_assistant_is_answered_before_the_callers_timeout_without_a_cause(self):
        call = {"entity_id": "climate.ac", "hvac_mode": "cool"}
        status, answer, headers, elapsed = self.post("/api/services/climate/set_hvac_mode", call)
        self.assertEqual(status, 504)
        self.assertEqual((headers["x-aisis-verify"], headers["x-aisis-verify-reason"]), ("unknown", "ha_no_answer"))
        self.assertLess(elapsed, QUASAR_FETCH_TIMEOUT_S)
        self.assertEqual(answer["error"], "ha_no_answer")
        self.assertNotIn("queued", answer)                         # not queued behind the speaker's back, not announced as a restart
        self.assertNotIn("x-aisis-queue", headers)
        self.assertNotIn("x-aisis-down", headers)
        self.assertEqual(hacli._queue, [])

    def test_client_timeout_stays_below_the_quasar_fetch_timeout(self):
        self.assertLess(hacli.CLIENT_TIMEOUT_S, QUASAR_FETCH_TIMEOUT_S)
        self.assertLess(hacli.CALL_BUDGET_S, QUASAR_FETCH_TIMEOUT_S)


if __name__ == "__main__":
    unittest.main()
