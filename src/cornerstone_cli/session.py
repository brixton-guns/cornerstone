"""Session flow (spec §5): snapshot, execute, snapshot, diff, ledger, verify, summary."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import SPEC_VERSION
from ._ulid import ulid
from .config import Config, load_config
from .diff import diff_events
from .ledger import canonical_line, verify_ledger, write_ledger
from .render import render_summary
from .snapshot import SnapshotError, take_snapshot

EXIT_LOCK = 96
EXIT_WORKSPACE = 97
EXIT_SNAPSHOT = 98
EXIT_INTERNAL = 99

_PUMP_CHUNK = 65536


class WorkspaceError(Exception):
    """The workspace failed validation (§5 step 1)."""


class LockError(Exception):
    """Another session holds the workspace lock (§16)."""


class CommandError(Exception):
    """The command could not be started at all."""

    def __init__(self, message: str, exit_code: int):
        super().__init__(message)
        self.exit_code = exit_code


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _TailBuffer:
    """Keeps at most `max_bytes`, discarding the oldest data (§12: the tail survives)."""

    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self.data = bytearray()
        self.truncated = False

    def add(self, chunk: bytes) -> None:
        self.data += chunk
        if len(self.data) > self.max_bytes:
            del self.data[: len(self.data) - self.max_bytes]
            self.truncated = True


def _pump(stream, sink, buffer: _TailBuffer) -> None:
    while chunk := stream.read(_PUMP_CHUNK):
        sink.write(chunk)
        sink.flush()
        buffer.add(chunk)
    stream.close()


def _wait_for_descendants(pgid: int) -> None:
    """Block until the observed process group is empty.

    Descendants that outlive the immediate child are part of the execution:
    the final snapshot must not run while they can still write. A group
    member that never exits keeps the session open until interrupted.
    """
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            pass  # a group member exists but is not ours to signal: still running
        time.sleep(0.05)


def _write_log(name: str, buffer: _TailBuffer, dir_fd: int) -> None:
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _O_CLOEXEC, 0o600, dir_fd=dir_fd)
    with os.fdopen(fd, "wb") as fh:
        fh.write(buffer.data)


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


def run_session(root: Path, actor: str, command: list[str], capture_output: bool) -> int:
    """Run one session end to end; return the process exit code for `stone run`.

    Validation opens and HOLDS directory descriptors to the real .stone/ and
    .stone/sessions/: every later write (lock, ledger, captured output, index)
    happens relative to those descriptors with O_NOFOLLOW, so swapping either
    directory for a symlink — before or during the session — cannot redirect
    writes outside the workspace. The same openat principle the snapshot uses.
    """
    # §5 step 1: validation.
    if not root.is_dir():
        raise WorkspaceError(f"workspace is not a directory: {root}")
    stone_dir = root / ".stone"
    if stone_dir.is_symlink():
        raise WorkspaceError(".stone/ is a symbolic link; refusing to write the ledger through it")
    try:
        stone_dir.mkdir(exist_ok=True)
    except OSError as exc:
        raise WorkspaceError(f"cannot create .stone/: {exc}") from exc
    try:
        stone_fd = os.open(str(stone_dir), _DIR_FLAGS)
    except OSError as exc:
        raise WorkspaceError(f".stone/ cannot be opened as a real directory: {exc}") from exc

    try:
        config = load_config(stone_fd)
        try:
            os.mkdir("sessions", dir_fd=stone_fd)
        except FileExistsError:
            pass
        except OSError as exc:
            raise WorkspaceError(f"cannot create .stone/sessions/: {exc}") from exc
        try:
            sessions_fd = os.open("sessions", _DIR_FLAGS, dir_fd=stone_fd)
        except OSError as exc:
            raise WorkspaceError(
                f".stone/sessions/ is a symbolic link or not a real directory; refusing to write through it ({exc})"
            ) from exc
        try:
            return _locked_session(root, stone_fd, sessions_fd, config, actor, command, capture_output)
        finally:
            os.close(sessions_fd)
    finally:
        os.close(stone_fd)


def _locked_session(
    root: Path,
    stone_fd: int,
    sessions_fd: int,
    config: Config,
    actor: str,
    command: list[str],
    capture_output: bool,
) -> int:
    session_id = ulid()  # §5 step 2
    try:
        lock_fd = os.open("lock", os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_CLOEXEC, 0o600, dir_fd=stone_fd)
    except FileExistsError:
        raise LockError(
            f"another session holds the lock: {root / '.stone' / 'lock'}\n"
            "If no session is running, the lock is orphaned: remove the file manually."
        ) from None
    except OSError as exc:
        raise WorkspaceError(f"cannot create lock: {exc}") from exc
    with os.fdopen(lock_fd, "w", encoding="utf-8") as fh:
        fh.write(canonical_line({"id": session_id, "pid": os.getpid()}) + "\n")

    try:
        return _run_locked(root, stone_fd, sessions_fd, config, session_id, actor, command, capture_output)
    finally:
        try:
            os.unlink("lock", dir_fd=stone_fd)
        except OSError:
            pass


def _run_locked(
    root: Path,
    stone_fd: int,
    sessions_fd: int,
    config: Config,
    session_id: str,
    actor: str,
    command: list[str],
    capture_output: bool,
) -> int:
    snapshot_before = take_snapshot(root, config.ignore)  # §5 step 3
    started_ts = _utc_now()
    clock_start = time.monotonic()

    # §5 step 4: execution.
    popen_kwargs: dict = {}
    if capture_output:
        popen_kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
    try:
        # The command gets its own process group so that descendants stay
        # observable: the session ends when the whole group has exited, not
        # when the immediate child does. A process that detaches from the
        # group (setsid, double fork) still escapes the observation window.
        process = subprocess.Popen(command, cwd=root, start_new_session=True, **popen_kwargs)
    except FileNotFoundError:
        raise CommandError(f"command not found: {command[0]}", 127) from None
    except PermissionError:
        raise CommandError(f"command not executable: {command[0]}", 126) from None
    pgid = process.pid

    stdout_buffer = stderr_buffer = None
    pumps: list[threading.Thread] = []
    if capture_output:
        stdout_buffer = _TailBuffer(config.capture_max_bytes)
        stderr_buffer = _TailBuffer(config.capture_max_bytes)
        pumps = [
            threading.Thread(target=_pump, args=(process.stdout, sys.stdout.buffer, stdout_buffer), daemon=True),
            threading.Thread(target=_pump, args=(process.stderr, sys.stderr.buffer, stderr_buffer), daemon=True),
        ]
        for pump in pumps:
            pump.start()

    # §11: on interruption, forward the signal to the whole group and wait.
    # The child no longer shares the terminal's process group, so forwarding
    # is what delivers Ctrl-C to it and to its descendants.
    interrupted = False

    def _forward(signum, _frame):
        nonlocal interrupted
        interrupted = True
        try:
            os.killpg(pgid, signum)
        except OSError:
            pass

    previous_handlers = {sig: signal.signal(sig, _forward) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        returncode = process.wait()
        _wait_for_descendants(pgid)
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
    for pump in pumps:
        pump.join(timeout=5)

    # §5 step 5: exit code and duration. The exit code is the immediate
    # child's; the duration covers the whole process group.
    duration = time.monotonic() - clock_start
    exit_code = returncode if returncode >= 0 else 128 - returncode

    # §5 steps 6-9: final snapshot and net effects.
    events: list[dict] = []
    try:
        snapshot_after = take_snapshot(root, config.ignore)
    except SnapshotError:
        outcome = "incomplete"
    else:
        events = diff_events(snapshot_before, snapshot_after)
        if interrupted or returncode < 0:
            outcome = "interrupted"
        else:
            outcome = "success" if exit_code == 0 else "failed"

    detected_ts = _utc_now()
    records: list[dict] = [
        {
            "type": "session.started",
            "id": session_id,
            "actor": actor,
            "command": list(command),
            "spec": SPEC_VERSION,
            "ts": started_ts,
        }
    ]
    for event in events:
        records.append({**event, "ts": detected_ts})
    records.append(
        {
            "type": "session.finished",
            "outcome": outcome,
            "exit_code": exit_code,
            "duration_s": round(duration, 3),
            "ts": detected_ts,
        }
    )

    # §5 step 10: write the ledger (and captured output, kept out of the
    # ledger), everything relative to the held directory descriptors.
    try:
        os.mkdir(session_id, dir_fd=sessions_fd)
        session_fd = os.open(session_id, _DIR_FLAGS, dir_fd=sessions_fd)
    except OSError as exc:
        raise WorkspaceError(f"cannot create the session directory: {exc}") from exc
    try:
        write_ledger("events.jsonl", records, dir_fd=session_fd)
        if capture_output:
            _write_log("stdout.log", stdout_buffer, session_fd)
            _write_log("stderr.log", stderr_buffer, session_fd)
        verify_ledger("events.jsonl", dir_fd=session_fd)  # §5 step 11
    finally:
        os.close(session_fd)

    index_line = canonical_line({"id": session_id, "started_at": started_ts, "outcome": outcome}) + "\n"
    index_fd = os.open(
        "index.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | _O_CLOEXEC, 0o644, dir_fd=stone_fd
    )
    try:
        os.write(index_fd, index_line.encode("utf-8"))
    finally:
        os.close(index_fd)

    print(render_summary(records))  # §5 step 12

    return EXIT_SNAPSHOT if outcome == "incomplete" else exit_code
