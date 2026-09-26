"""Process environment and interpreter for model-written check scripts.

Admission and candidate verification execute Python scripts that a model
wrote. They always run on a throwaway copy of the checkout, without a shell,
under the per-check timeout (``boundary/admission.py``). This module adds two
product-path rules:

- **Scrubbed environment.** Only an allowlist of variables needed to run
  Python and the project's tooling reaches the script (``PATH``, ``HOME``,
  locale, temp directories, virtualenv markers, and the Windows system
  variables). Everything else, including every credential-like variable
  (``*_API_KEY``, ``*_TOKEN``, ``AWS_*``, ``GH_*``, ``OPENAI_*``, ...), is
  dropped.
- **Project interpreter.** A check's ``python3``/``python`` runs with the
  project's virtualenv interpreter when one is found (the checkout's, or the
  main working tree's when the checkout is a linked git worktree, then an
  active ``VIRTUAL_ENV``), else ``python3`` from ``PATH``. The choice is
  recorded in the admission and verification receipts.

Residual risk, not addressed here: there is no OS sandbox. A check can still
read files the user can read (for example under ``HOME``), write outside its
copy, and use the network. Opt out with ``--no-check-package``,
``OUROBOROS_CHECK_PACKAGE=off``, or ``boundary.check_package: off``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import sys

ALLOWED_CHECK_ENV = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LANGUAGE",
        "TERM",
        "TZ",
        "TMPDIR",
        "TMP",
        "TEMP",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        "CONDA_DEFAULT_ENV",
        "PYTHONPATH",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONHASHSEED",
        "SOURCE_DATE_EPOCH",
        # Windows needs these to start any process and to locate temp dirs.
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "USERPROFILE",
        "LOCALAPPDATA",
        "APPDATA",
        "PROGRAMDATA",
    }
)
ALLOWED_CHECK_ENV_PREFIXES = ("LC_",)
CHECK_INTERPRETER_NAMES = frozenset({"python3", "python"})


def scrubbed_check_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return only the allowlisted variables of ``environ`` (default: this process)."""
    source = os.environ if environ is None else environ
    return {
        key: value
        for key, value in source.items()
        if key.upper() in ALLOWED_CHECK_ENV or key.upper().startswith(ALLOWED_CHECK_ENV_PREFIXES)
    }


@dataclass(frozen=True, slots=True)
class CheckInterpreter:
    """The interpreter that runs check scripts, and why it was chosen."""

    path: str
    source: str
    """``project_venv``, ``active_venv``, or ``python3_fallback``."""


def _python_in_venv(venv: Path) -> Path | None:
    names = ("Scripts/python.exe",) if sys.platform == "win32" else ("bin/python3", "bin/python")
    for name in names:
        candidate = venv / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _venv_python(root: Path) -> Path | None:
    for venv in (".venv", "venv"):
        found = _python_in_venv(root / venv)
        if found is not None:
            return found
    return None


def _main_worktree_root(checkout: Path) -> Path | None:
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


def resolve_check_interpreter(
    checkout: Path, environ: Mapping[str, str] | None = None
) -> CheckInterpreter:
    """Pick the interpreter for ``python3``/``python`` in a check's argv."""
    roots = [checkout]
    main_root = _main_worktree_root(checkout)
    if main_root is not None:
        roots.append(main_root)
    for root in roots:
        found = _venv_python(root)
        if found is not None:
            return CheckInterpreter(str(found), "project_venv")
    source = os.environ if environ is None else environ
    active = source.get("VIRTUAL_ENV", "").strip()
    if active:
        found = _python_in_venv(Path(active))
        if found is not None:
            return CheckInterpreter(str(found), "active_venv")
    return CheckInterpreter(shutil.which("python3") or "python3", "python3_fallback")


__all__ = [
    "ALLOWED_CHECK_ENV",
    "ALLOWED_CHECK_ENV_PREFIXES",
    "CHECK_INTERPRETER_NAMES",
    "CheckInterpreter",
    "resolve_check_interpreter",
    "scrubbed_check_environment",
]
