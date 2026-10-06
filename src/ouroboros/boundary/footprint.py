"""Which changed functions a process entered: the footprint behind the regression exemption.

A footprint is an observation of executed code, recorded by a hook the
controller writes into the process (never read from test or criterion text):

- ``changed_code``: the functions the change touched, ``C``, and whether it
  also changed code outside every function. Every changed or added non-test
  Python file of the candidate is diffed line by line against its base bytes
  (``difflib``). A function is changed when a changed line of the candidate
  falls inside its ``ast`` range (decorators included), or a removed base line
  falls inside a base function the candidate still defines. Any other changed
  line that is code (not blank, not only a comment) is outside every function:
  module or class level code, a deleted function, a removed import. Such a
  change has no function footprint. A function is keyed by ``(file,
  qualname, first line)``, the identity its code object carries
  (``co_filename`` relative to the checkout, ``co_qualname``,
  ``co_firstlineno``), never by a code object id: ids are reused once an
  object is freed. Only regular files under ``SOURCE_LIMIT`` bytes and
  ``DIFF_LINE_LIMIT`` lines are read; anything else counts as a change
  outside every function.
- ``pytest_bootstrap``: the ``python -c`` program every controller pytest run
  starts with. It puts the checkout first on ``sys.path`` exactly as
  ``python -m pytest`` does, and, given a plan, installs an in-memory plugin
  that records per test its node id and which functions of ``C`` it entered
  (``sys.monitoring`` ``PY_START`` on Python 3.12 and later,
  ``sys.setprofile`` before), and at the end where each changed module was
  imported from, at exit (``provenance``).
- ``oracle_program``: the oracle harness wrapped so its target process
  records the functions of ``C`` its case entered, appended as they are first
  entered (a target is killed as soon as its case is decided, so nothing is
  left for exit).

A plan (the watched keys and where to record) reaches a process as a file in
its scratch directory (``TMPDIR``), never in its argv; a pytest run deletes
its plan before any test code is imported. Every record is written by a
process the candidate's code runs in, so a record is an observation the
candidate could forge: it may only exempt a regression
(``base_regression.regressions_to_keep``), never accept anything, and a
missing, unreadable or ambiguous record exempts nothing. The hook never
raises into the code it watches, a file is always written through a ``with``
block so no handle outlives its write, and records are read under a size cap
keeping only keys of ``C``.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
import difflib
import json
import os
from pathlib import Path
import stat
from typing import Any

FunctionKey = tuple[str, str, int]
"""``(checkout-relative file, qualname, first line)`` of one function."""

PYTEST_PLAN = "ouroboros-pytest-plan.json"
"""The plan file a pytest bootstrap reads from its scratch directory (and deletes)."""
ORACLE_PLAN = "ouroboros-oracle-plan.json"
"""The plan file an oracle target reads from its check's scratch directory."""
SOURCE_LIMIT = 2 * 1024 * 1024
DIFF_LINE_LIMIT = 50_000
_RECORD_LIMIT = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ChangedCode:
    """What a change touched: ``C``, and whether it changed code outside every function."""

    functions: frozenset[FunctionKey] = frozenset()
    outside_functions: bool = False


def read_source(path: Path) -> str | None:
    """A regular file's text under ``SOURCE_LIMIT``, never through a link; else ``None``."""
    try:
        status = os.lstat(path)
        if not stat.S_ISREG(status.st_mode) or status.st_size > SOURCE_LIMIT:
            return None
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            data = handle.read(SOURCE_LIMIT + 1)
    except OSError:
        return None
    if len(data) > SOURCE_LIMIT:
        return None
    return data.decode("utf-8", errors="replace")


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


def _code_lines(lines: Sequence[str], start: int, end: int) -> list[int]:
    """1-based numbers of the lines in ``[start, end)`` (0-based) that are code."""
    return [
        number + 1
        for number in range(start, end)
        if lines[number].strip() and not lines[number].lstrip().startswith("#")
    ]


