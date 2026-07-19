"""Witness statements: bind a verified session ledger to its digest (spec v0.2 §5).

Statement emission and submission only. Receipt signatures are Witness's
territory: Prima Pietra stays dependency-free and never calls a receipt
"verified" (spec v0.2 §5, §9).
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .ledger import LedgerError, canonical_line, read_ledger_bytes, verify_ledger_bytes

LEDGER_MEDIA_TYPE = "application/vnd.cornerstone.ledger+jsonl"
BYTE_SCOPE = "entire-file-including-final-newline"

# Each ledger spec version maps to one (statement_version, spec_version) pair;
# agility lives in version bumps, never in negotiable fields (spec v0.2 §8).
_VERSIONS_BY_SPEC = {
    "0.1": ("witness.statement/0.1", "cornerstone/0.1"),
    "0.2": ("witness.statement/0.2", "cornerstone/0.2"),
}

_SESSION_ID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")

RECEIPT_FILENAME = "receipt.json"


class AttestationError(Exception):
    """The statement could not be submitted or the authority misbehaved."""


def build_statement(ledger_path: Path | str, session_id: str, *, dir_fd: int | None = None) -> str:
    """Return the canonical Witness statement for a verified session ledger.

    The ledger is read exactly once: the digest covers the same bytes that
    passed chain and structure verification, so the statement cannot describe
    bytes other than the ones checked.
    """
    if _SESSION_ID.fullmatch(session_id) is None:
        raise LedgerError(f"session id is not a valid ULID: {session_id!r}")
    raw = read_ledger_bytes(ledger_path, dir_fd=dir_fd)
    records = verify_ledger_bytes(raw)
    if records[0].get("id") != session_id:
        raise LedgerError(f"ledger belongs to session {records[0].get('id')!r}, not {session_id!r}")
    statement_version, spec_version = _VERSIONS_BY_SPEC[records[0]["spec"]]
    statement = {
        "artifact": {
            "byte_scope": BYTE_SCOPE,
            "digest": {"algorithm": "sha256", "value": hashlib.sha256(raw).hexdigest()},
            "media_type": LEDGER_MEDIA_TYPE,
        },
        "statement_version": statement_version,
        "subject": {"session_id": session_id, "spec_version": spec_version},
    }
    return canonical_line(statement)


def _host_is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def submit_statement(url: str, statement_line: str, timeout: float = 30.0) -> bytes:
    """POST the statement to a Witness authority; return the raw receipt bytes.

    The idempotency key is derived from the statement digest, so blind retries
    are safe. The receipt is checked to embed exactly the submitted statement;
    verifying its signature is `witness verify`'s job, not ours.
    """
    statement_bytes = statement_line.encode("utf-8")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise AttestationError(f"unsupported authority URL scheme: {parsed.scheme!r}")
    if parsed.scheme == "http" and not _host_is_loopback(parsed.hostname or ""):
        raise AttestationError("plain HTTP to a non-loopback authority is refused: use HTTPS")

    request = urllib.request.Request(
        url.rstrip("/") + "/v1/receipts",
        data=statement_bytes,
        headers={
            "Content-Type": "application/json",
            "Idempotency-Key": "sha256:" + hashlib.sha256(statement_bytes).hexdigest(),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise AttestationError(f"authority rejected the statement: HTTP {exc.code} {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise AttestationError(f"cannot reach the authority: {exc}") from exc

    try:
        receipt = json.loads(body)
    except ValueError as exc:
        raise AttestationError("authority returned a receipt that is not valid JSON") from exc
    if not isinstance(receipt, dict) or receipt.get("signed", {}).get("statement") != json.loads(statement_line):
        raise AttestationError("authority returned a receipt for a different statement")
    return body
