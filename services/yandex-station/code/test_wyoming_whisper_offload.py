"""The whisper VRAM-offload state machine (wyoming_whisper_offload.py) against a backend with CTranslate2's real semantics:
unload_model silently does nothing while a batch is active, and using an unloaded model raises. No GPU, no wyoming needed."""
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import wyoming_whisper_offload as offload


class FakeCt2:
    """ctranslate2.models.Whisper: model_is_loaded / unload_model(to_cpu) / load_model(); unload is a no-op while `busy`."""

    def __init__(self):
        self.model_is_loaded = True
        self.busy = False
        self.calls = []
        self.fail_load = None

    def unload_model(self, to_cpu=False):
        self.calls.append(("unload", to_cpu))
        if not self.busy:
            self.model_is_loaded = False

    def load_model(self, keep_cache=False):
        self.calls.append(("load",))
        if self.fail_load:
            raise self.fail_load
        self.model_is_loaded = True


class FakeBackend:
    def __init__(self, loaded=True):
        self.ct2 = FakeCt2()
        self.ct2.model_is_loaded = loaded

    def loaded(self):
        return self.ct2.model_is_loaded

    def load(self):
        self.ct2.load_model()

    def unload(self, to_cpu):
        self.ct2.unload_model(to_cpu=to_cpu)

    def weights_bytes(self):
        return 1000


