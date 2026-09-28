"""The project's own virtualenv, found from the checkout a command runs in.

A task worktree is a linked git worktree: it holds the tracked files only, so
a gitignored virtualenv (``.venv`` or ``venv``, the common layout) exists in
the main working tree and not in the worktree the worker edited. The commands
the controller runs for the project (the ``verify_command`` gate, transcript
replay, Stage 1 mechanical checks, and the check package's interpreter) must
resolve ``python`` and the project's tools through that virtualenv, so it is
found here, one way, for all of them: the checkout's own virtualenv first,
then the main working tree's when the checkout is a linked worktree.
"""

from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path
import sys

VENV_DIRECTORY_NAMES: tuple[str, ...] = (".venv", "venv")


def venv_python(venv: Path) -> Path | None:
    """The executable Python interpreter of the virtualenv at ``venv``, if any."""
    names = ("Scripts/python.exe",) if sys.platform == "win32" else ("bin/python3", "bin/python")
    for name in names:
        candidate = venv / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def main_worktree_root(checkout: Path) -> Path | None:
    """The main working tree of a linked git worktree, read from its ``.git`` file."""
    marker = checkout / ".git"
    try:
        if not marker.is_file():
            return None
        text = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    gitdir = Path(text.removeprefix("gitdir:").strip())
    if not gitdir.is_absolute():
        gitdir = (checkout / gitdir).resolve()
    # <main>/.git/worktrees/<name>
    if gitdir.parent.name == "worktrees" and gitdir.parent.parent.name == ".git":
        return gitdir.parent.parent.parent
    return None


def project_venv_python(checkout: Path) -> Path | None:
    """The interpreter of the project's virtualenv for ``checkout``, if one exists.

    The checkout's own virtualenv wins; a linked worktree without one uses the
    main working tree's.
    """
    roots = [checkout]
    main_root = main_worktree_root(checkout)
    if main_root is not None:
        roots.append(main_root)
    for root in roots:
        for name in VENV_DIRECTORY_NAMES:
            found = venv_python(root / name)
            if found is not None:
                return found
    return None


def project_venv_scripts(checkout: Path) -> Path | None:
    """The directory holding the project virtualenv's executables for ``checkout``."""
    python = project_venv_python(checkout)
    return python.parent if python is not None else None


def with_project_venv(env: Mapping[str, str], checkout: Path) -> dict[str, str]:
    """A copy of ``env`` in which the project's virtualenv resolves first.

    When ``checkout`` has a project virtualenv, its executables directory leads
    ``PATH`` and ``VIRTUAL_ENV`` names it, as an activated virtualenv does; the
    rest of ``env`` is unchanged. Without one, ``env`` is returned as a copy.
    """
    result = dict(env)
    python = project_venv_python(checkout)
    if python is None:
        return result
    scripts = python.parent
    result["VIRTUAL_ENV"] = str(scripts.parent)
    result["PATH"] = os.pathsep.join(filter(None, (str(scripts), env.get("PATH"))))
    return result


__all__ = [
    "VENV_DIRECTORY_NAMES",
    "main_worktree_root",
    "project_venv_python",
    "project_venv_scripts",
    "venv_python",
    "with_project_venv",
]
