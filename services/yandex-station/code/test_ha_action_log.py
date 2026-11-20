"""The ha-cli action journal: one JSON line per action, bounded on disk, readable by time, and a Russian line per record."""
import io
import json
import os
import tempfile
import unittest

import ha_action_log


def action(**over):
    record = {"v": 1, "kind": "action", "ts": 1000.0, "id": "a1", "service": "light.turn_off", "entities": ["light.studio"],
              "expect": "turn_off", "before": {"light.studio": "on"},
              "call": {"status": 0, "error": "timeout", "ms": 2003},
              "verify": {"result": "ok", "reason": "state_matched", "attempts": 3, "ms": 842, "after": {"light.studio": "off"}},
              "reply": {"status": 200, "why": "verified_state"}, "ms": 870}
    record.update(over)
    return record


class Journal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "ha-actions.jsonl")
        self.err = io.StringIO()

    def journal(self, **kw):
        return ha_action_log.Journal(self.path, stream=self.err, **kw)

    def test_one_json_line_per_record_on_disk_and_in_the_stream(self):
        self.journal().write(action())
        with open(self.path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["verify"]["reason"], "state_matched")
        self.assertEqual(self.err.getvalue(), "ha_action " + lines[0] + "\n")

    def test_the_file_is_bounded_by_rotation(self):
        journal = self.journal(max_bytes=2000, backups=2)
        for index in range(200):
            journal.write(action(id=f"a{index}", ts=1000.0 + index))
        sizes = [os.path.getsize(p) for p in (self.path, self.path + ".1", self.path + ".2") if os.path.exists(p)]
        self.assertFalse(os.path.exists(self.path + ".3"))
        self.assertLessEqual(max(sizes), 2000 + 700)          # one record may cross the limit, never more
        self.assertGreaterEqual(len(sizes), 3)
        newest = ha_action_log.read(self.path, 1000.0 + 195, 1000.0 + 200)
        self.assertEqual([r["id"] for r in newest], ["a195", "a196", "a197", "a198", "a199"])

    def test_a_huge_record_is_cut_to_a_bounded_line(self):
        self.journal().write(action(before={f"light.l{i}": "on" for i in range(500)}, data={"x": "y" * 5000}))
        with open(self.path, encoding="utf-8") as fh:
            line = fh.read().strip()
        self.assertLessEqual(len(line), ha_action_log.MAX_LINE)
        self.assertTrue(json.loads(line)["cut"])

    def test_an_oversized_late_record_still_reads_back_as_json(self):
        climate = {f"climate.{'x' * 200}{i}": {"hvac_mode": "cool", "temperature": 24} for i in range(8)}
        self.journal().write({"kind": "late_verify", "ts": 1000.0, "id": "a1", "service": "climate.set_hvac_mode", "entities": list(climate),
                              "verify": {"result": "ok", "reason": "state_matched", "ms": 4000}, "climate": climate})
        found = ha_action_log.read(self.path, 999.0, 1001.0)
        self.assertEqual([(r["kind"], r["id"], r["cut"]) for r in found], [("late_verify", "a1", True)])
        self.assertEqual(found[0]["verify"]["result"], "ok")

    def test_secrets_never_reach_the_journal(self):
        self.journal().write(action(data={"token": "abcdef", "message": "привет", "brightness_pct": 30}))
        with open(self.path, encoding="utf-8") as fh:
            record = json.loads(fh.read())
        self.assertNotIn("token", record["data"])
        self.assertEqual(record["data"]["brightness_pct"], 30)

    def test_lock_and_alarm_codes_never_reach_the_journal(self):
        self.journal().write(action(data={"code": "4711", "pin": "1", "color_name": "red"}))
        with open(self.path, encoding="utf-8") as fh:
            record = json.loads(fh.read())
        self.assertEqual(record["data"], {"color_name": "red"})

    def test_a_lone_surrogate_in_a_text_does_not_lose_the_record_or_break_reading(self):
        self.journal().write(action(data={"effect": "a\udc80b"}))
        self.assertEqual(len(ha_action_log.read(self.path, 0, 1e12)), 1)
        with open(self.path, "ab") as fh:
            fh.write(b'{"ts":1,"x":"\xd0"}\n')       # a Cyrillic letter cut in half by a short write
        self.assertEqual(len(ha_action_log.read(self.path, 0, 1e12)), 2)

    def test_unwritable_path_never_breaks_the_caller(self):
        journal = ha_action_log.Journal(os.path.join(self.tmp.name, "missing", "dir", "x.jsonl"), stream=self.err)
        journal.write(action())
        self.assertIn("ha_action ", self.err.getvalue())