def _innermost(ranges: Sequence[tuple[FunctionKey, int, int]], line: int) -> FunctionKey | None:
    inside = [(high - low, key) for key, low, high in ranges if low <= line <= high]
    return min(inside)[1] if inside else None


def _changed_in_file(
    relative: str, before: str | None, after: str
) -> tuple[set[FunctionKey], bool]:
    """The changed functions of one file, and whether a changed code line is outside them all."""
    new = after.splitlines()
    old = [] if before is None else before.splitlines()
    if len(new) > DIFF_LINE_LIMIT or len(old) > DIFF_LINE_LIMIT:
        return set(), True
    new_ranges = function_ranges(after, relative)
    old_ranges = function_ranges(before, relative) if before is not None else []
    by_qualname = {key[1]: key for key, _low, _high in new_ranges}
    functions: set[FunctionKey] = set()
    outside = False
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        for line in _code_lines(new, j1, j2):
            key = _innermost(new_ranges, line)
            if key is None:
                outside = True
            else:
                functions.add(key)
        for line in _code_lines(old, i1, i2):
            old_key = _innermost(old_ranges, line)
            kept = by_qualname.get(old_key[1]) if old_key is not None else None
            if kept is None:
                # Module or class level code, or a function the candidate deleted.
                outside = True
            else:
                functions.add(kept)
    return functions, outside


def changed_code(
    base: Path,
    candidate: Path,
    paths: Iterable[str],
    added: Iterable[str] = (),
) -> ChangedCode:
    """``C`` over ``paths`` (changed) and ``added``, and whether code outside functions changed.

    A file that cannot be read safely (a link, not a regular file, too large)
    is a change outside every function: nothing can be said about it.
    """
    keys: set[FunctionKey] = set()
    outside = False
    new_files = set(added)
    for relative in sorted({*paths, *new_files}):
        if not relative.endswith(".py"):
            continue
        after = read_source(candidate / relative)
        before = None if relative in new_files else read_source(base / relative)
        if after is None or (before is None and relative not in new_files):
            outside = True
            continue
        functions, outside_here = _changed_in_file(relative, before, after)
        keys |= functions
        outside = outside or outside_here
    return ChangedCode(frozenset(keys), outside)


# The hook both programs share. ``_fp_watch(root, keys)`` returns ``(start,
# entered)``: ``start(on_entry)`` begins recording and returns a restart
# function, ``entered`` is the set of keys entered since the last
# ``entered.clear()``. With ``sys.monitoring`` a location is disabled once
# seen, and re-enabled by ``restart_events``. An interpreter whose code
# objects carry no ``co_qualname`` (before 3.11) cannot match a key, so it
# records nothing at all: a footprint that is only empty because it could not
# be observed must never look like one that entered no changed function.
# Nothing here may raise into the code being watched.
_HOOK = r"""
import json as _fp_json, os as _fp_os, sys as _fp_sys, tempfile as _fp_tempfile
import threading as _fp_threading


def _fp_watch(root, keys):
    root = _fp_os.path.realpath(root)
    watched = {(_fp_os.path.join(root, f), q, line): (f, q, line) for f, q, line in keys}
    entered = set()
    files = {}
    listeners = []

    def hit(code):
        try:
            name = code.co_filename
            path = files.get(name)
            if path is None:
                path = files[name] = _fp_os.path.realpath(name)
            key = watched.get((path, code.co_qualname, code.co_firstlineno))
            if key is not None and key not in entered:
                entered.add(key)
                for listener in listeners:
                    listener(key)
        except Exception:
            pass

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
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(_fp_json.dumps(payload) + "\n")
    except Exception:
        pass


def _fp_plan(name, remove):
    try:
        path = _fp_os.path.join(_fp_tempfile.gettempdir(), name)
        with open(path, encoding="utf-8") as handle:
            plan = _fp_json.load(handle)
        if remove:
            _fp_os.unlink(path)
        return plan
    except Exception:
        return None
"""

