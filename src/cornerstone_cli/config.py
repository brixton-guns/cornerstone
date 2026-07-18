"""Optional workspace configuration: .stone/config.toml (spec §16)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass

DEFAULT_IGNORE = (".stone/", ".git/", "node_modules/", "__pycache__/", ".venv/", "venv/")
DEFAULT_CAPTURE_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB per output file (§12)

_GLOB_CHARS = set("*?[")
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)


class ConfigError(Exception):
    """The workspace configuration is unreadable or invalid."""


@dataclass(frozen=True)
class Config:
    ignore: tuple[str, ...]
    capture_max_bytes: int


def load_config(stone_fd: int) -> Config:
    """Read config.toml relative to the held .stone descriptor, never following symlinks."""
    extra_ignore: tuple[str, ...] = ()
    capture_max_bytes = DEFAULT_CAPTURE_MAX_BYTES

    try:
        fd = os.open("config.toml", _READ_FLAGS, dir_fd=stone_fd)
    except FileNotFoundError:
        fd = None
    except OSError as exc:
        raise ConfigError(f"cannot read .stone/config.toml: {exc}") from exc

    if fd is not None:
        try:
            with os.fdopen(fd, "rb") as fh:
                data = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"invalid configuration (.stone/config.toml): {exc}") from exc

        ignore_value = data.pop("ignore", [])
        if not isinstance(ignore_value, list) or not all(isinstance(item, str) for item in ignore_value):
            raise ConfigError("`ignore` must be a list of path prefixes")
        for item in ignore_value:
            if not item or item.startswith("/"):
                raise ConfigError(f"`ignore` entries must be non-empty paths relative to the root: {item!r}")
            if _GLOB_CHARS & set(item):
                raise ConfigError(f"`ignore` entries are plain prefixes, globs are not supported: {item!r}")
        extra_ignore = tuple(ignore_value)

        capture_value = data.pop("capture_max_bytes", capture_max_bytes)
        if not isinstance(capture_value, int) or isinstance(capture_value, bool) or capture_value <= 0:
            raise ConfigError("`capture_max_bytes` must be a positive integer")
        capture_max_bytes = capture_value

        if data:
            raise ConfigError(f"unknown configuration keys: {', '.join(sorted(data))}")

    return Config(DEFAULT_IGNORE + extra_ignore, capture_max_bytes)
