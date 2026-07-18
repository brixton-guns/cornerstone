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


EVENT_TYPES = ("file.created", "file.deleted", "file.metadata_modified", "file.modified", "file.renamed")
OUTCOMES = ("success", "failed", "interrupted", "incomplete")

_REQUIRED_FIELDS = {
    "session.started": {"actor", "command", "id", "prev", "spec", "ts", "type"},
    "file.created": {"entry_type", "hash", "path", "prev", "size", "ts", "type"},
    "file.deleted": {"entry_type", "hash_before", "path", "prev", "ts", "type"},
    "file.modified": {
        "entry_type_after", "entry_type_before", "hash_after", "hash_before",
        "path", "prev", "size_after", "size_before", "ts", "type",
    },
    "file.metadata_modified": {"mode_after", "mode_before", "path", "prev", "ts", "type"},
    "file.renamed": {"hash", "path", "path_before", "prev", "size", "ts", "type"},
    "session.finished": {"duration_s", "exit_code", "outcome", "prev", "ts", "type"},
}
_OPTIONAL_FIELDS = {
    "file.created": {"mode", "target"},
    "file.modified": {"mode_after", "mode_before"},
}


def verify_ledger(path: Path) -> int:
    """Recompute the whole hash chain and validate the ledger structure (§10).

    Chain verification alone cannot detect truncation from the end, so the
    structure is checked too: session.started first, session.finished last,
    known event types with their required fields, one event per path.
    Returns the record count or raises LedgerError.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise LedgerError(f"cannot read ledger: {exc}") from exc
    if not raw:
        raise LedgerError("ledger is empty")
    if not raw.endswith(b"\n"):
        raise LedgerError("last record is not newline-terminated")

    prev = GENESIS
    records: list[dict] = []
    for number, line in enumerate(raw[:-1].split(b"\n"), start=1):
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise LedgerError(f"record {number} is not valid JSON") from exc
        if not isinstance(record, dict) or record.get("prev") != prev:
            raise LedgerError(f"record {number} does not match the hash chain")
        prev = hashlib.sha256(line).hexdigest()
        records.append(record)

    _verify_structure(records)
    return len(records)


def _verify_structure(records: list[dict]) -> None:
    if len(records) < 2:
        raise LedgerError("ledger must contain at least session.started and session.finished")
    if records[0].get("type") != "session.started":
        raise LedgerError("first record is not session.started")
    if records[-1].get("type") != "session.finished":
        raise LedgerError("last record is not session.finished")

    for number, record in enumerate(records, start=1):
        record_type = record.get("type")
        if 1 < number < len(records) and record_type not in EVENT_TYPES:
            raise LedgerError(f"record {number} has unexpected type {record_type!r}")
        required = _REQUIRED_FIELDS[record_type]
        allowed = required | _OPTIONAL_FIELDS.get(record_type, set())
        missing = required - record.keys()
        if missing:
            raise LedgerError(f"record {number} is missing fields: {', '.join(sorted(missing))}")
        unknown = record.keys() - allowed
        if unknown:
            raise LedgerError(f"record {number} has unknown fields: {', '.join(sorted(unknown))}")

    started, finished = records[0], records[-1]
    if started.get("spec") != "0.1":
        raise LedgerError(f"unsupported spec version: {started.get('spec')!r}")
    if not isinstance(started.get("command"), list):
        raise LedgerError("session.started `command` is not an argv list")
    if finished.get("outcome") not in OUTCOMES:
        raise LedgerError(f"unknown outcome: {finished.get('outcome')!r}")
    if finished["outcome"] == "incomplete" and len(records) > 2:
        raise LedgerError("an incomplete session must not contain events")

    subjects: list[str] = []
    for record in records[1:-1]:
        subjects.append(record["path"])
        if "path_before" in record:
            subjects.append(record["path_before"])
    if len(subjects) != len(set(subjects)):
        raise LedgerError("a path is the subject of more than one event")
