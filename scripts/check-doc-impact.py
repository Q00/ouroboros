#!/usr/bin/env python3
"""Report the mapped docs a change did not touch (advisory).

Item D9 of Q00/ouroboros#2921. Prose about behavior cannot be checked
mechanically, so this only routes a reviewer's attention: ``docs/doc-map.yml``
maps source globs to the docs that describe them, and for a diff this lists
each mapped doc whose source changed while the doc itself did not. Whether the
doc is still right is for the reviewer to decide, so findings never fail the
run.

Map schema (``docs/doc-map.yml``): a non-empty list of entries, each a mapping
with ``sources`` (non-empty list of repository-relative globs), ``docs``
(non-empty list of repository-relative file paths) and an optional ``note``
(string). In a glob, ``*`` and ``?`` match within one path segment and a ``**``
segment matches any number of segments. Other glob syntax (``[``, ``]``,
``{``, ``}``), absolute paths, ``.``/``..`` segments, backslashes, unknown
keys, duplicate keys and duplicate list items are rejected, not guessed at.

Run locally (needs PyYAML, which ``uv run`` provides):
    uv run python3 scripts/check-doc-impact.py --validate
    uv run python3 scripts/check-doc-impact.py --base origin/main

``--validate`` checks the map against the checkout: every doc is a tracked
file and every source glob matches at least one tracked file (``git
ls-files``). ``--base REF`` reports on ``git diff --name-only REF...HEAD``:
human-readable lines on stdout; a Markdown table appended to
``$GITHUB_STEP_SUMMARY`` when that variable is set; one ``::notice``
annotation per untouched doc when ``GITHUB_ACTIONS`` is ``true``.

CI:
    .github/workflows/doc-impact.yml runs ``--validate`` and then ``--base``
    on every pull request to main. It is not a required check.

Exit codes:
    0 -- the map is well formed, and the report was produced (``--base``,
         whatever it found) or the map matches the checkout (``--validate``)
    1 -- the map is malformed, or ``--validate`` found a mapped doc that is not
         a tracked file or a source glob that matches no tracked file
    2 -- usage error
    3 -- git failed: not a repository, unknown base ref, or a timeout
    4 -- ``GITHUB_STEP_SUMMARY`` is set but could not be written
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
import os
from pathlib import Path
import subprocess
import sys

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

DEFAULT_MAP = "docs/doc-map.yml"
GIT_TIMEOUT_SECONDS = 60

EXIT_OK = 0
EXIT_MAP = 1
EXIT_USAGE = 2
EXIT_GIT = 3
EXIT_SUMMARY = 4

_STR_TAG = "tag:yaml.org,2002:str"
_KEYS = frozenset({"sources", "docs", "note"})
_UNSUPPORTED_GLOB = frozenset("[]{}")
_GLOB = frozenset("*?")


class MapError(Exception):
    """The map is malformed; ``problems`` holds one line per problem."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = problems


class GitError(Exception):
    """A git command failed, timed out, or could not start."""


@dataclass(frozen=True)
class Item:
    value: str
    line: int


@dataclass(frozen=True)
class Entry:
    line: int
    sources: tuple[Item, ...]
    docs: tuple[Item, ...]
    note: str | None


@dataclass
class Finding:
    """A mapped doc whose sources changed while the doc did not."""

    doc: str
    sources: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Globs
# --------------------------------------------------------------------------


def glob_matches(pattern: str, path: str) -> bool:
    """Whether a repository-relative ``path`` matches a map glob."""
    return _match_segments(tuple(pattern.split("/")), tuple(path.split("/")))


def _match_segments(pattern: tuple[str, ...], parts: tuple[str, ...]) -> bool:
    if not pattern:
        return not parts
    head, rest = pattern[0], pattern[1:]
    if head == "**":
        return any(_match_segments(rest, parts[index:]) for index in range(len(parts) + 1))
    return bool(parts) and fnmatchcase(parts[0], head) and _match_segments(rest, parts[1:])


# --------------------------------------------------------------------------
# The map
# --------------------------------------------------------------------------


def _path_problem(value: str, *, is_glob: bool) -> str | None:
    """Why a map path is unacceptable, or ``None``."""
    if value != value.strip() or not value:
        return "must be a non-empty path without surrounding whitespace"
    if "\\" in value:
        return "must use forward slashes"
    if value.startswith("/"):
        return "must be relative to the repository root"
    segments = value.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        return "must not contain empty, '.' or '..' segments"
    if is_glob:
        if _UNSUPPORTED_GLOB & set(value):
            return "uses glob syntax this map does not support (only *, ? and **)"
        if any("**" in segment and segment != "**" for segment in segments):
            return "may use ** only as a whole path segment"
    elif _GLOB & set(value) or _UNSUPPORTED_GLOB & set(value):
        return "must be a file path, not a glob"
    return None