class Reading(unittest.TestCase):
    def test_window_and_partial_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.jsonl")
            journal = ha_action_log.Journal(path, stream=io.StringIO())
            for ts in (10.0, 20.0, 30.0):
                journal.write(action(ts=ts, id=f"t{int(ts)}"))
            with open(path, "a", encoding="utf-8") as fh:
                fh.write('{"v": 1, "ts": 25.0, "kind"')     # a record being written
            got = ha_action_log.read(path, 15.0, 31.0)
            self.assertEqual([r["id"] for r in got], ["t20", "t30"])
            self.assertEqual(ha_action_log.read(path + ".missing", 0, 100), [])


class RussianLines(unittest.TestCase):
    def test_verified_after_a_timeout(self):
        self.assertEqual(ha_action_log.action_line(action()),
                         "HA light.turn_off light.studio → проверено: light.studio выкл за 0.8 с (ответ HA: таймаут 2.0 с)")

    def test_verified_plainly(self):
        record = action(call={"status": 200, "ms": 120}, verify={"result": "ok", "reason": "state_matched", "ms": 310,
                                                                  "after": {"light.studio": "off"}})
        self.assertEqual(ha_action_log.action_line(record), "HA light.turn_off light.studio → проверено: light.studio выкл за 0.3 с")

    def test_partial_verification_is_not_called_plain_verification(self):
        record = action(call={"status": 200, "ms": 120}, verify={"result": "ok", "reason": "state_matched", "ms": 310,
                                                                  "after": {"light.studio": "on"}, "unchecked": ["color_temp_kelvin"]})
        self.assertEqual(ha_action_log.action_line(record),
                         "HA light.turn_off light.studio → проверено частично: light.studio вкл за 0.3 с; не проверено: color_temp_kelvin")

    def test_unconfirmed_when_ha_acknowledged_but_the_device_did_not_change(self):
        record = action(call={"status": 200, "ms": 120}, verify={"result": "failed", "reason": "state_mismatch", "ms": 2400,
                                                                  "after": {"light.studio": "on"}, "wrong": {"light.studio": "on"}})
        self.assertEqual(ha_action_log.action_line(record),
                         "HA light.turn_off light.studio → не подтверждено: light.studio вкл (HA ответил 200)")

    def test_no_answer_is_not_a_reboot(self):
        record = action(call={"status": 0, "error": "timeout", "ms": 2400}, verify={"result": "unknown", "reason": "ha_no_answer", "ms": 2600})
        text = ha_action_log.action_line(record)
        self.assertIn("не проверено", text)
        self.assertNotIn("перезагруз", text)

    def test_observed_outage(self):
        record = action(call={"status": 0, "error": "refused", "ms": 3}, verify={"result": "unknown", "reason": "ha_down_observed", "ms": 260},
                        reply={"status": 202, "why": "queued_ha_down"})
        self.assertIn("HA недоступен (соединение отклонено)", ha_action_log.action_line(record))

    def test_skipped_and_rejected(self):
        self.assertIn("проверка невозможна", ha_action_log.action_line(
            action(service="tts.speak", entities=["tts.cloud"], verify={"result": "skipped", "reason": "not_verifiable"}, call={"status": 200, "ms": 90})))
        self.assertIn("HA отклонил", ha_action_log.action_line(
            action(call={"status": 400, "ms": 20}, verify={"result": "skipped", "reason": "rejected"})))

    def test_status_change_record(self):
        record = {"v": 1, "kind": "ha_status", "ts": 5.0, "from": "up", "to": "down", "error": "refused"}
        self.assertEqual(ha_action_log.action_line(record), "HA: up → down (соединение отклонено)")


if __name__ == "__main__":
    unittest.main()
