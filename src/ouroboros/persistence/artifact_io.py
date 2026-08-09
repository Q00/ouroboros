"""Small bounded-I/O primitives shared by disposable artifact persistence."""

from __future__ import annotations

import os


def read_fd_bounded(file_fd: int, *, max_bytes: int) -> bytes:
    """Read at most one byte beyond a fixed limit from an open descriptor."""
    read_limit = max_bytes + 1
    payload = bytearray()
    while len(payload) < read_limit:
        chunk = os.read(file_fd, min(64 * 1024, read_limit - len(payload)))
        if not chunk:
            break
        payload.extend(chunk)
    return bytes(payload)


__all__ = ["read_fd_bounded"]
