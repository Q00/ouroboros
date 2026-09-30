"""Start the pinned check interpreter from the very binary that was pinned.

Run as a standalone script by the controller's own interpreter, never
imported (``boundary/check_env.py``, ``check_command``):

    python -I -S -B _pinned_exec.py <path> <realpath> <sha256> <channel> -- <arg>...

``path`` is the pinned interpreter (a virtualenv's ``bin/python3``),
``realpath`` the binary it resolved to and ``sha256`` that binary's digest
when it was pinned; ``channel`` is the write end of a pipe the controller
reads. The binary is opened once, from its real path, and must still be a
regular file with the pinned digest, reached from ``path``. Then ``verified``
(or ``changed``) is written to the channel, which is closed before anything
else runs, and the command is executed as ``path <arg>...``: ``argv[0]`` is
the virtualenv's path, so the interpreter finds that virtualenv's
``pyvenv.cfg`` as if it had been started through ``path``.

Where the platform executes a file descriptor (``fexecve``: Linux), the
process becomes the binary that was verified; no name is looked up again.
Elsewhere (macOS), and for an interpreter that is a ``#!`` script wrapper
(which, run from a descriptor, would see ``/dev/fd/<n>`` as its ``$0``), the
real path is executed right after the check, and the residual window is
between that check and ``execve``. It depends on nothing
but the standard library, so it starts with ``-S`` and ``-I``.
"""

from __future__ import annotations

import hashlib
import os
import stat
import sys

_CHUNK = 1024 * 1024
REFUSED = 126


def _verified(path: str, real: str, digest: str) -> int | None:
    """A descriptor of the pinned binary, or ``None`` when ``path`` no longer leads to it."""
    try:
        if os.path.realpath(path) != real:
            return None
        descriptor = os.open(real, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("not a regular file")
        sha = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, _CHUNK)
            if not chunk:
                break
            sha.update(chunk)
        if sha.hexdigest() != digest:
            raise OSError("not the pinned binary")
    except OSError:
        os.close(descriptor)
        return None
    return descriptor


def main() -> None:
    arguments = sys.argv[1:]
    separator = arguments.index("--")
    path, real, digest, channel = arguments[:separator]
    command = [path, *arguments[separator + 1 :]]
    binary = _verified(path, real, digest)
    os.write(int(channel), b"verified" if binary is not None else b"changed")
    os.close(int(channel))
    if binary is None:
        os._exit(REFUSED)
    if os.execve in os.supports_fd and os.pread(binary, 2, 0) != b"#!":
        os.execve(binary, command, os.environ)
    os.close(binary)
    os.execve(real, command, os.environ)


if __name__ == "__main__":
    main()
