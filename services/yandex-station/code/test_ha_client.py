"""ha_client against a fake Home Assistant: the connection store, mDNS discovery and re-discovery, the standard sign-in, token refresh and the status
steps the dashboard shows. Nothing here talks to a real Home Assistant or to the real network (mDNS goes to a responder on loopback)."""
import functools
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_client  # noqa: E402
import ha_fakes  # noqa: E402

CLIENT_ID = "http://192.168.0.56:8781/"
UUID = "1e0f21555ff04f0e9867e454428ccebd"


def announcement(ha: ha_fakes.FakeHomeAssistant, uuid: str = UUID) -> bytes:
    return ha_fakes.mdns_response("_home-assistant._tcp.local", "Home Assistant", "homeassistant.local", ha.port, "127.0.0.1",
                                  {"location_name": "Test Home", "uuid": uuid, "version": "2026.9.3", "base_url": ha.url})


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = ha_client.Store(Path(self.dir.name) / "state" / "ha-connection.json")

    def test_saved_connection_is_private_and_complete(self):
        self.store.save(ha_client.Connection("http://ha:8123/", "oauth", "refresh-1", CLIENT_ID, UUID, "Home", 5.0))
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.store.path.parent.stat().st_mode), 0o700)
        loaded = self.store.load()
        self.assertEqual((loaded.url, loaded.kind, loaded.secret, loaded.client_id, loaded.uuid, loaded.name),
                         ("http://ha:8123", "oauth", "refresh-1", CLIENT_ID, UUID, "Home"))
        self.assertEqual(json.loads(self.store.path.read_text())["auth"], {"kind": "oauth", "refresh_token": "refresh-1"})

    def test_a_new_address_keeps_the_credential(self):
        self.store.save(ha_client.Connection("http://old:8123", "oauth", "refresh-1", CLIENT_ID, UUID))
        self.store.set_url("http://new:8123")
        loaded = self.store.load()
        self.assertEqual((loaded.url, loaded.secret, loaded.uuid), ("http://new:8123", "refresh-1", UUID))
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)

    def test_set_url_without_a_connection_stores_nothing(self):
        self.store.set_url("http://new:8123")
        self.assertIsNone(self.store.load())
        self.assertFalse(self.store.path.exists())

    def test_clear_forgets_the_connection(self):
        self.store.save(ha_client.Connection("http://ha:8123", "long_lived", "tok"))
        self.store.clear()
        self.assertIsNone(self.store.load())

    def test_a_damaged_file_is_no_connection(self):
        self.store.path.parent.mkdir(parents=True)
        self.store.path.write_text("{not json")
        self.assertIsNone(self.store.load())
        self.store.path.write_text(json.dumps({"url": "http://x", "auth": {"kind": "oauth"}}))
        self.assertIsNone(self.store.load())

    def test_another_process_changing_the_file_is_seen(self):
        self.store.save(ha_client.Connection("http://a:8123", "oauth", "r"))
        self.assertEqual(self.store.load().url, "http://a:8123")
        other = ha_client.Store(self.store.path)
        other.set_url("http://b:8123")
        self.assertEqual(self.store.load().url, "http://b:8123")


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.ha = ha_fakes.FakeHomeAssistant()
        self.addCleanup(self.ha.close)

    def test_the_announcement_of_home_assistant_is_parsed(self):
        [service] = ha_client.parse_mdns(announcement(self.ha))
        self.assertEqual((service.name, service.host, service.port, service.addresses), ("Home Assistant", "homeassistant.local", self.ha.port, ("127.0.0.1",)))
        self.assertEqual(service.txt["uuid"], UUID)
        self.assertEqual(service.txt["base_url"], self.ha.url)

    def test_garbage_and_questions_are_ignored(self):
        self.assertEqual(ha_client.parse_mdns(b"\x00" * 5), [])
        self.assertEqual(ha_client.parse_mdns(ha_client.mdns_query("_home-assistant._tcp.local")), [])
        self.assertEqual(ha_client.parse_mdns(announcement(self.ha)[:40]), [])

    def test_home_assistant_is_found_with_no_configured_address(self):
        mdns = ha_fakes.FakeMdns(announcement(self.ha))
        self.addCleanup(mdns.close)
        found = ha_client.discover(window=0.4, group=mdns.group, hostnames=())
        self.assertEqual([(f.url, f.name, f.uuid, f.via) for f in found], [(self.ha.url, "Test Home", UUID, "mdns")])
        self.assertGreaterEqual(mdns.queries, 1)

    def test_an_announcement_without_base_url_uses_the_address_and_port(self):
        response = ha_fakes.mdns_response("_home-assistant._tcp.local", "Home Assistant", "ha.local", self.ha.port, "127.0.0.1", {"uuid": UUID})
        mdns = ha_fakes.FakeMdns(response)
        self.addCleanup(mdns.close)
        self.assertEqual([f.url for f in ha_client.discover(window=0.4, group=mdns.group, hostnames=())], [self.ha.url])

    def test_a_server_that_is_not_home_assistant_is_not_offered(self):
        response = ha_fakes.mdns_response("_home-assistant._tcp.local", "Fake", "fake.local", 9, "127.0.0.1",
                                          {"uuid": UUID, "base_url": "http://127.0.0.1:9"})
        mdns = ha_fakes.FakeMdns(response)
        self.addCleanup(mdns.close)
        self.assertEqual(ha_client.discover(window=0.3, group=mdns.group, hostnames=()), [])

    def test_a_silent_network_finds_nothing(self):
        mdns = ha_fakes.FakeMdns(None)
        self.addCleanup(mdns.close)
        self.assertEqual(ha_client.discover(window=0.3, group=mdns.group, hostnames=()), [])

    def test_the_previous_address_is_a_candidate_when_nothing_announces(self):
        mdns = ha_fakes.FakeMdns(None)
        self.addCleanup(mdns.close)
        found = ha_client.discover(hint=self.ha.url, window=0.3, group=mdns.group, hostnames=())
        self.assertEqual([(f.url, f.via) for f in found], [(self.ha.url, "hint")])

    def test_a_neighbours_home_assistant_is_never_adopted(self):
        mdns = ha_fakes.FakeMdns(announcement(self.ha, uuid="someone-else"))
        self.addCleanup(mdns.close)
        self.assertEqual(ha_client.discover(uuid=UUID, window=0.3, group=mdns.group, hostnames=()), [])

    def test_the_same_instance_is_found_at_its_new_address(self):
        mdns = ha_fakes.FakeMdns(announcement(self.ha))
        self.addCleanup(mdns.close)
        found = ha_client.discover(hint="http://127.0.0.1:9", uuid=UUID, window=0.4, group=mdns.group, hostnames=())
        self.assertEqual([f.url for f in found], [self.ha.url])


