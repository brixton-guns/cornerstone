"""Confinement (spec v0.2 §3, §10): the observed process cannot touch .stone/."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cornerstone_cli import confine
from cornerstone_cli.cli import main
from cornerstone_cli.confine import probe, resolve_backend, wrap_command

EXPECTED_BACKEND = {"darwin": "seatbelt", "linux": "userns"}.get(sys.platform)
confinable = pytest.mark.skipif(EXPECTED_BACKEND is None, reason="no confinement backend on this platform")
linux_only = pytest.mark.skipif(sys.platform != "linux", reason="userns backend is Linux-only")


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def latest_records(workspace: Path) -> list[dict]:
    index = (workspace / ".stone" / "index.jsonl").read_text().splitlines()
    session_id = json.loads(index[-1])["id"]
    path = workspace / ".stone" / "sessions" / session_id / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


@confinable
def test_confined_run_records_effects_and_protects_the_ledger(workspace, capsys):
    assert main(["run", "--", "/bin/sh", "-c", "echo first > a.txt"]) == 0
    capsys.readouterr()
    old_session = json.loads((workspace / ".stone" / "index.jsonl").read_text().splitlines()[-1])["id"]
    old_ledger = workspace / ".stone" / "sessions" / old_session / "events.jsonl"
    old_bytes = old_ledger.read_bytes()

    script = (
        "echo legit > new.txt; "
        "echo evil > .stone/evil 2>/dev/null; "
        f"echo evil > .stone/sessions/{old_session}/events.jsonl 2>/dev/null; "
        "rm -f .stone/index.jsonl 2>/dev/null; "
        "mv .stone stolen 2>/dev/null; "
        "ln -s .stone alias 2>/dev/null; "
        "echo evil > alias/through-symlink 2>/dev/null; "
        "exit 0"
    )
    assert main(["run", "--confine", "--", "/bin/sh", "-c", script]) == 0
    out = capsys.readouterr().out
    assert f"Confinement: {EXPECTED_BACKEND} (ledger profile)" in out

    # The mask held: every attack path failed, the legitimate write landed.
    assert not (workspace / "stolen").exists()
    assert not (workspace / ".stone" / "evil").exists()
    assert not (workspace / ".stone" / "through-symlink").exists()
    assert old_ledger.read_bytes() == old_bytes
    assert (workspace / "new.txt").read_text() == "legit\n"

    records = latest_records(workspace)
    started = records[0]
    assert started["confinement_backend"] == EXPECTED_BACKEND
    assert started["confinement_profile"] == "ledger"
    assert started["confinement_signal_scope"] is False
    effects = {record["path"] for record in records[1:-1]}
    assert "new.txt" in effects  # the observer itself was not confined (criterion 5)


@confinable
def test_detached_descendants_stay_confined(workspace):
    script = '( setsid sh -c "echo evil > .stone/detached" 2>/dev/null || true ) & sleep 0.4; exit 0'
    assert main(["run", "--confine", "--", "/bin/sh", "-c", script]) == 0
    assert not (workspace / ".stone" / "detached").exists()


@confinable
def test_probe_failure_is_95_with_the_applying_cause(workspace, capsys, monkeypatch):
    monkeypatch.setattr(confine, "wrap_command", lambda backend, stone, command: list(command))
    assert main(["run", "--confine", "--", "true"]) == 95
    err = capsys.readouterr().err
    assert "failed to apply" in err
    assert not (workspace / ".stone" / "confinement-probe").exists()


def test_missing_backend_is_95_with_the_absent_cause(workspace, capsys, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert main(["run", "--confine", "--", "true"]) == 95
    assert "no confinement backend" in capsys.readouterr().err


@confinable
def test_unconfined_sessions_declare_no_confinement(workspace, capsys):
    assert main(["run", "--", "true"]) == 0
    assert "Confinement: none" in capsys.readouterr().out
    started = latest_records(workspace)[0]
    assert started["confinement_backend"] == "none"
    assert started["confinement_profile"] == "none"


@linux_only
def test_nested_namespaces_cannot_revoke_the_mask(workspace):
    stone_dir = workspace / ".stone"
    stone_dir.mkdir()
    (stone_dir / "keep").write_text("intact\n")
    backend = resolve_backend()
    probe(backend, stone_dir)
    stone = os.path.realpath(stone_dir)

    # Criterion 4: a nested user/mount namespace created by the observed
    # process cannot unmount the mask, remount it writable, or reach the real
    # bytes through an overlay.
    attacks = [
        f'umount "{stone}"; echo evil > "{stone}/evil" 2>/dev/null',
        f'mount -o remount,bind,rw "{stone}" && echo evil > "{stone}/evil"',
        f'mount -t tmpfs none "{stone}" && echo evil > "{stone}/evil"',
    ]
    for attack in attacks:
        nested = f"unshare --user --map-root-user --mount sh -c '{attack}'"
        subprocess.run(wrap_command(backend, stone_dir, ["sh", "-c", nested]), capture_output=True, timeout=30)
        assert not os.path.exists(os.path.join(stone, "evil")), attack
    assert (stone_dir / "keep").read_text() == "intact\n"
