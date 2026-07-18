"""Workspace snapshots (spec §6).

A snapshot observes regular files and symbolic links. Directories exist only
implicitly through file paths; FIFOs, sockets and device nodes are outside the
observed universe, like directories (§6, §14).
"""

from __future__ import annotations

import hashlib
import os
import stat as stat_module
from dataclasses import dataclass
from pathlib import Path

_CHUNK_SIZE = 1024 * 1024


class SnapshotError(Exception):
    """The observable state of the workspace could not be captured."""


@dataclass(frozen=True)
class Entry:
    entry_type: str  # "file" | "symlink"
    hash: str  # SHA-256 hex of content; for symlinks, of the target text
    size: int
    mode: str | None  # octal string such as "0644"; None when the filesystem does not expose it
    target: str | None = None  # symlink target text, when readable


def _mode_string(st_mode: int) -> str:
    return f"0{stat_module.S_IMODE(st_mode):03o}"


def _hash_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb", buffering=0) as fh:
        while chunk := fh.read(_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def is_ignored(rel_path: str, ignore: tuple[str, ...]) -> bool:
    """Path-prefix match relative to the workspace root, no globs (§13, §16)."""
    for prefix in ignore:
        prefix = prefix.rstrip("/")
        if prefix and (rel_path == prefix or rel_path.startswith(prefix + "/")):
            return True
    return False


def take_snapshot(root: Path, ignore: tuple[str, ...]) -> dict[str, Entry]:
    """Map each relative path under root to its observed Entry."""
    entries: dict[str, Entry] = {}
    _scan(str(root), "", tuple(ignore), entries)
    return entries


def _scan(dir_path: str, rel_prefix: str, ignore: tuple[str, ...], entries: dict[str, Entry]) -> None:
    try:
        with os.scandir(dir_path) as iterator:
            children = sorted(iterator, key=lambda entry: entry.name)
    except FileNotFoundError:
        return  # directory vanished while scanning: nothing left to observe here
    except OSError as exc:
        raise SnapshotError(f"cannot scan directory: {exc}") from exc

    for child in children:
        rel_path = rel_prefix + child.name
        if is_ignored(rel_path, ignore):
            continue
        try:
            if child.is_symlink():
                st = child.stat(follow_symlinks=False)
                try:
                    target = os.readlink(child.path)
                except OSError:
                    target = None
                digest = hashlib.sha256((target or "").encode("utf-8", "surrogateescape")).hexdigest()
                entries[rel_path] = Entry("symlink", digest, st.st_size, _mode_string(st.st_mode), target)
            elif child.is_dir(follow_symlinks=False):
                _scan(child.path, rel_path + "/", ignore, entries)
            elif child.is_file(follow_symlinks=False):
                st = child.stat(follow_symlinks=False)
                entries[rel_path] = Entry("file", _hash_file(child.path), st.st_size, _mode_string(st.st_mode))
        except FileNotFoundError:
            continue  # vanished between listing and reading: no persistent observable state
        except SnapshotError:
            raise
        except OSError as exc:
            raise SnapshotError(f"cannot read {rel_path}: {exc}") from exc
