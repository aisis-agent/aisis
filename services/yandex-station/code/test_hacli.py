"""aisis-ha-cli must answer a service call inside the quasar container's 3 s fetch timeout even when Home Assistant hangs.

2026-11-15 21:33: HA did not answer, ha-cli waited its 5 s probe timeout before queueing (202), quasar had aborted at 3 s and told the
owner the command failed; ha-cli then replayed it and the command ran.
"""
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.request
from http.server import ThreadingHTTPServer

_TMP = tempfile.TemporaryDirectory()
os.environ.setdefault("AISIS_HACLI_HA_URL", "http://127.0.0.1:1")
os.environ["AISIS_HACLI_CACHE"] = os.path.join(_TMP.name, "cache.json")
os.environ["AISIS_HACLI_OUTAGES"] = os.path.join(_TMP.name, "outages.json")

import hacli  # noqa: E402  (reads its config from the environment at import)

QUASAR_FETCH_TIMEOUT_S = 3.0  # FETCH_TIMEOUT_MS in the quasar container's ha-intents.ts


class HangingHomeAssistant:
    """Accepts connections and never answers: what an HA that is starting up looks like to a client."""

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
        self.addCleanup(hacli._queue.clear)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), hacli.Handler)
        thread = threading.Thread(target=self.server.serve_forever)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def post(self, path, body):
        request = urllib.request.Request("http://127.0.0.1:%d%s" % (self.server.server_address[1], path),
                                         data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
        started = time.monotonic()
        with urllib.request.urlopen(request, timeout=QUASAR_FETCH_TIMEOUT_S) as response:
            return response.status, json.loads(response.read()), time.monotonic() - started

    def test_service_call_is_queued_before_the_callers_timeout(self):
        call = {"entity_id": "climate.ac", "hvac_mode": "cool"}
        status, answer, elapsed = self.post("/api/services/climate/set_hvac_mode", call)
        self.assertEqual(status, 202)
        self.assertTrue(answer["queued"])
        self.assertLess(elapsed, QUASAR_FETCH_TIMEOUT_S)
        self.assertEqual(hacli._queue, [("/api/services/climate/set_hvac_mode", call)])

    def test_client_timeout_stays_below_the_quasar_fetch_timeout(self):
        self.assertLess(hacli.CLIENT_TIMEOUT_S, QUASAR_FETCH_TIMEOUT_S)


if __name__ == "__main__":
    unittest.main()
