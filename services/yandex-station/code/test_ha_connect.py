"""The dashboard's Home Assistant page (/ha): Connect through the standard login flow, status with the failing step, Disconnect, and the advanced
long-lived token. Through the dashboard's real request handler, against a fake Home Assistant with the auth endpoints and an mDNS responder."""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dashboard  # noqa: E402
import ha_client  # noqa: E402
import ha_connect  # noqa: E402
import ha_fakes  # noqa: E402

UUID = "1e0f21555ff04f0e9867e454428ccebd"


class DashboardHaPageTests(unittest.TestCase):
    def setUp(self):
        self.ha = ha_fakes.FakeHomeAssistant([ha_fakes.entity("light.kitchen", "on")])
        self.ha.require_auth = True
        self.ha.name = "Дом"
        self.addCleanup(self.ha.close)
        self.mdns = ha_fakes.FakeMdns(ha_fakes.mdns_response("_home-assistant._tcp.local", "Home Assistant", "ha.local", self.ha.port, "127.0.0.1",
                                                              {"location_name": "Дом", "uuid": UUID, "base_url": self.ha.url}))
        self.addCleanup(self.mdns.close)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = ha_client.Store(Path(self.dir.name) / "connection.json")
        client = ha_client.HaClient(self.store, discover_fn=lambda **_: [])
        import functools
        discover = functools.partial(ha_client.discover, group=self.mdns.group, hostnames=(), window=0.3)
        self.previous = (dashboard.HA_CLIENT, dashboard.HA_CONNECT)
        dashboard.HA_CLIENT = client
        dashboard.HA_CONNECT = ha_connect.Onboarding(client, discover)
        self.addCleanup(lambda: (setattr(dashboard, "HA_CLIENT", self.previous[0]), setattr(dashboard, "HA_CONNECT", self.previous[1])))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.origin = f"http://127.0.0.1:{self.port}"

    def call(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        data = urllib.parse.urlencode(body).encode() if body is not None else None
        conn.request(method, path, data, {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})})
        response = conn.getresponse()
        raw = response.read().decode()
        conn.close()
        return response.status, dict(response.getheaders()), raw

    def sign_in(self):
        """Press Connect, 'log in' on Home Assistant (the fake issues the code), come back through the callback."""
        status, headers, page = self.call("GET", "/ha")
        self.assertEqual(status, 200)
        status, headers, _ = self.call("POST", "/ha/connect", {"url": self.ha.url})
        self.assertEqual(status, 303)
        target = urllib.parse.urlparse(headers["Location"])
        query = urllib.parse.parse_qs(target.query)
        self.assertEqual(target.path, "/auth/authorize")
        client_id, redirect, state = query["client_id"][0], query["redirect_uri"][0], query["state"][0]
        self.assertEqual(client_id, self.origin + "/")
        self.assertEqual(redirect, self.origin + "/ha/callback")
        code = self.ha.issue_code(client_id)
        return self.call("GET", "/ha/callback?" + urllib.parse.urlencode({"code": code, "state": state})), state

    def test_the_page_offers_the_server_that_was_found_with_no_configured_address(self):
        status, headers, page = self.call("GET", "/ha")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("Дом", page)
        self.assertIn(self.ha.url, page)
        self.assertIn('action="/ha/connect"', page)
        self.assertLess(page.index('action="/ha/connect"'), page.index("Расширенно"))   # the main flow comes first, the token form is below it

    def test_connect_runs_the_standard_login_and_stores_a_private_refresh_token(self):
        (status, headers, _), _state = self.sign_in()
        self.assertEqual((status, headers["Location"]), (303, "/ha?connected=1"))
        conn = self.store.load()
        self.assertEqual((conn.url, conn.kind, conn.uuid, conn.name, conn.client_id), (self.ha.url, "oauth", UUID, "Дом", self.origin + "/"))
        self.assertEqual(oct(self.store.path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(self.store.path.read_text().count("refresh_token"), 1)
        status, _, body = self.call("GET", "/ha.json")
        data = json.loads(body)
        self.assertTrue(data["connected"])
        self.assertEqual([s["id"] for s in data["steps"]], ["store", "address", "reach", "auth", "api"])
        self.assertTrue(all(s["ok"] for s in data["steps"]))
        self.assertNotIn(conn.secret, body)

    def test_the_connected_page_shows_the_status_and_no_secret(self):
        self.sign_in()
        _, _, page = self.call("GET", "/ha")
        self.assertIn("Подключено", page)
        self.assertIn(self.ha.url, page)
        self.assertIn('action="/ha/disconnect"', page)
        self.assertNotIn(self.store.load().secret, page)

    def test_a_callback_with_an_unknown_state_is_refused(self):
        status, _, page = self.call("GET", "/ha/callback?code=abc&state=forged")
        self.assertEqual(status, 400)
        self.assertIsNone(self.store.load())

    def test_a_state_works_once(self):
        (_, _, _), state = self.sign_in()
        status, _, _ = self.call("GET", "/ha/callback?" + urllib.parse.urlencode({"code": self.ha.issue_code(self.origin + "/"), "state": state}))
        self.assertEqual(status, 400)

    def test_a_refusal_on_the_login_page_comes_back_as_a_message(self):
        _, headers, _ = self.call("POST", "/ha/connect", {"url": self.ha.url})
        state = urllib.parse.parse_qs(urllib.parse.urlparse(headers["Location"]).query)["state"][0]
        status, _, page = self.call("GET", "/ha/callback?" + urllib.parse.urlencode({"error": "access_denied", "state": state}))
        self.assertEqual(status, 200)
        self.assertIn("access_denied", page)
        self.assertIsNone(self.store.load())

    def test_connect_only_goes_to_a_server_that_was_found_or_entered_by_hand(self):
        status, _, _ = self.call("POST", "/ha/connect", {"url": "http://127.0.0.1:9"})
        self.assertEqual(status, 400)
        self.assertIsNone(self.store.load())

    def test_disconnect_revokes_the_grant_on_home_assistant_and_forgets_it(self):
        self.sign_in()
        refresh = self.store.load().secret
        status, headers, _ = self.call("POST", "/ha/disconnect", {})
        self.assertEqual((status, headers["Location"]), (303, "/ha?disconnected=1"))
        self.assertIsNone(self.store.load())
        self.assertNotIn(refresh, self.ha.refresh_tokens)
        with self.assertRaises(ha_client.AuthRevoked):
            ha_client.refresh_access(self.ha.url, self.origin + "/", refresh)

    def test_reconnecting_replaces_the_old_grant(self):
        self.sign_in()
        old = self.store.load().secret
        self.sign_in()
        self.assertNotEqual(self.store.load().secret, old)
        self.assertNotIn(old, self.ha.refresh_tokens)

    def test_a_revoked_grant_shows_the_failing_step_and_the_connect_button(self):
        self.sign_in()
        self.ha.refresh_tokens.clear()
        _, _, body = self.call("GET", "/ha.json")
        data = json.loads(body)
        self.assertFalse(data["connected"])
        self.assertEqual(data["failing"]["id"], "auth")
        _, _, page = self.call("GET", "/ha")
        self.assertIn("заново", page)
        self.assertIn('action="/ha/connect"', page)

    def test_a_post_from_another_site_is_refused(self):
        status, _, _ = self.call("POST", "/ha/disconnect", {}, {"Origin": "http://evil.example"})
        self.assertEqual(status, 403)

    def test_the_long_lived_token_is_an_advanced_option_and_is_checked_before_it_is_kept(self):
        self.ha.long_lived.add("llat")
        _, _, page = self.call("GET", "/ha")
        self.assertIn("Расширенно", page)
        status, _, _ = self.call("POST", "/ha/manual", {"url": self.ha.url, "token": "wrong"})
        self.assertEqual(status, 400)
        self.assertIsNone(self.store.load())
        status, headers, _ = self.call("POST", "/ha/manual", {"url": self.ha.url, "token": "llat"})
        self.assertEqual(status, 303)
        conn = self.store.load()
        self.assertEqual((conn.kind, conn.secret, conn.url), ("long_lived", "llat", self.ha.url))
        self.assertTrue(json.loads(self.call("GET", "/ha.json")[2])["connected"])

    def test_nothing_found_says_so_and_offers_the_advanced_address(self):
        self.mdns.response = None
        status, _, page = self.call("GET", "/ha")
        self.assertEqual(status, 200)
        self.assertIn("не найден", page)
        self.assertIn("Расширенно", page)


if __name__ == "__main__":
    unittest.main()
