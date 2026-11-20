"""wyoming-whisper with a VRAM control socket: idle unload, and "lend the GPU" leases for Snapmaker Orca's G-code preview.

Spec: snap-orca docs/superpowers/specs/2026-11-18-vram-offload-design.md (components 1, 2 and 7).

The process stays up in every state, so `Restart=`, the aisis-failover restarts and Home Assistant's connection never fire; only the
CTranslate2 weights move. States: `loaded`, `unloaded` (idle: the next real request reloads), `pinned` (a lease is held: never
reloads, a request gets a Wyoming `Error` event with code `gpu-lent`) and `loading`.

Run it instead of `python -m wyoming_faster_whisper`, with the same arguments:
    python wyoming_whisper_offload.py --uri tcp://… --model large-v3-turbo --device cuda …
and talk to the running daemon with:
    python wyoming_whisper_offload.py ctl status|offload|restore

Control protocol: newline-delimited JSON, one request per connection, on $XDG_RUNTIME_DIR/vram-offload/wyoming-whisper.sock
(directory 0700, socket 0600). The pure state machine (OffloadController) knows nothing of wyoming or CTranslate2, so it is
tested with a fake backend; the glue to the upstream classes is built lazily in `install()` after a version check, and on any
mismatch the upstream server runs untouched, without a socket (Orca then simply sees no consumer).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

LOG = logging.getLogger("wyoming_whisper_offload")

NAME = "wyoming-whisper"
SUPPORTED = {"wyoming-faster-whisper": "3.1.0", "ctranslate2": "4.7.1"}   # the versions the hook points were read from
DRAIN_TIMEOUT_S = 10.0      # an offload waits this long for requests in flight
UNLOAD_RETRY_S = 0.05
TICK_S = 30.0               # dead-lease pruning and the idle timer
GPU_LENT = "gpu-lent"


class GpuLent(RuntimeError):
    """A request arrived while a lease pins the model off the GPU."""


class ModelUnloaded(RuntimeError):
    """A probe arrived while the model is off the GPU: it never reloads it (only real requests do)."""


MODEL_UNLOADED = "model-unloaded"


class LeaseStateUnreadable(RuntimeError):
    """The persisted lease file exists but cannot be read or parsed."""


class UnloadBusy(Exception):
    """CTranslate2 kept the weights on the GPU: it ignores unload_model while a batch is active or its model lock is held."""


def is_oom(exc):
    text = f"{type(exc).__name__} {exc}".lower()
    return "out of memory" in text or "oom" in text.split()


# ---------------------------------------------------------------------------------------------------------------------------
# Holder liveness (pid + /proc/<pid>/stat start time, so a recycled pid is not mistaken for the holder)

def proc_stat_fields(pid, proc_root="/proc"):
    """Fields of /proc/<pid>/stat from the state letter on (index 0); None when the process does not exist. Any other read error
    (EMFILE, EIO) is raised: "cannot tell" must not read as "dead". The comm field may contain spaces and ")"."""
    try:
        stat = Path(proc_root, str(pid), "stat").read_bytes().decode("latin-1")    # comm may hold any bytes; only the tail is parsed
    except (FileNotFoundError, ProcessLookupError):
        return None
    try:
        return stat[stat.rindex(")") + 2:].split()
    except ValueError:
        return None


def proc_start_time(pid, proc_root="/proc"):
    try:
        fields = proc_stat_fields(pid, proc_root)
        return int(fields[19]) if fields else None      # field 22 (starttime)
    except (OSError, ValueError, IndexError):
        return None


def holder_alive(pid, start, proc_root="/proc"):
    """Same pid, same start time (pid reuse), and not a zombie: an exited holder its parent has not reaped is gone.
    When /proc cannot be read at all the holder is assumed alive: a wrongly dropped lease reloads the model into a lent GPU."""
    try:
        fields = proc_stat_fields(pid, proc_root)
        if not fields or fields[0] in ("Z", "X"):
            return False
        return int(fields[19]) == start
    except OSError:
        return True
    except (ValueError, IndexError):
        return False


# ---------------------------------------------------------------------------------------------------------------------------
# The state machine

class OffloadController:
    """Lease-counted offload of one model. Thread-safe: requests come from worker threads, control ops from `asyncio.to_thread`.

    `backend` provides loaded() -> bool, load(), unload(to_cpu) (may silently do nothing, like CTranslate2) and weights_bytes().
    Control ops hold `_control` for their whole duration (serialized); requests and the state fields use `_cond`."""

    def __init__(self, name, backend, state_dir, idle_unload_s=0.0, clock=time.monotonic, proc_root="/proc",
                 drain_timeout_s=DRAIN_TIMEOUT_S, pid=None):
        self.name = name
        self.backend = backend
        self.state_dir = Path(state_dir)
        self.idle_unload_s = idle_unload_s
        self.clock = clock
        self.proc_root = proc_root
        self.drain_timeout_s = drain_timeout_s
        self.pid = pid or os.getpid()
        self._control = threading.Lock()
        self._cond = threading.Condition()
        self._inflight = 0
        self._exclusive = False          # a control op is moving weights: new requests wait
        self._loading = False
        self.serving = True              # False while upstream preloads at startup: that load answers no request
        self._first_answer_from = None   # arrival time of the request whose load the next answer completes
        self.leases = {}                 # id -> {"id", "holder_pid", "holder_start"}
        self.loaded_before_lease = None  # the epoch's pre-lease state; None outside an epoch
        self.last_activity = clock()
        self.last_offload_s = self.last_restore_s = self.last_first_answer_s = None
        self._read_persisted()

    # -- persistence ----------------------------------------------------------------------------------------------------

    @property
    def leases_path(self):
        return self.state_dir / f"{self.name}.leases.json"

    def _persist(self):
        """Atomic (tmp + fsync + rename). Raises on failure; callers roll back."""
        if not self.leases:
            self.leases_path.unlink(missing_ok=True)
            return
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = json.dumps({"leases": list(self.leases.values()), "loaded_before_lease": self.loaded_before_lease})
        fd, tmp = tempfile.mkstemp(dir=self.state_dir, prefix=f".{self.name}.leases.")
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.leases_path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _read_persisted(self):
        """A restart with a live lease starts without the GPU copy (the backend starts unloaded) and keeps the epoch's pre-lease flag;
        a restart with only dead leases drops them and is a normal start (the usual preload is the final-lease reload)."""
        try:
            data = json.loads(self.leases_path.read_text())
            leases = {item["id"]: {"id": item["id"], "holder_pid": int(item["holder_pid"]), "holder_start": int(item["holder_start"])}
                      for item in data["leases"]}
            flag = data["loaded_before_lease"]
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # Only a missing file means "no leases". Unreadable or invalid state cannot establish who holds the GPU: starting would
            # preload onto a lent GPU, so the daemon refuses to start (systemd retries) until the file is fixed or removed.
            raise LeaseStateUnreadable(f"{self.leases_path}: {type(exc).__name__}") from exc
        live = {key: item for key, item in leases.items() if holder_alive(item["holder_pid"], item["holder_start"], self.proc_root)}
        if live:
            self.leases, self.loaded_before_lease = live, bool(flag)
            LOG.info("leases_restored count=%d loaded_before_lease=%s", len(live), self.loaded_before_lease)
        else:
            self.leases_path.unlink(missing_ok=True)

    # -- requests (worker threads) --------------------------------------------------------------------------------------

    def begin(self, probe=False):
        """Admit one real request: raises GpuLent while pinned, loads the model first when it is not on the GPU (a load of
        the same model by an earlier request is awaited, not repeated). Pair with end()."""
        self.prune()
        arrived = self.clock()
        with self._cond:
            while True:     # admission is re-checked after every wait: a lease, an offload or a failed load may have happened meanwhile
                if self._exclusive or self._loading:
                    self._cond.wait()
                    continue
                if self.leases:
                    raise GpuLent("the GPU is lent out")
                if self.backend.loaded():
                    break
                if probe:
                    raise ModelUnloaded("idle-unloaded: a probe does not reload it")
                self._loading = True
                break
            if not probe:
                self.last_activity = arrived
            self._inflight += 1
            if not self._loading:
                return
        try:
            started = self.clock()
            self.backend.load()
            seconds = self.clock() - started
        except BaseException:
            with self._cond:
                self._loading = False
                self._inflight -= 1
                self._cond.notify_all()
            raise
        with self._cond:
            self._loading = False
            self.last_restore_s = seconds
            if self.serving:
                self._first_answer_from = arrived
            self._cond.notify_all()
        LOG.info("model_reloaded restore_s=%.3f", seconds)

    def end(self, answered=False):
        with self._cond:
            if answered and self._first_answer_from is not None:
                self.last_first_answer_s = self.clock() - self._first_answer_from
                self._first_answer_from = None
                LOG.info("first_answer_s=%.3f", self.last_first_answer_s)
            self._inflight -= 1
            self._cond.notify_all()

    # -- control ops (serialized) ---------------------------------------------------------------------------------------

    def status(self):
        self.prune()
        with self._cond:
            if self._loading:
                state = "loading"
            elif self.leases:
                state = "pinned"
            elif self.backend.loaded():
                state = "loaded"
            else:
                state = "unloaded"
            return {"ok": True, "name": self.name, "pid": self.pid, "state": state, "weights_bytes": self.backend.weights_bytes(),
                    "idle_s": round(self.clock() - self.last_activity, 3),
                    "leases": [{k: v for k, v in item.items()} for item in self.leases.values()],
                    "last_offload_s": self.last_offload_s, "last_restore_s": self.last_restore_s,
                    "last_first_answer_s": self.last_first_answer_s}

    def offload(self, lease, holder_pid, holder_start):
        with self._control:
            self._prune_locked()
            began = self.clock()
            with self._cond:
                if lease in self.leases:
                    return {"ok": True, "seconds": 0.0, "freed_bytes": 0}
                if not self._cond.wait_for(lambda: not self._loading, self.drain_timeout_s):    # a load in progress ends on the GPU: sample after it
                    return {"ok": False, "error": "timeout"}      # before any lease is recorded: nothing to undo
                was_loaded = self.backend.loaded()
                before = (dict(self.leases), self.loaded_before_lease)
                if not self.leases:
                    self.loaded_before_lease = was_loaded
                self.leases[lease] = {"id": lease, "holder_pid": holder_pid, "holder_start": holder_start}
                try:
                    self._persist()      # lease and epoch state hit the disk BEFORE any weight moves
                except OSError as exc:
                    self.leases, self.loaded_before_lease = before
                    LOG.error("lease_persist_failed class=%s", type(exc).__name__)
                    return {"ok": False, "error": "persist"}
                self._exclusive = True
            error = None
            try:
                if was_loaded:
                    error = self._unload_for_lease()
            finally:
                with self._cond:
                    if error:
                        self.leases, self.loaded_before_lease = before
                        try:
                            self._persist()
                        except OSError as exc:
                            LOG.error("lease_rollback_persist_failed class=%s", type(exc).__name__)
                    self._exclusive = False
                    self._cond.notify_all()
            if error:
                return {"ok": False, "error": error}
            seconds = self.clock() - began
            if was_loaded:
                self.last_offload_s = seconds
            return {"ok": True, "seconds": round(seconds, 3), "freed_bytes": self.backend.weights_bytes() if was_loaded else 0}

    def _unload_for_lease(self):
        """Drain, then move the weights to the host. Returns None, or the error name after putting the model back as it was."""
        with self._cond:
            if not self._cond.wait_for(lambda: self._inflight == 0, self.drain_timeout_s):
                return "timeout"
        try:
            self._unload_retrying(True, self.drain_timeout_s)
        except UnloadBusy:
            return "busy"
        except Exception as exc:
            LOG.error("offload_unload_failed class=%s", type(exc).__name__)
            self._recover_unloaded_by_failure()
            return "failed"
        return None

    def _recover_unloaded_by_failure(self):
        if not self.backend.loaded():
            try:
                self.backend.load()
            except Exception as exc:
                LOG.error("offload_rollback_reload_failed class=%s", type(exc).__name__)

    def _unload_retrying(self, to_cpu, budget_s):
        """unload_model is a silent no-op while CTranslate2 is busy, so re-check `loaded` and retry."""
        deadline = time.monotonic() + budget_s
        while True:
            self.backend.unload(to_cpu)
            if not self.backend.loaded():
                return
            if time.monotonic() >= deadline:
                raise UnloadBusy
            time.sleep(UNLOAD_RETRY_S)

    def restore(self, lease):
        with self._control:
            self._prune_locked()
            with self._cond:
                if lease not in self.leases:
                    return {"ok": True, "seconds": 0.0}
            return self._release_locked([lease], strict=True)

    def _release_locked(self, lease_ids, strict=False):
        """Drop leases; the last one ends the epoch: reload only if the model was loaded before it. `_control` is held.
        `strict` (an explicit restore) refuses and keeps the previous state when the new lease set cannot be persisted, so a restart
        never resurrects a lease that was acknowledged as released; pruning dead holders is harmless to lose (a restart prunes again)."""
        began = self.clock()
        with self._cond:
            before = (dict(self.leases), self.loaded_before_lease)
            for lease in lease_ids:
                self.leases.pop(lease, None)
            final = not self.leases
            flag = self.loaded_before_lease
            if final:
                self.loaded_before_lease = None
            try:
                self._persist()
            except OSError as exc:
                LOG.error("lease_persist_failed class=%s", type(exc).__name__)
                if strict:
                    self.leases, self.loaded_before_lease = before
                    return {"ok": False, "error": "persist"}
            if not (final and flag):
                self._cond.notify_all()
                return {"ok": True, "seconds": 0.0}
            if self._loading or self.backend.loaded():
                return {"ok": True, "seconds": 0.0}
            self._loading = True
        try:
            started = self.clock()
            self.backend.load()
            seconds = self.clock() - started
        except Exception as exc:
            LOG.error("restore_failed class=%s", type(exc).__name__)    # stays unloaded: the next request reloads
            return {"ok": False, "error": "oom" if is_oom(exc) else "failed"}
        else:
            self.last_restore_s = seconds
            return {"ok": True, "seconds": round(self.clock() - began, 3)}
        finally:
            with self._cond:
                self._loading = False
                self._cond.notify_all()

    def prune(self):
        """Drop leases whose holder is gone. Cheap when there are none; a dead last lease reloads like `restore`."""
        with self._cond:
            if not any(not holder_alive(item["holder_pid"], item["holder_start"], self.proc_root) for item in self.leases.values()):
                return
        with self._control:
            self._prune_locked()

    def _prune_locked(self):
        with self._cond:
            dead = [key for key, item in self.leases.items() if not holder_alive(item["holder_pid"], item["holder_start"], self.proc_root)]
        if dead:
            LOG.warning("dead_leases_dropped count=%d", len(dead))
            self._release_locked(dead)

    def tick(self):
        """Every TICK_S: prune dead holders, then unload after idle_unload_s without a real request (host copy dropped too)."""
        self.prune()
        if self.idle_unload_s <= 0:
            return False
        with self._control:
            with self._cond:
                if (self.leases or self._loading or self._inflight or self._exclusive or not self.backend.loaded()
                        or self.clock() - self.last_activity < self.idle_unload_s):
                    return False
                self._exclusive = True
            try:
                self._unload_retrying(False, 2.0)
                LOG.info("idle_unload idle_s=%.0f", self.clock() - self.last_activity)
                return True
            except Exception as exc:    # still busy or failed: stays loaded, the next tick retries
                LOG.warning("idle_unload_skipped class=%s", type(exc).__name__)
                return False
            finally:
                with self._cond:
                    self._exclusive = False
                    self._cond.notify_all()


# ---------------------------------------------------------------------------------------------------------------------------
# Control socket

def idle_unload_setting(raw, default=900.0):
    """WHISPER_IDLE_UNLOAD_S: a bad or empty value must not put the unit in a restart loop."""
    if raw is None or not raw.strip():
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        LOG.warning("bad WHISPER_IDLE_UNLOAD_S=%r, using %s", raw, default)
        return default


def live_lease_on_disk(name=NAME, env=None):
    runtime = socket_path(name, env)
    if runtime is None:
        return False
    try:
        data = json.loads(Path(runtime.parent, f"{name}.leases.json").read_text())
        return any(holder_alive(int(item["holder_pid"]), int(item["holder_start"])) for item in data["leases"])
    except FileNotFoundError:
        return False
    except (OSError, ValueError, KeyError, TypeError):
        return True       # cannot tell who holds the GPU: do not start upstream over it


def socket_path(name=NAME, env=None):
    runtime = (env or os.environ).get("XDG_RUNTIME_DIR")
    return Path(runtime, "vram-offload", f"{name}.sock") if runtime else None


def handle_request(controller, request):
    """One decoded request -> reply dict (blocking: run it in a thread)."""
    if not isinstance(request, dict):
        return {"ok": False, "error": "bad_request"}
    op = request.get("op")
    if op == "status":
        return controller.status()
    lease = request.get("lease")
    if not isinstance(lease, str) or not 0 < len(lease) <= 128:
        return {"ok": False, "error": "bad_request"}
    if op == "restore":
        return controller.restore(lease)
    if op == "offload":
        pid, start = request.get("holder_pid"), request.get("holder_start")
        if not all(isinstance(value, int) and not isinstance(value, bool) for value in (pid, start)):
            return {"ok": False, "error": "bad_request"}
        return controller.offload(lease, pid, start)
    return {"ok": False, "error": "bad_request"}


async def serve_control(controller, path):
    directory = path.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    path.unlink(missing_ok=True)       # a stale socket of a previous run (systemd keeps one process per unit)

    async def handle(reader, writer):
        try:
            try:
                request = json.loads(await asyncio.wait_for(reader.readline(), 5))
            except (ValueError, asyncio.TimeoutError):
                reply = {"ok": False, "error": "bad_request"}
            else:
                reply = await asyncio.to_thread(handle_request, controller, request)
            writer.write(json.dumps(reply).encode() + b"\n")
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_unix_server(handle, path=str(path))   # the 0700 parent directory already keeps others out until the chmod
    os.chmod(path, 0o600)
    return server


async def tick_forever(controller, interval=TICK_S):
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(controller.tick)
        except Exception as exc:
            LOG.warning("tick_failed class=%s", type(exc).__name__)


def ask(path, request, timeout=60.0):
    """Blocking client for the `ctl` subcommand."""
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(timeout)
        sock.connect(str(path))
        sock.sendall(json.dumps(request).encode() + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    return json.loads(data)


def ctl_main(argv):
    parser = argparse.ArgumentParser(prog="wyoming_whisper_offload.py ctl")
    parser.add_argument("op", choices=("status", "offload", "restore"))
    parser.add_argument("--lease", help="lease id (offload default: a new uuid; restore: required)")
    parser.add_argument("--holder-pid", type=int, default=os.getppid(), help="process whose death releases the lease (default: the calling shell)")
    parser.add_argument("--socket", type=Path, default=socket_path())
    args = parser.parse_args(argv)
    if args.socket is None:
        print("XDG_RUNTIME_DIR is not set", file=sys.stderr)
        return 2
    request = {"op": args.op}
    if args.op != "status":
        request["lease"] = args.lease or (str(uuid.uuid4()) if args.op == "offload" else None)
        if request["lease"] is None:
            print("restore needs --lease", file=sys.stderr)
            return 2
    if args.op == "offload":
        start = proc_start_time(args.holder_pid)
        if start is None:
            print(f"holder pid {args.holder_pid} is not running", file=sys.stderr)
            return 2
        request.update(holder_pid=args.holder_pid, holder_start=start)
    try:
        reply = ask(args.socket, request)
    except OSError as exc:
        print(f"{args.socket}: {exc}", file=sys.stderr)
        return 1
    if args.op == "offload" and reply.get("ok"):
        reply["lease"] = request["lease"]
    print(json.dumps(reply, indent=2))
    return 0 if reply.get("ok") else 1


# ---------------------------------------------------------------------------------------------------------------------------
# CTranslate2 / wyoming-faster-whisper glue

class WhisperBackend:
    """The CT2 handle is `transcriber.model.model` (a ctranslate2.models.Whisper). Before the first load, and after a restart under
    a live lease, there is no transcriber at all: `load` builds it exactly like upstream's ModelLoader, so a lent GPU is never touched."""

    def __init__(self, make_transcriber):
        self._make = make_transcriber
        self.transcriber = None

    def _ct2(self):
        return self.transcriber.model.model

    def loaded(self):
        return self.transcriber is not None and bool(self._ct2().model_is_loaded)

    def load(self):
        if self.transcriber is None:
            self.transcriber = self._make()
        else:
            self._ct2().load_model()    # keep_cache=False: drops the host copy; from disk after an idle unload, from it after an offload

    def unload(self, to_cpu):
        if self.transcriber is not None:
            self._ct2().unload_model(to_cpu=to_cpu)

    def weights_bytes(self):
        try:
            return (Path(self.transcriber.model.model_path) / "model.bin").stat().st_size
        except (AttributeError, OSError):
            return 0


