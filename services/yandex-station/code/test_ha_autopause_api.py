"""HaApi (ha_autopause.py) and the WebSocket client under it (ha_ws.py) against a small Home Assistant of their own: the automation config REST
endpoints, the services, and the helper commands of the WebSocket (`input_boolean/list|create|delete`) with the sign-in. Test double only."""
import base64
import hashlib
import json
import os
import re
import struct
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_autopause  # noqa: E402
import ha_client  # noqa: E402
import ha_ws  # noqa: E402

TOKEN = "long-lived-1"


def read_frame(rfile) -> tuple[int, bytes] | None:
    head = rfile.read(2)
    if len(head) < 2:
        return None
    opcode, size = head[0] & 0x0F, head[1] & 0x7F
    if size == 126:
        size = struct.unpack(">H", rfile.read(2))[0]
    elif size == 127:
        size = struct.unpack(">Q", rfile.read(8))[0]
    key = rfile.read(4) if head[1] & 0x80 else b""
    payload = rfile.read(size)
    if key:
        payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return opcode, payload


class FakeConfigHa:
    def __init__(self):
        self.automations: dict[str, dict] = {}
        self.helpers: dict[str, dict] = {}
        self.services: list[tuple[str, dict]] = []
        self.ws_commands: list[dict] = []
        self.reject_ws_token = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def reply(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def authorized(self):
                if self.headers.get("Authorization") == f"Bearer {TOKEN}":
                    return True
                self.reply(401, {"message": "unauthorized"})
                return False

            def do_GET(self):
                if self.path == "/api/websocket":
                    return self.websocket()
                if not self.authorized():
                    return
                match = re.fullmatch(r"/api/config/automation/config/(\w+)", self.path)
                if match and match.group(1) in outer.automations:
                    return self.reply(200, outer.automations[match.group(1)])
                self.reply(404, {"message": "Resource not found"})

            def body(self):
                return json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"null")

            def do_POST(self):
                payload = self.body()
                if not self.authorized():
                    return
                match = re.fullmatch(r"/api/config/automation/config/(\w+)", self.path)
                if match:
                    outer.automations[match.group(1)] = payload
                    return self.reply(200, {"result": "ok"})
                match = re.fullmatch(r"/api/services/(\w+)/(\w+)", self.path)
                if match:
                    outer.services.append((f"{match.group(1)}.{match.group(2)}", payload))
                    return self.reply(200, [])
                self.reply(404, {})

            def do_DELETE(self):
                if not self.authorized():
                    return
                match = re.fullmatch(r"/api/config/automation/config/(\w+)", self.path)
                if match and outer.automations.pop(match.group(1), None) is not None:
                    return self.reply(200, {"result": "ok"})
                self.reply(404, {"message": "Resource not found"})

            def send_ws(self, message):
                self.wfile.write(ha_ws.encode_frame(json.dumps(message).encode(), 1, mask=False))
                self.wfile.flush()

            def websocket(self):
                accept = base64.b64encode(hashlib.sha1((self.headers["Sec-WebSocket-Key"] + ha_ws.GUID).encode()).digest()).decode()
                self.send_response(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept)
                self.end_headers()
                self.send_ws({"type": "auth_required"})
                while True:
                    frame = read_frame(self.rfile)
                    if frame is None or frame[0] == 8:
                        return
                    if frame[0] != 1:
                        continue
                    message = json.loads(frame[1])
                    outer.ws_commands.append(message)
                    if message["type"] == "auth":
                        ok = message["access_token"] == TOKEN and not outer.reject_ws_token
                        self.send_ws({"type": "auth_ok"} if ok else {"type": "auth_invalid", "message": "Invalid access token"})
                        if not ok:
                            return
                    else:
                        self.send_ws(outer.command(message))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.block_on_close = False
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def command(self, message):
        kind, _, action = message["type"].partition("/")
        if kind != "input_boolean":
            return {"id": message["id"], "type": "result", "success": False, "error": {"code": "unknown_command", "message": "Unknown command."}}
        if action == "list":
            result = list(self.helpers.values())
        elif action == "create":
            object_id = re.sub(r"[^a-z0-9]+", "_", message["name"].lower()).strip("_")
            self.helpers[object_id] = result = {"id": object_id, "name": message["name"]}
        else:
            self.helpers.pop(message["input_boolean_id"], None)
            result = None
        return {"id": message["id"], "type": "result", "success": True, "result": result}

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class HaApiTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.ha = FakeConfigHa()
        self.addCleanup(self.ha.close)
        store = ha_client.Store(Path(self.dir.name) / "connection.json")
        store.save(ha_client.Connection(self.ha.url, "long_lived", TOKEN))
        self.api = ha_autopause.HaApi(ha_client.HaClient(store))

    def test_a_full_install_status_and_uninstall_through_the_real_calls(self):
        spec = ha_autopause.Spec()
        report = ha_autopause.install(self.api, spec)
        self.assertEqual(len(report.created), 3)
        self.assertEqual(set(self.ha.helpers), {"tv_autopause_enabled", "tv_autopause_resume", "tv_autopause_we_paused"})
        self.assertEqual(set(self.ha.automations), {"tv_autopause_main", "tv_autopause_owner"})
        self.assertEqual(self.ha.services, [("input_boolean.turn_on", {"entity_id": "input_boolean.tv_autopause_resume"}), ("automation.reload", {})])
        self.assertEqual(ha_autopause.install(self.api, spec).written, [])
        self.assertEqual(ha_autopause.status(self.api, spec)["automations"], {"tv_autopause_main": True, "tv_autopause_owner": True})
        ha_autopause.uninstall(self.api, spec)
        self.assertEqual((self.ha.helpers, self.ha.automations), ({}, {}))

    def test_a_missing_automation_reads_as_none(self):
        self.assertIsNone(self.api.get_automation("nothing_here"))

    def test_helper_commands_use_home_assistants_field_names(self):
        self.api.create_helper("input_boolean", "x y")
        self.api.delete_helper("input_boolean.x_y")
        sent = [(m["type"], {k: v for k, v in m.items() if k not in ("id", "type")}) for m in self.ha.ws_commands if m["type"] != "auth"]
        self.assertEqual(sent, [("input_boolean/create", {"name": "x y"}), ("input_boolean/delete", {"input_boolean_id": "x_y"})])

    def test_a_refused_websocket_command_is_an_install_error(self):
        with self.assertRaises(ha_autopause.InstallError):
            self.api.ws("input_text/list")

    def test_a_rejected_websocket_sign_in_is_auth_revoked(self):
        self.ha.reject_ws_token = True
        with self.assertRaises(ha_client.AuthRevoked):
            self.api.helper_ids("input_boolean")

    def test_a_rejected_write_is_an_install_error_not_silence(self):
        store = ha_client.Store(Path(self.dir.name) / "other.json")
        store.save(ha_client.Connection(self.ha.url, "long_lived", "wrong"))
        with self.assertRaises(ha_autopause.InstallError):
            ha_autopause.HaApi(ha_client.HaClient(store)).put_automation("x", {})


class FrameTests(unittest.TestCase):
    def test_frames_of_every_length_class_round_trip_masked_and_not(self):
        import io
        for size in (0, 5, 125, 126, 65535, 65536):
            for mask in (True, False):
                payload = bytes(i % 251 for i in range(size))
                self.assertEqual(read_frame(io.BytesIO(ha_ws.encode_frame(payload, 1, mask=mask))), (1, payload))

    def test_the_accept_key_is_the_rfc_example(self):
        self.assertEqual(ha_ws.accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")


if __name__ == "__main__":
    unittest.main()