def fake_proc(root, pid, start):
    """/proc/<pid>/stat with a comm that has a space and a paren, like a real one can."""
    Path(root, str(pid)).mkdir(parents=True, exist_ok=True)
    fields = ["S", "1"] + ["0"] * 17 + [str(start)] + ["0"] * 5
    Path(root, str(pid), "stat").write_text(f"{pid} (my ) proc) " + " ".join(fields) + "\n")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class ControllerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.proc = os.path.join(self.tmp.name, "proc")
        self.state = os.path.join(self.tmp.name, "vram-offload")
        fake_proc(self.proc, 4242, 777)
        self.clock = Clock()

    def make(self, loaded=True, idle=0.0, **kwargs):
        backend = FakeBackend(loaded)
        controller = offload.OffloadController("wyoming-whisper", backend, self.state, idle_unload_s=idle, clock=self.clock,
                                               proc_root=self.proc, drain_timeout_s=0.3, **kwargs)
        return controller, backend

    def offload(self, controller, lease="a", pid=4242, start=777):
        return controller.offload(lease, pid, start)

    def test_start_time_survives_a_comm_with_spaces_and_parens(self):
        self.assertEqual(offload.proc_start_time(4242, self.proc), 777)
        self.assertTrue(offload.holder_alive(4242, 777, self.proc))
        self.assertFalse(offload.holder_alive(4242, 778, self.proc))      # pid reuse
        self.assertFalse(offload.holder_alive(1, 777, self.proc))

    def test_unreadable_lease_state_refuses_to_start_but_a_missing_file_is_fine(self):
        controller, _ = self.make()
        self.offload(controller)
        for content in (b"{not json", b'{"leases": 5}'):
            controller.leases_path.write_bytes(content)
            with self.assertRaises(offload.LeaseStateUnreadable):
                self.make(loaded=False)
        controller.leases_path.unlink()
        self.make(loaded=False)                                               # no file: a normal start

    def test_a_startup_preload_is_not_a_first_answer(self):
        controller, backend = self.make(loaded=False)
        controller.serving = False
        controller.begin()                                                    # upstream's preload at startup
        controller.end()
        controller.backend.unload(False)
        controller.serving = True
        self.clock.now += 600
        controller.begin()
        self.clock.now += 0.5
        controller.end(answered=True)
        self.assertAlmostEqual(controller.status()["last_first_answer_s"], 0.5)

    def test_a_non_utf8_process_name_keeps_the_holder_alive(self):
        controller, backend = self.make()
        self.offload(controller)
        stat = Path(self.proc, "4242", "stat")
        stat.write_bytes(stat.read_bytes().replace(b"(my ) proc)", b"(\xe2\x82 \xff)"))
        self.assertTrue(offload.holder_alive(4242, 777, self.proc))
        self.assertEqual(controller.status()["state"], "pinned")

    def test_an_unreaped_zombie_holder_is_dead(self):
        controller, backend = self.make()
        self.offload(controller)
        stat = Path(self.proc, "4242", "stat")
        stat.write_text(stat.read_text().replace(") S ", ") Z "))               # same pid and start time, state Z
        self.assertFalse(offload.holder_alive(4242, 777, self.proc))
        self.assertEqual(controller.status()["state"], "loaded")                 # pruned, the model came back

    def test_an_unreadable_proc_is_not_a_dead_holder(self):
        from unittest import mock
        controller, backend = self.make()
        self.offload(controller)
        with mock.patch.object(Path, "read_bytes", side_effect=OSError(24, "Too many open files")) as read:
            self.assertTrue(offload.holder_alive(4242, 777, self.proc))
            self.assertEqual(controller.status()["state"], "pinned")           # the lease survives a /proc that cannot be read
        self.assertTrue(read.called)
        self.assertTrue(offload.holder_alive(4242, 777, self.proc))

    @unittest.skipUnless(importlib.util.find_spec("wyoming_faster_whisper"), "run() imports the upstream package of the whisper host")
    def test_a_mismatch_fallback_does_not_start_upstream_over_a_live_lease(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "vram-offload").mkdir()
            Path(tmp, "vram-offload", "wyoming-whisper.leases.json").write_text(json.dumps(
                {"leases": [{"id": "x", "holder_pid": os.getpid(), "holder_start": offload.proc_start_time(os.getpid())}],
                 "loaded_before_lease": True}))
            with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmp}):
                self.assertTrue(offload.live_lease_on_disk())
                Path(tmp, "vram-offload", "wyoming-whisper.leases.json").write_text("{broken")
                self.assertTrue(offload.live_lease_on_disk())                  # unreadable: cannot tell, fail closed
                Path(tmp, "vram-offload", "wyoming-whisper.leases.json").write_text(json.dumps(
                    {"leases": [{"id": "x", "holder_pid": os.getpid(), "holder_start": offload.proc_start_time(os.getpid())}],
                     "loaded_before_lease": True}))
                with mock.patch.object(offload, "installed_versions_match", side_effect=RuntimeError("version")), \
                        mock.patch.object(offload.asyncio, "run") as run, mock.patch.object(sys, "argv", ["x"]):
                    with self.assertRaises(SystemExit) as stop:
                        offload.run()
            self.assertEqual(stop.exception.code, 75)
            run.assert_not_called()

    def test_loaded_offload_pins_and_restore_reloads(self):
        controller, backend = self.make()
        reply = self.offload(controller)
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["freed_bytes"], 1000)
        self.assertEqual(backend.ct2.calls, [("unload", True)])             # the Orca offload keeps the host copy
        self.assertEqual(controller.status()["state"], "pinned")
        self.assertTrue(controller.restore("a")["ok"])
        self.assertTrue(backend.loaded())
        self.assertEqual(controller.status()["state"], "loaded")
        self.assertIsNotNone(controller.status()["last_offload_s"])
        self.assertIsNotNone(controller.status()["last_restore_s"])

    def test_idle_unloaded_stays_unloaded_after_release(self):
        controller, backend = self.make(loaded=False)
        reply = self.offload(controller)
        self.assertEqual((reply["ok"], reply["freed_bytes"]), (True, 0))
        self.assertEqual(backend.ct2.calls, [])                              # an offload on an unloaded daemon moves nothing
        controller.restore("a")
        self.assertFalse(backend.loaded())
        self.assertEqual(controller.status()["state"], "unloaded")

    def test_leases_are_reference_counted(self):
        controller, backend = self.make()
        self.offload(controller, "a")
        self.offload(controller, "b")
        controller.restore("a")
        self.assertFalse(backend.loaded())
        self.assertEqual(controller.status()["state"], "pinned")
        controller.restore("b")
        self.assertTrue(backend.loaded())
        self.assertTrue(controller.restore("never-seen")["ok"])              # idempotent

    def test_a_request_while_pinned_is_gpu_lent_and_never_loads(self):
        controller, backend = self.make()
        self.offload(controller)
        with self.assertRaises(offload.GpuLent):
            controller.begin()
        self.assertFalse(backend.loaded())

    def test_a_request_reloads_an_idle_unloaded_model_and_the_times_are_reported(self):
        controller, backend = self.make(loaded=False)
        controller.begin()
        self.assertTrue(backend.loaded())
        self.clock.now += 0.7
        controller.end(answered=True)
        status = controller.status()
        self.assertEqual(status["state"], "loaded")
        self.assertIsNotNone(status["last_restore_s"])
        self.assertAlmostEqual(status["last_first_answer_s"], 0.7)

    def test_offload_rolls_back_when_a_request_does_not_drain(self):
        controller, backend = self.make()
        controller.begin()
        reply = self.offload(controller)
        self.assertEqual(reply, {"ok": False, "error": "timeout"})
        self.assertTrue(backend.loaded())
        self.assertEqual(controller.leases, {})
        self.assertFalse(controller.leases_path.exists())                     # the persisted lease set is back to what it was
        controller.end()
        self.assertTrue(self.offload(controller)["ok"])                       # and it works once the request is done

    def test_offload_rolls_back_when_ct2_keeps_the_weights(self):
        controller, backend = self.make()
        backend.ct2.busy = True
        self.assertEqual(self.offload(controller), {"ok": False, "error": "busy"})
        self.assertTrue(backend.loaded())
        self.assertEqual((controller.leases, controller.loaded_before_lease), ({}, None))
        self.assertFalse(controller.leases_path.exists())

    def test_rollback_keeps_the_earlier_lease_untouched(self):
        controller, backend = self.make(loaded=False)
        self.offload(controller, "a")
        backend.ct2.model_is_loaded = True           # something loaded it again behind our back
        backend.ct2.busy = True
        self.assertEqual(self.offload(controller, "b")["error"], "busy")
        self.assertEqual(list(controller.leases), ["a"])
        self.assertIs(json.loads(controller.leases_path.read_text())["loaded_before_lease"], False)

    def test_a_lease_that_cannot_be_persisted_is_refused_and_moves_nothing(self):
        controller, backend = self.make()
        controller._persist = lambda: (_ for _ in ()).throw(OSError(28, "No space left on device"))
        self.assertEqual(self.offload(controller), {"ok": False, "error": "persist"})
        self.assertEqual((backend.ct2.calls, controller.leases, controller.loaded_before_lease), ([], {}, None))
        self.assertEqual(controller.status()["state"], "loaded")

    def test_a_restore_that_cannot_be_persisted_is_refused_and_keeps_the_lease(self):
        controller, backend = self.make()
        self.offload(controller, "a")
        self.offload(controller, "b")
        original = controller._persist
        controller._persist = lambda: (_ for _ in ()).throw(OSError(28, "No space left on device"))
        self.assertEqual(controller.restore("a"), {"ok": False, "error": "persist"})
        self.assertEqual(sorted(controller.leases), ["a", "b"])               # a restart would resurrect it, so it is still held
        controller._persist = original
        self.assertTrue(controller.restore("a")["ok"])

    def test_an_offload_waiting_on_a_load_that_outlasts_the_deadline_records_no_lease(self):
        controller, backend = self.make(loaded=False)
        gate, started = threading.Event(), threading.Event()
        real = backend.load
        backend.load = lambda: (started.set(), gate.wait(2), real())
        loader = threading.Thread(target=lambda: (controller.begin(), controller.end()))
        loader.start()
        started.wait(2)
        self.assertEqual(self.offload(controller), {"ok": False, "error": "timeout"})     # drain_timeout_s is 0.3 here
        self.assertEqual((controller.leases, controller.leases_path.exists()), ({}, False))
        gate.set()
        loader.join()

    def test_the_lease_hits_the_disk_before_the_weights_move(self):
        controller, backend = self.make()
        seen = []
        original = backend.unload
        backend.unload = lambda to_cpu: (seen.append(json.loads(controller.leases_path.read_text())), original(to_cpu))
        self.offload(controller)
        self.assertEqual(seen[0]["loaded_before_lease"], True)
        self.assertEqual([item["id"] for item in seen[0]["leases"]], ["a"])

    def test_a_dead_holder_is_pruned_and_the_last_one_reloads_a_prior_loaded_model(self):
        controller, backend = self.make()
        self.offload(controller)
        Path(self.proc, "4242", "stat").unlink()
        self.assertEqual(controller.status()["state"], "loaded")              # status itself prunes and restores
        self.assertEqual(controller.leases, {})

    def test_a_dead_holder_leaves_a_prior_unloaded_model_unloaded(self):
        controller, backend = self.make(loaded=False)
        self.offload(controller)
        Path(self.proc, "4242", "stat").unlink()
        controller.tick()
        self.assertFalse(backend.loaded())
        self.assertEqual(controller.status()["state"], "unloaded")

    def test_a_request_prunes_dead_holders_first(self):
        controller, backend = self.make()
        self.offload(controller)
        Path(self.proc, "4242", "stat").unlink()
        controller.begin()                                                    # not GpuLent: the lease was dead
        controller.end()
        self.assertTrue(backend.loaded())

    def test_restore_oom_leaves_the_model_unloaded_for_the_next_request(self):
        controller, backend = self.make()
        self.offload(controller)
        backend.ct2.fail_load = RuntimeError("CUDA failed with error out of memory")
        self.assertEqual(controller.restore("a"), {"ok": False, "error": "oom"})
        self.assertEqual(controller.status()["state"], "unloaded")
        backend.ct2.fail_load = None
        controller.begin()
        controller.end()
        self.assertTrue(backend.loaded())

    def test_restart_under_a_live_lease_starts_pinned_and_keeps_the_epoch_flag(self):
        for before in (True, False):
            with self.subTest(loaded_before_lease=before):
                controller, _ = self.make(loaded=before)
                self.offload(controller)
                again, backend = self.make(loaded=False)                      # the new process: no GPU copy
                self.assertEqual(again.status()["state"], "pinned")
                with self.assertRaises(offload.GpuLent):
                    again.begin()
                again.restore("a")
                self.assertEqual(backend.loaded(), before)
                Path(self.state, "wyoming-whisper.leases.json").unlink(missing_ok=True)

    def test_a_restart_with_only_dead_leases_is_a_normal_start(self):
        controller, _ = self.make()
        self.offload(controller)
        Path(self.proc, "4242", "stat").unlink()
        again, _ = self.make(loaded=False)
        self.assertEqual(again.status()["state"], "unloaded")
        self.assertFalse(again.leases_path.exists())

    def test_idle_unload_counts_only_real_requests(self):
        controller, backend = self.make(idle=900)
        self.clock.now += 899
        self.assertFalse(controller.tick())
        controller.begin(probe=True)                                          # the aisis probe is not activity
        controller.end(answered=True)
        self.clock.now += 2
        self.assertTrue(controller.tick())
        self.assertEqual(backend.ct2.calls, [("unload", False)])              # host copy dropped too
        self.assertEqual(controller.status()["state"], "unloaded")

    def test_a_probe_never_reloads_an_unloaded_model(self):
        controller, backend = self.make(loaded=False)
        with self.assertRaises(offload.ModelUnloaded):
            controller.begin(probe=True)
        self.assertEqual((backend.loaded(), controller._inflight, backend.ct2.calls), (False, 0, []))
        controller.begin()                                                    # a real request still does
        controller.end()
        self.assertTrue(backend.loaded())

    def test_a_waiter_does_not_reload_under_a_lease_taken_while_the_first_load_failed(self):
        controller, backend = self.make(loaded=False)
        gate, started = threading.Event(), threading.Event()

        def failing_load():
            started.set()
            gate.wait(2)
            raise RuntimeError("CUDA failed with error out of memory")
        backend.load = failing_load
        first = threading.Thread(target=lambda: self.assertRaises(RuntimeError, controller.begin))
        first.start()
        started.wait(2)
        outcome = []
        waiter = threading.Thread(target=lambda: outcome.append(self.begin_outcome(controller)))
        waiter.start()
        waiter.join(0.2)
        self.assertTrue(waiter.is_alive())                       # waiting on the load in progress
        loads_under_lease = []

        def later_load():
            loads_under_lease.append(bool(controller.leases))    # the waiter may legitimately load first, then the offload moves it off
            raise RuntimeError("CUDA failed with error out of memory")
        backend.load = later_load
        gate.set()
        first.join()
        self.assertTrue(self.offload(controller)["ok"])          # races the waiter; either order must end lent and unloaded
        waiter.join(2)
        self.assertFalse(backend.loaded())
        self.assertIn(outcome[0], ("lent", "failed"))
        self.assertNotIn(True, loads_under_lease)                # never a load while the lease is held

    @staticmethod
    def begin_outcome(controller):
        try:
            controller.begin()
            return "served"
        except offload.GpuLent:
            return "lent"
        except RuntimeError:
            return "failed"

    def test_a_real_request_resets_the_idle_timer(self):
        controller, backend = self.make(idle=900)
        self.clock.now += 800
        controller.begin()
        controller.end(answered=True)
        self.clock.now += 800
        self.assertFalse(controller.tick())
        self.assertTrue(backend.loaded())

    def test_idle_unload_is_off_at_zero_and_never_touches_a_busy_model(self):
        controller, backend = self.make(idle=0)
        self.clock.now += 10 ** 6
        self.assertFalse(controller.tick())
        controller.idle_unload_s = 900
        controller.begin()
        self.assertFalse(controller.tick())
        controller.end()
        self.assertTrue(backend.loaded())

    def test_a_request_arriving_during_an_offload_waits_and_then_sees_the_lease(self):
        controller, backend = self.make()
        gate = threading.Event()
        original = backend.unload
        entered = threading.Event()
        backend.unload = lambda to_cpu: (entered.set(), gate.wait(2), original(to_cpu))
        result = {}
        thread = threading.Thread(target=lambda: result.setdefault("reply", self.offload(controller)))
        thread.start()
        entered.wait(2)
        outcome = []

        def request():
            try:
                controller.begin()
                outcome.append("served")
            except offload.GpuLent:
                outcome.append("lent")
        waiter = threading.Thread(target=request)
        waiter.start()
        waiter.join(0.2)
        self.assertTrue(waiter.is_alive())                                     # held, not served from a half-unloaded model
        gate.set()
        thread.join()
        waiter.join()
        self.assertEqual(outcome, ["lent"])


