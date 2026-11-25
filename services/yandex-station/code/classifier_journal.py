"""The adapter's writer of the classifier decision/evidence journal and of the private classifier incident events (aisis#250).

Contract: `classifier-journal/1.0` revision r4 for `request` records (agent-tools `lib/agenttools_classifier_quality`, `journal_schema.py`); the
`proposed` record does not carry r4's `facts_revision` / `expected_target` yet (aisis#302). This module
writes the records `laya_quality.py` builds; it is the adapter's own small writer because the shared package is not installable
into the adapter's service venv. The directory, file names, identities and the write rules are the contract's, so the shared
reader (`journal report`) reads these files as they are:

* `<state root>/journal/<record_id>.json`, one record per file, 0600 in a 0700 directory, at most 16 KiB, canonical JSON,
  created atomically (temp file, fsync, no-replace link). `record_id = uuid5(NAMESPACE, request_id|phase|decision_id|seq)`:
  a timestamp is never part of the identity. An identical retry is a no-op, a DIFFERENT payload under one identity is refused
  and kept apart in `conflicts/` (never overwritten, counted).
* Records carry enums, hashes, tokens and numbers only. `validate` is an ALLOW-LIST by exact path and type: an unregistered key,
  a free-text value, a secret-shaped string is refused, so a record can never carry what a speaker said or a model answered.
  Raw evidence belongs to the separate private evidence store, which this writer does not use.
* Incident events (schema v1, `events/`) go to the same guardian as every other classifier consumer: provider unavailable,
  control failed, provider failed. Same retention bounds as task-cli's writer (2000 files), warnings are removed first.

Every failure to write is returned to the caller as a status, never raised into a voice request: the journal is evidence, and a
broken disk must not break the classification (it makes the request count as "not recorded" in the coverage KPI instead).
Tests: `test_classifier_journal.py`.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import stat
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

LOG = logging.getLogger("aisis.classifier_journal")

SCHEMA_VERSION = 1
CONTRACT = "classifier-journal/1.0"
RECORD_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "agent-tools:classifier-journal:v1")
MAX_RECORD_BYTES = 16 * 1024
MAX_EVENT_FILES = 2000
DEDUPE_WINDOW_S = 3600.0
WRITTEN, NOOP, CONFLICT, FAILED = "written", "noop", "conflict", "failed"
MAX_AGE_DAYS = 90
MAX_BYTES = 64 * 1024 * 1024
PRUNE_EVERY_S = 3600.0
RETENTION_FILE = ".retention.json"

_TOKEN = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,63}")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,63}")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}")
_LANG = re.compile(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,2}")
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/#+~-]{0,199}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_HEX40 = re.compile(r"[0-9a-f]{40}")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z")
_ELIGIBILITY = re.compile(r"eligible|excluded:[a-z0-9][a-z0-9_.-]{0,62}")
_SECRET_PREFIX = re.compile(r"(?i)^(?:sk|pk|rk)-|^gh[pousr]_|^xox[abprs]-|^akia[0-9a-z]{12,}|^aiza|^eyj[a-z0-9_-]{10,}")
_SECRET_WORDS = re.compile(r"(?i)bearer|password|passwd|secret")
_LONG_OPAQUE = re.compile(r"[A-Za-z0-9+/_-]{40,}")

ROLES = ("main", "control_positive", "control_negative", "cache", "audit", "judge")
ATTEMPT_STATUSES = ("answered", "abstained", "invalid", "truncated", "unavailable", "failed", "cache_hit", "skipped")
CONTROL_RESULTS = ("pass", "fail", "route_unverified")
COST_STATUSES = ("known", "estimated", "unknown")
RUNNER_KINDS = ("production", "ci", "runpod", "local")
POLICY_KINDS = ("deterministic_rule", "model")
QUALIFICATION_STATUSES = ("qualified", "provisional", "none", "not_applicable")

INCIDENT_KINDS = ("provider_unavailable", "control_failed", "provider_failed", "qualification_failed", "judge_unavailable")


class JournalInvalid(ValueError):
    """A record that does not meet the contract; `path` names the first offending field, never its value."""

    def __init__(self, path: str, reason: str):
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


def _looks_secret(text: str, words: bool = True) -> bool:
    if _SECRET_PREFIX.search(text) or (words and _SECRET_WORDS.search(text)):
        return True
    return bool(_LONG_OPAQUE.fullmatch(text) and any(c.isdigit() for c in text) and any(c.isalpha() for c in text))


def _text(pattern, allow=(), words=True, secret_check=True):
    def check(value, path):
        if not isinstance(value, str):
            raise JournalInvalid(path, "must be a string")
        if value in allow:
            return
        if not pattern.fullmatch(value):
            raise JournalInvalid(path, "does not match the registered format")
        if secret_check and _looks_secret(value, words):
            raise JournalInvalid(path, "has the shape of a credential")
    return check


def _enum(*values):
    def check(value, path):
        if not isinstance(value, str) or value not in values:
            raise JournalInvalid(path, "must be one of " + ", ".join(values))
    return check


def _hash(allow=(), nullable=False):
    def check(value, path):
        if value is None and nullable:
            return
        if not (isinstance(value, str) and (value in allow or _HEX64.fullmatch(value))):
            raise JournalInvalid(path, "must be a lowercase sha256 hex" + (" or one of " + ", ".join(allow) if allow else ""))
    return check


def _int(lo=0, hi=2**53):
    def check(value, path):
        if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
            raise JournalInvalid(path, f"must be an integer in [{lo}, {hi}]")
    return check


def _bool(value, path):
    if not isinstance(value, bool):
        raise JournalInvalid(path, "must be a boolean")


def _number(lo, hi):
    def check(value, path):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
            raise JournalInvalid(path, f"must be a finite number in [{lo}, {hi}]")
    return check


def _nullable(inner):
    def check(value, path):
        if value is not None:
            inner(value, path)
    return check


def _uuid(value, path):
    try:
        ok = isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        ok = False
    if not ok:
        raise JournalInvalid(path, "must be a lowercase hyphenated UUID")


def _uuid_or_none(value, path):
    if value != "none":
        _uuid(value, path)


def _timestamp(value, path):
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        raise JournalInvalid(path, "must be an ISO 8601 UTC timestamp ending in Z")


def _object(fields):
    """An object with registered keys only: `{key: (check, required)}`."""
    def check(value, path):
        if not isinstance(value, dict):
            raise JournalInvalid(path, "must be an object")
        for key, item in value.items():
            where = f"{path}.{key}" if path else str(key)
            if key not in fields:
                raise JournalInvalid(where, "is not a registered field")
            fields[key][0](item, where)
        for key, (_, required) in fields.items():
            if required and key not in value:
                raise JournalInvalid(f"{path}.{key}" if path else key, "is required")
    return check


def _list(item, limit):
    def check(value, path):
        if not isinstance(value, list) or len(value) > limit:
            raise JournalInvalid(path, f"must be a list of at most {limit} items")
        for index, entry in enumerate(value):
            item(entry, f"{path}[{index}]")
    return check


def _map(key_pattern, item, limit):
    def check(value, path):
        if not isinstance(value, dict) or len(value) > limit:
            raise JournalInvalid(path, f"must be an object of at most {limit} entries")
        for key, entry in value.items():
            if not isinstance(key, str) or not key_pattern.fullmatch(key):
                raise JournalInvalid(f"{path}.{key}", "is not an allowed key name")
            item(entry, f"{path}.{key}")
    return check


UNKNOWN, NONE = "unknown", "none"
_TOKEN_CHECK = _text(_TOKEN, words=False)
_NAME_CHECK = _text(_NAME)
_NAME_OR_UNKNOWN = _text(_NAME, allow=(UNKNOWN,))
_MODEL_OR_UNKNOWN = _text(_MODEL, allow=(UNKNOWN,))
_HASH_NONE_UNKNOWN = _hash(allow=(NONE, UNKNOWN))


def model_or_unknown(value: Any) -> str:
    """`value` when the journal accepts it as a model id, checkpoint or revision (format, and not shaped like a credential), else `unknown`."""
    try:
        _MODEL_OR_UNKNOWN(value, "")
    except JournalInvalid:
        return UNKNOWN
    return value


_COST = _object({"status": (_enum(*COST_STATUSES), True), "micro_usd": (_nullable(_int()), True)})
_ATTEMPT = _object({
    "call_id": (_uuid, True), "stage": (_int(0, 64), True), "tier": (_NAME_CHECK, True), "role": (_enum(*ROLES), True),
    "provider": (_NAME_OR_UNKNOWN, True), "model": (_MODEL_OR_UNKNOWN, True), "engine": (_NAME_OR_UNKNOWN, False),
    "checkpoint": (_MODEL_OR_UNKNOWN, False), "revision": (_MODEL_OR_UNKNOWN, False), "status": (_enum(*ATTEMPT_STATUSES), True),
    "reason_code": (_nullable(_TOKEN_CHECK), False), "control_result": (_nullable(_enum(*CONTROL_RESULTS)), False),
    "fallback_reason": (_nullable(_TOKEN_CHECK), False), "latency_ms": (_nullable(_int(0, 10**9)), False),
    "questions": (_int(0, 64), False), "cost": (_COST, True)})
_ANSWER = _object({
    "primary_answered": (_bool, True), "answering_call_id": (_nullable(_uuid), True),
    "answering_tier": (_text(_NAME, allow=(NONE,)), True), "outcome": (_enum("accepted", "unknown"), True),
    "typed_reason": (_nullable(_TOKEN_CHECK), False), "questions_asked": (_int(0, 64), True), "questions_answered": (_int(0, 64), True)})
_TOTALS = _object({"latency_ms": (_nullable(_int(0, 10**9)), True), "cost": (_object({
    "main": (_COST, True), "controls": (_COST, True), "failed_tiers": (_COST, True), "audits": (_COST, True)}), True)})
_SELECTION = _object({"reason_token": (_TOKEN_CHECK, True), "abstained": (_bool, True), "thresholds_applied": (_map(_TOKEN, _object({
    "value": (_number(-1e12, 1e12), True), "calibrated_for_revision": (_nullable(_text(_MODEL)), True)}), 16), False)})
_UNKNOWN_REASONS = _map(_TOKEN, _TOKEN_CHECK, 32)
_TARGET = _object({"entity": (_text(re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@#+-]{0,127}")), True),
                   "state_revision": (_nullable(_NAME_CHECK), False), "state_hash": (_hash(nullable=True), False),
                   "diff_hash": (_hash(nullable=True), False)})
_DECISION = {"decision_id": (_uuid, True), "use_case": (_NAME_CHECK, True), "domain": (_NAME_CHECK, True),
             "language": (_text(_LANG, allow=(UNKNOWN,)), True)}
_COMMON = {"schema_version": (_int(SCHEMA_VERSION, SCHEMA_VERSION), True), "record_id": (_uuid, True), "phase": (_enum("request", "proposed"), True),
           "request_id": (_uuid, True), "timestamp": (_timestamp, True), "component": (_NAME_CHECK, True)}
# The adapter writes the `request` and `proposed` phases only: its caller (the quasar semantic shadow) never acts on a verdict, so
# there is nothing to `applied`/`rejected`, and usefulness `outcome`s come from the adjudicating side, not from the writer.
_PHASES = {
    "request": {
        "domain": (_NAME_CHECK, False), "language": (_text(_LANG, allow=(UNKNOWN,)), False),
        "source": (_object({"release": (_NAME_OR_UNKNOWN, True), "commit": (_text(_HEX40, allow=(UNKNOWN,), secret_check=False), True),
                            "diff_hash": (_hash(allow=(UNKNOWN,), nullable=True), False), "code_hash": (_HASH_NONE_UNKNOWN, True)}), True),
        "identity": (_object({"prompt_hash": (_HASH_NONE_UNKNOWN, True), "runner": (_object({
            "kind": (_enum(*RUNNER_KINDS), True), "job_ref": (_nullable(_text(_REF)), False)}), True)}), True),
        "input_hash": (_hash(allow=(UNKNOWN,)), True), "eligibility": (_text(_ELIGIBILITY), True),
        "model": (_object({"provider": (_NAME_OR_UNKNOWN, True), "name": (_MODEL_OR_UNKNOWN, True), "engine": (_NAME_OR_UNKNOWN, True),
                           "checkpoint": (_MODEL_OR_UNKNOWN, True), "immutable_revision": (_MODEL_OR_UNKNOWN, True)}), True),
        "attempts": (_list(_ATTEMPT, 64), True), "answer": (_ANSWER, True), "totals": (_TOTALS, True), "unknown_reasons": (_UNKNOWN_REASONS, False)},
    "proposed": {
        **_DECISION, "call_id": (_uuid_or_none, True), "question_ids": (_list(_TOKEN_CHECK, 64), False),
        "policy_kind": (_enum(*POLICY_KINDS), True), "policy_hash": (_HASH_NONE_UNKNOWN, True), "controlset_hash": (_HASH_NONE_UNKNOWN, True),
        "domain_spec_hash": (_HASH_NONE_UNKNOWN, True), "dataset_hash": (_HASH_NONE_UNKNOWN, True), "split_hash": (_HASH_NONE_UNKNOWN, True),
        "baseline_action": (_NAME_OR_UNKNOWN, True), "baseline_policy_hash": (_HASH_NONE_UNKNOWN, True),
        "candidate_action": (_NAME_OR_UNKNOWN, True), "selection": (_SELECTION, True),
        "qualification_status": (_enum(*QUALIFICATION_STATUSES), True), "qualification_ref": (_HASH_NONE_UNKNOWN, True),
        "rule_ref": (_HASH_NONE_UNKNOWN, True), "target": (_TARGET, False), "unknown_reasons": (_UNKNOWN_REASONS, False)},
}


def record_id_for(request_id: str, phase: str, decision_id: str | None = None, seq: str = "0") -> str:
    return str(uuid.uuid5(RECORD_NAMESPACE, "|".join((request_id, phase, decision_id or "-", seq))))


def canonical_bytes(record: Mapping[str, Any]) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def iso_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def validate(record: Any) -> None:
    """Raises `JournalInvalid` unless `record` is a request or proposed record of the contract with its derived identity."""
    if not isinstance(record, dict):
        raise JournalInvalid("", "a record is a JSON object")
    phase = record.get("phase")
    if phase not in _PHASES:
        raise JournalInvalid("phase", "must be one of " + ", ".join(_PHASES))
    _object({**_COMMON, **_PHASES[phase]})(record, "")
    if record["record_id"] != record_id_for(record["request_id"], phase, record.get("decision_id")):
        raise JournalInvalid("record_id", "is not the identity derived from request_id, phase and decision_id")
    if phase == "request":
        _request_rules(record)
    else:
        _proposed_rules(record)
    if len(canonical_bytes(record)) + 1 > MAX_RECORD_BYTES:
        raise JournalInvalid("", f"larger than {MAX_RECORD_BYTES} bytes")


def _require(condition: bool, path: str, reason: str) -> None:
    if not condition:
        raise JournalInvalid(path, reason)


def _cost_coherent(cost: Mapping[str, Any], path: str) -> None:
    _require((cost["status"] == "unknown") == (cost["micro_usd"] is None), path, "micro_usd is null exactly when the status is unknown")


def _request_rules(record: Mapping[str, Any]) -> None:
    by_call: dict[str, Any] = {}
    for index, attempt in enumerate(record["attempts"]):
        where = f"attempts[{index}]"
        _require(attempt["call_id"] not in by_call, where + ".call_id", "call ids must be unique inside a request")
        by_call[attempt["call_id"]] = attempt
        _cost_coherent(attempt["cost"], where + ".cost")
        _require(attempt["role"].startswith("control_") == (attempt.get("control_result") is not None), where + ".control_result",
                 "set exactly on control attempts")
        _require((attempt["tier"] == "jev") == (attempt["provider"] == "jev"), where + ".provider",
                 "the jev seat is an actual jev provider, never another model")
    for name, cost in record["totals"]["cost"].items():
        _cost_coherent(cost, "totals.cost." + name)
    answer = record["answer"]
    _require(answer["questions_answered"] <= answer["questions_asked"], "answer.questions_answered", "cannot exceed questions_asked")
    call = answer["answering_call_id"]
    if not answer["primary_answered"]:
        _require(call is None, "answer.answering_call_id", "must be null when no main call answered")
        _require(answer["outcome"] == "unknown" and answer["questions_answered"] == 0, "answer", "no valid main reply means outcome unknown")
        return
    _require(call in by_call and by_call[call]["role"] == "main" and by_call[call]["status"] == "answered", "answer.answering_call_id",
             "must be a main attempt with status answered")
    _require(record["model"]["provider"] == by_call[call]["provider"], "model.provider", "must equal the answering attempt's provider")


def _proposed_rules(record: Mapping[str, Any]) -> None:
    status, ref, kind = record["qualification_status"], record["qualification_ref"], record["policy_kind"]
    if status == "qualified":
        _require(kind == "model" and _HEX64.fullmatch(ref) is not None, "qualification_ref", "qualified needs a model decision and a 64-hex ref")
    elif status in ("provisional", "none"):
        _require(kind == "model" and ref == NONE, "qualification_ref", "provisional/none are model decisions with qualification_ref none")
    else:
        _require(kind == "deterministic_rule" and _HEX64.fullmatch(record["rule_ref"]) is not None and ref == NONE, "rule_ref",
                 "not_applicable needs a deterministic rule with a 64-hex rule_ref")
    _require(kind == "deterministic_rule" or record["rule_ref"] == NONE, "rule_ref", "a model decision has no rule_ref")
    if record["selection"]["abstained"]:
        _require(record["candidate_action"] == UNKNOWN, "candidate_action", "an abstention proposes no action")
    for name, entry in record["selection"].get("thresholds_applied", {}).items():
        _require(entry["calibrated_for_revision"] is not None or kind != "model", "selection.thresholds_applied." + name,
                 "a model threshold needs the revision it was calibrated for")
    target = record.get("target")
    if target is not None:
        _require(target.get("state_hash") is not None or target.get("diff_hash") is not None, "target", "needs a state_hash or a diff_hash")


# ── state directories ───────────────────────────────────────────────────────

def state_root(env: Mapping[str, str]) -> Path:
    """`$XDG_STATE_HOME` (when absolute) or `~/.local/state`, then `agent-tools/classifier-quality`: the shared guardian's root."""
    base = env.get("XDG_STATE_HOME")
    root = Path(base) if base and os.path.isabs(base) else Path(env.get("HOME") or Path.home()) / ".local" / "state"
    return root / "agent-tools" / "classifier-quality"


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise OSError(f"{path.name} is not a plain directory")
    if info.st_uid != os.getuid():
        raise OSError(f"{path.name} is owned by another user")
    if info.st_mode & 0o077:
        os.chmod(path, 0o700)
    return path


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _create_exclusive(path: Path, payload: bytes) -> bool:
    """Create `path` atomically and never over an existing file; False when it exists."""
    temp = path.with_name(f".tmp-{os.getpid()}-{uuid.uuid4().hex}")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp, path)
        except FileExistsError:
            return False
    finally:
        try:
            os.unlink(temp)
        except OSError:
            pass
    _fsync_dir(path.parent)
    return True


