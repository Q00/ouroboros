"""Test commands as data: where a regression run's command comes from, and when one may be used.

The base regression check must work for any project, not only one pytest
can drive. It needs, per selected test target (a file), a command that runs
that target and exits 0 when its tests pass. Nothing here parses console
text: a command's verdict is its exit status, or, when it writes one, a
JUnit XML report (``{report}``).

A command is a template: argv tokens, run directly (never through a shell),
in which these placeholders stand for the target:

- ``{target}``: its repository-relative path;
- ``{module}``: its dotted Python module (``tests/test_x.py`` is ``tests.test_x``);
- ``{label}``: a Django test label (``tests/app/test_x.py`` is ``app.test_x``);
- ``{gopkg}``: its Go package (``./pkg``, or ``.`` at the root);
- ``{stem}``: its file name without the extension;
- ``{report}``: a path in the run's scratch directory where the runner may
  write a JUnit XML report.

Sources, in priority order, every one of them data (``CommandSource``):

1. a criterion's ``verify_command`` in the Seed, when one of its tokens names
   a base-tree test file (that token becomes ``{target}``);
2. the ``test_command`` the constructor may declare in the reply it already
   writes (``declared_test_command``; no extra model call);
3. the worker's own test invocations, as the transcript verifier reads them
   structurally (``evidence.call_citation.recorded_calls``), when one names a
   base-tree test file;
4. built-in defaults read from the base tree's structure: Django's
   ``tests/runtests.py``, SymPy's ``bin/test``, a ``manage.py``,
   ``package.json``'s ``scripts.test``, ``go.mod``, ``Cargo.toml``.

A command whose program is pytest is not used as a command: the product's
own pytest run (``base_regression``, with its per-test JUnit results,
configuration hardening and footprints) covers those targets.

Admission (``admit``) is what makes a command safe to rely on, and it reads
no output: for a target, the command must exit 0 on two fresh copies of the
base, and must fail on a third copy in which the controller replaced the
target with bytes no language parses. A command that does nothing, runs in
the wrong place, or runs something other than the target cannot pass that
canary. With a JUnit report the base runs need not exit 0 (a test that fails
on the base is just not a stable pass), but the canary must still fail and
must pass none of the target's stable tests. No admitted command for a
target is no observation for it, never a decision.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
import json
import posixpath
import shlex
from typing import Any

from ouroboros.orchestrator.evidence.test_reexecution import safe_test_argv

PLACEHOLDERS = ("{target}", "{module}", "{label}", "{gopkg}", "{stem}")
"""The placeholders that name the target; a template must use one of them."""
REPORT = "{report}"
TEMPLATE_LIMIT = 500
CANARY = b"\x00\x00 not a source file in any language }}}{{{ \x00\n"
"""What admission writes over a target on a throwaway base copy."""

_JS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
_JVM = (".java", ".kt")
_SOURCE_EXTENSIONS = (".go", ".rs", *_JS, *_JVM)
"""Non-Python sources this module pairs with tests by path (Python uses ``ast`` importers)."""


class CommandSource(StrEnum):
    """Where a test command came from, in priority order."""

    SEED = "seed"
    CONSTRUCTOR = "constructor"
    TRANSCRIPT = "transcript"
    DEFAULT = "default"


@dataclass(frozen=True, slots=True)
class TargetCommand:
    """A command template and its source."""

    template: tuple[str, ...]
    source: CommandSource

    def argv(self, target: str, report: str) -> tuple[str, ...] | None:
        """The argv for ``target``, or ``None`` when a placeholder has no value for it."""
        values = placeholder_values(target)
        out = []
        for token in self.template:
            for name, value in values.items():
                if name in token:
                    if value is None:
                        return None
                    token = token.replace(name, value)
            out.append(token.replace(REPORT, report))
        return tuple(out)

    @property
    def writes_report(self) -> bool:
        return any(REPORT in token for token in self.template)


def placeholder_values(target: str) -> dict[str, str | None]:
    """The value of every target placeholder for ``target`` (``None``: not applicable)."""
    stem, extension = posixpath.splitext(posixpath.basename(target))
    directory = posixpath.dirname(target)
    module = target[:-3].replace("/", ".") if extension == ".py" else None
    label = (
        target[len("tests/") : -3].replace("/", ".")
        if extension == ".py" and target.startswith("tests/")
        else None
    )
    return {
        "{target}": target,
        "{module}": module,
        "{label}": label,
        "{gopkg}": (f"./{directory}" if directory else ".") if extension == ".go" else None,
        "{stem}": stem,
    }


def parse_template(text: str) -> tuple[str, ...] | None:
    """A template's argv tokens, or ``None`` when it is not a direct command naming its target.

    Leading ``NAME=value`` assignments become an ``env`` prefix (the command
    runs without a shell). A token with shell syntax is refused, as for any
    command the controller runs (``test_reexecution.safe_test_argv``).
    """
    if not isinstance(text, str) or not text.strip() or len(text) > TEMPLATE_LIMIT:
        return None
    try:
        tokens = shlex.split(text)
    except ValueError:
        return None
    blank = text
    for name in (*PLACEHOLDERS, REPORT):
        blank = blank.replace(name, "X")
    if not tokens or safe_test_argv(blank) is None:
        return None
    if not any(name in token for token in tokens for name in PLACEHOLDERS):
        return None
    assignments = 0
    while assignments < len(tokens) and _assignment(tokens[assignments]):
        assignments += 1
    if assignments == len(tokens):
        return None
    if assignments:
        tokens = ["env", *tokens]
    return tuple(tokens)


def _assignment(token: str) -> bool:
    name, equals, _value = token.partition("=")
    return bool(equals) and name.isidentifier()


def template_from_command(command: str, test_files: Collection[str]) -> tuple[str, ...] | None:
    """A concrete command as a template: the token naming a base test file becomes ``{target}``.

    ``pytest tests/test_x.py::test_a`` gives ``pytest {target}`` (the node
    part is dropped: the target is the file). ``None`` when no token names
    exactly one of ``test_files``.
    """
    if not isinstance(command, str) or len(command) > TEMPLATE_LIMIT:
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    named = [i for i, token in enumerate(tokens) if token.partition("::")[0] in test_files]
    if len(named) != 1:
        return None
    tokens[named[0]] = "{target}"
    return parse_template(shlex.join(tokens))


def declared_test_command(reply: object) -> str | None:
    """The ``test_command`` a constructor reply declares, when it is a usable template."""
    if not isinstance(reply, Mapping):
        return None
    value = reply.get("test_command")
    return value if isinstance(value, str) and parse_template(value) is not None else None


def runs_pytest(template: Sequence[str]) -> bool:
    """Whether a template's program is pytest (the product's own pytest run covers it)."""
    argv = list(template)
    if argv and argv[0] == "env":
        argv = argv[1:]
        while argv and _assignment(argv[0]):
            argv = argv[1:]
    if not argv:
        return False
    program = posixpath.basename(argv[0])
    if program in ("pytest", "py.test"):
        return True
    return program.startswith("python") and argv[1:3] == ["-m", "pytest"]


def default_commands(
    base_paths: Collection[str], read: Any, *, python_targets: bool
) -> tuple[TargetCommand, ...]:
    """Built-in templates the base tree's structure names (``read(path)`` gives a file's text).

    Python project runners only when ``python_targets`` (Django's
    ``tests/runtests.py``, SymPy's ``bin/test``, a ``manage.py``); then
    ``package.json``'s ``scripts.test``, ``go.mod`` and ``Cargo.toml``.
    """
    templates: list[str] = []
    if python_targets:
        if "tests/runtests.py" in base_paths:
            templates.append("python tests/runtests.py --parallel 1 {label}")
        if "bin/test" in base_paths:
            templates.append("python bin/test {target}")
        if "manage.py" in base_paths:
            templates.append("python manage.py test {module}")
    if "package.json" in base_paths:
        try:
            scripts = json.loads(read("package.json") or "{}").get("scripts")
        except (ValueError, AttributeError):
            scripts = None
        if isinstance(scripts, dict) and isinstance(scripts.get("test"), str):
            templates.append("npm test -- {target}")
    if "go.mod" in base_paths:
        templates.append("go test {gopkg}")
    if "Cargo.toml" in base_paths:
        templates.append("cargo test --test {stem}")
    commands = (parse_template(text) for text in templates)
    return tuple(TargetCommand(argv, CommandSource.DEFAULT) for argv in commands if argv)


def transcript_commands(results: Iterable[Any], task_cwd: str | None) -> tuple[str, ...]:
    """The test invocations the worker's transcripts record, workspace-relative.

    Read as the transcript verifier reads them (``call_citation.recorded_calls``):
    every execution of a call it classifies as a test run, as a command line.
    """
    from ouroboros.orchestrator.evidence.call_citation import KIND_TEST, recorded_calls

    commands: list[str] = []
    stack = list(results)
    while stack:
        result = stack.pop()
        stack.extend(getattr(result, "sub_results", ()) or ())
        try:
            calls = recorded_calls(tuple(getattr(result, "messages", ()) or ()), task_cwd=task_cwd)
        except Exception:  # noqa: BLE001 - a transcript that cannot be read names nothing
            continue
        for call in calls:
            if call.kind != KIND_TEST:
                continue
            for argv in call.relative_executions:
                command = shlex.join(argv)
                if command not in commands:
                    commands.append(command)
    return tuple(commands)


@dataclass(frozen=True, slots=True)
class CommandSet:
    """Every usable command for one run, in priority order."""

    commands: tuple[TargetCommand, ...] = ()
    pytest_declared: bool = False
    """A declared command was a pytest invocation (the product's pytest run takes it)."""

    def for_target(self, target: str) -> tuple[TargetCommand, ...]:
        """The commands that can name ``target``."""
        return tuple(command for command in self.commands if command.argv(target, "r"))


def command_set(
    *,
    seed_commands: Sequence[str],
    constructor_command: str | None,
    transcript: Sequence[str],
    test_files: Collection[str],
    defaults: Sequence[TargetCommand],
) -> CommandSet:
    """The run's commands: Seed, constructor, transcript, defaults; pytest ones set aside."""
    ordered: list[TargetCommand] = []
    pytest_declared = False
    sources = (
        *((template_from_command(text, test_files), CommandSource.SEED) for text in seed_commands),
        (
            parse_template(constructor_command) if constructor_command else None,
            CommandSource.CONSTRUCTOR,
        ),
        *(
            (template_from_command(text, test_files), CommandSource.TRANSCRIPT)
            for text in transcript
        ),
    )
    for template, source in sources:
        if template is None:
            continue
        if runs_pytest(template):
            pytest_declared = True
            continue
        if all(existing.template != template for existing in ordered):
            ordered.append(TargetCommand(template, source))
    for command in defaults:
        if all(existing.template != command.template for existing in ordered):
            ordered.append(command)
    return CommandSet(tuple(ordered), pytest_declared)


# --------------------------------------------------------------------------
# Selection for languages other than Python: by path only.


def is_paired_test(path: str) -> bool:
    """A test file by the naming conventions this module pairs with (not Python)."""
    name = posixpath.basename(path)
    stem, extension = posixpath.splitext(name)
    if extension == ".go":
        return stem.endswith("_test")
    if extension in _JS:
        return ".test." in name or ".spec." in name or "__tests__" in path.split("/")
    if extension in _JVM:
        return stem.endswith(("Test", "Tests"))
    if extension == ".rs":
        return path.split("/")[0] == "tests"
    return False


def changed_other_sources(changed: Iterable[str]) -> tuple[str, ...]:
    """Changed non-Python sources this module can pair (never a test file)."""
    return tuple(
        sorted(
            path
            for path in changed
            if path.endswith(_SOURCE_EXTENSIONS) and not is_paired_test(path)
        )
    )


def paired_other_tests(base_paths: Collection[str], source: str) -> tuple[str, ...]:
    """The base test files paired with a non-Python ``source`` by path conventions.

    ``foo.go`` with ``foo_test.go`` beside it; ``x.ts`` with ``x.test.ts``,
    ``x.spec.ts`` or ``__tests__/x.ts`` (any JavaScript or TypeScript
    extension); ``Foo.java`` with ``FooTest.java`` or ``FooTests.java``;
    ``src/foo.rs`` with ``tests/foo.rs``.
    """
    directory = posixpath.dirname(source)
    stem, extension = posixpath.splitext(posixpath.basename(source))
    out: set[str] = set()
    for path in base_paths:
        if not is_paired_test(path):
            continue
        name = posixpath.basename(path)
        test_stem, test_extension = posixpath.splitext(name)
        if extension == ".go" and test_extension == ".go":
            if posixpath.dirname(path) == directory and test_stem == f"{stem}_test":
                out.add(path)
        elif extension in _JS and test_extension in _JS:
            if name.split(".")[0] == stem:
                out.add(path)
        elif extension in _JVM and test_extension in _JVM:
            if test_stem in (f"{stem}Test", f"{stem}Tests"):
                out.add(path)
        elif extension == ".rs" and test_extension == ".rs":
            if test_stem == stem:
                out.add(path)
    return tuple(sorted(out))


def select_other_tests(base_paths: Collection[str], changed: Iterable[str]) -> tuple[str, ...]:
    """The non-Python base test files a change selects, by path pairing."""
    selected: set[str] = set()
    for source in changed_other_sources(changed):
        selected.update(paired_other_tests(base_paths, source))
    return tuple(sorted(selected))


# --------------------------------------------------------------------------
# Admission: the canary.


class Tier(StrEnum):
    """How an admitted command's result is read."""

    JUNIT = "junit"
    """Per-test results from the JUnit XML report it writes."""
    EXIT = "exit"
    """Its exit status for the whole target (the floor)."""


@dataclass(frozen=True, slots=True)
class CommandRun:
    """One command run: its exit status and, when it wrote one, its report's per-test results."""

    exit_code: int | None
    statuses: dict[str, str] | None = None
    timed_out: bool = False
    unavailable: bool = False

    @property
    def observed(self) -> bool:
        return not self.timed_out and not self.unavailable and self.exit_code is not None


@dataclass(frozen=True, slots=True)
class Admission:
    """Whether a command observes a target on the base, and what the base showed."""

    command: TargetCommand | None
    tier: Tier | None = None
    stable: tuple[str, ...] = ()
    """The tests that passed on both base runs (the JUnit tier only)."""
    transient: bool = False
    """It failed for a reason a later attempt may not meet (a timeout, a refused run)."""

    @property
    def admitted(self) -> bool:
        return self.command is not None


def judge_admission(
    command: TargetCommand, base_runs: Sequence[CommandRun], canary: CommandRun
) -> Admission:
    """The canary rule over two base runs and one canary run (see the module docstring)."""
    runs = (*base_runs, canary)
    if any(not run.observed for run in runs):
        return Admission(None, transient=True)
    reports = [run.statuses for run in base_runs]
    if command.writes_report and all(report for report in reports):
        first = reports[0] or {}
        stable = tuple(
            sorted(
                test
                for test, status in first.items()
                if status == "pass"
                and all((report or {}).get(test) == "pass" for report in reports)
            )
        )
        canary_passes = {
            test for test, status in (canary.statuses or {}).items() if status == "pass"
        }
        if stable and canary.exit_code != 0 and not canary_passes & set(stable):
            return Admission(command, Tier.JUNIT, stable)
        return Admission(None)
    if all(run.exit_code == 0 for run in base_runs) and canary.exit_code != 0:
        return Admission(command, Tier.EXIT)
    return Admission(None)


@dataclass
class AdmissionCache:
    """Admissions per (base digest, template, target); a transient refusal is retried once."""

    entries: dict[tuple[str, tuple[str, ...], str], Admission] = field(default_factory=dict)
    attempts: dict[tuple[str, tuple[str, ...], str], int] = field(default_factory=dict)

    def get(self, key: tuple[str, tuple[str, ...], str]) -> Admission | None:
        found = self.entries.get(key)
        if found is not None and found.transient and self.attempts.get(key, 0) < 2:
            return None
        return found

    def put(self, key: tuple[str, tuple[str, ...], str], admission: Admission) -> None:
        self.attempts[key] = self.attempts.get(key, 0) + 1
        self.entries[key] = admission


__all__ = [
    "CANARY",
    "PLACEHOLDERS",
    "REPORT",
    "Admission",
    "AdmissionCache",
    "CommandRun",
    "CommandSet",
    "CommandSource",
    "TargetCommand",
    "Tier",
    "changed_other_sources",
    "command_set",
    "declared_test_command",
    "default_commands",
    "is_paired_test",
    "judge_admission",
    "paired_other_tests",
    "parse_template",
    "placeholder_values",
    "runs_pytest",
    "select_other_tests",
    "template_from_command",
    "transcript_commands",
]
