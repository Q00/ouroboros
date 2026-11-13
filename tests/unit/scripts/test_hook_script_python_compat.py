"""Hook scripts stay runnable on Python 3.9, the oldest ``python3`` they meet.

The package needs Python 3.12, but the plugin hook commands (``hooks/hooks.json``,
``.claude/settings.json``, ``.codex/hooks.json``) start their scripts with the
first ``python3`` (or ``python``) on the host PATH. On macOS that is usually the
system ``/usr/bin/python3``, Python 3.9. CI runs Python 3.12 only, so most of
these checks are static: every hook-executed script, and every sibling script it
loads, must parse with the 3.9 grammar and hold no ``X | Y`` union that Python
3.9 would evaluate.

``ast.parse(..., feature_version=(3, 9))`` is best effort in CPython: it rejects
``match``, ``except*`` and type parameters, but accepts PEP 701 f-strings and
starred subscripts. The last test closes that gap where a real 3.9 interpreter is
found, by running each hook command under it; it is skipped otherwise.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
_HOOKS_PATH = _REPO_ROOT / "hooks" / "hooks.json"
_HOOK_MANIFESTS = (
    _HOOKS_PATH,
    _REPO_ROOT / ".claude" / "settings.json",
    _REPO_ROOT / ".codex" / "hooks.json",
)
_FLOOR = (3, 9)
_SCRIPT_REFERENCE = re.compile(r"scripts/([A-Za-z0-9_.-]+\.py)")
_BUILTIN_TYPES = frozenset(
    {
        "bool",
        "bytearray",
        "bytes",
        "complex",
        "dict",
        "float",
        "frozenset",
        "int",
        "list",
        "object",
        "set",
        "str",
        "tuple",
        "type",
    }
)


def _hook_commands(manifest_path: Path) -> list[str]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return [
        hook[field]
        for entries in manifest["hooks"].values()
        for entry in entries
        for hook in entry["hooks"]
        for field in ("command", "commandWindows")
        if field in hook
    ]


def _hook_executed_scripts() -> list[str]:
    """Scripts the hook manifests run, plus sibling scripts those load by file name."""
    found = {
        name
        for manifest_path in _HOOK_MANIFESTS
        for command in _hook_commands(manifest_path)
        for name in _SCRIPT_REFERENCE.findall(command)
    }
    pending = sorted(found)
    while pending:
        tree = ast.parse((_SCRIPTS_DIR / pending.pop()).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.endswith(".py")
                and (_SCRIPTS_DIR / node.value).is_file()
                and node.value not in found
            ):
                found.add(node.value)
                pending.append(node.value)
    return sorted(found)


_HOOK_SCRIPTS = _hook_executed_scripts()


def _has_future_annotations(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body
    )


def _annotations(tree: ast.Module) -> list[ast.expr]:
    """Every annotation in the module.

    Local variable annotations are never evaluated, but they are included: the
    rule stays simple and the remedy is the same one-line ``__future__`` import.
    """
    found: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            arguments = node.args
            for arg in (
                *arguments.posonlyargs,
                *arguments.args,
                *arguments.kwonlyargs,
                *(a for a in (arguments.vararg, arguments.kwarg) if a is not None),
            ):
                if arg.annotation is not None:
                    found.append(arg.annotation)
            if node.returns is not None:
                found.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            found.append(node.annotation)
    return found


def _is_union(node: ast.AST) -> bool:
    return isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr)


def _looks_like_type(node: ast.expr) -> bool:
    """An operand only a type union would have: ``None``, a builtin type, a generic."""
    if isinstance(node, ast.Constant):
        return node.value is None
    if isinstance(node, ast.Name):
        return node.id in _BUILTIN_TYPES
    return isinstance(node, ast.Subscript)


def _python_39_union_problems(source: str) -> list[str]:
    """``X | Y`` unions that Python 3.9 would evaluate and reject with ``TypeError``.

    An annotation union is a problem unless the module imports
    ``from __future__ import annotations``. Outside annotations, which that import
    does not affect, a union is always a problem in the type argument of
    ``isinstance``/``issubclass``, and wherever an operand is ``None``, a builtin
    type or a subscripted generic (``Alias = str | None``). Unions of other names,
    such as ``re.IGNORECASE | re.MULTILINE`` or two sets, are left alone.
    """
    tree = ast.parse(source)
    classified: set[int] = set()
    problems: list[tuple[int, int, str]] = []

    def report(node: ast.expr, kind: str) -> None:
        problems.append(
            (node.lineno, node.col_offset, f"{kind} `{ast.unparse(node)}` at line {node.lineno}")
        )

    future = _has_future_annotations(tree)
    for annotation in _annotations(tree):
        for node in ast.walk(annotation):
            classified.add(id(node))
            if _is_union(node) and not future:
                report(node, "annotation")
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"isinstance", "issubclass"}
            and len(node.args) == 2
        ):
            for inner in ast.walk(node.args[1]):
                if _is_union(inner):
                    classified.add(id(inner))
                    report(inner, f"{node.func.id} type")
    for node in ast.walk(tree):
        if (
            _is_union(node)
            and id(node) not in classified
            and (_looks_like_type(node.left) or _looks_like_type(node.right))
        ):
            report(node, "runtime union")
    return [message for _line, _column, message in sorted(problems)]


def test_hook_script_set_is_derived_from_the_manifests() -> None:
    """The derivation finds every entry point and the checker session-start loads."""
    assert {
        "drift-monitor.py",
        "keyword-detector.py",
        "session-start.py",
        "version-check.py",
    } <= set(_HOOK_SCRIPTS)


@pytest.mark.parametrize("name", _HOOK_SCRIPTS)
def test_hook_script_parses_with_the_python_39_grammar(name: str) -> None:
    ast.parse((_SCRIPTS_DIR / name).read_text(encoding="utf-8"), feature_version=_FLOOR)


@pytest.mark.parametrize("name", _HOOK_SCRIPTS)
def test_hook_script_has_no_union_python_39_evaluates(name: str) -> None:
    assert _python_39_union_problems((_SCRIPTS_DIR / name).read_text(encoding="utf-8")) == []


def test_the_python_39_grammar_check_rejects_newer_statements() -> None:
    for source in (
        "match x:\n    case 1:\n        pass\n",
        "try:\n    pass\nexcept* ValueError:\n    pass\n",
        "type Alias = int\n",
    ):
        with pytest.raises(SyntaxError):
            ast.parse(source, feature_version=_FLOOR)


def test_the_union_check_sees_each_form() -> None:
    unguarded = (
        "import re\n"
        "def get_installed_version() -> str | None:\n"
        "    held: dict | None = None\n"
        "def f(a: int | None = None, *rest: tuple[int, ...] | None) -> None: ...\n"
        "class C:\n"
        "    field: list[int] | None\n"
        "Alias = str | None\n"
        "ok = isinstance(1, int | str)\n"
        "flags = re.IGNORECASE | re.MULTILINE\n"
        "merged = {1} | {2}\n"
    )
    assert _python_39_union_problems(unguarded) == [
        "annotation `str | None` at line 2",
        "annotation `dict | None` at line 3",
        "annotation `int | None` at line 4",
        "annotation `tuple[int, ...] | None` at line 4",
        "annotation `list[int] | None` at line 6",
        "runtime union `str | None` at line 7",
        "isinstance type `int | str` at line 8",
    ]
    guarded = "from __future__ import annotations\n" + unguarded
    assert _python_39_union_problems(guarded) == [
        "runtime union `str | None` at line 8",
        "isinstance type `int | str` at line 9",
    ]


def _python_version(executable: str) -> tuple[int, int] | None:
    try:
        probe = subprocess.run(
            [executable, "-c", "import sys; print('%d %d' % sys.version_info[:2])"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    parts = probe.stdout.split()
    if probe.returncode != 0 or len(parts) != 2:
        return None
    return int(parts[0]), int(parts[1])


@pytest.fixture(scope="module")
def floor_python() -> str:
    """A real Python 3.9, or skip.

    The macOS ``/usr/bin/python3`` shim opens an installer dialog when the
    Command Line Tools are missing, so the binary it would start is probed
    instead.
    """
    if os.name == "nt":
        pytest.skip("hook commands are POSIX shell here")
    candidates = [
        shutil.which("python3.9"),
        "/Library/Developer/CommandLineTools/usr/bin/python3",
        None if sys.platform == "darwin" else "/usr/bin/python3",
    ]
    for candidate in dict.fromkeys(c for c in candidates if c and Path(c).is_file()):
        if _python_version(candidate) == _FLOOR:
            return candidate
    pytest.skip("no Python 3.9 interpreter found")


def _hook_command(event_name: str) -> str:
    manifest = json.loads(_HOOKS_PATH.read_text(encoding="utf-8"))
    (entry,) = manifest["hooks"][event_name]
    (hook,) = entry["hooks"]
    return hook["command"]


@pytest.mark.parametrize(
    ("event_name", "payload"),
    [
        ("SessionStart", {"hook_event_name": "SessionStart", "session_id": "s"}),
        (
            "UserPromptSubmit",
            {"hook_event_name": "UserPromptSubmit", "session_id": "s", "prompt": "ooo help"},
        ),
        ("PostToolUse", {"hook_event_name": "PostToolUse", "tool_name": "Write"}),
    ],
)
def test_hook_command_runs_cleanly_under_python_39(
    floor_python: str, event_name: str, payload: dict, tmp_path: Path
) -> None:
    """The real hook command, with ``python3`` on PATH resolving to Python 3.9.

    The wrappers fail open, and session-start.py reports a failed update check
    only on stderr, so exit code 0 alone proves nothing: stderr must be empty and
    stdout must carry the script's normal output.
    """
    home = tmp_path / "home"
    (home / ".ouroboros").mkdir(parents=True)
    now = time.time()
    # A fresh cache that advertises a newer release on both channels: the update
    # notice path runs, including the locked cache write, without the network.
    (home / ".ouroboros" / "version-check-cache.json").write_text(
        json.dumps(
            {
                "latest_version": "999.0.0",
                "latest_version_checked_at": now,
                "latest_version_pre": "999.0.0",
                "latest_version_pre_checked_at": now,
                "timestamp": now,
            }
        ),
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python3").symlink_to(floor_python)
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"}
    }
    env.update(
        HOME=str(home),
        PATH=f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
        PLUGIN_ROOT=str(_REPO_ROOT),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONIOENCODING="utf-8",
        # Fail fast instead of reaching PyPI if the cache were ever bypassed.
        HTTPS_PROXY="http://127.0.0.1:9",
        https_proxy="http://127.0.0.1:9",
    )

    result = subprocess.run(
        _hook_command(event_name),
        shell=True,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    if event_name == "SessionStart":
        assert result.stdout.startswith("Ouroboros update available: v")
        assert "→ v999.0.0." in result.stdout
    elif event_name == "UserPromptSubmit":
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    else:
        assert result.stdout == "Success\n"