def input_hash(root: Path, text: str) -> str:
    """HMAC-SHA256 of the normalized input under an install-private key (join and dedupe only); `unknown` when no key can be kept.

    The key is 32 random bytes in `<root>/journal-input.key` (0600), created once. It never leaves the host, so the hash
    cannot be dictionary-attacked from the journal alone, and a text can never be read back from it.
    """
    path = root / "journal-input.key"
    try:
        try:
            key = path.read_bytes()
        except FileNotFoundError:
            _private_dir(root)
            _create_exclusive(path, secrets.token_bytes(32))
            key = path.read_bytes()
        if len(key) != 32 or os.lstat(path).st_mode & 0o077:
            return UNKNOWN
    except OSError:
        return UNKNOWN
    return hmac.new(key, " ".join(text.split()).casefold().encode("utf-8"), hashlib.sha256).hexdigest()


_RECORD_FILE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.json")


class Journal:
    """Append-only, idempotent, never-raising writer of journal records; counts what it did and keeps the store inside its bounds."""

    def __init__(self, root: Path, clock: Callable[[], float] = time.time):
        self.root = root
        self.directory = root / "journal"
        self.counts = {WRITTEN: 0, NOOP: 0, CONFLICT: 0, FAILED: 0}
        self._clock = clock
        self._lock = threading.RLock()
        self._last_prune: float | None = None
        self.pruned = 0
        self.last_failure: str | None = None

    def write(self, record: Mapping[str, Any]) -> str:
        """`written` | `noop` | `conflict` | `failed`. An invalid record or an unwritable disk is `failed`, never an exception."""
        with self._lock:  # classifications run on worker threads: the counters, the prune schedule and the retention marker are one critical section
            status = self._write(record)
            self.counts[status] += 1
            if status == WRITTEN:
                self._prune_when_due()
            return status

    def _prune_when_due(self) -> None:
        now = self._clock()
        if self._last_prune is None or now - self._last_prune >= PRUNE_EVERY_S:
            self._last_prune = now
            self.prune(now)

    def prune(self, now: float | None = None, max_age_days: float = MAX_AGE_DAYS, max_bytes: int = MAX_BYTES) -> int:
        """Remove the oldest records beyond the age and size bounds (default 90 days, 64 MiB) and remember how far the history was
        cut, so a reader reports "the history before X is gone" and not "no use". Only record files this module names are touched.
        Returns how many were removed; a failure removes nothing more and never raises."""
        now = self._clock() if now is None else now
        found: list[tuple[float, int, Path]] = []
        try:
            for name in os.listdir(self.directory):
                if _RECORD_FILE.fullmatch(name):
                    info = os.lstat(self.directory / name)
                    if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid():
                        found.append((info.st_mtime, info.st_size, self.directory / name))
        except OSError:
            return 0
        total = sum(size for _, size, _ in found)
        removed: list[float] = []
        for mtime, size, path in sorted(found):
            if now - mtime <= max_age_days * 86400 and total <= max_bytes:
                break
            try:
                path.unlink()
            except OSError:
                continue
            total -= size
            removed.append(mtime)
        if removed:
            self.pruned += len(removed)
            self._remember_pruned(max(removed), len(removed), now)
        return len(removed)

    def _remember_pruned(self, through: float, count: int, now: float) -> None:
        marker = self.directory / RETENTION_FILE
        try:
            previous = json.loads(marker.read_text("ascii"))
            earlier_total, earlier_through = int(previous["pruned_total"]), previous["pruned_through"]
        except (OSError, ValueError, KeyError, TypeError):
            earlier_total, earlier_through = 0, None
        latest = through
        if isinstance(earlier_through, str) and _TIMESTAMP.fullmatch(earlier_through):
            fmt = "%Y-%m-%dT%H:%M:%S.%fZ" if "." in earlier_through else "%Y-%m-%dT%H:%M:%SZ"
            latest = max(latest, datetime.strptime(earlier_through, fmt).replace(tzinfo=timezone.utc).timestamp())
        state = {"pruned_through": iso_utc(latest), "pruned_total": earlier_total + count, "updated": iso_utc(now)}
        try:
            temp = self.directory / f".tmp-retention-{os.getpid()}-{uuid.uuid4().hex}"
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write((json.dumps(state, sort_keys=True) + "\n").encode("ascii"))
            os.replace(temp, marker)
        except OSError:
            pass

    def _write(self, record: Mapping[str, Any]) -> str:
        try:
            validate(record)
            payload = canonical_bytes(record) + b"\n"
            _private_dir(self.directory)
            path = self.directory / f"{record['record_id']}.json"
            if _create_exclusive(path, payload):
                return WRITTEN
            try:
                existing = json.loads(path.read_bytes())
            except (OSError, ValueError):
                existing = None
            if existing is not None and canonical_bytes(existing) == canonical_bytes(record):
                return NOOP
            _create_exclusive(_private_dir(self.directory / "conflicts") / f"{record['record_id']}-{hashlib.sha256(payload).hexdigest()[:16]}.json", payload)
            return CONFLICT
        except JournalInvalid as error:
            return self._failed(f"invalid:{error.path}")
        except (OSError, ValueError, TypeError) as error:
            return self._failed(type(error).__name__)

    def _failed(self, why: str) -> str:
        """A refused or unwritable record: remembered value-free (a field path or an exception class) for the health output and the log."""
        self.last_failure = why
        LOG.warning("classifier journal record not written: %s", why)
        return FAILED


