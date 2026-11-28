"""Music Assistant's address: MA_URL when configured, else what `_mass._tcp` announces on the local network (cached a minute) — but only an
announcement that names the host of the connected Home Assistant: the bearer token goes where the owner already trusted, never to whoever
answers a multicast question."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_client  # noqa: E402
import ha_fakes  # noqa: E402
import music_airplay  # noqa: E402

HA_HOST = "10.1.2.3"


class MusicAssistantAddress(unittest.TestCase):
    def setUp(self):
        music_airplay._discovered = None
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = ha_client.Store(Path(tmp.name) / "connection.json")
        store.save(ha_client.Connection(f"http://{HA_HOST}:8123", "long_lived", "llat"))
        self.client = ha_client.HaClient(store, discover_fn=lambda **_: [])
        for patcher in (mock.patch.dict(os.environ, {"MA_URL": "", "MA_TOKEN": "t"}), mock.patch.object(music_airplay, "MA_ENV_FILE", "/nonexistent"),
                        mock.patch.object(ha_client, "default", lambda: self.client)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def announce(self, txt, address=HA_HOST):
        mdns = ha_fakes.FakeMdns(ha_fakes.mdns_response(music_airplay.MA_SERVICE_TYPE, "Music Assistant", "ma.local", 8095, address, txt))
        self.addCleanup(mdns.close)
        browse = ha_client.browse
        patcher = mock.patch.object(ha_client, "browse", lambda service_type, **kw: browse(service_type, window=0.3, group=mdns.group))
        patcher.start()
        self.addCleanup(patcher.stop)
        return mdns

    def test_the_announced_address_of_the_home_assistant_host_is_used(self):
        self.announce({})
        self.assertEqual(music_airplay._cfg()["url"], f"http://{HA_HOST}:8095")

    def test_the_announced_base_url_wins_on_the_home_assistant_host(self):
        self.announce({"base_url": f"http://{HA_HOST}:9095/"})
        self.assertEqual(music_airplay._cfg()["url"], f"http://{HA_HOST}:9095")

    def test_an_announcement_of_another_host_never_gets_the_token(self):
        self.announce({"base_url": "http://attacker.lan:8095"}, address="10.9.9.9")
        self.assertEqual(music_airplay._cfg()["url"], "")

    def test_an_address_of_another_host_is_not_used_either(self):
        self.announce({}, address="10.9.9.9")
        self.assertEqual(music_airplay._cfg()["url"], "")

    def test_without_a_home_assistant_connection_nothing_is_trusted(self):
        self.announce({})
        with mock.patch.object(ha_client, "default", side_effect=ha_client.NotConnected("нет подключения")):
            self.assertEqual(music_airplay._cfg()["url"], "")

    def test_the_announced_https_is_kept(self):
        self.announce({"base_url": f"https://{HA_HOST}:9095"})
        self.assertEqual(music_airplay._cfg()["url"], f"https://{HA_HOST}:9095")

    def test_an_ipv6_home_assistant_gets_a_bracketed_url(self):
        self.client.store.save(ha_client.Connection("http://[fd00::1]:8123", "long_lived", "llat"))
        self.announce({"base_url": "http://[fd00::1]:8095"})
        self.assertEqual(music_airplay._cfg()["url"], "http://[fd00::1]:8095")

    def test_an_announced_default_port_and_base_path_are_kept(self):
        self.announce({"base_url": f"https://{HA_HOST}/music"})
        self.assertEqual(music_airplay._cfg()["url"], f"https://{HA_HOST}/music")

    def test_a_malformed_announcement_is_skipped_not_raised(self):
        self.announce({"base_url": "http://["})
        self.assertEqual(music_airplay._cfg()["url"], "")
        self.announce({"base_url": f"http://{HA_HOST}:bad"})
        self.assertEqual(music_airplay._cfg()["url"], "")

    def test_a_home_assistant_connected_by_name_discovers_nothing(self):
        """A name is answered by the network too: only an owner-confirmed IP address is trusted; MA_URL covers the rest."""
        self.client.store.save(ha_client.Connection("http://localhost:8123", "long_lived", "llat"))
        self.announce({}, address="127.0.0.1")
        self.assertEqual(music_airplay._cfg()["url"], "")

    def test_a_warm_answer_is_not_served_after_the_connection_goes(self):
        self.announce({})
        self.assertEqual(music_airplay._cfg()["url"], f"http://{HA_HOST}:8095")
        with mock.patch.object(ha_client, "default", side_effect=ha_client.NotConnected("нет подключения")):
            self.assertEqual(music_airplay._cfg()["url"], "")
        self.client.store.save(ha_client.Connection("http://10.9.9.9:8123", "long_lived", "llat"))
        self.assertEqual(music_airplay._cfg()["url"], "")          # another host is a new look, whose announcement names the old one

    def test_the_answer_is_cached_for_a_minute(self):
        mdns = self.announce({})
        music_airplay._cfg()
        asked = mdns.queries
        music_airplay._cfg()
        self.assertEqual(mdns.queries, asked)
        music_airplay._discovered = (music_airplay._discovered[0] - 61, music_airplay._discovered[1])
        music_airplay._cfg()
        self.assertGreater(mdns.queries, asked)

    def test_a_configured_url_needs_no_discovery(self):
        with mock.patch.dict(os.environ, {"MA_URL": "http://ma.example:8095/"}), mock.patch.object(ha_client, "browse", side_effect=AssertionError):
            self.assertEqual(music_airplay._cfg()["url"], "http://ma.example:8095")

    def test_nothing_announced_is_an_honest_error(self):
        mdns = ha_fakes.FakeMdns(None)
        self.addCleanup(mdns.close)
        browse = ha_client.browse
        with mock.patch.object(ha_client, "browse", lambda service_type, **kw: browse(service_type, window=0.2, group=mdns.group)):
            self.assertEqual(music_airplay._cfg()["url"], "")
            with self.assertRaisesRegex(music_airplay.MaError, "Music Assistant не найден в сети"):
                music_airplay.ma_call("music/artists/library_items")


if __name__ == "__main__":
    unittest.main()
