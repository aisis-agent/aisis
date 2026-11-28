"""Home Assistant's own logbook as the record of what AISIS did: only the entries of AISIS's user, never a raise, never a long wait."""
import os
import sys
import tempfile
import time
import unittest
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_client  # noqa: E402
import ha_fakes  # noqa: E402
import ha_logbook  # noqa: E402

START, END = 1_800_000_000.0, 1_800_000_060.0


def entry(name, state, user, when="2026-11-26T10:00:05+00:00", **extra):
    return {"when": when, "name": name, "state": state, "entity_id": "light.x", "context_user_id": user, **extra}


class Logbook(unittest.TestCase):
    def setUp(self):
        self.ha = ha_fakes.FakeHomeAssistant()
        self.ha.require_auth = True
        self.ha.long_lived.add("llat")
        self.addCleanup(self.ha.close)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        store = ha_client.Store(Path(self.dir.name) / "connection.json")
        store.save(ha_client.Connection(self.ha.url, "long_lived", "llat"))
        self.client = ha_client.HaClient(store, discover_fn=lambda **_: [])

    def test_only_the_entries_of_the_signed_in_user_become_lines(self):
        self.ha.logbook = [entry("Студия", "выключен", "user-aisis"), entry("Кухня", "включен", "someone-else"), entry("Датчик", "ok", None)]
        got = ha_logbook.lines(self.client, START, END)
        self.assertEqual(len(got), 1)
        rank, text = got[0]
        self.assertIsInstance(rank, int)
        clock = datetime(2026, 11, 26, 10, 0, 5, tzinfo=timezone.utc).astimezone().strftime("%H:%M:%S")
        self.assertEqual(text, f"Home Assistant: Студия → выключен ({clock})")

    def test_a_message_stands_in_for_a_missing_state(self):
        self.ha.logbook = [{"when": "2026-11-26T10:00:05+00:00", "name": "Сценарий", "message": "запущен", "context_user_id": "user-aisis"}]
        self.assertIn("Home Assistant: Сценарий → запущен (", ha_logbook.lines(self.client, START, END)[0][1])

    def test_the_window_goes_to_home_assistant_as_iso_times(self):
        ha_logbook.lines(self.client, START, END)
        path = next(p for m, p, _ in self.ha.requests if m == "GET" and p.startswith("/api/logbook/"))
        start_part, query = path.removeprefix("/api/logbook/").split("?")
        self.assertEqual(urllib.parse.unquote(start_part), datetime.fromtimestamp(START, timezone.utc).isoformat(timespec="seconds"))
        self.assertEqual(urllib.parse.parse_qs(query)["end_time"], [datetime.fromtimestamp(END, timezone.utc).isoformat(timespec="seconds")])

    def test_signing_in_as_another_user_changes_whose_entries_are_shown(self):
        """The same Home Assistant address, a new grant for another user: the old user's actions are not AISIS' any more."""
        self.ha.logbook = [entry("Студия", "выключен", "user-aisis"), entry("Кухня", "включен", "user-new")]
        self.assertEqual([text.split(" → ")[0] for _, text in ha_logbook.lines(self.client, START, END)], ["Home Assistant: Студия"])
        self.ha.user_id = "user-new"
        self.assertEqual([text.split(" → ")[0] for _, text in ha_logbook.lines(self.client, START, END)], ["Home Assistant: Кухня"])

    def test_no_connection_gives_no_lines(self):
        client = ha_client.HaClient(ha_client.Store(Path(self.dir.name) / "none.json"), discover_fn=lambda **_: [])
        self.assertEqual(ha_logbook.lines(client, START, END), [])

    def test_home_assistant_down_gives_no_lines(self):
        self.ha.close()
        started = time.monotonic()
        self.assertEqual(ha_logbook.lines(self.client, START, END), [])
        self.assertLess(time.monotonic() - started, ha_logbook.TOTAL_S + 0.5)

    def test_a_wedged_home_assistant_is_cut_at_the_total_cap(self):
        self.ha.hang_ws = True
        started = time.monotonic()
        self.assertEqual(ha_logbook.lines(self.client, START, END), [])
        self.assertLess(time.monotonic() - started, ha_logbook.TOTAL_S + 0.5)

    def test_a_refused_logbook_gives_no_lines(self):
        self.ha.long_lived.clear()
        self.assertEqual(ha_logbook.lines(self.client, START, END), [])


if __name__ == "__main__":
    unittest.main()
