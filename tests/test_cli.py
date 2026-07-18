"""End-to-end tests through the CLI: run, show, verify, list, lock, capture."""

import json
import os
import stat
from pathlib import Path

import pytest

from cornerstone_cli.cli import main


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def read_ledger_lines(workspace: Path) -> list[dict]:
    index = (workspace / ".stone" / "index.jsonl").read_text().splitlines()
    session_id = json.loads(index[-1])["id"]
    path = workspace / ".stone" / "sessions" / session_id / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_run_records_all_effect_types(workspace, capsys):
    (workspace / "old.txt").write_text("to be deleted")
    (workspace / "keep.txt").write_text("same content, new name")
    (workspace / "mod.txt").write_text("v1")
    (workspace / "perm.txt").write_text("permissions change")
    os.chmod(workspace / "perm.txt", 0o644)

    script = "echo fresh > new.txt; rm old.txt; mv keep.txt moved.txt; chmod 600 perm.txt; echo v2 > mod.txt"
    code = main(["run", "--actor", "test", "--", "/bin/sh", "-c", script])
    assert code == 0

    records = read_ledger_lines(workspace)
    started, finished = records[0], records[-1]
    assert started["type"] == "session.started"
    assert started["actor"] == "test"
    assert started["spec"] == "0.1"
    assert started["command"] == ["/bin/sh", "-c", script]
    assert finished["type"] == "session.finished"
    assert finished["outcome"] == "success"
    assert finished["exit_code"] == 0

    effects = {(r["type"], r["path"]) for r in records[1:-1]}
    assert effects == {
        ("file.created", "new.txt"),
        ("file.deleted", "old.txt"),
        ("file.renamed", "moved.txt"),
        ("file.metadata_modified", "perm.txt"),
        ("file.modified", "mod.txt"),
    }
    renamed = next(r for r in records[1:-1] if r["type"] == "file.renamed")
    assert renamed["path_before"] == "keep.txt"

    out = capsys.readouterr().out
    assert "Net effects detected:" in out
    assert "RENAMED" in out
    assert "keep.txt → moved.txt" in out
    assert "The order shown does not necessarily represent" in out


def test_run_propagates_child_exit_code(workspace):
    assert main(["run", "--", "/bin/sh", "-c", "exit 7"]) == 7
    records = read_ledger_lines(workspace)
    assert records[-1]["outcome"] == "failed"
    assert records[-1]["exit_code"] == 7
    assert records[0]["actor"] == "undeclared"


def test_run_child_killed_by_signal_is_interrupted(workspace):
    assert main(["run", "--", "/bin/sh", "-c", "kill -TERM $$"]) == 143
    records = read_ledger_lines(workspace)
    assert records[-1]["outcome"] == "interrupted"
    assert records[-1]["exit_code"] == 143


def test_run_refuses_when_lock_is_held(workspace, capsys):
    (workspace / ".stone").mkdir()
    (workspace / ".stone" / "lock").write_text('{"id":"X","pid":1}\n')
    assert main(["run", "--", "true"]) == 96
    assert "lock" in capsys.readouterr().err


def test_lock_is_removed_after_the_session(workspace):
    assert main(["run", "--", "true"]) == 0
    assert not (workspace / ".stone" / "lock").exists()


def test_run_command_not_found(workspace, capsys):
    assert main(["run", "--", "no-such-command-xyz"]) == 127
    assert "command not found" in capsys.readouterr().err


def test_verify_show_list_and_latest(workspace, capsys):
    (workspace / "a.txt").write_text("a")
    assert main(["run", "--actor", "alice", "--", "/bin/sh", "-c", "echo b > b.txt"]) == 0
    capsys.readouterr()

    assert main(["verify", "latest"]) == 0
    assert "Ledger intact" in capsys.readouterr().out

    assert main(["show", "latest"]) == 0
    out = capsys.readouterr().out
    assert "Declared actor: alice" in out
    assert "CREATED" in out and "b.txt" in out

    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert "alice" in out and "success" in out


def test_verify_detects_tampered_ledger(workspace, capsys):
    assert main(["run", "--", "/bin/sh", "-c", "echo x > x.txt"]) == 0
    capsys.readouterr()
    index = (workspace / ".stone" / "index.jsonl").read_text().splitlines()
    session_id = json.loads(index[-1])["id"]
    ledger = workspace / ".stone" / "sessions" / session_id / "events.jsonl"
    ledger.write_bytes(ledger.read_bytes().replace(b"x.txt", b"y.txt"))

    assert main(["verify", "latest"]) == 1
    assert "NOT intact" in capsys.readouterr().out


def test_descendants_are_awaited_before_the_final_snapshot(workspace):
    script = "( sleep 0.3; echo late > late.txt ) & exit 0"
    assert main(["run", "--", "/bin/sh", "-c", script]) == 0
    records = read_ledger_lines(workspace)
    assert ("file.created", "late.txt") in {(r["type"], r.get("path")) for r in records[1:-1]}
    assert records[-1]["duration_s"] >= 0.3


def test_rename_with_chmod_is_recorded_as_delete_plus_create(workspace):
    (workspace / "f.txt").write_text("some content")
    os.chmod(workspace / "f.txt", 0o644)
    assert main(["run", "--", "/bin/sh", "-c", "mv f.txt g.txt; chmod 600 g.txt"]) == 0
    records = read_ledger_lines(workspace)
    events = {r["type"]: r for r in records[1:-1]}
    assert set(events) == {"file.created", "file.deleted"}
    assert events["file.created"]["path"] == "g.txt"
    assert events["file.created"]["mode"] == "0600"
    assert events["file.deleted"]["path"] == "f.txt"


