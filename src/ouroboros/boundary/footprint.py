"""Which changed functions a process entered: the footprint behind the regression exemption.

A footprint is an observation of executed code, recorded by a hook the
controller writes into the process (never read from test or criterion text):

- ``changed_functions``: the functions the change touched, ``C``. Every
  changed or added non-test Python file of the candidate is diffed line by
  line against its base bytes (``difflib``), and a function is changed when a
  changed line range of the candidate side falls inside its ``ast`` range
  (decorators included). A function is keyed by ``(file, qualname, first
  line)``, the identity its code object carries (``co_filename`` relative to
  the checkout, ``co_qualname``, ``co_firstlineno``), never by a code object
  id: ids are reused once an object is freed.
- ``pytest_bootstrap``: the ``python -c`` program a regression run starts
  pytest with, carrying an in-memory plugin that records, per test, which
  functions of ``C`` the test entered (``sys.monitoring`` ``PY_START`` on
  Python 3.12 and later, ``sys.setprofile`` before), one JSON line per test.
- ``oracle_program``: the oracle harness wrapped so its target process
  records the functions of ``C`` its case entered, appended as they are first
  entered (a target is killed as soon as its case is decided, so nothing is
  left for exit).

Every record is written by a process the candidate's code runs in, so a
record is an observation the candidate could forge: it may only exempt a
regression (``base_regression.regressions_to_keep``), never accept anything,
and a missing or unreadable record exempts nothing. A file is always written
through a ``with`` block, so no handle outlives its write. Records are read
under a size cap and only keys of ``C`` are kept.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
import difflib
import json
import os
from pathlib import Path
from typing import Any

FunctionKey = tuple[str, str, int]
"""``(checkout-relative file, qualname, first line)`` of one function."""

_RECORD_LIMIT = 16 * 1024 * 1024


def changed_line_ranges(base_text: str | None, candidate_text: str) -> list[tuple[int, int]]:
    """1-based inclusive line ranges of ``candidate_text`` that differ from ``base_text``.

    An added file is one range over every line; a pure deletion marks the
    candidate lines on both sides of where the base lines were.
    """
    new = candidate_text.splitlines()
    if base_text is None:
        return [(1, max(len(new), 1))]
    matcher = difflib.SequenceMatcher(None, base_text.splitlines(), new, autojunk=False)
    ranges = []
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "insert"):
            ranges.append((j1 + 1, j2))
        elif tag == "delete":
            ranges.append((max(j1, 1), j1 + 1))
    return ranges


def function_ranges(text: str, relative: str) -> list[tuple[FunctionKey, int, int]]:
    """Every function of a module with its key and line range (decorators included)."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    out: list[tuple[FunctionKey, int, int]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualname = f"{prefix}{child.name}"
                first = min([child.lineno, *(item.lineno for item in child.decorator_list)])
                out.append(((relative, qualname, first), first, child.end_lineno or first))
                visit(child, f"{qualname}.<locals>.")
            elif isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")
            else:
                visit(child, prefix)

    visit(tree, "")
    return out


def changed_functions(
    base: Path, candidate: Path, paths: Iterable[str], added: Iterable[str] = ()
) -> frozenset[FunctionKey]:
    """``C``: the functions of ``paths`` (changed) and ``added`` whose range a changed line hits."""
    keys: set[FunctionKey] = set()
    new_files = set(added)
    for relative in sorted({*paths, *new_files}):
        if not relative.endswith(".py"):
            continue
        try:
            text = (candidate / relative).read_text(encoding="utf-8", errors="replace")
            before = (
                None
                if relative in new_files
                else (base / relative).read_text(encoding="utf-8", errors="replace")
            )
        except OSError:
            continue
        ranges = changed_line_ranges(before, text)
        for key, low, high in function_ranges(text, relative):
            if any(start <= high and end >= low for start, end in ranges):
                keys.add(key)
    return frozenset(keys)


def _keys_json(watched: Iterable[FunctionKey]) -> str:
    return json.dumps(sorted([list(key) for key in watched]))


# The hook both programs share. ``_fp_watch(root, keys)`` returns ``(start,
# entered)``: ``start(on_entry)`` begins recording and returns a restart
# function, ``entered`` is the set of keys entered since the last
# ``entered.clear()``. With ``sys.monitoring`` a location is disabled once
# seen, and re-enabled by ``restart_events``. An interpreter whose code
# objects carry no ``co_qualname`` (before 3.11) cannot match a key, so it
# records nothing at all: a footprint that is only empty because it could not
# be observed must never look like one that entered no changed function.
_HOOK = r"""
import json as _fp_json, os as _fp_os, sys as _fp_sys, threading as _fp_threading


def _fp_watch(root, keys):
    root = _fp_os.path.realpath(root)
    watched = {(_fp_os.path.join(root, f), q, line): (f, q, line) for f, q, line in keys}
    entered = set()
    files = {}
    listeners = []

    def hit(code):
        name = code.co_filename
        path = files.get(name)
        if path is None:
            path = files[name] = _fp_os.path.realpath(name)
        key = watched.get((path, code.co_qualname, code.co_firstlineno))
        if key is not None and key not in entered:
            entered.add(key)
            for listener in listeners:
                listener(key)

    def start(listener=None):
        if not hasattr(start.__code__, "co_qualname"):
            return None
        if listener is not None:
            listeners.append(listener)
        monitoring = getattr(_fp_sys, "monitoring", None)
        if monitoring is not None:
            for tool in range(6):
                if monitoring.get_tool(tool) is None:
                    monitoring.use_tool_id(tool, "ouroboros-footprint")

                    def on_start(code, offset):
                        hit(code)
                        return monitoring.DISABLE

                    monitoring.register_callback(tool, monitoring.events.PY_START, on_start)
                    monitoring.set_events(tool, monitoring.events.PY_START)
                    return monitoring.restart_events
        def profile(frame, event, arg):
            if event == "call":
                hit(frame.f_code)
        _fp_sys.setprofile(profile)
        _fp_threading.setprofile(profile)
        return lambda: None

    return start, entered


def _fp_append(path, payload):
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(_fp_json.dumps(payload) + "\n")
"""


