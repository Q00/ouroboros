"""Guard: a SKILL.md runs long programs from its bundle; it never inlines them.

The model cannot execute Markdown, so every program inside a SKILL.md is
retyped as tool input on each run (paid as output tokens, one at a time) and
every transcription slip is a silent bug. Programs belong in the skill's
``scripts/`` directory, where they are linted, tested, and run by one line.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SKILL_FILES = sorted((_REPO_ROOT / "skills").glob("*/SKILL.md"))
_PROGRAM_TAGS = frozenset({"bash", "sh", "shell", "python"})
_MAX_PROGRAM_LINES = 25
_RULE = "docs/contributing/key-patterns.md#9-bundled-scripts-in-skills"
# Any indentation, so a fence nested in a list item is still seen.
_OPENING_FENCE = re.compile(r"^[ \t]*(?P<fence>`{3,}|~{3,})(?P<info>.*)$")


def _fenced_blocks(text: str) -> list[tuple[int, str, int]]:
    """Return ``(opening line, tag, body line count)`` for each fenced block.

    As in CommonMark, a block closes only on a bare fence of the same
    character that is at least as long, so a four-backtick ``markdown`` block
    may contain three-backtick fences. An unclosed block runs to the end.
    """
    lines = text.splitlines()
    blocks: list[tuple[int, str, int]] = []
    index = 0
    while index < len(lines):
        opening = _OPENING_FENCE.match(lines[index])
        if opening is None or (opening["fence"][0] == "`" and "`" in opening["info"]):
            index += 1
            continue
        fence, info = opening["fence"], opening["info"].split()
        closing = re.compile(rf"^[ \t]*{re.escape(fence[0])}{{{len(fence)},}}[ \t]*$")
        end = index + 1
        while end < len(lines) and not closing.match(lines[end]):
            end += 1
        blocks.append((index + 1, info[0].lower() if info else "", end - index - 1))
        index = end + 1
    return blocks


def _long_programs(text: str) -> list[tuple[int, str, int]]:
    return [
        block
        for block in _fenced_blocks(text)
        if block[1] in _PROGRAM_TAGS and block[2] > _MAX_PROGRAM_LINES
    ]


def test_every_skill_is_scanned() -> None:
    assert len(_SKILL_FILES) >= 20
    assert _REPO_ROOT / "skills" / "welcome" / "SKILL.md" in _SKILL_FILES


def test_guard_points_at_an_existing_rule() -> None:
    doc, anchor = _RULE.split("#")
    headings = re.findall(r"(?m)^#+ (.+)$", (_REPO_ROOT / doc).read_text(encoding="utf-8"))
    slugs = {
        re.sub(r"[^\w\- ]", "", heading).strip().lower().replace(" ", "-") for heading in headings
    }
    assert anchor in slugs


@pytest.mark.parametrize(
    "skill_path", _SKILL_FILES, ids=[path.parent.name for path in _SKILL_FILES]
)
def test_skill_has_no_long_inline_program(skill_path: Path) -> None:
    relative = skill_path.relative_to(_REPO_ROOT)
    offenders = _long_programs(skill_path.read_text(encoding="utf-8"))

    assert not offenders, "\n".join(
        f"{relative}:{line}: a ```{tag} block of {count} lines (limit {_MAX_PROGRAM_LINES}). "
        "Programs belong in the skill bundle's scripts/ directory next to SKILL.md; "
        "have SKILL.md run them with one command instead of asking the model to "
        f"retype them. See {_RULE}."
        for line, tag, count in offenders
    )


def _block(fence: str, info: str, body_lines: int, indent: str = "") -> str:
    body = [f"{indent}line {n}" for n in range(body_lines)]
    return "\n".join([f"{indent}{fence}{info}", *body, f"{indent}{fence}"]) + "\n"


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        (_block("```", "bash", 25), []),
        (_block("```", "bash", 26), [(1, "bash", 26)]),
        (_block("```", "sh", 26), [(1, "sh", 26)]),
        (_block("```", "shell", 30), [(1, "shell", 30)]),
        (_block("~~~", "python", 26), [(1, "python", 26)]),
        (_block("```", "Python title=x", 26), [(1, "python", 26)]),
        (_block("```", " bash", 26), [(1, "bash", 26)]),
        (_block("```", "bash", 26, indent="      "), [(1, "bash", 26)]),
        (_block("```", "markdown", 200), []),
        (_block("```", "", 200), []),
        (_block("```", "yaml", 200), []),
        ("````markdown\n" + _block("```", "bash", 30) + "````\n", []),
        ("````markdown\n" + "x\n" * 30 + "````\n" + _block("```", "bash", 26), [(33, "bash", 26)]),
        ("text\n```bash\n" + "x\n" * 26, [(2, "bash", 26)]),
        (_block("```", "bash", 3) + _block("```", "python", 26), [(6, "python", 26)]),
        ("``` `bash`\n" + "x\n" * 30, []),
    ],
)
def test_guard_counts_program_fences_like_commonmark(
    markdown: str, expected: list[tuple[int, str, int]]
) -> None:
    assert _long_programs(markdown) == expected