def test_show_refuses_a_tampered_ledger(workspace, capsys):
    assert main(["run", "--", "/bin/sh", "-c", "echo x > x.txt"]) == 0
    capsys.readouterr()
    session_id = json.loads((workspace / ".stone" / "index.jsonl").read_text().splitlines()[-1])["id"]
    ledger = workspace / ".stone" / "sessions" / session_id / "events.jsonl"
    ledger.write_bytes(ledger.read_bytes().replace(b"x.txt", b"y.txt"))

    assert main(["show", "latest"]) == 1
    assert "NOT intact" in capsys.readouterr().out


def test_verify_detects_a_truncated_ledger(workspace, capsys):
    assert main(["run", "--", "/bin/sh", "-c", "echo x > x.txt"]) == 0
    capsys.readouterr()
    session_id = json.loads((workspace / ".stone" / "index.jsonl").read_text().splitlines()[-1])["id"]
    ledger = workspace / ".stone" / "sessions" / session_id / "events.jsonl"
    ledger.write_bytes(ledger.read_bytes().split(b"\n")[0] + b"\n")

    assert main(["verify", "latest"]) == 1
    assert "NOT intact" in capsys.readouterr().out


def test_verify_detects_a_ledger_copied_under_another_session_id(workspace, capsys):
    import shutil

    assert main(["run", "--", "/bin/sh", "-c", "echo x > x.txt"]) == 0
    capsys.readouterr()
    session_id = json.loads((workspace / ".stone" / "index.jsonl").read_text().splitlines()[-1])["id"]
    fake_id = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    shutil.copytree(workspace / ".stone" / "sessions" / session_id, workspace / ".stone" / "sessions" / fake_id)

    assert main(["verify", fake_id]) == 1
    assert "NOT intact" in capsys.readouterr().out


def test_latest_without_sessions_fails_explicitly(workspace, capsys):
    assert main(["show", "latest"]) == 1
    assert "no sessions" in capsys.readouterr().err


def test_ignored_zones_are_not_observed(workspace):
    config_dir = workspace / ".stone"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text('ignore = ["secret/"]\n')
    (workspace / "secret").mkdir()
    (workspace / "node_modules").mkdir()

    script = "echo x > secret/hidden.txt; echo y > node_modules/dep.js; echo z > seen.txt"
    assert main(["run", "--", "/bin/sh", "-c", script]) == 0
    records = read_ledger_lines(workspace)
    assert {r["path"] for r in records[1:-1]} == {"seen.txt"}


def test_capture_output_saves_logs_with_restrictive_permissions(workspace, capsys):
    assert main(["run", "--capture-output", "--", "/bin/sh", "-c", "echo out; echo err >&2"]) == 0
    session_id = json.loads((workspace / ".stone" / "index.jsonl").read_text().splitlines()[-1])["id"]
    session_dir = workspace / ".stone" / "sessions" / session_id
    assert (session_dir / "stdout.log").read_bytes() == b"out\n"
    assert (session_dir / "stderr.log").read_bytes() == b"err\n"
    for name in ("stdout.log", "stderr.log"):
        assert stat.S_IMODE((session_dir / name).stat().st_mode) == 0o600
    assert "out" in capsys.readouterr().out  # captured output is still forwarded


def test_capture_output_truncation_keeps_the_tail(workspace):
    config_dir = workspace / ".stone"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text("capture_max_bytes = 10\n")
    assert main(["run", "--capture-output", "--", "/bin/sh", "-c", "printf 0123456789ABCDEF"]) == 0
    session_id = json.loads((workspace / ".stone" / "index.jsonl").read_text().splitlines()[-1])["id"]
    log = (workspace / ".stone" / "sessions" / session_id / "stdout.log").read_bytes()
    assert log == b"6789ABCDEF"


def test_symlinked_stone_dir_is_refused(workspace, capsys):
    outside = workspace.parent / f"{workspace.name}-outside-stone"
    outside.mkdir()
    os.symlink(str(outside), workspace / ".stone")

    assert main(["run", "--", "true"]) == 97
    assert "symbolic link" in capsys.readouterr().err
    assert list(outside.iterdir()) == []  # nothing was written outside the workspace


def test_symlinked_sessions_dir_is_refused(workspace, capsys):
    outside = workspace.parent / f"{workspace.name}-outside-sessions"
    outside.mkdir()
    (workspace / ".stone").mkdir()
    os.symlink(str(outside), workspace / ".stone" / "sessions")

    assert main(["run", "--", "true"]) == 97
    assert "sessions" in capsys.readouterr().err
    assert list(outside.iterdir()) == []  # nothing was written outside the workspace


def test_stone_swapped_during_the_session_cannot_redirect_writes(workspace, capsys):
    outside = workspace.parent / f"{workspace.name}-outside-swap"
    outside.mkdir()

    script = f"mv .stone stone-moved; ln -s {outside} .stone"
    assert main(["run", "--", "/bin/sh", "-c", script]) == 0
    capsys.readouterr()

    assert list(outside.iterdir()) == []  # writes followed the held descriptor, not the name
    moved_sessions = workspace / "stone-moved" / "sessions"
    assert any(moved_sessions.iterdir())  # the ledger landed in the real directory


def test_invalid_config_is_a_workspace_error(workspace, capsys):
    config_dir = workspace / ".stone"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text('ignore = ["*.tmp"]\n')
    assert main(["run", "--", "true"]) == 97
    assert "glob" in capsys.readouterr().err


def test_run_requires_separator(workspace, capsys):
    assert main(["run", "echo", "hi"]) == 2
    assert "--" in capsys.readouterr().err


def test_no_net_effects(workspace, capsys):
    assert main(["run", "--", "true"]) == 0
    assert "No net effects detected." in capsys.readouterr().out
