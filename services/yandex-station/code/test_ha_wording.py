"""The Russian wording of a Home Assistant call's verdict and of the reason of a reply."""
import unittest

import ha_wording


def verdict(verify, ha=None, ha_ms=None, down=None):
    return ha_wording.verdict_line(verify, ha=ha, ha_ms=ha_ms, down=down)


class VerdictLine(unittest.TestCase):
    def test_verified_after_a_timeout(self):
        self.assertEqual(verdict({"result": "ok", "reason": "state_matched", "ms": 842, "after": {"light.studio": "off"}}, ha="timeout", ha_ms=2003),
                         "проверено: light.studio выкл за 0.8 с (ответ HA: таймаут 2.0 с)")

    def test_verified_plainly(self):
        self.assertEqual(verdict({"result": "ok", "reason": "state_matched", "ms": 310, "after": {"light.studio": "off"}}, ha="200"),
                         "проверено: light.studio выкл за 0.3 с")

    def test_partial_verification_is_not_called_plain_verification(self):
        verify = {"result": "ok", "reason": "state_matched", "ms": 310, "after": {"light.studio": "on"}, "unchecked": ["color_temp_kelvin"]}
        self.assertEqual(verdict(verify, ha="200"), "проверено частично: light.studio вкл за 0.3 с; не проверено: color_temp_kelvin")

    def test_unconfirmed_when_ha_acknowledged_but_the_device_did_not_change(self):
        verify = {"result": "failed", "reason": "state_mismatch", "ms": 2400, "after": {"light.studio": "on"}, "wrong": {"light.studio": "on"}}
        self.assertEqual(verdict(verify, ha="200"), "не подтверждено: light.studio вкл (HA ответил 200)")

    def test_no_answer_is_not_a_reboot(self):
        text = verdict({"result": "unknown", "reason": "ha_no_answer", "ms": 2600}, ha="timeout", ha_ms=2400)
        self.assertIn("не проверено", text)
        self.assertNotIn("перезагруз", text)

    def test_an_observed_outage_says_the_command_was_not_sent(self):
        text = verdict({"result": "unknown", "reason": "ha_down_observed", "ms": 260}, ha="refused", down="refused")
        self.assertEqual(text, "Home Assistant был недоступен, команда не отправлена (соединение отклонено)")
        self.assertNotIn("очеред", text)

    def test_skipped_and_rejected(self):
        self.assertIn("проверка невозможна", verdict({"result": "skipped", "reason": "not_verifiable"}, ha="200"))
        self.assertIn("HA отклонил", verdict({"result": "skipped", "reason": "rejected"}, ha="400"))

    def test_a_call_lost_in_flight_names_no_queue(self):
        text = verdict({"result": "unknown", "reason": "ha_down_in_flight"})
        self.assertIn("HA пропал", text)
        self.assertNotIn("очеред", text)


class ReplyReasons(unittest.TestCase):
    def test_no_reason_mentions_a_queue_or_the_old_proxy(self):
        for code, text in ha_wording.WHY_RU.items():
            self.assertNotIn("очеред", text, code)
        self.assertNotIn("ha_queued_unverified", ha_wording.WHY_RU)
        self.assertNotIn("queued_ha_down", ha_wording.WHY_RU)

    def test_outage_reason(self):
        self.assertEqual(ha_wording.WHY_RU["ha_down_observed"], "Home Assistant был недоступен, команда не отправлена")


if __name__ == "__main__":
    unittest.main()
