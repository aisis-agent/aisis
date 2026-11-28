"""Quasar log lines that are not failures: a closed WebSocket is how the speaker ends every connection (owner tg#30668).

The quasar router logs `UniProxy WebSocket closed` at WARN for every connection that ends: the dialog's own socket after each
exchange, the spotter-log socket half a second after each wake word, the deploy smoke probes. The ingester used to take any
WARN/ERROR line that fell into an open dialog for that dialog's failure, so 173 of 492 dialogs (2026-11-19) showed a red
«ошибка: uniproxy.alice.yandex.net.ts:49: UniProxy WebSocket closed» although the exchange went fine. The same holds for an optional
helper that fails open (ASR correction, the semantic shadow): its warning is a note, not the dialog's failure.
"""
import os
import tempfile
import time
import unittest

import dialogstore

SRC = "docker:aisis-quasar-prod-green"
ROUTER = "src/routers/uniproxy.alice.yandex.net.ts:49"
CONN = "src/routers/uniproxy/uniproxy-connection.ts"
DIALOG = "7915068e-1111-4222-8333-444455556666"


def log(clock, level, where, message):
    return f"2026-11-19 {clock}\t{level}\t{where}\t{message}"


def spoken_exchange(*middle):
    """The shape of 2026-11-19 11:21: a dialog, the spotter-log socket closing, what was heard, the dialog finishing."""
    return [log("11:21:05.838", "INFO", f"{CONN}:758", f"Dialog {DIALOG} started"), *middle,
            log("11:21:07.481", "INFO", "src/routers/uniproxy/voice-input-handler.ts:185", "Heard: Какая погода."),
            log("11:21:12.949", "INFO", f"{CONN}:270", f"Dialog {DIALOG} ended: serverFinished")]


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = dialogstore.init_db(os.path.join(self.tmp.name, "dialogs.sqlite3"))
        self.addCleanup(self.conn.close)

    def replay(self, lines):
        parser = dialogstore.DialogParser(self.conn, SRC)
        for line in lines:
            parser.feed(self.conn, line)
        self.conn.commit()
        return dialogstore.row_to_api(self.conn.execute("SELECT * FROM dialogs WHERE id=?", (DIALOG,)).fetchone(), time.time())

    def error_turns(self):
        return [r["text"] for r in self.conn.execute("SELECT text FROM dialog_turns WHERE kind='error'")]


class NormalClose(Store):
    def test_a_close_during_an_exchange_that_went_fine_is_not_an_error(self):
        closed = log("11:21:06.883", "WARN", ROUTER, "UniProxy WebSocket closed")
        dialog = self.replay(spoken_exchange(closed))
        self.assertEqual(dialog["err"], "")
        self.assertTrue(dialog["ok"], "the dashboard lists the exchange as fine")
        self.assertEqual(self.error_turns(), [])
        self.assertEqual(dialog["heard"], ["Какая погода."])

    def test_the_close_line_stays_in_the_raw_log_and_is_counted_once_per_close(self):
        closes = [log("11:21:06.883", "WARN", ROUTER, "UniProxy WebSocket closed"),
                  log("11:21:06.990", "WARN", ROUTER, "UniProxy WebSocket closed"),
                  log("11:21:07.100", "INFO", ROUTER, "UniProxy WebSocket closed (normal): code=1000 device=D age=900ms")]
        dialog = self.replay(spoken_exchange(*closes))
        self.assertEqual(sum("UniProxy WebSocket closed" in line for line in dialog["raw"]), 3)
        self.assertEqual(dialogstore.normal_close_count(dialog["raw"]), 3)
        self.assertEqual(dialog["err"], "")

    def test_a_real_warning_still_fails_the_dialog_and_a_close_before_it_does_not_hide_it(self):
        closed = log("11:21:06.883", "WARN", ROUTER, "UniProxy WebSocket closed")
        broken = log("11:21:07.000", "ERROR", f"{CONN}:1218", "Error happened in VoiceInputHandler: ")
        dialog = self.replay(spoken_exchange(closed, broken))
        self.assertEqual(dialog["err"], "uniproxy-connection.ts:1218: Error happened in VoiceInputHandler: ")
        self.assertFalse(dialog["ok"])
        self.assertEqual(self.error_turns(), ["uniproxy-connection.ts:1218: Error happened in VoiceInputHandler: "])

    def test_a_follow_up_listen_that_ended_without_a_voice_input_is_still_a_warning(self):
        text = "WebSocket closed 3529ms after a ShouldListen response (1134ms after the last TTS stream closed) without a follow-up VoiceInput"
        dialog = self.replay(spoken_exchange(log("11:21:06.883", "WARN", f"{CONN}:329", text)))
        self.assertTrue(dialog["err"].startswith("uniproxy-connection.ts:329: WebSocket closed 3529ms"))