class SignInTests(unittest.TestCase):
    def setUp(self):
        self.ha = ha_fakes.FakeHomeAssistant()
        self.ha.require_auth = True
        self.addCleanup(self.ha.close)

    def test_authorize_url_is_the_standard_one(self):
        url = ha_client.authorize_url("http://ha:8123/", CLIENT_ID, CLIENT_ID + "ha/callback", "s1")
        self.assertEqual(url, "http://ha:8123/auth/authorize?client_id=http%3A%2F%2F192.168.0.56%3A8781%2F&redirect_uri="
                              "http%3A%2F%2F192.168.0.56%3A8781%2Fha%2Fcallback&state=s1")

    def test_the_code_becomes_a_refresh_token_and_that_an_access_token(self):
        tokens = ha_client.exchange_code(self.ha.url, CLIENT_ID, self.ha.issue_code(CLIENT_ID))
        self.assertTrue(tokens.refresh_token)
        access = ha_client.refresh_access(self.ha.url, CLIENT_ID, tokens.refresh_token)
        self.assertIn(access.access_token, self.ha.access_tokens)
        self.assertEqual(access.expires_in, 1800)

    def test_a_code_of_another_client_is_refused(self):
        with self.assertRaises(ha_client.AuthRevoked):
            ha_client.exchange_code(self.ha.url, "http://evil/", self.ha.issue_code(CLIENT_ID))

    def test_a_revoked_refresh_token_is_reported_as_revoked(self):
        tokens = ha_client.exchange_code(self.ha.url, CLIENT_ID, self.ha.issue_code(CLIENT_ID))
        self.assertTrue(ha_client.revoke(self.ha.url, tokens.refresh_token))
        with self.assertRaises(ha_client.AuthRevoked):
            ha_client.refresh_access(self.ha.url, CLIENT_ID, tokens.refresh_token)

    def test_an_unreachable_server_is_an_error_not_a_revocation(self):
        self.ha.stop_listening()
        with self.assertRaises(ha_client.HaError) as caught:
            ha_client.refresh_access(self.ha.url, CLIENT_ID, "x")
        self.assertNotIsInstance(caught.exception, ha_client.AuthRevoked)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.ha = ha_fakes.FakeHomeAssistant([ha_fakes.entity("light.kitchen", "on")])
        self.ha.require_auth = True
        self.addCleanup(self.ha.close)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = ha_client.Store(Path(self.dir.name) / "ha-connection.json")
        refresh = ha_client.exchange_code(self.ha.url, CLIENT_ID, self.ha.issue_code(CLIENT_ID)).refresh_token
        self.store.save(ha_client.Connection(self.ha.url, "oauth", refresh, CLIENT_ID, UUID, "Test Home"))
        self.client = ha_client.HaClient(self.store, discover_fn=lambda **_: [])

    def token_calls(self):
        return [r for r in self.ha.requests if r[1] == "/auth/token" and r[2].get("grant_type") == "refresh_token"]

    def test_no_connection_is_not_connected(self):
        with self.assertRaises(ha_client.NotConnected):
            ha_client.HaClient(ha_client.Store(Path(self.dir.name) / "missing.json")).get("/api/states")

    def test_a_read_carries_a_token_obtained_from_the_refresh_token(self):
        reply = self.client.get("/api/states/light.kitchen")
        self.assertEqual((reply.status, reply.json()["state"]), (200, "on"))

    def test_the_access_token_is_reused_until_it_is_about_to_expire(self):
        for _ in range(3):
            self.client.get("/api/states")
        self.assertEqual(len(self.token_calls()), 1)

    def test_an_expired_access_token_is_renewed_before_use(self):
        now = [1000.0]
        client = ha_client.HaClient(self.store, clock=lambda: now[0], discover_fn=lambda **_: [])
        client.get("/api/states")
        now[0] += 1800 - ha_client.ACCESS_MARGIN_S + 1
        client.get("/api/states")
        self.assertEqual(len(self.token_calls()), 2)

    def test_a_token_that_home_assistant_expired_early_is_renewed_once_and_the_request_repeated(self):
        self.client.get("/api/states")
        for token in list(self.ha.access_tokens):
            self.ha.access_tokens[token] = 0
        reply = self.client.get("/api/states")
        self.assertEqual(reply.status, 200)
        self.assertEqual(len(self.token_calls()), 2)

    def test_a_long_lived_token_is_sent_as_it_is(self):
        self.ha.long_lived.add("llat")
        self.store.save(ha_client.Connection(self.ha.url, "long_lived", "llat"))
        self.assertEqual(self.client.get("/api/states").status, 200)
        self.assertEqual(self.token_calls(), [])

    def test_a_service_call_posts_the_standard_path_and_body(self):
        reply = self.client.service("light", "turn_off", {"entity_id": "light.kitchen"})
        self.assertEqual(reply.status, 200)
        self.assertEqual(self.ha.calls(), [("/api/services/light/turn_off", {"entity_id": "light.kitchen"})])
        self.assertEqual(self.ha.states["light.kitchen"]["state"], "off")

    def test_return_response_keeps_its_query(self):
        self.client.service("weather", "get_forecasts", {"entity_id": "weather.home"}, return_response=True)
        self.assertEqual(self.ha.calls()[0][0], "/api/services/weather/get_forecasts?return_response")

    def test_a_revoked_grant_raises_auth_revoked(self):
        self.ha.refresh_tokens.clear()
        with self.assertRaises(ha_client.AuthRevoked):
            self.client.get("/api/states")