PYTEST_BOOTSTRAP = (
    _HOOK
    + f"""
# ``python -m pytest`` puts the absolute working directory first on the path;
# ``python -c`` puts ``''``, which follows a later ``chdir``.
_fp_sys.path[0] = _fp_os.getcwd()
import pytest as _fp_pytest

_fp_plan_data = _fp_plan({PYTEST_PLAN!r}, True)


class _FootprintPlugin:
    def __init__(self, plan):
        self.record = plan["record"]
        self.root = _fp_os.path.realpath(plan["root"])
        self.modules = list(plan.get("modules") or ())
        self.start, self.entered = _fp_watch(plan["root"], plan.get("watched") or ())
        self.restart = self.start() if plan.get("watched") else None
        # At exit, not at session end: a run that dies loading a conftest
        # still reports where it imported the changed modules from.
        import atexit

        atexit.register(self.provenance)

    @_fp_pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item, nextitem):
        self.entered.clear()
        if self.restart is not None:
            self.restart()
        yield
        try:
            from _pytest.junitxml import mangle_test_address

            names = mangle_test_address(item.nodeid)
            _fp_append(
                self.record,
                {{
                    "test": ".".join(names[:-1]) + "::" + names[-1],
                    "nodeid": item.nodeid,
                    "entered": (
                        None
                        if self.restart is None
                        else sorted([list(key) for key in self.entered])
                    ),
                }},
            )
        except Exception:
            pass

    def provenance(self):
        inside = {{}}
        for name in self.modules:
            module = _fp_sys.modules.get(name)
            path = getattr(module, "__file__", None) if module is not None else None
            if path:
                real = _fp_os.path.realpath(path)
                inside[name] = real.startswith(self.root + _fp_os.sep)
        _fp_append(self.record, {{"provenance": inside}})


_fp_plugins = [] if _fp_plan_data is None else [_FootprintPlugin(_fp_plan_data)]
_fp_sys.exit(_fp_pytest.main(_fp_sys.argv[1:], plugins=_fp_plugins))
"""
)
"""The program of every controller pytest run (``sys.argv[1:]`` are pytest's arguments)."""


def pytest_plan(
    root: Path, record: Path, watched: Iterable[FunctionKey] | None, modules: Iterable[str]
) -> dict[str, Any]:
    """The plan a pytest bootstrap reads: where to record, what to watch, which modules to place.

    ``watched`` ``None`` records no footprint (the base runs); a test's
    ``entered`` is then ``null``.
    """
    return {
        "root": str(root),
        "record": str(record),
        "watched": None if watched is None else sorted([list(key) for key in watched]),
        "modules": sorted(modules),
    }


def oracle_program(harness: str) -> str:
    """The harness program of an oracle target that records the changed functions it enters.

    The plan (``ORACLE_PLAN`` in the check's scratch directory) names the
    checkout root, the watched keys and the record; each key is appended the
    first time it is entered, so the record holds what ran before the
    controller ended the process. Without a readable plan nothing is
    recorded. The harness itself always runs unchanged, as ``__main__``,
    with its own arguments.
    """
    return (
        _HOOK
        + f"""
try:
    _fp_plan_data = _fp_plan({ORACLE_PLAN!r}, False)
    if _fp_plan_data is not None:
        _fp_start, _fp_entered = _fp_watch(_fp_plan_data["root"], _fp_plan_data["watched"])
        _fp_record = _fp_plan_data["record"]
        _fp_start(lambda key: _fp_append(_fp_record, list(key)))
except Exception:
    pass
exec(compile({harness!r}, "<string>", "exec"), {{"__name__": "__main__"}})
"""
    )


def write_plan(path: Path, plan: Mapping[str, Any]) -> bool:
    """Write a plan file for a process to read; ``False`` when it cannot be written."""
    try:
        with open(path, "x", encoding="utf-8") as handle:
            json.dump(plan, handle)
    except (OSError, TypeError, ValueError):
        return False
    return True


