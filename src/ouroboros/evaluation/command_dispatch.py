"""Platform adaptation for already-validated mechanical command arguments.

This is not a command validator or a shell parser. Repository command validation
still happens before this boundary; native executable arguments are not rewritten.
"""

from collections.abc import Mapping
import ntpath
import os
import shutil
import sys

_WINDOWS = sys.platform == "win32"
_BATCH_METACHARACTERS = frozenset('&|<>^%!"()')


def prepare_command(command: tuple[str, ...], env: Mapping[str, str]) -> tuple[str, ...]:
    """Resolve Windows PATH shims without introducing shell interpretation.

    Looking up a fully qualified candidate avoids shutil.which's implicit CWD
    search on Windows. Empty/relative PATH entries do not expand this lookup's
    trust boundary. Explicit executable paths and POSIX dispatch stay unchanged.
    If no candidate exists, retain the native spawn's existing not-found behavior.
    """
    if not _WINDOWS or not command:
        return command
    executable, *arguments = command
    if not ntpath.dirname(executable) and not ntpath.splitdrive(executable)[0]:
        for directory in env.get("PATH", os.defpath).split(os.pathsep):
            directory = directory.strip('"')
            if not ntpath.isabs(directory) or not ntpath.splitdrive(directory)[0]:
                continue
            candidate = shutil.which(ntpath.join(directory, executable))
            if candidate:
                executable = candidate
                break
    prepared = (executable, *arguments)
    if ntpath.splitext(executable)[1].lower() in {".cmd", ".bat"}:
        # Windows can interpret batch files even with shell=False. Do not let
        # command-line quoting/expansion reinterpret paths or user arguments.
        # Unsupported values fail explicitly, rather than falling back to a
        # shell or reporting an unexecuted check as skipped/passed.
        for value in prepared:
            if any(char in _BATCH_METACHARACTERS or ord(char) < 32 for char in value) or (
                " " in value and value.endswith("\\")
            ):
                raise ValueError("Unsafe Windows batch command argument; use a native executable")
    return prepared
