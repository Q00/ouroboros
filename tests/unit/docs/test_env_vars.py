"""Maintained docs must not name ``OUROBOROS_*`` variables that nothing reads.

Item D3 of #2921; policy in docs/contributing/docs-freshness.md.

A variable counts as known when one of these names it:

* a literal ``OUROBOROS_*`` name in shipped code, scripts, hooks, workflows,
  or non-Markdown skill files (``KNOWN_NAME_SOURCES``);
* a reader that builds the name at runtime. Two exist: per-phase
  ``OUROBOROS_PHASE_TIMEOUT_<PHASE>_SECONDS`` (``auto/state.py``) and
  per-backend ``OUROBOROS_<BACKEND>_RPM`` / ``_TPM``
  (``orchestrator/backend_limits.py``). Their names are not listed by hand:
  each reader is called with ``os.environ`` replaced by a recorder, so the
  names are the ones the code looks up. ``DYNAMIC_READERS`` lists them, and
  ``test_every_runtime_built_name_has_a_reader`` fails when another source
  file builds a name or holds an ``"OUROBOROS_..._"`` prefix, so the list
  cannot silently fall behind.

``cli/commands/mcp.py`` matches ``"OUROBOROS_"`` as a prefix when it imports
the login-shell environment. That filter forwards whatever is set and gives no
particular name a meaning, so it does not make every name known; it is the one
entry in ``NOT_READERS``.

A doc token may be a pattern: ``*`` matches any run of name characters,
``<BACKEND>`` or ``{BACKEND}`` matches one or more, and a trailing ``_`` makes
the token a prefix. A pattern is valid when at least one known name matches it.

Known limit: a literal in a comment or docstring also counts as known. The
check catches names that appear nowhere in the code, which is the drift that
happens in practice (a renamed or removed variable).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
import difflib
from functools import cache
import os
from pathlib import Path
import re
from unittest import mock

from tests.unit.docs._corpus import REPO_ROOT, DocLine, corpus_files, iter_lines

#: Files whose ``OUROBOROS_*`` literals define the known names. Markdown is
#: skipped everywhere, so a skill's prose cannot vouch for itself.
KNOWN_NAME_SOURCES: tuple[str, ...] = (
    "src/**/*.py",
    "src/**/*.ts",  # OpenCode bridge plugin
    "gui/vite.config.ts",  # console front end's dev-server proxy target
    "scripts/**/*",  # includes install.sh and install.ps1
    "hooks/**/*",
    "crates/**/*.rs",
    ".github/workflows/*",
    "skills/**/*",
)
_SKIPPED_DIRS = frozenset({"node_modules", "target", "__pycache__"})

# A complete name: optional leading underscores (``_OUROBOROS_DEPTH`` is a
# different variable from ``OUROBOROS_DEPTH``), then upper-case name characters.
_NAME_RUN = re.compile(r"(?<![A-Za-z0-9_])_*OUROBOROS_[A-Za-z0-9_]*")
_COMPLETE_NAME = re.compile(r"_*OUROBOROS_[A-Z0-9_]*[A-Z0-9]")

# A name fragment followed by interpolation or concatenation (``f"OUROBOROS_{x}"``,
# ``"OUROBOROS_" + x``, ``"OUROBOROS_%s" % x``, ``OUROBOROS_${x}`` in shell or TS),
# or a quoted prefix such as ``"OUROBOROS_X_"`` used to build or scan names.
_RUNTIME_BUILT = re.compile(
    r"(?<![A-Za-z0-9_])_*OUROBOROS_[A-Z0-9_]*(?:\{|\$|%[s(]|[\"'`]\s*\+)"
    r"|[\"'`]_*OUROBOROS_(?:[A-Z0-9_]*_)?[\"'`]"
)

# A doc token: name characters plus ``*`` wildcards and ``<X>`` / ``{X}`` placeholders.
_DOC_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_])_*OUROBOROS_(?:[A-Z0-9_]|\*|<[A-Za-z0-9_]+>|\{[A-Za-z0-9_]+\})*"
)
_PLACEHOLDER = re.compile(r"\*|<[A-Za-z0-9_]+>|\{[A-Za-z0-9_]+\}")


def source_files() -> Iterator[tuple[str, str]]:
    """Yield ``(repo-relative path, text)`` for every known-name source."""
    seen: set[Path] = set()
    for pattern in KNOWN_NAME_SOURCES:
        for path in sorted(REPO_ROOT.glob(pattern)):
            rel = path.relative_to(REPO_ROOT)
            if (
                path in seen
                or not path.is_file()
                or path.suffix == ".md"
                or path.name.endswith(".test.ts")
                or _SKIPPED_DIRS.intersection(rel.parts)
            ):
                continue
            seen.add(path)
            yield rel.as_posix(), path.read_text(encoding="utf-8", errors="replace")


def literal_names(text: str) -> set[str]:
    """Complete ``OUROBOROS_*`` names in ``text``.

    A run that ends in ``_`` or contains lower case is a fragment of a name
    built at runtime (``f"OUROBOROS_PHASE_TIMEOUT_{phase}"``) or some other
    identifier, so it is not a name.
    """
    return {
        match.group(0)
        for match in _NAME_RUN.finditer(text)
        if _COMPLETE_NAME.fullmatch(match.group(0))
    }


def runtime_built_lines(text: str) -> dict[int, str]:
    """Lines (1-based number to stripped text) that build or scan ``OUROBOROS_*`` names."""
    lines = text.splitlines()
    found: dict[int, str] = {}
    for match in _RUNTIME_BUILT.finditer(text):
        lineno = text.count("\n", 0, match.start()) + 1
        found[lineno] = lines[lineno - 1].strip()
    return found


class _EnvReadRecorder(dict[str, str]):
    """Stand-in for ``os.environ`` that records every key looked up.

    It holds the real environment minus ``OUROBOROS_*`` keys, so every lookup
    of a variable misses and the reader takes its default path, while ``HOME``
    and the rest still resolve.
    """

    def __init__(self, base: Iterable[tuple[str, str]]) -> None:
        super().__init__((key, value) for key, value in base if "OUROBOROS_" not in key)
        self.keys_read: set[str] = set()

    def get(self, key: str, default: str | None = None) -> str | None:  # type: ignore[override]
        self.keys_read.add(key)
        return super().get(key, default)

    def __getitem__(self, key: str) -> str:
        self.keys_read.add(key)
        return super().__getitem__(key)

    def __contains__(self, key: object) -> bool:
        if isinstance(key, str):
            self.keys_read.add(key)
        return super().__contains__(key)


def env_names_read_by(call: Callable[[], object]) -> set[str]:
    """Run ``call`` with a recording ``os.environ``; return the names it looked up."""
    recorder = _EnvReadRecorder(os.environ.items())
    with mock.patch.object(os, "environ", recorder):
        call()
    return {key for key in recorder.keys_read if _COMPLETE_NAME.fullmatch(key)}


def _phase_timeout_names() -> set[str]:
    from ouroboros.auto import state

    return env_names_read_by(
        lambda: state._apply_phase_timeout_env_overrides(
            dict(state.DEFAULT_TIMEOUT_SECONDS_BY_PHASE)
        )
    )


def _backend_rate_names() -> set[str]:
    from ouroboros.config.models import VALID_RUNTIME_BACKENDS
    from ouroboros.orchestrator.backend_limits import resolve_backend_limits

    def resolve_all() -> None:
        for backend in sorted(VALID_RUNTIME_BACKENDS):
            resolve_backend_limits(backend)

    return env_names_read_by(resolve_all)


#: Source files that build ``OUROBOROS_*`` names at runtime, with the reader
#: that yields the names they accept.
DYNAMIC_READERS: dict[str, Callable[[], set[str]]] = {
    "src/ouroboros/auto/state.py": _phase_timeout_names,
    "src/ouroboros/orchestrator/backend_limits.py": _backend_rate_names,
}

#: Lines that match the runtime-built pattern but read no variable, keyed by
#: (path, stripped line), with the reason.
NOT_READERS: dict[tuple[str, str], str] = {
    ("src/ouroboros/cli/commands/mcp.py", 'or key.startswith("OUROBOROS_")'): (
        "login-shell import forwards every OUROBOROS_* key that is set; it reads none itself"
    ),
}


@cache
def known_names() -> frozenset[str]:
    names: set[str] = set()
    for _rel, text in source_files():
        names |= literal_names(text)
    for reader in DYNAMIC_READERS.values():
        names |= reader()
    return frozenset(names)


def doc_tokens(text: str) -> list[str]:
    return [match.group(0) for match in _DOC_TOKEN.finditer(text)]


def token_pattern(token: str) -> re.Pattern[str]:
    """Regex a known name must fully match for ``token`` to be valid."""
    parts: list[str] = []
    last = 0
    for match in _PLACEHOLDER.finditer(token):
        parts.append(re.escape(token[last : match.start()]))
        parts.append("[A-Z0-9_]*" if match.group(0) == "*" else "[A-Z0-9_]+")
        last = match.end()
    parts.append(re.escape(token[last:]))
    if token.endswith("_"):
        parts.append("[A-Z0-9_]*")
    return re.compile("".join(parts))


def token_is_known(token: str, known: frozenset[str]) -> bool:
    if token in known:
        return True
    if not (token.endswith("_") or _PLACEHOLDER.search(token)):
        return False
    pattern = token_pattern(token)
    return any(pattern.fullmatch(name) for name in known)


def find_violations(lines: Iterable[DocLine], known: frozenset[str]) -> list[str]:
    violations: list[str] = []
    for line in lines:
        for token in doc_tokens(line.text):
            if token_is_known(token, known):
                continue
            remedy = "no code, script, or workflow reads it; fix or delete the reference"
            close = difflib.get_close_matches(token, known, n=1, cutoff=0.8)
            if close:
                remedy += f" (did you mean {close[0]}?)"
            remedy += ", or add <!-- docs-drift: ignore --> if the line names it on purpose"
            violations.append(f"{line.where()}: {token} — {remedy}")
    return violations


def test_corpus_is_not_empty() -> None:
    assert corpus_files(), "the maintained-docs corpus matched no files"


def test_docs_name_only_env_vars_the_code_reads() -> None:
    violations = find_violations(iter_lines(), known_names())
    assert not violations, (
        f"{len(violations)} doc reference(s) to unknown OUROBOROS_* variables:\n"
        + "\n".join(violations)
    )


def test_every_runtime_built_name_has_a_reader() -> None:
    problems: list[str] = []
    builders: set[str] = set()
    not_readers_seen: set[tuple[str, str]] = set()
    for rel, text in source_files():
        for lineno, line in runtime_built_lines(text).items():
            if (rel, line) in NOT_READERS:
                not_readers_seen.add((rel, line))
                continue
            builders.add(rel)
            if rel not in DYNAMIC_READERS:
                problems.append(
                    f"{rel}:{lineno}: {line} — builds or scans OUROBOROS_* names at runtime; "
                    "add a reader to DYNAMIC_READERS in tests/unit/docs/test_env_vars.py"
                )
    problems += [
        f"{rel}: DYNAMIC_READERS entry, but the file no longer builds an OUROBOROS_* name"
        for rel in DYNAMIC_READERS
        if rel not in builders
    ]
    problems += [
        f"{rel}: NOT_READERS entry {line!r} no longer matches; remove it"
        for rel, line in NOT_READERS
        if (rel, line) not in not_readers_seen
    ]
    assert not problems, "\n".join(problems)


def test_dynamic_readers_yield_the_names_the_code_looks_up() -> None:
    phase_names = _phase_timeout_names()
    assert "OUROBOROS_PHASE_TIMEOUT_INTERVIEW_SECONDS" in phase_names
    assert "OUROBOROS_PHASE_TIMEOUT_UNSTUCK_LATERAL_SECONDS" in phase_names
    rate_names = _backend_rate_names()
    assert {"OUROBOROS_CODEX_RPM", "OUROBOROS_OPENCODE_TPM", "OUROBOROS_HERMES_RPM"} <= rate_names
    # ``hermes_cli`` canonicalizes to ``hermes``; the handle-named variable is never read.
    assert "OUROBOROS_HERMES_CLI_RPM" not in rate_names


def test_literal_scan_keeps_complete_names_only() -> None:
    text = (
        'os.environ.get("OUROBOROS_AGENT_RUNTIME")\n'
        'f"OUROBOROS_PHASE_TIMEOUT_{phase}_SECONDS"\n'
        "OUROBOROS_<BACKEND>_RPM\n"
        "_OUROBOROS_DEPTH MY_OUROBOROS_THING OUROBOROS_mixed"
    )
    assert literal_names(text) == {"OUROBOROS_AGENT_RUNTIME", "_OUROBOROS_DEPTH"}


def test_runtime_built_detection() -> None:
    built = (
        'f"OUROBOROS_{token}"\n'
        'name = "OUROBOROS_X_" + suffix\n'
        'f"OUROBOROS_PHASE_TIMEOUT_{phase}"\n'
        'run "OUROBOROS_${NAME}"\n'
    )
    assert list(runtime_built_lines(built + 'if key.startswith("OUROBOROS_X_"):\n')) == [
        1,
        2,
        3,
        4,
        5,
    ]
    complete = (
        'f"OUROBOROS_CODEX_CLI_PATH={path}"\n'
        'f"OUROBOROS_CHECK_FAILED:{check_id}"\n'
        "${OUROBOROS_INSTALL_REF:-direct}\n"
        'os.environ.get("OUROBOROS_AGENT_RUNTIME")\n'
    )
    assert runtime_built_lines(complete) == {}


def test_checker_reports_unknown_names_and_accepts_known_ones() -> None:
    known = known_names()
    bad = DocLine("docs/example.md", 7, "export `OUROBOROS_AGENT_RUNTIMES=codex`", False)
    good = DocLine("docs/example.md", 8, "export OUROBOROS_AGENT_RUNTIME=codex", True)
    violations = find_violations([bad, good], known)
    assert len(violations) == 1
    assert violations[0].startswith(
        "docs/example.md:7: OUROBOROS_AGENT_RUNTIMES — no code, script, or workflow reads it"
    )
    assert "did you mean OUROBOROS_AGENT_RUNTIME?" in violations[0]


def test_patterns_are_valid_only_when_a_known_name_matches() -> None:
    known = frozenset({"OUROBOROS_POSTHOG_HOST", "OUROBOROS_CODEX_RPM", "OUROBOROS_CODEX_CLI_PATH"})
    lines = [
        DocLine("docs/p.md", 1, "`OUROBOROS_POSTHOG_*` and `OUROBOROS_POSTHOG_`", False),
        DocLine("docs/p.md", 2, "`OUROBOROS_<BACKEND>_RPM`, `OUROBOROS_{BACKEND}_RPM`", False),
        DocLine("docs/p.md", 3, "`OUROBOROS_*_CLI_PATH`", False),
        DocLine("docs/p.md", 4, "`OUROBOROS_TELEMETRY_*`, `OUROBOROS_<BACKEND>_TPM`", False),
    ]
    violations = find_violations(lines, known)
    assert [v.split(" — ")[0] for v in violations] == [
        "docs/p.md:4: OUROBOROS_TELEMETRY_*",
        "docs/p.md:4: OUROBOROS_<BACKEND>_TPM",
    ]
