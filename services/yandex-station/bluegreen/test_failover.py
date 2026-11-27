"""aisis-failover's `asr` component: Wyoming is restarted only on the gateway's explicit 503, never because the gateway itself
is down, does not know the route, or skipped the probe for live traffic (found in review: a dead gateway made the watchdog
restart a healthy recognizer that Home Assistant shares). A probe without a verdict neither counts nor clears a failure.

The `pm` component is the voice assistant's ChatGPT reserve bridge (aisis-station-pm). It cannot run without the browser the pm
product owns on the debugging port, so while that port has no listener the bridge's failed probe is no verdict (2026-11-27: the
watchdog alerted and restarted the unit every 30 s for 399 s, then again, while the Guardian stack and its browser were stopped
on purpose)."""
import dataclasses
import http.server
import math
import os
import socket
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import failover


def serve(status):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.send_header("content-length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def watchdog(state_dir):
    return failover.Watchdog(env="stage", interval=1.5, threshold=3, component_threshold=6, http_timeout=1.2,
                             components=True, alert=False, state_dir=Path(state_dir))


class AsrComponentTest(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.state_dir = scratch.name

    asr = next(comp for comp in failover.COMPONENTS if comp.name == "asr")

    def verdict(self, status):
        """probe_component's verdict for the asr component against a server answering `status` (None: nothing listens)."""
        if status is None:
            comp = failover.Component("asr", "http", "http://127.0.0.1:1/healthz/asr", fails_on=503)
            return watchdog(self.state_dir).probe_component(comp)[0]
        server = serve(status)
        try:
            comp = failover.Component("asr", "http", f"http://127.0.0.1:{server.server_port}/healthz/asr", fails_on=503)
            return watchdog(self.state_dir).probe_component(comp)[0]
        finally:
            server.shutdown()
            server.server_close()

    def test_the_component_restarts_the_recognizer_not_the_gateway_and_is_probed_off_the_tick(self):
        self.assertEqual(self.asr.unit, "wyoming-whisper.service")
        self.assertEqual(self.asr.fails_on, 503)
        self.assertTrue(self.asr.background)

    def test_only_the_gateways_503_fails_the_probe(self):
        self.assertIs(self.verdict(200), True)
        self.assertIs(self.verdict(503), False)

    def test_a_dead_gateway_a_missing_route_or_a_skipped_probe_is_no_verdict(self):
        for status, why in ((None, "connection refused"), (404, "gateway predates /healthz/asr"),
                            (501, "no recognizer configured"), (202, "live utterances in flight"),
                            (504, "recognizer slow, refused or unreachable")):
            self.assertIsNone(self.verdict(status), why)

    def test_two_503s_restart_the_recognizer_and_a_verdictless_probe_in_between_does_not_reset_the_count(self):
        dog = watchdog(self.state_dir)
        answers = iter([(False, "http 503"), (None, "http 202"), (False, "http 503")])
        restarts = []

        def tick():
            """One background probe cycle for the asr component, as tick() runs it (probe on the first tick of every 40)."""
            with ThreadPoolExecutor(max_workers=2) as pool, \
                    mock.patch.object(failover, "COMPONENTS", (self.asr,)), \
                    mock.patch.object(dog, "probe_component", side_effect=lambda comp: next(answers)), \
                    mock.patch.object(dog, "restart_unit", side_effect=lambda unit, key, cooldown: restarts.append((unit, cooldown))), \
                    mock.patch.object(dog, "maintenance", return_value=False), \
                    mock.patch.object(failover.bgctl, "active_color", return_value="blue"), \
                    mock.patch.object(failover.bgctl, "health", return_value={"ok": True}), \
                    mock.patch.object(dog, "_probe_switch", return_value=(True, "ok")), \
                    mock.patch.object(failover.speaker_clock, "probe", return_value=(True, "ok")):
                dog.ticks = 0
                dog.tick(pool)                                   # submits the background probe
                for _ in range(200):
                    if dog.pending["asr"].done():
                        break
                    time.sleep(0.01)
                dog.tick(pool)                                   # reads the finished probe

        tick()
        self.assertEqual(restarts, [])                           # one 503 is not enough
        tick()
        self.assertEqual(dog.trackers["asr"].fails, 1)           # no verdict: neither counted nor cleared
        self.assertEqual(restarts, [])
        tick()
        self.assertEqual(restarts, [("wyoming-whisper.service", 600.0)])  # the second 503; a restart cannot fix a full disk, so not every minute

    def test_a_slow_asr_probe_does_not_hold_up_the_tick(self):
        """background=True is why: the quasar failover decision of a tick must not wait for a hung recognizer's probe."""
        dog = watchdog(self.state_dir)
        with ThreadPoolExecutor(max_workers=2) as pool, \
                mock.patch.object(failover, "COMPONENTS", (self.asr,)), \
                mock.patch.object(dog, "probe_component", side_effect=lambda comp: (time.sleep(1.5), (True, "http 200"))[1]), \
                mock.patch.object(dog, "maintenance", return_value=False), \
                mock.patch.object(failover.bgctl, "active_color", return_value="blue"), \
                mock.patch.object(failover.bgctl, "health", return_value={"ok": True}), \
                mock.patch.object(dog, "_probe_switch", return_value=(True, "ok")), \
                mock.patch.object(failover.speaker_clock, "probe", return_value=(True, "ok")):
            started = time.monotonic()
            dog.tick(pool)
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertIn("asr", dog.pending)               # still running; its verdict is read by a later tick


def closed_port():
    """A TCP port nothing listens on."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def closed_low_port():
    """A TCP port below 10000 that nothing listens on (so that a leading zero still leaves five digits)."""
    for port in range(9300, 9999):
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            continue
        finally:
            probe.close()
        return port
    raise RuntimeError("no free port between 9300 and 9999")


class PmComponentTest(unittest.TestCase):
    pm = next(comp for comp in failover.COMPONENTS if comp.name == "pm")

    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.dog = watchdog(scratch.name)
        self.runtime_env = Path(scratch.name) / "runtime.env"  # the bridge's settings file; absent unless a test writes it
        patcher = mock.patch.object(failover, "RUNTIME_ENV", self.runtime_env)
        patcher.start()
        self.addCleanup(patcher.stop)
        environ = mock.patch.dict(os.environ)  # the watchdog's own environment must not name a port in these tests
        environ.start()
        self.addCleanup(environ.stop)
        os.environ.pop("AISIS_PM_CDP", None)
        self.alerts, self.restarts, self.events = [], [], []
        for name, recorder in (("notify", self.alerts.append),
                               ("restart_unit", lambda unit, key, cooldown: self.restarts.append(unit)),
                               ("event", lambda kind, **fields: self.events.append((kind, fields)))):
            patcher = mock.patch.object(self.dog, name, side_effect=recorder)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.bridge_down = f"http://127.0.0.1:{closed_port()}/health"
        self.browser = None  # the listener standing in for the pm browser's debugging port, once opened
        self.addCleanup(self.close_browser)

    def open_browser(self, port=0):
        """The pm browser comes up: a listener (on `port`, else a fresh one); returns the component pointing at it."""
        self.browser = socket.socket()
        self.browser.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.browser.bind(("127.0.0.1", port))
        self.browser.listen(8)
        return self.component(needs_port=self.browser.getsockname()[1])

    def close_browser(self):
        if self.browser is not None:
            self.browser.close()
            self.browser = None

    def component(self, **changes):
        changes.setdefault("target", self.bridge_down)
        changes.setdefault("needs_port", closed_port())
        return dataclasses.replace(self.pm, **changes)

    def ticks(self, comp, count):
        with ThreadPoolExecutor(max_workers=4) as pool, \
                mock.patch.object(failover, "COMPONENTS", (comp,)), \
                mock.patch.object(self.dog, "maintenance", return_value=False), \
                mock.patch.object(failover.bgctl, "active_color", return_value="blue"), \
                mock.patch.object(failover.bgctl, "health", return_value={"ok": True}), \
                mock.patch.object(self.dog, "_probe_switch", return_value=(True, "ok")), \
                mock.patch.object(failover.speaker_clock, "probe", return_value=(True, "ok")):
            for _ in range(count):
                self.dog.tick(pool)

    def kinds(self, kind):
        return [fields for recorded, fields in self.events if recorded == kind]

    def test_the_component_restarts_the_bridge_unit_and_needs_the_browsers_debugging_port(self):
        self.assertEqual(self.pm.unit, "aisis-station-pm.service")
        self.assertEqual(self.pm.needs_port, 9226)  # default of pm_cdp_server.py
        self.assertEqual(self.pm.needs_port_env, "AISIS_PM_CDP")  # what the bridge's unit reads from runtime.env

    def test_a_dead_bridge_without_its_browser_is_no_verdict(self):
        healthy, detail = self.dog.probe_component(self.component())
        self.assertIsNone(healthy)
        self.assertIn("http 0", detail)

    def test_a_dead_bridge_with_its_browser_listening_fails_the_probe(self):
        healthy, detail = self.dog.probe_component(self.open_browser())
        self.assertIs(healthy, False)
        self.assertEqual(detail, "http 0")

    def test_a_healthy_bridge_is_healthy_whether_or_not_the_browser_port_answers(self):
        server = serve(200)
        try:
            comp = self.component(target=f"http://127.0.0.1:{server.server_port}/health")
            self.assertEqual(self.dog.probe_component(comp), (True, "http 200"))
        finally:
            server.shutdown()
            server.server_close()

    def test_the_browser_port_is_the_one_the_bridge_is_configured_with(self):
        """AISIS_PM_CDP moves the browser: a dead bridge is restarted while the browser listens on the new port, and the
        default port closed must not silence it (review finding)."""
        browser = self.open_browser()
        port = self.browser.getsockname()[1]
        self.runtime_env.write_text(f"# AISIS_PM_CDP=http://127.0.0.1:1\nAISIS_PM_CDP=http://127.0.0.1:2\nAISIS_PM_CDP='http://127.0.0.1:{port}'\n")
        moved = dataclasses.replace(browser, needs_port=closed_port(), needs_port_env="AISIS_PM_CDP")
        self.assertEqual(self.dog.probe_component(moved), (False, "http 0"))
        self.runtime_env.write_text(f"AISIS_PM_CDP=http://127.0.0.1:{closed_port()}\n")  # moved away from the listening default
        elsewhere = dataclasses.replace(browser, needs_port=port, needs_port_env="AISIS_PM_CDP")
        healthy, detail = self.dog.probe_component(elsewhere)
        self.assertIsNone(healthy)
        self.assertIn("nothing listens", detail)

    def test_an_unset_or_unreadable_setting_leaves_the_declared_port(self):
        comp = dataclasses.replace(self.open_browser(), needs_port_env="AISIS_PM_CDP")
        self.assertEqual(self.dog.probe_component(comp), (False, "http 0"))  # no runtime.env at all
        self.runtime_env.write_text("OTHER=1\n")
        self.assertEqual(self.dog.probe_component(comp), (False, "http 0"))

    def assertUnchecked(self, comp, why):
        """The bridge is judged failed and the browser port is not checked: the setting or its file is no usable endpoint, and the
        detail (which reaches the alert) says so."""
        healthy, detail = self.dog.probe_component(comp)
        self.assertIs(healthy, False, why)
        self.assertIn("browser port not checked", detail, why)

    def test_a_setting_that_is_no_usable_endpoint_claims_no_prerequisite_and_the_bridge_is_restarted(self):
        """The default port is closed here, so falling back to it would silence the restart; so would raising (port 99999
        overflows create_connection) or reading the setting differently from systemd."""
        comp = dataclasses.replace(self.component(), needs_port_env="AISIS_PM_CDP")
        for setting, why in (("http://127.0.0.1:99999", "port out of range"), ("http://127.0.0.1:0", "port zero"),
                             ("http://localhost:9300", "not the loopback address the bridge accepts"),
                             ("http://127.0.0.1:9300/", "a path the bridge rejects"),
                             (f'http://127.0.0.1:{closed_port()}"', "an unbalanced quote, which systemd keeps"),
                             ("", "empty: systemd passes it on and the bridge rejects it; that is not the same as unset"),
                             (f"http://127.0.0.1:0{closed_low_port()}", "a leading zero, which the bridge's validator rejects")):
            self.runtime_env.write_text(f"AISIS_PM_CDP={setting}\n")
            self.assertUnchecked(comp, why)

    def test_the_setting_is_read_as_systemd_reads_it(self):
        browser = self.open_browser()
        comp = dataclasses.replace(browser, needs_port=closed_port(), needs_port_env="AISIS_PM_CDP")
        endpoint = f"http://127.0.0.1:{self.browser.getsockname()[1]}"
        for text in (f"AISIS_PM_CDP ={endpoint}\n", f'  AISIS_PM_CDP = "{endpoint}"  \n', f"X=1\nAISIS_PM_CDP={endpoint}"):
            self.runtime_env.write_text(text)
            self.assertEqual(self.dog.probe_component(comp), (False, "http 0"), text)

    def test_a_continued_line_swallows_the_assignment_that_follows_it(self):
        comp = dataclasses.replace(self.open_browser(), needs_port=closed_port(), needs_port_env="AISIS_PM_CDP")
        port = self.browser.getsockname()[1]
        self.runtime_env.write_text(f"OTHER=1\\\nAISIS_PM_CDP=http://127.0.0.1:{port}\n")  # systemd: all of it is the value of OTHER
        healthy, detail = self.dog.probe_component(comp)  # so AISIS_PM_CDP is unset and the declared default, closed here, counts
        self.assertIsNone(healthy)
        self.assertIn("nothing listens", detail)

    def test_a_continued_line_is_joined_without_a_space(self):
        comp = dataclasses.replace(self.open_browser(), needs_port_env="AISIS_PM_CDP")  # the declared default listens
        closed = str(closed_port())
        self.runtime_env.write_text(f"AISIS_PM_CDP=http://127.0.0.1:{closed[:2]}\\\n{closed[2:]}\n")  # the closed port, over two lines
        healthy, detail = self.dog.probe_component(comp)
        self.assertIsNone(healthy)
        self.assertIn(f"127.0.0.1:{closed}", detail)

    def test_an_assignment_inside_a_multiline_quoted_value_cannot_be_told_from_a_real_one(self):
        """systemd lets a quoted value span lines; the line-by-line reader cannot follow it, so such a file claims no prerequisite
        (the declared default listens here, and the 'assignment' inside OTHER points at a closed port)."""
        comp = dataclasses.replace(self.open_browser(), needs_port_env="AISIS_PM_CDP")
        for text in (f'OTHER="first\nAISIS_PM_CDP=http://127.0.0.1:{closed_port()}\nlast"\n',
                     f'OTHER="\nAISIS_PM_CDP=http://127.0.0.1:{closed_port()}\n"\n'):
            self.runtime_env.write_text(text)
            self.assertUnchecked(comp, text)

    def test_a_quoted_value_the_reader_cannot_follow_exactly_claims_no_prerequisite(self):
        """Each form below reads differently in systemd than in this line-by-line reader; each would otherwise let the closed port
        of the AISIS_PM_CDP line after it (or inside it) count while the declared default listens, which silences the restart."""
        comp = dataclasses.replace(self.open_browser(), needs_port_env="AISIS_PM_CDP")
        closed = closed_port()
        later = f"AISIS_PM_CDP=http://127.0.0.1:{closed}\n"
        for text, why in ((f"AISIS_PM_CDP='http://127.0.0.1:{str(closed)[:2]}\\\n{str(closed)[2:]}'\n",
                           "a backslash and newline inside single quotes stay in the value"),
                          ('X="abc\\"\n' + later, "an escaped closing quote leaves the value open"),
                          ("X='a'b'c'\n" + later, "a second quote inside the value"),
                          ('X="a" # note\n' + later, "text after the closing quote")):
            self.runtime_env.write_text(text)
            self.assertUnchecked(comp, why)

    def test_carriage_returns_end_lines_as_they_do_in_systemd(self):
        """CRLF files, a lone CR, a comment ended by CR and a backslash before CRLF all read as systemd reads them: the line
        AISIS_PM_CDP is a real assignment of a closed port here, so the probe has no verdict (the declared default listens)."""
        comp = dataclasses.replace(self.open_browser(), needs_port_env="AISIS_PM_CDP")
        endpoint = f"AISIS_PM_CDP=http://127.0.0.1:{closed_port()}"
        for text in (endpoint + "\r\n", "OTHER=x\r" + endpoint + "\n", "# note\r" + endpoint + "\n", "F=q\\\r\n" + endpoint + "\n"):
            self.runtime_env.write_bytes(text.encode())
            healthy, detail = self.dog.probe_component(comp)
            self.assertIsNone(healthy, text)
            self.assertIn("nothing listens", detail)

    def test_a_nul_byte_and_a_quote_opened_after_a_continuation_are_not_trusted(self):
        comp = dataclasses.replace(self.open_browser(), needs_port_env="AISIS_PM_CDP")  # the declared default listens
        endpoint = f"http://127.0.0.1:{closed_port()}"
        for data, why in ((f"X=1\0\nAISIS_PM_CDP={endpoint}\n".encode(), "systemd stops reading at a NUL"),
                          (f'AISIS_PM_CDP=\\\n"{endpoint}"\n'.encode(), "after an escaped newline the quotes stay in the value")):
            self.runtime_env.write_bytes(data)
            self.assertUnchecked(comp, why)

    def test_only_a_newline_ends_a_line_as_in_systemd(self):
        comp = dataclasses.replace(self.open_browser(), needs_port_env="AISIS_PM_CDP")  # the declared default listens
        self.runtime_env.write_text(f"OTHER=x\u2028AISIS_PM_CDP=http://127.0.0.1:{closed_port()}\n")  # one assignment, of OTHER
        self.assertEqual(self.dog.probe_component(comp), (False, "http 0"))  # AISIS_PM_CDP is unset: the listening default

    def test_a_nameless_assignment_ending_in_a_backslash_continues_nothing(self):
        comp = dataclasses.replace(self.open_browser(), needs_port_env="AISIS_PM_CDP")  # the declared default listens
        self.runtime_env.write_text(f"=x\\\nAISIS_PM_CDP=http://127.0.0.1:{closed_port()}\n")  # systemd ignores the first line
        healthy, detail = self.dog.probe_component(comp)
        self.assertIsNone(healthy)
        self.assertIn("nothing listens", detail)

    def test_a_backslash_ending_a_line_without_an_assignment_continues_nothing(self):
        comp = dataclasses.replace(self.open_browser(), needs_port_env="AISIS_PM_CDP")  # the declared default listens
        self.runtime_env.write_text(f"OLD_SETTING\\\nAISIS_PM_CDP=http://127.0.0.1:{closed_port()}\n")
        healthy, detail = self.dog.probe_component(comp)
        self.assertIsNone(healthy)  # the second line is a real assignment, of a closed port
        self.assertIn("nothing listens", detail)

    def test_a_settings_file_that_cannot_be_read_claims_no_prerequisite(self):
        comp = dataclasses.replace(self.component(), needs_port_env="AISIS_PM_CDP")  # the declared default is closed
        with mock.patch.object(Path, "read_bytes", side_effect=PermissionError("denied")):
            self.assertUnchecked(comp, "permission denied")

    def test_an_empty_assignment_elsewhere_in_the_file_is_not_a_multiline_value(self):
        comp = dataclasses.replace(self.open_browser(), needs_port_env="AISIS_PM_CDP")  # the declared default listens
        self.runtime_env.write_text(f"AISIS_PM_INITIAL_TARGET=\nAISIS_PM_CDP=http://127.0.0.1:{closed_port()}\n")
        healthy, detail = self.dog.probe_component(comp)
        self.assertIsNone(healthy)  # the closed port of the setting counts, so the file was read
        self.assertIn("nothing listens", detail)

    def test_a_port_the_user_manager_passes_on_counts_when_the_file_does_not_set_it(self):
        comp = dataclasses.replace(self.open_browser(), needs_port_env="AISIS_PM_CDP")  # the declared default listens
        inherited = {"AISIS_PM_CDP": f"http://127.0.0.1:{closed_port()}"}  # environment.d / set-environment: inherited by the watchdog when it started
        with mock.patch.dict(os.environ, inherited):
            healthy, detail = self.dog.probe_component(comp)
            self.assertIsNone(healthy)
            self.assertIn("nothing listens", detail)
            self.runtime_env.write_text(f"AISIS_PM_CDP=http://127.0.0.1:{self.browser.getsockname()[1]}\n")  # the file wins, as for the unit
            self.assertEqual(self.dog.probe_component(comp), (False, "http 0"))

    def test_a_comment_is_neither_an_assignment_nor_a_quoted_value_and_does_not_continue(self):
        comp = dataclasses.replace(self.open_browser(), needs_port_env="AISIS_PM_CDP")  # the declared default listens
        endpoint = f"AISIS_PM_CDP=http://127.0.0.1:{closed_port()}\n"  # a closed port: read, it means no verdict
        for text in ('# example: AISIS_JEV="shadow" for a week\n' + endpoint,
                     "# example=value \\\n" + endpoint,
                     "; also a comment \\\n" + endpoint,
                     "OTHER=C:\\\\\n" + endpoint):  # two backslashes at the end of a line are not a continuation
            self.runtime_env.write_text(text)
            healthy, detail = self.dog.probe_component(comp)
            self.assertIsNone(healthy, text)
            self.assertIn("nothing listens", detail)

    def test_a_browser_that_does_not_answer_is_not_a_browser_that_is_gone(self):
        """Only a refused connection proves nothing listens: a hung browser whose accept queue is full times out, and a
        restart of the bridge is then the usual response, not silence."""
        hung = socket.socket()
        self.addCleanup(hung.close)
        hung.bind(("127.0.0.1", 0))
        hung.listen(0)
        port = hung.getsockname()[1]
        for _ in range(4):  # the queue holds one connection: the next ones are neither accepted nor refused
            filler = socket.socket()
            self.addCleanup(filler.close)
            filler.settimeout(0.3)
            try:
                filler.connect(("127.0.0.1", port))
            except OSError:
                pass
        self.assertEqual(self.dog.probe_component(self.component(needs_port=port)), (False, "http 0"))

    def test_failures_before_a_browser_absence_do_not_count_toward_the_next_run(self):
        port = closed_port()
        self.ticks(self.open_browser(port), 1)  # one failed tick with the browser up
        self.close_browser()
        self.ticks(self.component(needs_port=port), 20)  # then it is gone for a long time
        comp = self.open_browser(port)
        self.ticks(comp, self.grace() + 5)
        self.assertEqual(self.restarts, [])  # five fresh failures after the grace are not six
        self.ticks(comp, 1)
        self.assertEqual(self.restarts, ["aisis-station-pm.service"])

    def test_a_file_that_does_not_decode_claims_no_prerequisite_rather_than_hiding_a_valid_line(self):
        """systemd drops only the undecodable assignment; here the valid AISIS_PM_CDP line names the listening browser and the
        declared default is closed, so reading the file as unreadable (and using the default) would silence the restart."""
        comp = self.open_browser()
        port = self.browser.getsockname()[1]
        comp = dataclasses.replace(comp, needs_port=closed_port(), needs_port_env="AISIS_PM_CDP")
        self.runtime_env.write_bytes(f"AISIS_PM_CDP=http://127.0.0.1:{port}\nX=\xff\xfe\n".encode("latin-1"))
        self.assertUnchecked(comp, "undecodable")

    def test_a_stopped_browser_never_opens_an_incident_sends_an_alert_or_restarts_the_unit(self):
        self.ticks(self.component(), 40)  # the threshold is 6 ticks
        self.assertEqual(self.alerts, [])
        self.assertEqual(self.restarts, [])
        self.assertEqual(self.kinds("incident"), [])

    def test_an_incident_open_when_the_browser_goes_does_not_swallow_the_alert_of_the_next_one(self):
        port = closed_port()
        self.ticks(self.open_browser(port), 6)  # the bridge hangs with the browser up: incident, alert, restart
        self.assertEqual((len(self.alerts), len(self.restarts)), (1, 1))
        self.close_browser()
        self.ticks(self.component(needs_port=port), 5)  # the browser is stopped: no verdict, and that failure is over
        self.assertEqual(len(self.kinds("incident_suspended")), 1)
        self.ticks(self.open_browser(port), self.grace() + 6)  # it is back and the bridge is still down: a new incident
        self.assertEqual((len(self.alerts), len(self.restarts)), (3, 2))  # the failure alert, the closing notice, the new failure alert
        self.assertIn("инцидент закрыт", self.alerts[1])

    def test_the_absence_is_journaled_once_per_change_not_once_per_tick(self):
        port = closed_port()
        self.ticks(self.component(needs_port=port), 12)
        self.assertEqual(len(self.kinds("probe_no_verdict")), 1)
        self.ticks(self.open_browser(port), 1)  # a verdict again: the bridge is down, the browser is up
        self.close_browser()
        self.ticks(self.component(needs_port=port), 3)  # the same absence, so the same detail: only the reset repeats the line
        self.assertEqual(len(self.kinds("probe_no_verdict")), 2)

    def grace(self):
        """Ticks a returning browser gives the bridge to start by itself before failures count."""
        return math.ceil(failover.PREREQUISITE_GRACE_S / self.dog.interval)

    def test_a_returning_browser_gets_the_bridge_restarted_after_the_grace_and_the_usual_threshold(self):
        self.ticks(self.component(), 10)
        self.assertEqual(self.restarts, [])
        comp = self.open_browser()
        self.ticks(comp, self.grace() + 5)
        self.assertEqual(self.restarts, [])
        self.ticks(comp, 1)
        self.assertEqual(self.restarts, ["aisis-station-pm.service"])

    def test_a_bridge_that_comes_up_by_itself_after_the_browser_returns_costs_no_alert_and_no_restart(self):
        self.ticks(self.component(), 10)
        comp = self.open_browser()
        self.ticks(comp, 10)  # more than the six ticks of the usual threshold, fewer than the grace: systemd's restart has not got there yet
        server = serve(200)
        try:
            self.ticks(dataclasses.replace(comp, target=f"http://127.0.0.1:{server.server_port}/health"), 3)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual((self.alerts, self.restarts), ([], []))

    def test_the_alerts_say_which_part_of_the_voice_stack_pm_is_and_recovery_names_it_too(self):
        comp = self.open_browser()
        self.ticks(comp, 6)
        self.assertEqual(self.restarts, ["aisis-station-pm.service"])
        self.assertEqual(len(self.alerts), 1)
        for word in ("aisis-station-pm", "ChatGPT", "не отвечает (http 0), перезапускаю"):
            self.assertIn(word, self.alerts[0])
        server = serve(200)
        try:
            self.ticks(dataclasses.replace(comp, target=f"http://127.0.0.1:{server.server_port}/health"), 1)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(len(self.alerts), 2)
        for word in ("aisis-station-pm", "ChatGPT", "восстановлен"):
            self.assertIn(word, self.alerts[1])

    def test_a_component_with_no_prerequisite_is_still_restarted_and_named_by_its_name(self):
        self.ticks(self.component(needs_port=None, label=None), 6)
        self.assertEqual(self.restarts, ["aisis-station-pm.service"])
        self.assertTrue(self.alerts[0].endswith("pm не отвечает (http 0), перезапускаю"), self.alerts[0])


if __name__ == "__main__":
    unittest.main()