class RediscoveryTests(unittest.TestCase):
    def setUp(self):
        self.old = ha_fakes.FakeHomeAssistant()
        self.new = ha_fakes.FakeHomeAssistant([ha_fakes.entity("light.kitchen", "on")])
        for ha in (self.old, self.new):
            ha.require_auth = True
            self.addCleanup(ha.close)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = ha_client.Store(Path(self.dir.name) / "ha-connection.json")
        # Both servers share one refresh token: one Home Assistant that moved to another address.
        refresh = "r" * 32
        for ha in (self.old, self.new):
            ha.refresh_tokens[refresh] = CLIENT_ID
        self.store.save(ha_client.Connection(self.old.url, "oauth", refresh, CLIENT_ID, UUID))
        self.mdns = ha_fakes.FakeMdns(announcement(self.new))
        self.addCleanup(self.mdns.close)
        self.discover = functools.partial(ha_client.discover, group=self.mdns.group, hostnames=())

    def test_a_request_to_a_dead_address_answers_at_once_and_the_address_is_found_in_the_background(self):
        client = ha_client.HaClient(self.store, discover_fn=self.discover)
        client.get("/api/states")                         # a token is held
        self.old.stop_listening()
        started = time.monotonic()
        reply = client.get("/api/states/light.kitchen")
        self.assertEqual((reply.status, reply.error), (0, "refused"))
        self.assertLess(time.monotonic() - started, 0.5)  # the look for the new address is not waited for
        deadline = time.monotonic() + 3
        while self.store.load().url != self.new.url and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.store.load().url, self.new.url)
        self.assertEqual(client.get("/api/states/light.kitchen").json()["state"], "on")

    def test_looking_is_rate_limited(self):
        looks = []
        client = ha_client.HaClient(self.store, discover_fn=lambda **kw: looks.append(kw) or [])
        self.old.stop_listening()
        for _ in range(5):
            client.get("/api/states")
        deadline = time.monotonic() + 1
        while not looks and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.1)
        self.assertEqual(len(looks), 1)
        self.assertEqual((looks[0]["hint"], looks[0]["uuid"]), (self.old.url, UUID))

    def test_an_answer_from_home_assistant_never_starts_a_look(self):
        looks = []
        client = ha_client.HaClient(self.store, discover_fn=lambda **kw: looks.append(kw) or [])
        client.get("/api/states/none")                    # a 404 from a server that is there
        time.sleep(0.1)
        self.assertEqual(looks, [])

    def test_rediscover_stores_the_new_address(self):
        client = ha_client.HaClient(self.store, discover_fn=self.discover)
        self.old.stop_listening()
        self.assertEqual(client.rediscover(window=0.4), self.new.url)
        self.assertEqual(self.store.load().url, self.new.url)


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.ha = ha_fakes.FakeHomeAssistant()
        self.ha.require_auth = True
        self.addCleanup(self.ha.close)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = ha_client.Store(Path(self.dir.name) / "ha-connection.json")
        self.client = ha_client.HaClient(self.store, discover_fn=lambda **_: [])

    def connect(self):
        refresh = ha_client.exchange_code(self.ha.url, CLIENT_ID, self.ha.issue_code(CLIENT_ID)).refresh_token
        self.store.save(ha_client.Connection(self.ha.url, "oauth", refresh, CLIENT_ID, UUID))
        return refresh

    def test_not_connected_fails_at_the_store(self):
        status = self.client.status()
        self.assertEqual((status.connected, status.failing.id), (False, "store"))

    def test_a_working_connection_passes_every_step(self):
        self.connect()
        status = self.client.status()
        self.assertTrue(status.connected)
        self.assertEqual([(s.id, s.ok) for s in status.steps], [("store", True), ("address", True), ("reach", True), ("auth", True), ("api", True)])

    def test_a_dead_server_fails_at_reach(self):
        self.connect()
        self.ha.stop_listening()
        status = self.client.status(timeout=0.5)
        self.assertEqual((status.connected, status.failing.id), (False, "reach"))
        self.assertIn(self.ha.url, status.failing.detail)

    def test_a_revoked_grant_fails_at_auth_and_says_to_reconnect(self):
        refresh = self.connect()
        ha_client.revoke(self.ha.url, refresh)
        status = self.client.status()
        self.assertEqual(status.failing.id, "auth")
        self.assertIn("заново", status.failing.detail)

    def test_a_bad_long_lived_token_fails_at_the_api(self):
        self.store.save(ha_client.Connection(self.ha.url, "long_lived", "wrong"))
        status = self.client.status()
        self.assertEqual(status.failing.id, "api")
        self.assertIn("401", status.failing.detail)


if __name__ == "__main__":
    unittest.main()
