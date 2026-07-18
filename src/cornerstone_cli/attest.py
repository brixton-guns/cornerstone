"""Witness statements: bind a verified session ledger to its digest (witness/0.1 §4-5)."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from . import SPEC_VERSION
from .ledger import LedgerError, canonical_line, read_ledger_bytes, verify_ledger_bytes

STATEMENT_VERSION = "witness.statement/0.1"
LEDGER_MEDIA_TYPE = "application/vnd.cornerstone.ledger+jsonl"
BYTE_SCOPE = "entire-file-including-final-newline"

_SESSION_ID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")


def build_statement(ledger_path: Path, session_id: str) -> str:
    """Return the canonical witness/0.1 statement for a verified session ledger.

    The ledger is read exactly once: the digest covers the same bytes that
    passed chain and structure verification, so the statement cannot describe
    bytes other than the ones checked.
    """
    if _SESSION_ID.fullmatch(session_id) is None:
        raise LedgerError(f"session id is not a valid ULID: {session_id!r}")
    raw = read_ledger_bytes(ledger_path)
    records = verify_ledger_bytes(raw)
    if records[0].get("id") != session_id:
        raise LedgerError(f"ledger belongs to session {records[0].get('id')!r}, not {session_id!r}")
    statement = {
        "artifact": {
            "byte_scope": BYTE_SCOPE,
            "digest": {"algorithm": "sha256", "value": hashlib.sha256(raw).hexdigest()},
            "media_type": LEDGER_MEDIA_TYPE,
        },
        "statement_version": STATEMENT_VERSION,
        "subject": {"session_id": session_id, "spec_version": f"cornerstone/{SPEC_VERSION}"},
    }
    return canonical_line(statement)