def pytest_bootstrap(root: Path, watched: Iterable[FunctionKey], record: Path) -> str:
    """The ``python -c`` program of a regression run: pytest with the footprint plugin.

    ``sys.argv[1:]`` are pytest's arguments. For every test the plugin appends
    ``{"test": "<classname>::<name>", "entered": [[file, qualname, line], ...]}``
    to ``record``, naming the test as the JUnit report does. A test pytest
    never ran has no line, and so no footprint.
    """
    return (
        _HOOK
        + f"""
import pytest as _fp_pytest

_fp_start, _fp_entered = _fp_watch({str(root)!r}, {_keys_json(watched)})
_fp_restart = _fp_start()


class _FootprintPlugin:
    @_fp_pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item, nextitem):
        _fp_entered.clear()
        _fp_restart()
        yield
        try:
            from _pytest.junitxml import mangle_test_address

            names = mangle_test_address(item.nodeid)
            _fp_append(
                {str(record)!r},
                {{
                    "test": ".".join(names[:-1]) + "::" + names[-1],
                    "entered": sorted([list(key) for key in _fp_entered]),
                }},
            )
        except Exception:
            pass


_fp_plugins = [] if _fp_restart is None else [_FootprintPlugin()]
_fp_sys.exit(_fp_pytest.main(_fp_sys.argv[1:], plugins=_fp_plugins))
"""
    )


def oracle_program(harness: str, root: Path, watched: Iterable[FunctionKey], record: Path) -> str:
    """The harness program of an oracle target that records the changed functions it enters.

    Each key is appended to ``record`` the first time it is entered, so the
    record holds what ran before the controller ended the process. The
    harness itself runs unchanged, as ``__main__``, with its own arguments.
    """
    return (
        _HOOK
        + f"""
_fp_start, _fp_entered = _fp_watch({str(root)!r}, {_keys_json(watched)})
_fp_start(lambda key: _fp_append({str(record)!r}, list(key)))
exec(compile({harness!r}, "<string>", "exec"), {{"__name__": "__main__"}})
"""
    )


def _read_lines(record: Path) -> list[object]:
    try:
        if not record.is_file() or record.is_symlink() or record.stat().st_size > _RECORD_LIMIT:
            return []
        with open(record, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _key(value: object, watched: frozenset[FunctionKey]) -> FunctionKey | None:
    if not isinstance(value, list) or len(value) != 3:
        return None
    key = (value[0], value[1], value[2])
    return key if key in watched else None  # type: ignore[return-value]


def read_test_footprints(
    record: Path, watched: frozenset[FunctionKey]
) -> dict[str, frozenset[FunctionKey]]:
    """Per test id, the functions of ``watched`` it entered; a test without a line is absent."""
    footprints: dict[str, frozenset[FunctionKey]] = {}
    for line in _read_lines(record):
        if not isinstance(line, dict) or not isinstance(line.get("test"), str):
            continue
        entered = line.get("entered")
        keys = [_key(item, watched) for item in (entered if isinstance(entered, list) else ())]
        footprints[line["test"]] = frozenset(key for key in keys if key is not None)
    return footprints


def read_entered(record: Path, watched: frozenset[FunctionKey]) -> frozenset[FunctionKey]:
    """The functions of ``watched`` an oracle target recorded entering."""
    keys = (_key(line, watched) for line in _read_lines(record))
    return frozenset(key for key in keys if key is not None)


@dataclass
class OracleFootprint:
    """What the oracle checks of one verification entered of ``watched`` (``C``).

    Handed to the verification (``oracle_run.run_oracle_check``), which fills
    ``entered`` per check id; the caller keeps only the checks that passed.
    """

    watched: frozenset[FunctionKey]
    entered: dict[str, set[FunctionKey]] = field(default_factory=dict)

    def passed(self, oracle_results: Mapping[str, Any]) -> frozenset[FunctionKey] | None:
        """The functions the passing oracle checks entered; ``None`` when none passed.

        ``oracle_results`` maps a check id to its ``OracleResult`` on this
        candidate; a check passed when it ran cases and every one passed.
        """
        passing = [
            check_id
            for check_id, result in oracle_results.items()
            if result.cases and all(case.passed for case in result.cases)
        ]
        if not passing:
            return None
        return frozenset(key for check_id in passing for key in self.entered.get(check_id, ()))


def record_path(directory: Path, name: str) -> Path:
    """A fresh record path in ``directory`` (any file already there is removed first)."""
    path = directory / name
    if os.path.lexists(path):
        os.unlink(path)
    return path


__all__ = [
    "FunctionKey",
    "OracleFootprint",
    "changed_functions",
    "changed_line_ranges",
    "function_ranges",
    "oracle_program",
    "pytest_bootstrap",
    "read_entered",
    "read_test_footprints",
    "record_path",
]
