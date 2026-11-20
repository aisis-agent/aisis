"""/healthz/asr: the gateway's real-transcription probe of the Wyoming recognizer (read by aisis-failover).

The outage behind it (2026-11-17): wyoming-whisper's temp WAV writes hit the tmpfs user quota (`Errno 122 Disk quota
exceeded`), every transcription died inside the server and the client got an invalid header, while the process stayed
up and the port still answered. A port or Describe probe stays green on that; this one must go red, and only on that:
aisis-failover restarts the recognizer on a 503 and ignores every other answer, so 503 must mean "the recognizer answered
wrongly" and nothing else.
"""
import asyncio
import json
import os
import sys
import types
import unittest
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

try:
    import numpy  # noqa: F401  (the gateway's level front-end needs it; the probe never uses it)
except ImportError:
    sys.modules.setdefault("speech_frontend", types.ModuleType("speech_frontend"))   # CI has no numpy: only the import is satisfied

import stt_wyoming


class FakeRecognizer:
    """A Wyoming server that reads one whole utterance (up to audio-stop), then answers like a healthy, a broken or a hung whisper."""

    def __init__(self, mode):
        self.mode = mode        # "healthy" | "closes" (a crashed handler task: the client reads an empty header) | "garbage" | "hangs";
        # any other string is sent as the raw header line of the reply
        self.audio_bytes = 0

    async def handle(self, reader, writer):
        while True:
            line = await reader.readline()
            if not line:
                break
            header = json.loads(line)
            for key in ("data_length", "payload_length"):
                chunk = await reader.readexactly(header.get(key, 0))
                if key == "payload_length":
                    self.audio_bytes += len(chunk)
            if header["type"] == "audio-stop":
                if self.mode == "healthy":
                    body = json.dumps({"text": "  "}).encode()
                    writer.write(json.dumps({"type": "transcript", "data_length": len(body)}).encode() + b"\n" + body)
                    await writer.drain()
                elif self.mode == "garbage":
                    writer.write(b"not-json\n")
                    await writer.drain()
                elif self.mode.startswith("raw:"):
                    writer.write(self.mode[4:].encode() + b"\n")
                    await writer.drain()
                elif self.mode == "hangs":
                    await reader.read()          # never answers; returns when the client gives up and closes
                break
        writer.close()


