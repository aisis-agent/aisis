"""Music Assistant's address: MA_URL when configured, else what `_mass._tcp` announces on the local network (cached a minute)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_client  # noqa: E402
import ha_fakes  # noqa: E402
import music_airplay  # noqa: E402


class MusicAssistantAddress(unittest.TestCase):
    def setUp(self):
        music_airplay._discovered = None
        for patcher in (mock.patch.dict(os.environ, {"MA_URL": "", "MA_TOKEN": "t"}), mock.patch.object(music_airplay, "MA_ENV_FILE", "/nonexistent")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def announce(self, txt):
        mdns = ha_fakes.FakeMdns(ha_fakes.mdns_response(music_airplay.MA_SERVICE_TYPE, "Music Assistant", "ma.local", 8095, "10.1.2.3", txt))
        self.addCleanup(mdns.close)
        browse = ha_client.browse
        patcher = mock.patch.object(ha_client, "browse", lambda service_type, **kw: browse(service_type, window=0.3, group=mdns.group))
        patcher.start()
        self.addCleanup(patcher.stop)
        return mdns

    def test_the_announced_address_is_used(self):
        self.announce({})
        self.assertEqual(music_airplay._cfg()["url"], "http://10.1.2.3:8095")

    def test_the_announced_base_url_wins(self):
        self.announce({"base_url": "http://music.lan:8095/"})
        self.assertEqual(music_airplay._cfg()["url"], "http://music.lan:8095")

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
