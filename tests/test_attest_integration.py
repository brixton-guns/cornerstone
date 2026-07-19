"""run --attest against a live fake authority (spec v0.2 §5, §10)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from cornerstone_cli.cli import main


class _FakeAuthority(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, tamper: bool = False):
        self.tamper = tamper
        self.submissions = 0
        super().__init__(("127.0.0.1", 0), _Handler)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class _Handler(BaseHTTPRequestHandler):
    server: _FakeAuthority

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.server.submissions += 1
        statement = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.server.tamper:
            statement["artifact"]["digest"]["value"] = "0" * 64
        receipt = {
            "receipt_version": "witness.receipt/0.1",
            "signature": {"algorithm": "ed25519", "value": "A" * 85 + "A"},
            "signed": {
                "authority": {"id": "witness.fake", "key_id": "ed25519:" + "0" * 64},
                "log_index": 1,
                "receipt_id": "01ARZ3NDEKTSV4RRFFQ69G5FAW",
                "received_at": "2026-07-19T12:00:00Z",
                "statement": statement,
            },
        }
        body = json.dumps(receipt).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture()
def authority():
    server = _FakeAuthority()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def receipt_path(workspace):
    index = (workspace / ".stone" / "index.jsonl").read_text().splitlines()
    session_id = json.loads(index[-1])["id"]
    return workspace / ".stone" / "sessions" / session_id / "receipt.json"


def test_run_attest_saves_the_receipt(workspace, authority, capsys):
    assert main(["run", "--attest", authority.url, "--", "/bin/sh", "-c", "echo x > x.txt"]) == 0
    out = capsys.readouterr().out
    assert "Attestation: receipt accepted" in out
    saved = json.loads(receipt_path(workspace).read_bytes())
    assert saved["signed"]["statement"]["subject"]["spec_version"] == "cornerstone/0.2"

    assert main(["verify", "latest"]) == 0
    assert "Receipt present" in capsys.readouterr().out


def test_unreachable_authority_is_declared_and_open_policy_keeps_the_exit_code(workspace, capsys):
    assert main(["run", "--attest", "http://127.0.0.1:1", "--", "/bin/sh", "-c", "exit 0"]) == 0
    out = capsys.readouterr().out
    assert "Attestation FAILED" in out
    assert not receipt_path(workspace).exists()
    # The ledger stays intact and verifiable (criterion 10).
    assert main(["verify", "latest"]) == 0


def test_strict_policy_turns_the_same_failure_into_94(workspace, capsys):
    assert main(["run", "--attest", "http://127.0.0.1:1", "--attest-policy", "strict",
                 "--", "/bin/sh", "-c", "exit 0"]) == 94
    assert "Attestation FAILED" in capsys.readouterr().out
    assert main(["verify", "latest"]) == 0


def test_a_lying_authority_is_reported_not_trusted(workspace, capsys):
    server = _FakeAuthority(tamper=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert main(["run", "--attest", server.url, "--", "/bin/sh", "-c", "exit 0"]) == 0
        out = capsys.readouterr().out
        assert "Attestation FAILED" in out
        assert "different statement" in out
        assert not receipt_path(workspace).exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_standalone_attest_submits_once_and_never_replaces(workspace, authority, capsys):
    assert main(["run", "--", "/bin/sh", "-c", "echo x > x.txt"]) == 0
    capsys.readouterr()

    assert main(["attest", "--url", authority.url, "latest"]) == 0
    assert "receipt accepted" in capsys.readouterr().out
    first = receipt_path(workspace).read_bytes()

    assert main(["attest", "--url", authority.url, "latest"]) == 0
    assert "never replaced" in capsys.readouterr().out
    assert receipt_path(workspace).read_bytes() == first
    assert authority.submissions == 1  # the second call never resubmitted


def test_plain_http_to_non_loopback_is_refused(workspace, capsys):
    assert main(["run", "--", "true"]) == 0
    capsys.readouterr()
    assert main(["attest", "--url", "http://example.com", "latest"]) == 1
    assert "use HTTPS" in capsys.readouterr().err


def test_strict_policy_requires_a_url(workspace, capsys):
    assert main(["run", "--attest-policy", "strict", "--", "true"]) == 2
    assert "requires --attest" in capsys.readouterr().err