def installed_versions_match():
    from importlib import metadata
    for package, wanted in SUPPORTED.items():
        found = metadata.version(package)
        if found != wanted:
            raise RuntimeError(f"{package} {found} (hook points were read from {wanted})")


def make_glue():
    """Build the loader/handler subclasses. Imports wyoming lazily; raises when a hook point is not what the spec read."""
    installed_versions_match()
    import wyoming_faster_whisper.__main__ as upstream
    from wyoming.asr import Transcribe
    from wyoming.error import Error
    from wyoming_faster_whisper.faster_whisper_handler import FasterWhisperTranscriber
    for owner, attribute in ((upstream.ModelLoader, "load_transcriber"), (upstream.DispatchEventHandler, "handle_event")):
        if not callable(getattr(owner, attribute, None)):
            raise RuntimeError(f"{owner.__name__}.{attribute} is missing")
    base_loader, base_handler = upstream.ModelLoader, upstream.DispatchEventHandler

    class GuardedTranscriber:
        """What the handler calls in its worker thread: admits the request (reload / gpu-lent) around the real transcribe."""

        def __init__(self, controller, backend, probe):
            self.controller, self.backend, self.probe = controller, backend, probe

        def transcribe(self, *args, **kwargs):
            self.controller.begin(self.probe)
            try:
                text = self.backend.transcriber.transcribe(*args, **kwargs)
            except BaseException:
                self.controller.end()
                raise
            self.controller.end(answered=True)
            return text

    class OffloadLoader(base_loader):
        _probe = False

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.backend = WhisperBackend(self._make_transcriber)
            idle = idle_unload_setting(os.environ.get("WHISPER_IDLE_UNLOAD_S"))
            runtime = socket_path()
            # without a runtime directory there is no socket, and the lease file lives in a fresh private directory, never a shared /tmp path
            state_dir = runtime.parent if runtime else Path(tempfile.mkdtemp(prefix="vram-offload-"))
            self.controller = OffloadController(NAME, self.backend, state_dir, idle_unload_s=idle)
            self.controller.serving = False
            self._tasks = []
            if runtime is None:
                LOG.error("XDG_RUNTIME_DIR is not set: no control socket")
            else:
                self._tasks.append(asyncio.ensure_future(self._serve(runtime)))
            self._tasks.append(asyncio.ensure_future(tick_forever(self.controller)))

        async def _serve(self, path):
            try:
                self.server = await serve_control(self.controller, path)
                LOG.info("control_socket path=%s idle_unload_s=%s", path, self.controller.idle_unload_s)
            except OSError as exc:
                LOG.error("control_socket_failed class=%s", type(exc).__name__)

        def _make_transcriber(self):
            return FasterWhisperTranscriber(self.model, cache_dir=self.download_dir, device=self.device, compute_type=self.compute_type,
                                            cpu_threads=self.cpu_threads, vad_parameters=self.vad_parameters)

        def bound(self, probe):
            view = copy_object(self)
            view._probe = probe
            return view

        async def load_transcriber(self, language=None):
            """Upstream calls this at the first audio chunk, so the reload overlaps the audio still arriving, and at startup (preload).
            While a lease pins the GPU nothing loads; the answer is decided in the guarded transcribe."""
            if not self.controller.leases:
                try:
                    await asyncio.to_thread(self._warm)
                except (GpuLent, ModelUnloaded):
                    pass     # any other failure propagates as upstream's did: a boot OOM must crash the unit, not look "unloaded"
            return GuardedTranscriber(self.controller, self.backend, self._probe)

        def _warm(self):
            self.controller.begin(self._probe)
            self.controller.end()

    class OffloadHandler(base_handler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            if hasattr(self._loader, "controller"):
                self._loader.controller.serving = True      # the server runs: loads from here on are caused by requests

        async def handle_event(self, event):
            if Transcribe.is_type(event.type):
                context = Transcribe.from_event(event).context
                probe = bool(context.get("aisis_probe")) if isinstance(context, dict) else False
                self._loader = self._loader.bound(probe) if hasattr(self._loader, "bound") else self._loader
            try:
                return await super().handle_event(event)
            except (GpuLent, ModelUnloaded) as refused:
                lent = isinstance(refused, GpuLent)
                await self.write_event(Error(text="the GPU is lent to another program; speech recognition is paused" if lent
                                             else "the model is unloaded (idle)", code=GPU_LENT if lent else MODEL_UNLOADED).event())
                self._language = None
                self._transcriber = None
                return False

    return upstream, OffloadLoader, OffloadHandler


def copy_object(obj):
    import copy
    return copy.copy(obj)


def run():
    if len(sys.argv) > 1 and sys.argv[1] == "ctl":
        sys.exit(ctl_main(sys.argv[2:]))
    try:
        upstream, loader, handler = make_glue()
    except Exception as exc:
        import wyoming_faster_whisper.__main__ as upstream
        LOG.error("offload control disabled, running upstream unchanged: %s: %s", type(exc).__name__, exc)
        if live_lease_on_disk():     # upstream would preload onto a GPU that a live holder has been lent
            LOG.error("a live lease is held and the offload hooks do not match: not starting upstream over the lent GPU")
            sys.exit(75)
    else:
        upstream.ModelLoader, upstream.DispatchEventHandler = loader, handler
    try:
        asyncio.run(upstream.main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
