"""stone attest: witness/0.1 statements over verified ledgers."""

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from cornerstone_cli.cli import main
from cornerstone_cli.ledger import chain_records

# Cross-project vector, copied verbatim from the Witness repository
# (test-vectors/sample-events.jsonl and the statement inside the Ed25519-signed
# receipt in test-vectors/receipt-v0.1.json). `stone attest` on this ledger must
# reproduce that signed statement exactly, or the two implementations disagree
# on the digest or the statement shape.
SAMPLE_SESSION_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
SAMPLE_LEDGER = (
    b'{"actor":"test-vector","command":["true"],"id":"01ARZ3NDEKTSV4RRFFQ69G5FAV",'
    b'"prev":"0000000000000000000000000000000000000000000000000000000000000000",'
    b'"spec":"0.1","ts":"2026-07-18T11:59:59Z","type":"session.started"}\n'
    b'{"duration_s":0.0,"exit_code":0,"outcome":"success",'
    b'"prev":"c521b763b4cdab0c2fe3afab2421e2825d0904809899134bbb1b6a0ee2ae8392",'
    b'"ts":"2026-07-18T12:00:00Z","type":"session.finished"}\n'
)
SIGNED_STATEMENT = {
    "artifact": {
        "byte_scope": "entire-file-including-final-newline",
        "digest": {
            "algorithm": "sha256",
            "value": "eeb163873b7a68a19ce2b5eb974ba5968b491c8e9135b25ed749bce60e14c90d",
        },
        "media_type": "application/vnd.cornerstone.ledger+jsonl",
    },
    "statement_version": "witness.statement/0.1",
    "subject": {
        "session_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "spec_version": "cornerstone/0.1",
    },
}


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def plant_ledger(workspace: Path, session_id: str, ledger: bytes) -> None:
    session_dir = workspace / ".stone" / "sessions" / session_id
    session_dir.mkdir(parents=True)
    (session_dir / "events.jsonl").write_bytes(ledger)


def latest_session_id(workspace: Path) -> str:
    index = (workspace / ".stone" / "index.jsonl").read_text().splitlines()
    return json.loads(index[-1])["id"]


def test_attest_reproduces_the_witness_signed_vector(workspace, capsys):
    plant_ledger(workspace, SAMPLE_SESSION_ID, SAMPLE_LEDGER)
    assert main(["attest", SAMPLE_SESSION_ID]) == 0
    assert json.loads(capsys.readouterr().out) == SIGNED_STATEMENT


def test_attest_output_is_canonical_and_deterministic(workspace, capsys):
    plant_ledger(workspace, SAMPLE_SESSION_ID, SAMPLE_LEDGER)
    assert main(["attest", SAMPLE_SESSION_ID]) == 0
    first = capsys.readouterr().out
    assert main(["attest", SAMPLE_SESSION_ID]) == 0
    assert capsys.readouterr().out == first
    canonical = json.dumps(json.loads(first), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert first == canonical + "\n"


def test_attest_after_a_real_session(workspace, capsys):
    assert main(["run", "--", "/bin/sh", "-c", "echo x > x.txt"]) == 0
    capsys.readouterr()
    assert main(["attest", "latest"]) == 0
    statement = json.loads(capsys.readouterr().out)
    session_id = latest_session_id(workspace)
    ledger = workspace / ".stone" / "sessions" / session_id / "events.jsonl"
    assert statement["subject"]["session_id"] == session_id
    assert statement["artifact"]["digest"]["value"] == hashlib.sha256(ledger.read_bytes()).hexdigest()
    # Fresh ledgers are spec 0.2 and map to the 0.2 statement (spec v0.2 §8).
    assert statement["statement_version"] == "witness.statement/0.2"
    assert statement["subject"]["spec_version"] == "cornerstone/0.2"


def test_attest_refuses_a_tampered_ledger(workspace, capsys):
    assert main(["run", "--", "/bin/sh", "-c", "echo x > x.txt"]) == 0
    capsys.readouterr()
    ledger = workspace / ".stone" / "sessions" / latest_session_id(workspace) / "events.jsonl"
    ledger.write_bytes(ledger.read_bytes().replace(b"x.txt", b"y.txt"))

    assert main(["attest", "latest"]) == 1
    captured = capsys.readouterr()
    assert "refusing to attest" in captured.err
    assert captured.out == ""  # a broken ledger must not leak a statement


def test_attest_refuses_a_ledger_copied_under_another_session_id(workspace, capsys):
    assert main(["run", "--", "/bin/sh", "-c", "echo x > x.txt"]) == 0
    capsys.readouterr()
    session_id = latest_session_id(workspace)
    shutil.copytree(
        workspace / ".stone" / "sessions" / session_id,
        workspace / ".stone" / "sessions" / SAMPLE_SESSION_ID,
    )

    assert main(["attest", SAMPLE_SESSION_ID]) == 1
    captured = capsys.readouterr()
    assert "belongs to session" in captured.err
    assert captured.out == ""


def test_attest_refuses_a_session_id_that_is_not_a_ulid(workspace, capsys):
    bad_id = "aaaaaaaaaaaaaaaaaaaaaaaaaa"  # lowercase: outside the Crockford alphabet
    records = [
        {
            "type": "session.started",
            "id": bad_id,
            "actor": "test",
            "command": ["true"],
            "spec": "0.1",
            "ts": "2026-07-18T12:00:00Z",
        },
        {
            "type": "session.finished",
            "outcome": "success",
            "exit_code": 0,
            "duration_s": 0.0,
            "ts": "2026-07-18T12:00:00Z",
        },
    ]
    ledger = "".join(line + "\n" for line in chain_records(records)).encode("utf-8")
    plant_ledger(workspace, bad_id, ledger)

    assert main(["attest", bad_id]) == 1
    captured = capsys.readouterr()
    assert "ULID" in captured.err
    assert captured.out == ""


def test_attest_without_a_ledger_fails_explicitly(workspace, capsys):
    (workspace / ".stone").mkdir()
    assert main(["attest", SAMPLE_SESSION_ID]) == 1
    assert "no ledger" in capsys.readouterr().err
