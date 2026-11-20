"""The adapter's /laya proxy finds the CURRENT Laya socket: the file name carries the task-cli version and changes at every upgrade.

2026-11-20 11:18 the quasar semantic shadow logged `laya http 503` for every phrase: the adapter looked for a hard-coded
laya-0.23.1.sock that no longer existed (the live one was laya-0.34.0.sock, now 0.35.1).
"""
import asyncio
import json
import socket
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from adapter import Adapter, laya_sockets

TOKEN = "test-only-token"
AUTH = {"Authorization": "Bearer " + TOKEN}


def abandoned_socket(path):
    """A socket file nobody listens on: bound, then closed (closing does not remove the inode)."""
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.close()
    return path


class SocketListTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.base = Path(self.dir.name)

    def test_newest_version_first_and_lock_files_are_ignored(self):
        old = abandoned_socket(self.base / "laya-0.9.0.sock")
        newest = abandoned_socket(self.base / "laya-0.35.1.sock")
        (self.base / "laya-0.36.0.sock.lock").write_text("")
        self.assertEqual(laya_sockets({}, self.base), [newest, old])

    def test_explicit_env_overrides_discovery(self):
        abandoned_socket(self.base / "laya-0.35.1.sock")
        self.assertEqual(laya_sockets({"LAYA_SOCKET": "/run/x.sock"}, self.base), [Path("/run/x.sock")])

    def test_no_socket_is_an_empty_list(self):
        (self.base / "laya-0.34.0.sock.lock").write_text("")
        self.assertEqual(laya_sockets({}, self.base), [])


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.base = Path(self.dir.name)

    async def client(self, **extra):
        env = {"AISIS_ALLOWED_STATIONS": "st1", "AISIS_STATE_DB": str(self.base / "adapter.sqlite3"), "AISIS_MEMORY": "0",
               "AISIS_CHATGPT_COMMAND": "[]", "AISIS_ADAPTER_TOKEN": TOKEN, "AISIS_HA_ACTIONS": "off",
               "AISIS_DIALOGS_DB": str(self.base / "dialogs.sqlite3"), **extra}
        client = TestClient(TestServer(Adapter(env).app()))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    async def serve(self, path):
        async def handle(reader, writer):
            request = json.loads(await reader.readline())
            writer.write(json.dumps({"ok": True, "echo": request["message"]}).encode() + b"\n")
            await writer.drain()
            writer.close()

        server = await asyncio.start_unix_server(handle, str(path))
        self.addCleanup(server.close)
        return server

    async def ask(self, client):
        return await client.post("/laya", json={"message": "включи свет"}, headers=AUTH)

    async def test_proxy_answers_through_the_explicit_socket(self):
        await self.serve(self.base / "laya-0.35.1.sock")
        client = await self.client(LAYA_SOCKET=str(self.base / "laya-0.35.1.sock"))
        response = await self.ask(client)
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["echo"], "включи свет")

    async def test_a_live_older_server_wins_over_an_abandoned_newer_socket(self):
        client = await self.client(XDG_STATE_HOME=str(self.base / "state"))
        state = self.base / "state" / "task-cli" / "classify"
        state.mkdir(parents=True)
        abandoned_socket(state / "laya-0.36.0.sock")
        await self.serve(state / "laya-0.35.1.sock")
        response = await self.ask(client)
        self.assertEqual(response.status, 200)

    async def test_only_abandoned_sockets_is_503_not_a_crash(self):
        state = self.base / "state" / "task-cli" / "classify"
        state.mkdir(parents=True)
        abandoned_socket(state / "laya-0.1.0.sock")
        client = await self.client(XDG_STATE_HOME=str(self.base / "state"))
        response = await self.ask(client)
        self.assertEqual(response.status, 503)

    async def test_no_socket_is_503(self):
        client = await self.client(XDG_STATE_HOME=str(self.base / "empty"))
        response = await self.ask(client)
        self.assertEqual(response.status, 503)


if __name__ == "__main__":
    unittest.main()
