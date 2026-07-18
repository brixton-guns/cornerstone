"""Unit tests: ULID, canonical JSON, hash chain, snapshot."""

import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from cornerstone_cli._ulid import ulid
from cornerstone_cli.ledger import GENESIS, LedgerError, canonical_line, chain_records, verify_ledger, write_ledger
from cornerstone_cli.snapshot import Entry, SnapshotError, _hash_file, take_snapshot

IGNORE = (".stone/", ".git/")

TS = "2026-07-18T00:00:00Z"


def valid_records(events=None):
    """A minimal structurally valid session: started, optional events, finished."""
    started = {
        "type": "session.started",
        "id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "actor": "test",
        "command": ["true"],
        "spec": "0.1",
        "ts": TS,
    }
    finished = {"type": "session.finished", "outcome": "success", "exit_code": 0, "duration_s": 0.5, "ts": TS}
    return [started, *(events or []), finished]


def created_event(path="a.txt"):
    return {"type": "file.created", "path": path, "entry_type": "file", "hash": "0" * 64, "size": 1, "mode": "0644", "ts": TS}


def test_ulid_shape_and_time_ordering():
    first = ulid()
    assert len(first) == 26
    assert set(first) <= set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
    later = ulid(timestamp_ms=int(time.time() * 1000) + 10_000)
    assert first < later  # lexicographic order follows creation time


def test_canonical_line_sorts_keys_compactly():
    line = canonical_line({"z": 1, "a": [1, 2], "m": "à"})
    assert line == '{"a":[1,2],"m":"à","z":1}'


def test_chain_records_links_by_sha256():
    lines = chain_records([{"type": "session.started"}, {"type": "session.finished"}])
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["prev"] == GENESIS
    assert second["prev"] == hashlib.sha256(lines[0].encode()).hexdigest()


def test_verify_ledger_detects_tampering(tmp_path):
    path = tmp_path / "events.jsonl"
    write_ledger(path, valid_records([created_event()]))
    assert verify_ledger(path) == 3

    raw = path.read_bytes()
    path.write_bytes(raw.replace(b'"size":1', b'"size":2'))
    with pytest.raises(LedgerError):
        verify_ledger(path)


def test_verify_ledger_detects_truncation(tmp_path):
    path = tmp_path / "events.jsonl"
    write_ledger(path, valid_records([created_event()]))
    first_line = path.read_bytes().split(b"\n")[0] + b"\n"
    path.write_bytes(first_line)
    with pytest.raises(LedgerError, match="session.finished"):
        verify_ledger(path)


def test_verify_ledger_rejects_missing_required_fields(tmp_path):
    path = tmp_path / "events.jsonl"
    records = valid_records()
    del records[0]["actor"]
    write_ledger(path, records)
    with pytest.raises(LedgerError, match="actor"):
        verify_ledger(path)


def test_verify_ledger_rejects_duplicate_path_subjects(tmp_path):
    path = tmp_path / "events.jsonl"
    write_ledger(path, valid_records([created_event("same.txt"), created_event("same.txt")]))
    with pytest.raises(LedgerError, match="more than one event"):
        verify_ledger(path)


def test_verify_ledger_rejects_events_in_incomplete_sessions(tmp_path):
    path = tmp_path / "events.jsonl"
    records = valid_records([created_event()])
    records[-1]["outcome"] = "incomplete"
    write_ledger(path, records)
    with pytest.raises(LedgerError, match="incomplete"):
        verify_ledger(path)


def test_hash_file_refuses_symlinks(tmp_path):
    (tmp_path / "real.txt").write_text("content")
    os.symlink("real.txt", tmp_path / "link")
    with pytest.raises(OSError):
        _hash_file(str(tmp_path / "link"))


def test_snapshot_observes_files_and_symlinks(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.txt").write_text("hello")
    os.chmod(tmp_path / "docs" / "a.txt", 0o644)
    os.symlink("docs/a.txt", tmp_path / "link")
    (tmp_path / "docs" / "empty_dir_stays_invisible").mkdir()

    snap = take_snapshot(tmp_path, IGNORE)

    assert set(snap) == {"docs/a.txt", "link"}
    file_entry = snap["docs/a.txt"]
    assert file_entry.entry_type == "file"
    assert file_entry.hash == hashlib.sha256(b"hello").hexdigest()
    assert file_entry.size == 5
    assert file_entry.mode == "0644"

    link_entry = snap["link"]
    assert link_entry.entry_type == "symlink"
    assert link_entry.target == "docs/a.txt"
    assert link_entry.hash == hashlib.sha256(b"docs/a.txt").hexdigest()


def test_snapshot_does_not_follow_symlinked_directories(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside the workspace")
    os.symlink(str(outside), tmp_path / "portal")

    snap = take_snapshot(tmp_path, IGNORE)

    assert set(snap) == {"portal"}
    assert snap["portal"].entry_type == "symlink"


def test_snapshot_skips_ignored_prefixes(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref")
    (tmp_path / "kept.txt").write_text("kept")

    snap = take_snapshot(tmp_path, IGNORE)
    assert set(snap) == {"kept.txt"}


def test_snapshot_fails_on_unreadable_directory(tmp_path):
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (blocked / "x.txt").write_text("x")
    os.chmod(blocked, 0o000)
    try:
        with pytest.raises(SnapshotError):
            take_snapshot(tmp_path, IGNORE)
    finally:
        os.chmod(blocked, 0o755)
