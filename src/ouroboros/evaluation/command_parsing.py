"""Shared argv tokenization for mechanical command validation and reading."""

import os
import shlex

_WINDOWS = os.name == "nt"


def split_command(command: str) -> list[str]:
    """Split config syntax, not shell syntax; raise ValueError on bad quoting.

    POSIX keeps shlex's normal escaping. Windows accepts single/double quote
    groups but preserves path backslashes. Backslash-adjacent quotes are
    ambiguous (literal quotes vs. path separators), so reject them rather
    than silently change argv. Use forward slashes or omit a quoted path's
    trailing separator. This is not a cmd.exe or PowerShell command parser.
    """
    if not _WINDOWS:
        return shlex.split(command)
    if '\\"' in command or "\\'" in command:
        raise ValueError("Backslash-adjacent quotes are ambiguous in Windows mechanical commands")
    lexer = shlex.shlex(command, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    lexer.escape = ""
    return list(lexer)
