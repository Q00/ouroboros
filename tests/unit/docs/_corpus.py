"""Maintained-docs corpus shared by the docs drift tests.

Each test module in this package checks one kind of fact that the docs restate
from the code: repository paths, environment variables, MCP tool and agent
names, CLI commands and flags, config keys. They all read the same corpus, so
"which docs are maintained" is decided in one place.

Policy and rationale: docs/contributing/docs-freshness.md.

This file is introduced verbatim by several independent PRs, so keep it
byte-identical between them; change the corpus rules in a dedicated PR.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from functools import cache
from pathlib import Path
import re

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Maintained documentation, as globs relative to the repository root.
CORPUS_GLOBS: tuple[str, ...] = (
    "README*.md",
    "CONTRIBUTING.md",
    "llms.txt",
    "llms-full.txt",
    "docs/**/*.md",
    "skills/**/*.md",
)

#: Point-in-time records. They describe a past snapshot or a proposal, so a
#: path or name that no longer (or not yet) exists there is not drift.
EXCLUDED_DIRS: tuple[str, ...] = (
    "docs/history/",
    "docs/rfc/",
    "docs/research/",
)

#: Dated plans and handoffs that still live outside ``docs/history/``.
EXCLUDED_FILES: frozenset[str] = frozenset(
    {
        "docs/handoff-auto-contract-ac-grounding-2026-07.md",
        "docs/ooo-auto-analysis.md",
        "docs/ooo-auto-evolution-plan.md",
        "docs/run-metaharness-plan-2026-07.md",
    }
)

#: Put on a line that deliberately names something that does not exist, such
#: as a counter-example in a rubric. Invisible when the Markdown renders.
IGNORE_LINE = "<!-- docs-drift: ignore -->"
#: Bracket a region (for example a whole fenced block) to skip it.
IGNORE_START = "<!-- docs-drift: ignore-start -->"
IGNORE_END = "<!-- docs-drift: ignore-end -->"


@dataclass(frozen=True)
class DocLine:
    """One checkable line of a maintained doc."""

    path: str  # repository-relative POSIX path
    lineno: int  # 1-based
    text: str
    in_code_block: bool

    def where(self) -> str:
        return f"{self.path}:{self.lineno}"


def is_excluded(rel_path: str) -> bool:
    return rel_path in EXCLUDED_FILES or rel_path.startswith(EXCLUDED_DIRS)


@cache
def corpus_files() -> tuple[str, ...]:
    """Repository-relative paths of every maintained doc, sorted."""
    found: set[str] = set()
    for pattern in CORPUS_GLOBS:
        for path in REPO_ROOT.glob(pattern):
            if not path.is_file():
                continue
            rel = path.relative_to(REPO_ROOT).as_posix()
            if not is_excluded(rel):
                found.add(rel)
    return tuple(sorted(found))


_INLINE_CODE = re.compile(r"`+[^`]*`+")


def _outside_inline_code(line: str) -> str:
    """The line with inline code removed, so a marker quoted in prose is inert."""
    return _INLINE_CODE.sub("", line)


def iter_lines(files: Iterable[str] | None = None) -> Iterator[DocLine]:
    """Yield each checkable line, honouring the ignore markers.

    ``IGNORE_START`` / ``IGNORE_END`` count only as a line of their own, and
    ``IGNORE_LINE`` only outside inline code, so a doc that *mentions* a marker
    does not switch checking off. An unbalanced region raises ``ValueError``
    rather than silently skipping the rest of the file.

    Fence lines themselves are not yielded; lines inside a fenced block are
    yielded with ``in_code_block=True`` so a check can treat examples and prose
    differently.
    """
    for rel in corpus_files() if files is None else files:
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        fence = ""
        ignore_opened_at = 0
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped == IGNORE_START:
                if ignore_opened_at:
                    raise ValueError(f"{rel}:{lineno}: nested {IGNORE_START}")
                ignore_opened_at = lineno
                continue
            if stripped == IGNORE_END:
                if not ignore_opened_at:
                    raise ValueError(f"{rel}:{lineno}: {IGNORE_END} without {IGNORE_START}")
                ignore_opened_at = 0
                continue
            if stripped.startswith(("```", "~~~")):
                marker = stripped[:3]
                if not fence:
                    fence = marker
                elif marker == fence:
                    fence = ""
                continue
            if ignore_opened_at or IGNORE_LINE in _outside_inline_code(line):
                continue
            yield DocLine(rel, lineno, line, in_code_block=bool(fence))
        if ignore_opened_at:
            raise ValueError(f"{rel}:{ignore_opened_at}: {IGNORE_START} is never closed")
