"""Owner-specific, bounded maintenance using the watchdog's existing marker."""
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

# failover.py uses the same 900-second marker expiry. A crashed deployment
# cannot renew it, and a live deployment cannot extend its absolute deadline.
MAX_AGE_SECONDS = 900
MAX_LIFETIME_SECONDS = 7200
HEARTBEAT_SECONDS = 30


@contextmanager
def locked(state: Path):
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(state / '.maintenance.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def fresh(marker: Path) -> bool:
    return time.time() - marker.stat().st_mtime < MAX_AGE_SECONDS


def read_owner(marker: Path) -> dict:
    if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 4096:
        raise RuntimeError('maintenance marker is missing or unsafe')
    try:
        value = json.loads(marker.read_text())
    except (ValueError, OSError) as exc:
        raise RuntimeError('maintenance marker is not a valid lease') from exc
    if not isinstance(value, dict):
        raise RuntimeError('maintenance marker is not a valid lease')
    return value


def check_locked(state: Path, owner: str) -> None:
    marker = state / 'maintenance'
    value = read_owner(marker)
    if re.fullmatch(r'[0-9a-f]{32}', owner) is None or value.get('owner') != owner:
        raise RuntimeError('maintenance lease belongs to another owner')
    created, deadline = value.get('created_monotonic'), value.get('deadline_monotonic')
    if any(type(item) not in (int, float) or not math.isfinite(item) for item in (created, deadline)):
        raise RuntimeError('maintenance lease has invalid timing')
    if not 0 < deadline - created <= MAX_LIFETIME_SECONDS or not created <= time.monotonic() < deadline:
        raise RuntimeError('maintenance lease reached its absolute deadline')
    if not fresh(marker):
        raise RuntimeError('maintenance lease expired; renewal is refused')


def acquire(state: Path, parent: int) -> str:
    if type(parent) is not int or parent <= 1:
        raise ValueError('maintenance needs the live deployment parent PID')
    with locked(state):
        marker = state / 'maintenance'
        if marker.is_symlink():
            raise RuntimeError('maintenance marker must not be a link')
        if marker.exists() and (not marker.is_file() or fresh(marker)):
            raise RuntimeError('another maintenance owner is still active')
        owner = uuid.uuid4().hex
        started = time.monotonic()
        value = {'owner': owner, 'parent_pid': parent, 'created_monotonic': started,
                 'deadline_monotonic': started + MAX_LIFETIME_SECONDS}
        fd, name = tempfile.mkstemp(prefix='.maintenance-', dir=state)
        temporary = Path(name)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(value, stream)
                stream.write('\n')
            os.replace(temporary, marker)
        finally:
            temporary.unlink(missing_ok=True)
        return owner


def check(state: Path, owner: str) -> None:
    with locked(state):
        check_locked(state, owner)


def renew(state: Path, owner: str) -> None:
    with locked(state):
        check_locked(state, owner)
        os.utime(state / 'maintenance', None)


def release(state: Path, owner: str) -> bool:
    with locked(state):
        marker = state / 'maintenance'
        if not marker.exists():
            return False
        if read_owner(marker).get('owner') != owner:
            return False
        marker.unlink()
        return True


def keepalive(state: Path, owner: str, parent: int) -> None:
    # This process is a direct child of deploy.sh. Reparenting after shell exit
    # stops renewal even if SIGKILL prevented the normal EXIT cleanup.
    while os.getppid() == parent:
        renew(state, owner)
        time.sleep(HEARTBEAT_SECONDS)