# ── private incident events (schema v1, read by the guardian) ───────────────

def fingerprint_of(component, domain, provider, model, kind, reason_code, version_identity) -> str:
    blob = json.dumps([component, domain, provider, model, kind, reason_code, version_identity], ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("ascii")).hexdigest()


class IncidentSink:
    """Writes one small JSON file per incident into `<root>/events/`; repeats of one fingerprint inside an hour are folded.

    A failure is swallowed (and counted): an incident store that cannot be written must not break a classification.
    The dedupe window is per process, which is the unit that floods: a restart may repeat an incident once.
    """

    def __init__(self, root: Path, clock: Callable[[], float] = time.time):
        self.directory = root / "events"
        self._clock = clock
        self._lock = threading.Lock()
        self._seen: dict[tuple[str, str], tuple[float, int]] = {}
        self.written = 0
        self.suppressed = 0
        self.failed = 0

    def emit(self, *, component: str, domain: str, provider: str, model: str, kind: str, severity: str, reason_code: str,
             version_identity: str, request_id: str, provenance: Mapping[str, Any], counts: Mapping[str, int]) -> bool:
        with self._lock:  # classifications run on worker threads: two requests at the start of an outage write one incident, not two
            return self._emit(component, domain, provider, model, kind, severity, reason_code, version_identity, request_id, provenance, counts)

    def _emit(self, component, domain, provider, model, kind, severity, reason_code, version_identity, request_id, provenance, counts) -> bool:
        fingerprint = fingerprint_of(component, domain, provider, model, kind, reason_code, version_identity)
        now = self._clock()
        key = (fingerprint, severity)
        last, folded = self._seen.get(key, (None, 0))
        if last is not None and now - last < DEDUPE_WINDOW_S:
            self._seen[key] = (last, folded + 1)
            self.suppressed += 1
            return False
        event = {"schema_version": 1, "event_id": str(uuid.uuid4()), "request_id": request_id, "timestamp": iso_utc(now),
                 "component": component, "domain": domain, "provider": provider, "model": model, "kind": kind, "severity": severity,
                 "reason_code": reason_code, "fingerprint": fingerprint, "provenance": dict(provenance),
                 "aggregate_counts": {**counts, **({"suppressed_repeats": folded} if folded else {})}}
        try:
            payload = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
            if len(payload) > 8 * 1024:
                raise ValueError("event too large")
            _private_dir(self.directory)
            stamp = re.sub(r"[^0-9TZ]", "", event["timestamp"])
            _create_exclusive(self.directory / f"{stamp}-{'c' if severity == 'critical' else 'w'}-{event['event_id']}.json", payload)
            self._seen[key] = (now, 0)
            self.written += 1
            self._prune()
            return True
        except (OSError, ValueError) as error:
            self.failed += 1
            LOG.warning("classifier incident not written: %s", type(error).__name__)
            return False

    def _prune(self) -> None:
        """Keep at most MAX_EVENT_FILES: the oldest warnings go before any critical incident. Only files named like ours."""
        try:
            names = [n for n in os.listdir(self.directory) if re.fullmatch(r"[0-9TZ]+-[wc]-[0-9a-f-]{36}\.json", n)]
            excess = len(names) - MAX_EVENT_FILES
            if excess <= 0:
                return
            for name in sorted(names, key=lambda n: (n.split("-")[1] == "c", n))[:excess]:
                os.unlink(self.directory / name)
        except OSError:
            pass
