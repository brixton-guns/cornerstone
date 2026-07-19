"""Confinement of the observed process (spec v0.2 §3): the ledger profile.

The observed command and every descendant lose write access to the real
.stone/ subtree for the whole session. Linux stages a read-only bind mount in
a user+mount namespace and then re-enters a user namespace mapped to the real
UID, so exec drops every capability over the mount namespace holding the mask;
nested namespaces created by the command inherit the mask with locked flags.
macOS uses a Seatbelt profile with an explicit deny on the subtree.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


class ConfinementError(Exception):
    """Confinement was requested but is unavailable or failed to apply (exit 95)."""


_SEATBELT_PROFILE = """\
(version 1)
(allow default)
(deny file-write* (subpath "{stone}"))
"""

_USERNS_SCRIPT = (
    "set -e; d=$1; u=$2; g=$3; shift 3; "
    'mount --bind "$d" "$d"; '
    'mount -o remount,bind,ro "$d"; '
    'exec unshare --user --map-user="$u" --map-group="$g" -- "$@"'
)

_PROBE_TIMEOUT = 30


def _seatbelt_escape(path: str) -> str:
    return path.replace("\\", "\\\\").replace('"', '\\"')


def resolve_backend() -> str:
    """Pick the platform backend, or fail closed naming the missing piece."""
    if sys.platform == "darwin":
        if shutil.which("sandbox-exec") is None:
            raise ConfinementError("no confinement backend: sandbox-exec not found on this system")
        return "seatbelt"
    if sys.platform.startswith("linux"):
        if shutil.which("unshare") is None:
            raise ConfinementError("no confinement backend: unshare(1) not found on this system")
        return "userns"
    raise ConfinementError(f"no confinement backend for this platform: {sys.platform}")


def wrap_command(backend: str, stone_dir: Path, command: list[str]) -> list[str]:
    """Wrap the observed command so the real .stone/ subtree is unwritable."""
    stone = os.path.realpath(stone_dir)
    if backend == "seatbelt":
        profile = _SEATBELT_PROFILE.format(stone=_seatbelt_escape(stone))
        return ["sandbox-exec", "-p", profile, *command]
    if backend == "userns":
        return [
            "unshare", "--user", "--map-root-user", "--mount",
            "sh", "-c", _USERNS_SCRIPT, "sh",
            stone, str(os.getuid()), str(os.getgid()),
            *command,
        ]
    raise ConfinementError(f"unknown confinement backend: {backend}")


def probe(backend: str, stone_dir: Path) -> None:
    """Fail closed unless the mask provably works (spec v0.2 §3).

    Two stages so exit 95 can name its cause: first the wrapper must run a
    trivial command at all (backend functional), then a write into .stone/
    under the mask must fail (mask effective). A wrapper failure and a denied
    write both exit non-zero; only the marker file tells them apart, so the
    liveness stage is not optional.
    """
    liveness = wrap_command(backend, stone_dir, ["true"])
    try:
        alive = subprocess.run(liveness, capture_output=True, timeout=_PROBE_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfinementError(f"confinement backend {backend} cannot run: {exc}") from exc
    if alive.returncode != 0:
        detail = alive.stderr.decode("utf-8", errors="replace").strip()
        raise ConfinementError(
            f"confinement backend {backend} is not functional on this system"
            f" (backend unavailable): {detail or f'exit {alive.returncode}'}"
        )

    marker = os.path.join(os.path.realpath(stone_dir), "confinement-probe")
    denied = wrap_command(backend, stone_dir, ["sh", "-c", 'echo probe > "$1" 2>/dev/null', "sh", marker])
    try:
        result = subprocess.run(denied, capture_output=True, timeout=_PROBE_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfinementError(f"confinement probe cannot run under {backend}: {exc}") from exc
    if os.path.exists(marker):
        os.unlink(marker)
        raise ConfinementError(
            f"confinement failed to apply: a probe write into .stone/ succeeded under {backend}"
        )
    if result.returncode == 0:
        raise ConfinementError(
            f"confinement probe is inconclusive under {backend}: the denied write reported success"
        )