class AsrProbeTest(unittest.TestCase):
    def probe(self, mode="healthy", env=None, before=None, recognizer=None):
        """GET /healthz/asr against a gateway wired to a fake Wyoming server. `before(gateway)` runs before the request."""
        fake = FakeRecognizer(mode)

        async def run():
            server = await asyncio.start_server(fake.handle, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            settings = {"AISIS_WYOMING_URI": f"tcp://127.0.0.1:{port}"} if env is None else env
            kwargs = {"recognizer": recognizer} if recognizer else {}
            gateway = stt_wyoming.STTGateway(env=settings, archive=False, **kwargs)
            if before:
                await before(gateway)
            async with TestClient(TestServer(gateway.app())) as client:
                response = await client.get("/healthz/asr")
                body = await response.json()
            server.close()
            await server.wait_closed()
            return response.status, body, gateway

        status, body, gateway = asyncio.run(run())
        return status, body, fake, gateway

    def test_a_working_recognizer_is_green_and_received_the_whole_probe_utterance(self):
        status, body, fake, gateway = self.probe()
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(fake.audio_bytes, stt_wyoming.PROBE_PCM_BYTES)
        self.assertEqual(gateway.slots._value, gateway.SLOTS)

    def test_a_recognizer_that_dies_after_the_audio_is_503(self):
        status, body, _, gateway = self.probe("closes")
        self.assertEqual((status, body), (503, {"error": "RuntimeError"}))
        self.assertEqual(gateway.slots._value, gateway.SLOTS)

    def test_a_malformed_reply_is_503_not_a_setting_error(self):
        status, body, _, _ = self.probe("garbage")
        self.assertEqual((status, body), (503, {"error": "JSONDecodeError"}))   # a ValueError subclass: must not read as a bad address

    def test_replies_of_the_wrong_shape_are_503(self):
        for reply in ("{}", "[]", "null", '{"type":"transcript","data":null}', '{"type":"transcript","data":{"text":5}}',
                      '{"type":"transcript","data_length":"x"}'):
            status, body, _, _ = self.probe("raw:" + reply)
            self.assertEqual(status, 503, reply)

    def test_a_refused_connection_is_no_verdict(self):
        """A dead or still-starting recognizer is systemd's to restart; the watchdog restarting it again could only interrupt a start."""
        status, body, _, _ = self.probe(env={"AISIS_WYOMING_URI": "tcp://127.0.0.1:1"})
        self.assertEqual((status, body), (504, {"error": "ConnectionRefusedError"}))

    def test_the_probe_holds_one_slot_and_leaves_the_other_open_to_live_utterances(self):
        seen = []
        holder = []

        async def recognizer(uri, pcm, rate, language, **extras):
            seen.append(holder[0].slots._value)       # free slots while the probe's own exchange runs
            return ""

        async def remember(gateway):
            holder.append(gateway)

        status, _, _, gateway = self.probe(env={"AISIS_WYOMING_URI": "tcp://127.0.0.1:1"}, before=remember, recognizer=recognizer)
        self.assertEqual(status, 200)
        self.assertEqual(seen, [gateway.SLOTS - 1])
        self.assertEqual(gateway.slots._value, gateway.SLOTS)

    def test_a_slow_or_contended_recognizer_is_no_verdict_not_a_restart(self):
        """Slow, hung and busy-with-Home-Assistant all look like a timeout from here; a restart fixes none of them and drops the
        other clients' work, so a timeout must never be the 503 that the watchdog acts on."""
        with mock.patch.object(stt_wyoming, "PROBE_TIMEOUT_S", 0.3):
            status, body, _, gateway = self.probe("hangs")
        self.assertEqual((status, body), (504, {"error": "TimeoutError"}))
        self.assertEqual(gateway.slots._value, gateway.SLOTS)

    def test_a_setting_that_is_not_a_recognizer_failure_is_501(self):
        self.assertEqual(self.probe(env={})[0], 501)                                       # nothing configured
        status, body, _, _ = self.probe(env={"AISIS_WYOMING_URI": "100.75.131.84:10300"})  # no tcp:// scheme
        self.assertEqual((status, body), (501, {"error": "bad_uri"}))

    def test_live_utterances_make_the_probe_step_aside_with_no_verdict(self):
        """A valid utterance in flight on a slow recognizer must never turn the probe into a 503 (the watchdog would restart a
        working recognizer), and the probe must not queue in front of the real request either: it sends nothing."""
        for busy in (1, 2):
            async def hold(gateway, busy=busy):
                for _ in range(busy):
                    await gateway.slots.acquire()

            status, body, fake, gateway = self.probe(before=hold)
            self.assertEqual((status, body), (202, {"skipped": "busy"}), f"{busy} slot(s) busy")
            self.assertEqual(fake.audio_bytes, 0)
            self.assertEqual(gateway.slots._value, gateway.SLOTS - busy)    # the probe took nothing and kept nothing

    def test_the_probe_speaks_the_live_language_and_profile(self):
        """The recognizer profile of live traffic is the one probed: a broken profile with a working default must not read as healthy."""
        calls = []

        async def recognizer(uri, pcm, rate, language, **extras):
            calls.append((language, extras))
            return ""

        env = {"AISIS_WYOMING_URI": "tcp://127.0.0.1:1", "AISIS_STT_LANGUAGE": "en", "AISIS_STT_PROFILE": "home"}
        status, _, _, _ = self.probe(env=env, recognizer=recognizer)
        self.assertEqual(status, 200)
        self.assertEqual(calls[0][0], "en")
        self.assertEqual(calls[0][1]["profile"], "home")


class FakeControlSocket:
    """The wyoming-whisper offload control socket answering `status`."""

    def __init__(self, tmp, state):
        self.path = f"{tmp}/ctl.sock"
        self.state = state
        self.asked = []

    async def handle(self, reader, writer):
        self.asked.append(json.loads(await reader.readline()))
        writer.write(json.dumps({"ok": True, "state": self.state, "idle_s": 12.5, "leases": [{"id": "x"}] * (self.state == "pinned")}).encode() + b"\n")
        await writer.drain()
        writer.close()


class AsrProbeOffloadTest(unittest.TestCase):
    """Idle-unloaded or lent whisper (docs/superpowers/specs/2026-11-18-vram-offload-design.md): no verdict, never a restart."""

    probe = AsrProbeTest.probe

    def probe_with_ctl(self, state, mode="healthy"):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            ctl = FakeControlSocket(tmp, state)
            env = {}

            fake = FakeRecognizer(mode)

            async def run():
                server = await asyncio.start_server(fake.handle, "127.0.0.1", 0)
                port = server.sockets[0].getsockname()[1]
                env.update({"AISIS_WYOMING_URI": f"tcp://127.0.0.1:{port}", "AISIS_WHISPER_CTL_SOCKET": ctl.path})
                gateway = stt_wyoming.STTGateway(env=env, archive=False)
                ctl_server = await asyncio.start_unix_server(ctl.handle, path=ctl.path)
                async with TestClient(TestServer(gateway.app())) as client:
                    response = await client.get("/healthz/asr")
                    body = await response.json()
                server.close()
                ctl_server.close()
                return response.status, body

            status, body = asyncio.run(run())
            return status, body, fake, ctl

    def test_unloaded_and_pinned_are_202_and_the_probe_sends_nothing(self):
        for state in ("unloaded", "pinned"):
            status, body, fake, ctl = self.probe_with_ctl(state)
            self.assertEqual((status, body["skipped"]), (202, state))
            self.assertEqual(fake.audio_bytes, 0)               # the probe must not reload the model
            self.assertEqual(ctl.asked, [{"op": "status"}])

    def test_a_loaded_recognizer_is_probed_as_before(self):
        status, body, fake, _ = self.probe_with_ctl("loaded")
        self.assertEqual(status, 200)
        self.assertEqual(fake.audio_bytes, stt_wyoming.PROBE_PCM_BYTES)

    def test_a_gpu_lent_error_event_is_202_not_a_restart(self):
        status, body, _, _ = self.probe(f"raw:" + json.dumps({"type": "error", "data": {"text": "lent", "code": "gpu-lent"}}))
        self.assertEqual((status, body), (202, {"skipped": "gpu-lent"}))

    def test_a_model_unloaded_error_event_is_202_unloaded(self):
        """The model idle-unloaded between the status and the probe: the daemon refuses the probe instead of reloading for it."""
        status, body, _, _ = self.probe("raw:" + json.dumps({"type": "error", "data": {"text": "unloaded", "code": "model-unloaded"}}))
        self.assertEqual((status, body), (202, {"skipped": "unloaded"}))

    def test_a_socket_that_is_not_configured_is_never_consulted(self):
        """An unloaded local whisper must not hide a broken recognizer at another address: no AISIS_WHISPER_CTL_SOCKET, no lookup."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(f"{tmp}/vram-offload")
            ctl = FakeControlSocket(tmp, "unloaded")
            ctl.path = f"{tmp}/vram-offload/wyoming-whisper.sock"

            async def run(env):
                server = await asyncio.start_unix_server(ctl.handle, path=ctl.path)
                try:
                    return await stt_wyoming.whisper_ctl_status(env)
                finally:
                    server.close()

            self.assertIsNone(asyncio.run(run({"AISIS_WYOMING_URI": "tcp://127.0.0.1:10301", "XDG_RUNTIME_DIR": tmp})))
            self.assertEqual(ctl.asked, [])
            self.assertEqual(asyncio.run(run({"AISIS_WHISPER_CTL_SOCKET": ctl.path}))["state"], "unloaded")

    def test_a_malformed_control_reply_is_no_status(self):
        for reply in ('{"ok":true,"state":"unloaded","leases":1}', '{"ok":true,"state":5}', '[]', 'nope', '{"ok":"yes","state":"pinned"}'):
            async def handle(reader, writer, reply=reply):
                await reader.readline()
                writer.write(reply.encode() + b"\n")
                await writer.drain()
                writer.close()

            async def run():
                import tempfile
                with tempfile.TemporaryDirectory() as tmp:
                    path = f"{tmp}/ctl.sock"
                    server = await asyncio.start_unix_server(handle, path=path)
                    try:
                        return await stt_wyoming.whisper_ctl_status({"AISIS_WHISPER_CTL_SOCKET": path})
                    finally:
                        server.close()

            self.assertIsNone(asyncio.run(run()), reply)

    def test_a_missing_control_socket_changes_nothing(self):
        status, _, _, _ = self.probe(env={"AISIS_WYOMING_URI": "tcp://127.0.0.1:1", "AISIS_WHISPER_CTL_SOCKET": "/nonexistent/ctl.sock"},
                                     recognizer=self.ok_recognizer)
        self.assertEqual(status, 200)

    def test_the_probe_carries_the_probe_context(self):
        calls = []

        async def recognizer(uri, pcm, rate, language, **extras):
            calls.append(extras["context"])
            return ""

        self.probe(env={"AISIS_WYOMING_URI": "tcp://127.0.0.1:1"}, recognizer=recognizer)
        self.assertEqual(calls, [{"aisis_probe": True}])

    @staticmethod
    async def ok_recognizer(uri, pcm, rate, language, **extras):
        return ""


if __name__ == "__main__":
    unittest.main()
