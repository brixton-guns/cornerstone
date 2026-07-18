"""Command-line interface (spec §16): stone run / show / verify / attest / list."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .attest import build_statement
from .config import ConfigError
from .ledger import LedgerError, read_ledger, verify_ledger
from .render import render_summary
from .session import (
    EXIT_INTERNAL,
    EXIT_LOCK,
    EXIT_SNAPSHOT,
    EXIT_WORKSPACE,
    CommandError,
    LockError,
    WorkspaceError,
    run_session,
)
from .snapshot import SnapshotError

USAGE = """\
usage: stone <command> [...]

  stone run [--actor NAME] [--capture-output] -- COMMAND [ARG...]
  stone show   <session_id | latest>
  stone verify <session_id | latest>
  stone attest <session_id | latest>
  stone list
"""


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        return _dispatch(args)
    except (WorkspaceError, ConfigError) as exc:
        print(f"stone: {exc}", file=sys.stderr)
        return EXIT_WORKSPACE
    except LockError as exc:
        print(f"stone: {exc}", file=sys.stderr)
        return EXIT_LOCK
    except SnapshotError as exc:
        print(f"stone: initial snapshot failed: {exc}", file=sys.stderr)
        return EXIT_SNAPSHOT
    except LedgerError as exc:
        print(f"stone: ledger verification failed after writing: {exc}", file=sys.stderr)
        return EXIT_INTERNAL
    except CommandError as exc:
        print(f"stone: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # §16: dedicated code for internal errors
        print(f"stone: internal error: {exc}", file=sys.stderr)
        return EXIT_INTERNAL


def _dispatch(args: list[str]) -> int:
    if not args or args[0] in ("-h", "--help"):
        print(USAGE, end="")
        return 0
    command, rest = args[0], args[1:]
    if command == "run":
        return _cmd_run(rest)
    if command == "show" and len(rest) == 1:
        return _cmd_show(rest[0])
    if command == "verify" and len(rest) == 1:
        return _cmd_verify(rest[0])
    if command == "attest" and len(rest) == 1:
        return _cmd_attest(rest[0])
    if command == "list" and not rest:
        return _cmd_list()
    print(USAGE, end="", file=sys.stderr)
    return 2


def _cmd_run(args: list[str]) -> int:
    if "--" not in args:
        print("stone run: missing `--` before the command to observe", file=sys.stderr)
        print(USAGE, end="", file=sys.stderr)
        return 2
    separator = args.index("--")
    options, command = args[:separator], args[separator + 1 :]
    if not command:
        print("stone run: no command given after `--`", file=sys.stderr)
        return 2

    actor = "undeclared"
    capture_output = False
    while options:
        option = options.pop(0)
        if option == "--actor":
            if not options:
                print("stone run: --actor requires a value", file=sys.stderr)
                return 2
            actor = options.pop(0)
        elif option.startswith("--actor="):
            actor = option[len("--actor=") :]
        elif option == "--capture-output":
            capture_output = True
        else:
            print(f"stone run: unknown option: {option}", file=sys.stderr)
            return 2

    return run_session(Path.cwd(), actor, command, capture_output)


def _stone_dir() -> Path:
    return Path.cwd() / ".stone"


def _read_index(stone_dir: Path) -> list[dict]:
    index_path = stone_dir / "index.jsonl"
    if not index_path.is_file():
        return []
    rows = []
    for line in index_path.read_text(encoding="utf-8").splitlines():
        if line:
            rows.append(json.loads(line))
    return rows


def _resolve(stone_dir: Path, reference: str) -> str | None:
    if reference != "latest":
        return reference
    rows = _read_index(stone_dir)
    if not rows:
        print("stone: no sessions recorded in this workspace", file=sys.stderr)
        return None
    return max(rows, key=lambda row: (row["started_at"], row["id"]))["id"]


def _verified_records(stone_dir: Path, session_id: str) -> list[dict] | None:
    """Load a session ledger only after full verification (chain + structure + id)."""
    path = stone_dir / "sessions" / session_id / "events.jsonl"
    if not path.is_file():
        print(f"stone: no ledger for session {session_id}", file=sys.stderr)
        return None
    try:
        verify_ledger(path)
    except LedgerError as exc:
        print(f"Ledger NOT intact: {exc}")
        return None
    records = read_ledger(path)
    if records[0].get("id") != session_id:
        print(f"Ledger NOT intact: it belongs to session {records[0].get('id')!r}, not {session_id!r}")
        return None
    return records


def _cmd_show(reference: str) -> int:
    stone_dir = _stone_dir()
    session_id = _resolve(stone_dir, reference)
    if session_id is None:
        return 1
    records = _verified_records(stone_dir, session_id)
    if records is None:
        return 1
    print(render_summary(records))
    return 0


def _cmd_verify(reference: str) -> int:
    stone_dir = _stone_dir()
    session_id = _resolve(stone_dir, reference)
    if session_id is None:
        return 1
    records = _verified_records(stone_dir, session_id)
    if records is None:
        return 1
    print(f"Ledger intact: {len(records)} records, hash chain and structure verified.")
    return 0


def _cmd_attest(reference: str) -> int:
    """Print the witness/0.1 statement for a session. stdout carries the statement only."""
    stone_dir = _stone_dir()
    session_id = _resolve(stone_dir, reference)
    if session_id is None:
        return 1
    path = stone_dir / "sessions" / session_id / "events.jsonl"
    if not path.is_file():
        print(f"stone: no ledger for session {session_id}", file=sys.stderr)
        return 1
    try:
        statement = build_statement(path, session_id)
    except LedgerError as exc:
        print(f"stone: refusing to attest: {exc}", file=sys.stderr)
        return 1
    print(statement)
    return 0


def _cmd_list() -> int:
    stone_dir = _stone_dir()
    rows = _read_index(stone_dir)
    if not rows:
        print("No sessions recorded.")
        return 0
    rows.sort(key=lambda row: (row["started_at"], row["id"]), reverse=True)
    print(f"{'ID':<26}  {'STARTED':<20}  {'ACTOR':<16}  OUTCOME")
    for row in rows:
        actor = "?"
        path = stone_dir / "sessions" / row["id"] / "events.jsonl"
        if path.is_file():
            try:
                actor = read_ledger(path)[0].get("actor", "?")
            except (LedgerError, ValueError, IndexError):
                actor = "?"
        print(f"{row['id']:<26}  {row['started_at']:<20}  {actor:<16}  {row['outcome']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