def _string_list(
    node: Node, *, key: str, rel: str, problems: list[str], is_glob: bool
) -> tuple[Item, ...]:
    line = node.start_mark.line + 1
    if not isinstance(node, SequenceNode) or not node.value:
        problems.append(f"{rel}:{line}: {key} — must be a non-empty list; list at least one path")
        return ()
    items: list[Item] = []
    seen: set[str] = set()
    for child in node.value:
        child_line = child.start_mark.line + 1
        if not isinstance(child, ScalarNode) or child.tag != _STR_TAG:
            problems.append(f"{rel}:{child_line}: {key} — every item must be a string")
            continue
        problem = _path_problem(child.value, is_glob=is_glob)
        if problem:
            problems.append(f"{rel}:{child_line}: {child.value!r} — {key} item {problem}")
            continue
        if child.value in seen:
            problems.append(f"{rel}:{child_line}: {child.value} — listed twice; remove one")
            continue
        seen.add(child.value)
        items.append(Item(child.value, child_line))
    return tuple(items)


def parse_map(text: str, rel: str = DEFAULT_MAP) -> list[Entry]:
    """Parse and validate map text; raise ``MapError`` listing every problem."""
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = mark.line + 1 if mark is not None else 1
        problem = getattr(exc, "problem", None) or type(exc).__name__
        raise MapError(
            [f"{rel}:{line}: YAML — does not parse ({problem}); fix the syntax"]
        ) from exc
    if not isinstance(root, SequenceNode) or not root.value:
        line = root.start_mark.line + 1 if root is not None else 1
        raise MapError([f"{rel}:{line}: map — must be a non-empty list of entries"])
    problems: list[str] = []
    entries: list[Entry] = []
    for node in root.value:
        line = node.start_mark.line + 1
        if not isinstance(node, MappingNode):
            problems.append(f"{rel}:{line}: entry — must be a mapping with sources and docs")
            continue
        values: dict[str, Node] = {}
        for key_node, value_node in node.value:
            key = key_node.value if isinstance(key_node, ScalarNode) else None
            key_line = key_node.start_mark.line + 1
            if key not in _KEYS:
                problems.append(
                    f"{rel}:{key_line}: {key!r} — unknown key; use sources, docs, or note"
                )
            elif key in values:
                problems.append(f"{rel}:{key_line}: {key} — given twice in one entry; keep one")
            else:
                values[key] = value_node
        for required in ("sources", "docs"):
            if required not in values:
                problems.append(f"{rel}:{line}: {required} — missing from the entry; add it")
        sources = (
            _string_list(values["sources"], key="sources", rel=rel, problems=problems, is_glob=True)
            if "sources" in values
            else ()
        )
        docs = (
            _string_list(values["docs"], key="docs", rel=rel, problems=problems, is_glob=False)
            if "docs" in values
            else ()
        )
        note: str | None = None
        if "note" in values:
            note_node = values["note"]
            if (
                not isinstance(note_node, ScalarNode)
                or note_node.tag != _STR_TAG
                or not note_node.value.strip()
            ):
                problems.append(
                    f"{rel}:{note_node.start_mark.line + 1}: note — must be a non-empty string"
                )
            else:
                note = " ".join(note_node.value.split())
        entries.append(Entry(line, sources, docs, note))
    if problems:
        raise MapError(problems)
    return entries


def load_map(path: Path, rel: str) -> list[Entry]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise MapError([f"{rel}:1: map — cannot be read ({exc})"]) from exc
    return parse_map(text, rel)


def validate(entries: Sequence[Entry], tracked: Sequence[str], rel: str) -> list[str]:
    """Problems that make the map disagree with the checkout."""
    tracked_set = set(tracked)
    problems: list[str] = []
    for entry in entries:
        for source in entry.sources:
            if not any(glob_matches(source.value, path) for path in tracked):
                problems.append(
                    f"{rel}:{source.line}: {source.value} — source glob matches no tracked "
                    "file; fix the glob or remove it"
                )
        for doc in entry.docs:
            if doc.value not in tracked_set:
                problems.append(
                    f"{rel}:{doc.line}: {doc.value} — mapped doc is not a tracked file; "
                    "fix the path or remove it"
                )
    return problems


def impact(entries: Sequence[Entry], changed: Sequence[str]) -> list[Finding]:
    """Mapped docs whose sources are in ``changed`` while they are not."""
    changed_set = set(changed)
    findings: dict[str, Finding] = {}
    for entry in entries:
        hits = sorted(
            path
            for path in changed_set
            if any(glob_matches(source.value, path) for source in entry.sources)
        )
        if not hits:
            continue
        for doc in entry.docs:
            if doc.value in changed_set:
                continue
            finding = findings.setdefault(doc.value, Finding(doc.value))
            finding.sources.extend(path for path in hits if path not in finding.sources)
            if entry.note and entry.note not in finding.notes:
                finding.notes.append(entry.note)
    for finding in findings.values():
        finding.sources.sort()
    return [findings[doc] for doc in sorted(findings)]


# --------------------------------------------------------------------------
# Git
# --------------------------------------------------------------------------