def _read_lines(record: Path) -> list[object]:
    try:
        status = os.lstat(record)
        if not stat.S_ISREG(status.st_mode) or status.st_size > _RECORD_LIMIT:
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


@dataclass(frozen=True, slots=True)
class RunRecord:
    """What a pytest bootstrap recorded for one run."""

    footprints: Mapping[str, frozenset[FunctionKey]] = field(default_factory=dict)
    """Per test id, the watched functions it entered; absent: no footprint."""
    nodeids: Mapping[str, str] = field(default_factory=dict)
    """Per test id, the node id that selects it again."""
    imported_outside: bool = False
    """A changed module was imported from outside the checkout copy."""


def read_run_record(record: Path, watched: frozenset[FunctionKey] | None) -> RunRecord:
    """A bootstrap's record; a test id recorded twice has neither footprint nor node id."""
    footprints: dict[str, frozenset[FunctionKey]] = {}
    nodeids: dict[str, str] = {}
    seen: set[str] = set()
    ambiguous: set[str] = set()
    outside = False
    for line in _read_lines(record):
        if not isinstance(line, dict):
            continue
        provenance = line.get("provenance")
        if isinstance(provenance, dict):
            outside = outside or any(value is False for value in provenance.values())
            continue
        test, nodeid = line.get("test"), line.get("nodeid")
        if not isinstance(test, str) or not isinstance(nodeid, str):
            continue
        if test in seen:
            ambiguous.add(test)
            continue
        seen.add(test)
        nodeids[test] = nodeid
        entered = line.get("entered")
        if watched is not None and isinstance(entered, list):
            keys = [_key(item, watched) for item in entered]
            footprints[test] = frozenset(key for key in keys if key is not None)
    for test in ambiguous:
        footprints.pop(test, None)
        nodeids.pop(test, None)
    return RunRecord(footprints, nodeids, outside)


def read_entered(record: Path, watched: frozenset[FunctionKey]) -> frozenset[FunctionKey]:
    """The functions of ``watched`` an oracle target recorded entering."""
    keys = (_key(line, watched) for line in _read_lines(record))
    return frozenset(key for key in keys if key is not None)


@dataclass
class OracleFootprint:
    """What the oracle checks of one verification entered of ``watched`` (``C``).

    Handed to the verification (``oracle_run.run_oracle_check``), which fills
    ``entered`` per check id, or marks ``unrecorded`` a check whose recorder
    could not be set up; the caller keeps only the checks that passed.
    """

    watched: frozenset[FunctionKey]
    entered: dict[str, set[FunctionKey]] = field(default_factory=dict)
    unrecorded: set[str] = field(default_factory=set)

    def passed(self, oracle_results: Mapping[str, Any]) -> frozenset[FunctionKey] | None:
        """The functions the passing oracle checks entered; ``None`` when none passed.

        ``oracle_results`` maps a check id to its ``OracleResult`` on this
        candidate; a check passed when it ran cases and every one passed. A
        passing check whose recorder failed makes the whole footprint
        unknown (``None``): it may exempt nothing.
        """
        passing = [
            check_id
            for check_id, result in oracle_results.items()
            if result.cases and all(case.passed for case in result.cases)
        ]
        if not passing or self.unrecorded & set(passing):
            return None
        return frozenset(key for check_id in passing for key in self.entered.get(check_id, ()))


__all__ = [
    "DIFF_LINE_LIMIT",
    "ORACLE_PLAN",
    "PYTEST_BOOTSTRAP",
    "PYTEST_PLAN",
    "SOURCE_LIMIT",
    "ChangedCode",
    "FunctionKey",
    "OracleFootprint",
    "RunRecord",
    "changed_code",
    "function_ranges",
    "oracle_program",
    "pytest_plan",
    "read_entered",
    "read_run_record",
    "read_source",
    "write_plan",
]