class SettingsAndRacesTest(unittest.TestCase):
    def test_idle_setting_tolerates_bad_values(self):
        self.assertEqual([offload.idle_unload_setting(v) for v in (None, "", "15m", "0", "30")], [900.0, 900.0, 900.0, 0.0, 30.0])

    def test_offload_during_a_load_waits_and_then_unloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = os.path.join(tmp, "proc")
            fake_proc(proc, 4242, 777)
            backend = FakeBackend(loaded=False)
            gate = threading.Event()
            started = threading.Event()
            real_load = backend.load
            backend.load = lambda: (started.set(), gate.wait(2), real_load())
            controller = offload.OffloadController("w", backend, os.path.join(tmp, "v"), proc_root=proc, drain_timeout_s=2)
            loader = threading.Thread(target=lambda: (controller.begin(), controller.end()))
            loader.start()
            started.wait(2)
            reply = {}
            offloader = threading.Thread(target=lambda: reply.update(controller.offload("a", 4242, 777)))
            offloader.start()
            gate.set()
            loader.join()
            offloader.join()
            self.assertTrue(reply["ok"])
            self.assertFalse(backend.loaded())                                 # the weights really left the GPU
            self.assertTrue(controller.loaded_before_lease)


class ControlSocketTest(unittest.TestCase):
    def test_protocol_over_the_unix_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = os.path.join(tmp, "proc")
            fake_proc(proc, 4242, 777)
            backend = FakeBackend()
            controller = offload.OffloadController("wyoming-whisper", backend, os.path.join(tmp, "v"), clock=Clock(), proc_root=proc)
            path = Path(tmp, "v", "wyoming-whisper.sock")

            async def call(request):
                reader, writer = await asyncio.open_unix_connection(str(path))
                writer.write(request if isinstance(request, bytes) else json.dumps(request).encode() + b"\n")
                await writer.drain()
                reply = json.loads(await reader.readline())
                writer.close()
                return reply

            async def run():
                server = await offload.serve_control(controller, path)
                modes = (os.stat(path).st_mode & 0o777, os.stat(path.parent).st_mode & 0o777)
                replies = [await call({"op": "status"}),
                           await call({"op": "offload", "lease": "x", "holder_pid": 4242, "holder_start": 777}),
                           await call({"op": "restore", "lease": "x"}),
                           await call({"op": "offload", "lease": "x"}),
                           await call(b"not json\n"),
                           await call({"op": "nope"})]
                server.close()
                await server.wait_closed()
                return modes, replies

            modes, replies = asyncio.run(run())
            self.assertEqual(modes, (0o600, 0o700))
            self.assertEqual(replies[0]["state"], "loaded")
            self.assertEqual(replies[0]["name"], "wyoming-whisper")
            self.assertTrue(replies[1]["ok"])
            self.assertTrue(replies[2]["ok"])
            self.assertEqual([r["error"] for r in replies[3:]], ["bad_request"] * 3)


