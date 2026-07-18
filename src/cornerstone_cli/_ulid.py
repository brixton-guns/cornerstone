"""ULID generation (spec §4: session ids sort lexicographically by creation time)."""

from __future__ import annotations

import os
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid(timestamp_ms: int | None = None) -> str:
    """Return a 26-character Crockford base32 ULID: 48-bit ms timestamp + 80-bit randomness."""
    if timestamp_ms is None:
        timestamp_ms = time.time_ns() // 1_000_000
    value = (timestamp_ms & (2**48 - 1)) << 80 | int.from_bytes(os.urandom(10), "big")
    return "".join(_CROCKFORD[(value >> (5 * shift)) & 31] for shift in range(25, -1, -1))