class AbnormalClose(Store):
    def test_a_connection_that_dropped_without_a_close_frame_is_a_failure(self):
        dropped = log("11:21:06.883", "WARN", ROUTER, "UniProxy WebSocket closed abnormally: code=1006 device=D age=900ms")
        dialog = self.replay(spoken_exchange(dropped))
        self.assertTrue(dialog["err"].endswith("UniProxy WebSocket closed abnormally: code=1006 device=D age=900ms"))
        self.assertFalse(dialog["ok"])
        self.assertEqual(dialogstore.normal_close_count(dialog["raw"]), 0)


class Classifier(unittest.TestCase):
    def test_which_lines_are_failures(self):
        cases = [("UniProxy WebSocket closed", False),
                 ("UniProxy WebSocket closed (normal): code=1000 device=D age=5ms", False),
                 ("UniProxy WebSocket closed (normal): code=1005 device=D age=5ms", False),
                 ("UniProxy WebSocket closed abnormally: code=1006 device=D age=5ms", True),
                 ("UniProxy WebSocket closed: code=1006", True),   # no known form: kept as a failure
                 ("UniProxy WebSocket closed (normal)", False),
                 ("UniProxy WebSocket error: Error: read ECONNRESET", True),
                 ("Error happened in VoiceInputHandler: ", True),
                 ("Fast intent 'включи свет' failed: HA 500", True),
                 ("Filler index not available at /src/fillers/index.json", True),
                 # optional helpers that fail open: the quasar source says the turn goes on exactly as without them
                 ("ASR correction failed for 'Громкость.' after 851ms: TimeoutError: The operation was aborted due to timeout", False),
                 ("ASR correction http 500 for 'Громкость.'", False),
                 ("SEMANTIC shadow: laya http 503 for 'Какая погода.' (zone=nomatch)", False),
                 ("SEMANTIC shadow: laya unavailable for 'Какая погода.' (zone=nomatch)", False),
                 ("SEMANTIC shadow: laya error for 'Какая погода.' (zone=nomatch): TypeError", False)]
        for message, failure in cases:
            self.assertEqual(dialogstore.is_failure("WARN", message), failure, message)

    def test_notes_name_the_helper_that_did_not_answer(self):
        raw = ["12:00:01 [WARN] SEMANTIC shadow: laya http 503 for 'Какая погода.' (zone=nomatch)",
               "12:00:02 [WARN] ASR correction failed for 'Малис.' after 851ms: TimeoutError: The operation was aborted due to timeout",
               "12:00:03 [WARN] UniProxy WebSocket closed", "12:00:04 [ERROR] Error happened in VoiceInputHandler: ",
               "12:00:05 [INFO] SEMANTIC shadow: text='включи свет' scoreIntent=null laya=abstain(0.10) zone=nomatch ms=12"]
        self.assertEqual(dialogstore.log_notes(raw),
                         [("12:00:01", "теневая проверка смысла (на ответ не влияет): SEMANTIC shadow: laya http 503 for 'Какая погода.' (zone=nomatch)"),
                          ("12:00:02", "коррекция ASR не ответила, взят исходный текст: ASR correction failed for 'Малис.' after 851ms: "
                                       "TimeoutError: The operation was aborted due to timeout")])

    def test_a_dialog_whose_only_warning_is_a_helper_that_failed_open_is_not_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = dialogstore.init_db(os.path.join(tmp, "d.sqlite3"))
            try:
                lines = spoken_exchange(log("11:21:06.883", "WARN", ROUTER, "UniProxy WebSocket closed"),
                                        log("11:21:06.990", "WARN", "src/backend/semantic-shadow.ts:62",
                                            "SEMANTIC shadow: laya http 503 for 'Какая погода.' (zone=nomatch)"))
                parser = dialogstore.DialogParser(conn, SRC)
                for line in lines:
                    parser.feed(conn, line)
                row = dialogstore.row_to_api(conn.execute("SELECT * FROM dialogs WHERE id=?", (DIALOG,)).fetchone(), time.time())
                self.assertEqual(row["err"], "")
                self.assertTrue(row["ok"])
                self.assertEqual(len(dialogstore.log_notes(row["raw"])), 1)
            finally:
                conn.close()

    def test_replaying_the_stored_lines_of_one_day_flags_no_dialog_for_a_close(self):
        """The 2026-11-19 11:20-11:21 lines as the container logged them."""
        lines = [log("11:20:01.855", "INFO", f"{CONN}:758", "Dialog 6eb5dabd-1111-4222-8333-444455556666 started"),
                 log("11:20:03.140", "WARN", ROUTER, "UniProxy WebSocket closed"),
                 log("11:20:03.172", "INFO", f"{CONN}:270", "Dialog 6eb5dabd-1111-4222-8333-444455556666 ended: serverFinished"),
                 *spoken_exchange(log("11:21:06.883", "WARN", ROUTER, "UniProxy WebSocket closed"))]
        with tempfile.TemporaryDirectory() as tmp:
            conn = dialogstore.init_db(os.path.join(tmp, "d.sqlite3"))
            try:
                parser = dialogstore.DialogParser(conn, SRC)
                for line in lines:
                    parser.feed(conn, line)
                self.assertEqual(conn.execute("SELECT count(*) FROM dialogs WHERE errors!=''").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT count(*) FROM dialog_turns WHERE kind='error'").fetchone()[0], 0)
            finally:
                conn.close()


class Migration(Store):
    """Dialogs stored before the rule changed carry the close as their error; one start of the ingester clears them, once."""

    def seed(self):
        for line in spoken_exchange(log("11:21:06.883", "WARN", ROUTER, "UniProxy WebSocket closed"),
                                    log("11:21:07.000", "WARN", f"{CONN}:329", "WebSocket closed 3529ms after a ShouldListen response without a follow-up")):
            clock = line.split("\t")[0][11:]
            dialogstore.store_line(self.conn, None, f"2026-11-19T{clock}000000Z", SRC, line)
        stale = "uniproxy.alice.yandex.net.ts:49: UniProxy WebSocket closed"
        self.conn.execute("INSERT INTO dialogs(id,station,kind,src,ts_start,status,errors) VALUES(?,?,?,?,?,?,?)",
                          (DIALOG, "S", "voice", SRC, 1.0, "serverFinished", stale))
        self.conn.execute("INSERT INTO dialogs(id,station,kind,src,ts_start,status,errors) VALUES(?,?,?,?,?,?,?)",
                          ("t-1", "S", "voice", "transcript", 2.0, "complete", stale))
        self.conn.execute("INSERT INTO dialogs(id,station,kind,src,ts_start,status,errors) VALUES(?,?,?,?,?,?,?)",
                          ("t-2", "S", "voice", "transcript", 3.0, "complete", "uniproxy-connection.ts:1218: Error happened in VoiceInputHandler: "))
        self.conn.execute("INSERT INTO dialog_turns(dialog_id,ts,role,kind,text) VALUES('t-1',2.0,'event','error',?)", (stale,))
        self.conn.commit()

    def errors(self):
        return {r["id"]: r["errors"] for r in self.conn.execute("SELECT id, errors FROM dialogs")}

    def test_stored_closes_stop_being_errors_and_real_errors_stay(self):
        self.seed()
        self.assertTrue(dialogstore.reclassify_failures(self.conn))
        errors = self.errors()
        self.assertTrue(errors[DIALOG].startswith("uniproxy-connection.ts:329: WebSocket closed 3529ms"), "a docker dialog is re-derived from its lines")
        self.assertEqual(errors["t-1"], "", "a dialog imported from a transcript has no lines: its stored close text is cleared")
        self.assertEqual(errors["t-2"], "uniproxy-connection.ts:1218: Error happened in VoiceInputHandler: ")
        self.assertEqual(self.error_turns(), [e for e in self.error_turns() if "UniProxy WebSocket closed" not in e])

    def test_an_imported_dialog_keeps_its_station_through_the_rebuild(self):
        self.seed()
        self.conn.execute("UPDATE dialogs SET station='other' WHERE id=?", (DIALOG,))
        self.conn.commit()
        dialogstore.reclassify_failures(self.conn)
        stations = {r["id"]: r["station"] for r in self.conn.execute("SELECT id, station FROM dialogs")}
        self.assertEqual(stations[DIALOG], "other")
        self.assertEqual(stations["t-1"], "S")

    def test_it_runs_once(self):
        self.seed()
        self.assertTrue(dialogstore.reclassify_failures(self.conn))
        self.conn.execute("UPDATE dialogs SET errors='x' WHERE id='t-1'")
        self.conn.commit()
        self.assertFalse(dialogstore.reclassify_failures(self.conn))
        self.assertEqual(self.errors()["t-1"], "x")


class StationNamed(Store):
    """UB-277: the quasar fork ends its dialog start/end lines with the station id; the store reads them as it read the old lines."""

    def test_a_dialog_whose_lines_name_the_station_is_stored_with_its_status(self):
        lines = spoken_exchange()
        lines[0] = lines[0] + " station=M0TESTSPEAKER1"
        lines[-1] = lines[-1] + " station=M0TESTSPEAKER1"
        dialog = self.replay(lines)
        self.assertEqual(dialog["status"], "serverFinished")
        self.assertEqual(dialog["heard"], ["Какая погода."])
        self.assertTrue(dialog["ok"])


if __name__ == "__main__":
    unittest.main()