def run_git(repo: Path, *args: str) -> str:
    command = ["git", "-C", str(repo), *args]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"`{' '.join(args)}` timed out after {GIT_TIMEOUT_SECONDS}s") from exc
    except OSError as exc:
        raise GitError(f"could not run git ({exc})") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"exit status {completed.returncode}"
        raise GitError(f"`git {' '.join(args)}` failed: {detail}")
    return completed.stdout


def repo_root(start: Path) -> Path:
    return Path(run_git(start, "rev-parse", "--show-toplevel").strip())


def tracked_files(root: Path) -> list[str]:
    return [path for path in run_git(root, "ls-files", "-z").split("\0") if path]


def changed_files(root: Path, base: str) -> list[str]:
    try:
        run_git(root, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
    except GitError as exc:
        raise GitError(f"base ref {base!r} is not a commit in this repository") from exc
    output = run_git(root, "diff", "--name-only", "--no-renames", "-z", f"{base}...HEAD", "--")
    return [path for path in output.split("\0") if path]


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def report_lines(findings: Sequence[Finding], changed_count: int) -> list[str]:
    lines = [
        f"doc-impact: {changed_count} changed file(s); {len(findings)} mapped doc(s) "
        "not changed in this diff (advisory)."
    ]
    for finding in findings:
        lines.append(f"{finding.doc} — not changed; mapped from {', '.join(finding.sources)}")
        lines.extend(f"    check: {note}" for note in finding.notes)
    return lines


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def summary_markdown(findings: Sequence[Finding], changed_count: int, map_rel: str) -> str:
    out = ["### Doc impact (advisory)", ""]
    if not findings:
        out.append(
            f"{changed_count} changed file(s). Every doc that `{map_rel}` maps to them "
            "changed too, or none is mapped."
        )
        return "\n".join(out) + "\n"
    out.append(
        f"{changed_count} changed file(s). `{map_rel}` maps them to these docs, which this "
        "pull request does not change. Check that each still describes the code; nothing "
        "needs to change if it does. This check never blocks a merge."
    )
    out += ["", "| Doc | Changed sources | What to check |", "|---|---|---|"]
    for finding in findings:
        sources = "<br>".join(f"`{_cell(path)}`" for path in finding.sources)
        notes = "<br>".join(_cell(note) for note in finding.notes) or "—"
        out.append(f"| `{_cell(finding.doc)}` | {sources} | {notes} |")
    return "\n".join(out) + "\n"


def _escape_data(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(text: str) -> str:
    return _escape_data(text).replace(":", "%3A").replace(",", "%2C")


def annotation_lines(findings: Sequence[Finding]) -> list[str]:
    lines = []
    for finding in findings:
        message = (
            f"Not changed in this PR, but its mapped sources are: {', '.join(finding.sources)}"
        )
        lines.append(
            f"::notice file={_escape_property(finding.doc)},"
            f"title={_escape_property('Doc impact (advisory)')}::{_escape_data(message)}"
        )
    return lines


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--validate",
        action="store_true",
        help="check that every mapped doc is tracked and every source glob matches a file",
    )
    mode.add_argument(
        "--base",
        metavar="REF",
        help="report mapped docs not changed in `git diff REF...HEAD`",
    )
    parser.add_argument(
        "--map",
        default=DEFAULT_MAP,
        help=f"map path relative to the repository (default {DEFAULT_MAP})",
    )
    parser.add_argument(
        "--repo", default=".", help="any path inside the repository (default: current directory)"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.base is not None and (not args.base.strip() or args.base.startswith("-")):
        parser.error("--base must name a git ref, and must not start with '-'")

    try:
        root = repo_root(Path(args.repo))
    except GitError as exc:
        print(f"doc-impact: {exc}", file=sys.stderr)
        return EXIT_GIT

    map_path = Path(args.map) if Path(args.map).is_absolute() else root / args.map
    map_rel = args.map
    try:
        entries = load_map(map_path, map_rel)
    except MapError as exc:
        for problem in exc.problems:
            print(problem, file=sys.stderr)
        return EXIT_MAP

    if args.validate:
        try:
            problems = validate(entries, tracked_files(root), map_rel)
        except GitError as exc:
            print(f"doc-impact: {exc}", file=sys.stderr)
            return EXIT_GIT
        for problem in problems:
            print(problem, file=sys.stderr)
        if problems:
            return EXIT_MAP
        print(f"doc-impact: {map_rel} has {len(entries)} entries; every doc and glob resolves.")
        return EXIT_OK

    try:
        changed = changed_files(root, args.base)
    except GitError as exc:
        print(f"doc-impact: {exc}", file=sys.stderr)
        return EXIT_GIT

    findings = impact(entries, changed)
    for line in report_lines(findings, len(changed)):
        print(line)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for line in annotation_lines(findings):
            print(line)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as handle:
                handle.write(summary_markdown(findings, len(changed), map_rel))
        except OSError as exc:
            print(f"doc-impact: cannot write GITHUB_STEP_SUMMARY ({exc})", file=sys.stderr)
            return EXIT_SUMMARY
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
