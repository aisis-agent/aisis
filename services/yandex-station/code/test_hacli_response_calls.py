"""ha-cli forwards the query string of a service call: `?return_response` is what makes HA answer with data."""
import http.client
import json
import os
import tempfile
import threading
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class StubHa(BaseHTTPRequestHandler):
    paths: list[str] = []

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        StubHa.paths.append(self.path)
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = json.dumps({"service_response": {"ok": True}}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class QueryForwardingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ha = ThreadingHTTPServer(("127.0.0.1", 0), StubHa)
        threading.Thread(target=cls.ha.serve_forever, daemon=True).start()
        cls.tmp = tempfile.TemporaryDirectory()
        with mock.patch.dict(os.environ, {"AISIS_HACLI_HA_URL": "http://127.0.0.1:1",
                                          "AISIS_HACLI_CACHE": cls.tmp.name + "/cache.json",
                                          "AISIS_HACLI_OUTAGES": cls.tmp.name + "/outages.json",
                                          "AISIS_HACLI_JOURNAL": cls.tmp.name + "/journal.jsonl"}):
            import hacli
        cls.hacli = hacli
        # hacli reads its configuration at import: point the module at this class's stub for the class lifetime.
        cls.config = mock.patch.object(hacli, "HA_URL", f"http://127.0.0.1:{cls.ha.server_port}")
        cls.config.start()
        cls.proxy = ThreadingHTTPServer(("127.0.0.1", 0), hacli.Handler)
        threading.Thread(target=cls.proxy.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.proxy.shutdown()
        cls.proxy.server_close()
        cls.ha.shutdown()
        cls.ha.server_close()
        cls.config.stop()
        cls.tmp.cleanup()

    def post(self, path: str) -> tuple[int, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy.server_port, timeout=5)
        try:
            conn.request("POST", path, json.dumps({"entity_id": "weather.home", "type": "daily"}),
                         {"Content-Type": "application/json"})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def test_return_response_reaches_home_assistant(self):
        StubHa.paths.clear()
        status, body = self.post("/api/services/weather/get_forecasts?return_response")
        self.assertEqual(status, 200)
        self.assertIn(b"service_response", body)
        self.assertEqual(StubHa.paths, ["/api/services/weather/get_forecasts?return_response"])

    def test_plain_service_call_keeps_its_path(self):
        StubHa.paths.clear()
        self.assertEqual(self.post("/api/services/light/turn_on")[0], 200)
        self.assertEqual(StubHa.paths, ["/api/services/light/turn_on"])

    def test_response_call_is_not_queued_while_home_assistant_is_down(self):
        self.hacli._queue.clear()
        with mock.patch.object(self.hacli, "HA_URL", "http://127.0.0.1:1"):  # nothing listens there
            for path in ("/api/services/weather/get_forecasts?return_response",
                         "/api/services/weather/get_forecasts?return_response=true",
                         "/api/services/weather/get_forecasts?return%5fresponse"):
                with self.subTest(path=path):
                    self.assertEqual(self.post(path)[0], 503)
            self.assertEqual(self.hacli._queue, [])
            # a key or value that merely contains the words does not make it a response call
            for path in ("/api/services/light/turn_on", "/api/services/light/turn_on?source=return_response"):
                with self.subTest(path=path):
                    self.assertEqual(self.post(path)[0], 202)
        self.hacli._queue.clear()


if __name__ == "__main__":
    unittest.main()
