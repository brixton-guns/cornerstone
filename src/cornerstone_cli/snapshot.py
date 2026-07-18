"""Workspace snapshots (spec §6).

A snapshot observes regular files and symbolic links. Directories exist only
implicitly through file paths; FIFOs, sockets and device nodes are outside the
observed universe, like directories (§6, §14).

The tree is walked with directory file descriptors (openat-style): every
directory is opened with O_DIRECTORY | O_NOFOLLOW and every file with
O_NOFOLLOW, both relative to the parent directory's descriptor, and file
metadata comes from fstat on the same descriptor that was hashed. Swapping any
path component for a symbolic link while the snapshot runs cannot lead outside
the workspace.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat as stat_module
from dataclasses import dataclass
from pathlib import Path

_CHUNK_SIZE = 1024 * 1024
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _O_CLOEXEC
# O_NONBLOCK so that a file swapped for a FIFO cannot block the open forever;
# it has no effect on regular-file reads.
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | _O_CLOEXEC
# What open() raises when the listed entry type changed under us.
_SWAP_ERRNOS = {errno.ELOOP, errno.EMLINK, errno.ENOTDIR}


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
    try:
        root_fd = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY | _O_CLOEXEC)
    except OSError as exc:
        raise SnapshotError(f"cannot open workspace root: {exc}") from exc
    try:
        _scan(root_fd, "", tuple(ignore), entries)
    finally:
        os.close(root_fd)
    return entries


def _scan(dir_fd: int, rel_prefix: str, ignore: tuple[str, ...], entries: dict[str, Entry]) -> None:
    try:
        with os.scandir(dir_fd) as iterator:
            children = sorted(iterator, key=lambda entry: entry.name)
    except OSError as exc:
        raise SnapshotError(f"cannot scan directory: {exc}") from exc

    for child in children:
        rel_path = rel_prefix + child.name
        if is_ignored(rel_path, ignore):
            continue
        try:
            if child.is_symlink():
                _record_symlink(child.name, dir_fd, rel_path, entries)
            elif child.is_dir(follow_symlinks=False):
                try:
                    child_fd = os.open(child.name, _DIR_FLAGS, dir_fd=dir_fd)
                except OSError as exc:
                    if exc.errno not in _SWAP_ERRNOS:
                        raise
                    # Swapped after being listed as a directory: record what
                    # the path is now, without ever following it.
                    _record_symlink(child.name, dir_fd, rel_path, entries)
                    continue
                try:
                    _scan(child_fd, rel_path + "/", ignore, entries)
                finally:
                    os.close(child_fd)
            elif child.is_file(follow_symlinks=False):
                try:
                    observed = _read_regular(child.name, dir_fd)
                except OSError as exc:
                    if exc.errno not in _SWAP_ERRNOS:
                        raise
                    _record_symlink(child.name, dir_fd, rel_path, entries)
                    continue
                if observed is not None:
                    digest, size, mode = observed
                    entries[rel_path] = Entry("file", digest, size, mode)
            # FIFOs, sockets and devices are outside the observed universe (§6).
        except FileNotFoundError:
            continue  # vanished between listing and reading: no persistent observable state
        except SnapshotError:
            raise
        except OSError as exc:
            raise SnapshotError(f"cannot read {rel_path}: {exc}") from exc


def _read_regular(name: str, dir_fd: int | None) -> tuple[str, int, str] | None:
    """Hash a regular file opened with O_NOFOLLOW relative to dir_fd.

    Size and mode come from fstat on the same descriptor that was hashed, so
    hash and metadata always describe the same inode. Returns None when the
    opened descriptor is not a regular file (the entry changed type under us).
    """
    fd = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
    with os.fdopen(fd, "rb", buffering=0) as fh:
        st = os.fstat(fd)
        if not stat_module.S_ISREG(st.st_mode):
            return None
        digest = hashlib.sha256()
        while chunk := fh.read(_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest(), st.st_size, _mode_string(st.st_mode)


def _record_symlink(name: str, dir_fd: int | None, rel_path: str, entries: dict[str, Entry]) -> None:
    st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    if not stat_module.S_ISLNK(st.st_mode):
        return  # the entry type is flapping mid-snapshot: no stable observable state
    try:
        target = os.readlink(name, dir_fd=dir_fd)
    except OSError:
        target = None
    digest = hashlib.sha256((target or "").encode("utf-8", "surrogateescape")).hexdigest()
    entries[rel_path] = Entry("symlink", digest, st.st_size, _mode_string(st.st_mode), target)