class BackendTest(unittest.TestCase):
    def test_the_whisper_backend_builds_lazily_and_moves_only_ct2_weights(self):
        class Transcriber:
            def __init__(self):
                self.model = type("M", (), {"model": FakeCt2(), "model_path": "/nonexistent"})()

        built = []
        backend = offload.WhisperBackend(lambda: built.append(1) or Transcriber())
        self.assertFalse(backend.loaded())
        backend.unload(True)                       # nothing to unload yet, nothing built
        self.assertEqual(built, [])
        backend.load()
        self.assertTrue(backend.loaded())
        backend.unload(False)
        self.assertFalse(backend.loaded())
        backend.load()                              # reloads the same object, never builds a second one
        self.assertEqual((len(built), backend.loaded()), (1, True))
        self.assertEqual(backend.weights_bytes(), 0)


try:
    import wyoming_faster_whisper  # noqa: F401
    HAVE_WYOMING = True
except ImportError:
    HAVE_WYOMING = False


@unittest.skipUnless(HAVE_WYOMING, "wyoming-faster-whisper is installed on the whisper host only")
class WyomingGlueTest(unittest.TestCase):
    """The real upstream handler and server, a fake CT2 model: a lent GPU is a Wyoming Error event with code gpu-lent."""

    def test_a_failed_preload_propagates_like_upstream_and_a_mismatch_runs_upstream_untouched(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmp}):
            upstream, Loader, Handler = offload.make_glue()

            class Boom:
                def __init__(self):
                    raise RuntimeError("CUDA failed with error out of memory")

            async def preload():
                loader = Loader(preferred_stt_library=upstream.SttLibrary.FASTER_WHISPER, preferred_language=None, download_dir=tmp,
                                local_files_only=True, model="m", compute_type="default", device="cuda", beam_size=1, cpu_threads=1,
                                initial_prompt=None, vad_parameters=None)
                loader.backend._make = Boom
                try:
                    await loader.load_transcriber()
                finally:
                    for task in loader._tasks:
                        task.cancel()
                    await asyncio.sleep(0)

            with self.assertRaises(RuntimeError):                  # a boot OOM crashes the unit; it must not look "unloaded"
                asyncio.run(preload())
        saved = (upstream.ModelLoader, upstream.DispatchEventHandler)
        self.addCleanup(lambda: (setattr(upstream, "ModelLoader", saved[0]), setattr(upstream, "DispatchEventHandler", saved[1])))
        with tempfile.TemporaryDirectory() as empty, mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": empty}), \
                mock.patch.object(offload, "installed_versions_match", side_effect=RuntimeError("version")), \
                mock.patch.object(offload.asyncio, "run") as run, mock.patch.object(sys, "argv", ["x"]):
            offload.run()
        run.assert_called_once()
        self.assertEqual((upstream.ModelLoader, upstream.DispatchEventHandler), saved)

    def test_pinned_request_gets_gpu_lent_and_restored_one_a_transcript(self):
        from wyoming.asr import Transcribe, Transcript
        from wyoming.audio import AudioChunk, AudioStart, AudioStop
        from wyoming.client import AsyncClient
        from wyoming.error import Error
        from wyoming.server import AsyncServer

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["XDG_RUNTIME_DIR"] = tmp
            os.environ["WHISPER_IDLE_UNLOAD_S"] = "0"
            self.addCleanup(os.environ.pop, "XDG_RUNTIME_DIR")
            self.addCleanup(os.environ.pop, "WHISPER_IDLE_UNLOAD_S")
            upstream, Loader, Handler = offload.make_glue()
            fake = FakeCt2()
            Loader._make_transcriber = lambda loader: (_ for _ in ()).throw(AssertionError("the real transcriber must not be built"))

            class Transcriber:
                model = type("M", (), {"model": fake, "model_path": "/nonexistent"})()
                vad_filter = False

                def transcribe(self, *args, **kwargs):
                    if not fake.model_is_loaded:
                        raise RuntimeError("The model for this translator was unloaded")
                    return "hello"

            async def run():
                loader = Loader(preferred_stt_library=upstream.SttLibrary.FASTER_WHISPER, preferred_language=None, download_dir=tmp,
                                local_files_only=True, model="m", compute_type="default", device="cuda", beam_size=1, cpu_threads=1,
                                initial_prompt=None, vad_parameters=None)
                loader.backend._make = Transcriber    # before anything can load: no real model, no GPU
                await loader.load_transcriber()
                server = AsyncServer.from_uri("tcp://127.0.0.1:0")
                import socket as sock
                probe = sock.socket()
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
                probe.close()
                server = AsyncServer.from_uri(f"tcp://127.0.0.1:{port}")
                from functools import partial
                from wyoming.info import Info
                task = asyncio.ensure_future(server.run(partial(Handler, Info(), loader)))
                await asyncio.sleep(0.2)

                async def utterance(context=None):
                    async with AsyncClient.from_uri(f"tcp://127.0.0.1:{port}") as client:
                        await client.write_event(Transcribe(language="en", context=context).event())
                        await client.write_event(AudioStart(rate=16000, width=2, channels=1).event())
                        await client.write_event(AudioChunk(rate=16000, width=2, channels=1, audio=bytes(3200)).event())
                        await client.write_event(AudioStop().event())
                        return await asyncio.wait_for(client.read_event(), 5)

                try:
                    first = await utterance()
                    ctl = Path(tmp, "vram-offload", "wyoming-whisper.sock")
                    reader, writer = await asyncio.open_unix_connection(str(ctl))
                    writer.write(json.dumps({"op": "offload", "lease": "o", "holder_pid": os.getpid(),
                                             "holder_start": offload.proc_start_time(os.getpid())}).encode() + b"\n")
                    offloaded = json.loads(await reader.readline())
                    writer.close()
                    lent = await utterance()
                    reader, writer = await asyncio.open_unix_connection(str(ctl))
                    writer.write(b'{"op":"restore","lease":"o"}\n')
                    restored = json.loads(await reader.readline())
                    writer.close()
                    after = await utterance({"aisis_probe": True})
                    return first, offloaded, lent, restored, after
                finally:
                    task.cancel()
                    loader.server.close()

            first, offloaded, lent, restored, after = asyncio.run(run())
            self.assertEqual(Transcript.from_event(first).text, "hello")
            self.assertTrue(offloaded["ok"])
            self.assertTrue(Error.is_type(lent.type))
            self.assertEqual(Error.from_event(lent).code, "gpu-lent")
            self.assertTrue(restored["ok"])
            self.assertEqual(Transcript.from_event(after).text, "hello")


if __name__ == "__main__":
    unittest.main()
