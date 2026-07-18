"""The ledger: canonical JSON Lines with a SHA-256 hash chain (spec §10)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

GENESIS = "0" * 64


class LedgerError(Exception):
    """The ledger is missing, malformed, or fails hash-chain verification."""


def canonical_line(record: dict) -> str:
    """Canonical JSON: keys sorted alphabetically, UTF-8, no superfluous whitespace."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def chain_records(records: list[dict]) -> list[str]:
    """Add the `prev` field to each record and serialize. First record chains to 64 zeros."""
    lines: list[str] = []
    prev = GENESIS
    for record in records:
        full = dict(record)
        full["prev"] = prev
        line = canonical_line(full)
        lines.append(line)
        prev = hashlib.sha256(line.encode("utf-8")).hexdigest()
    return lines


def write_ledger(path: Path, records: list[dict]) -> None:
    path.write_bytes("".join(line + "\n" for line in chain_records(records)).encode("utf-8"))


def read_ledger(path: Path) -> list[dict]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise LedgerError(f"cannot read ledger: {exc}") from exc
    return [json.loads(line) for line in raw.decode("utf-8").splitlines() if line]


def verify_ledger(path: Path) -> int:
    """Recompute the whole hash chain; return the record count or raise LedgerError."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise LedgerError(f"cannot read ledger: {exc}") from exc
    if not raw:
        raise LedgerError("ledger is empty")
    if not raw.endswith(b"\n"):
        raise LedgerError("last record is not newline-terminated")

    prev = GENESIS
    lines = raw[:-1].split(b"\n")
    for number, line in enumerate(lines, start=1):
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise LedgerError(f"record {number} is not valid JSON") from exc
        if not isinstance(record, dict) or record.get("prev") != prev:
            raise LedgerError(f"record {number} does not match the hash chain")
        prev = hashlib.sha256(line).hexdigest()
    return len(lines)
