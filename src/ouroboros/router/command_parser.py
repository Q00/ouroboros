"""Pure deterministic parser for supported Ouroboros skill command prefixes."""

from __future__ import annotations

import re

from ouroboros.router.types import ParsedOooCommand

_SKILL_COMMAND_PREFIX_PATTERN = re.compile(
    r"^\s*(?:(?P<ooo_prefix>ooo)\s+(?P<ooo_skill>[a-z0-9][a-z0-9_-]*)|"
    r"(?P<slash_prefix>/ouroboros:)(?P<slash_skill>[a-z0-9][a-z0-9_-]*))",
    re.IGNORECASE,
)


def _extract_command_remainder(prompt: str, start: int) -> tuple[bool, str | None]:
    """Extract command arguments while preserving multiline payload bytes."""
    if start >= len(prompt):
        return True, None
    if not prompt[start].isspace():
        return False, None

    for index in range(start, len(prompt)):
        char = prompt[index]
        if char == "\r":
            next_index = (
                index + 2 if index + 1 < len(prompt) and prompt[index + 1] == "\n" else index + 1
            )
            return True, prompt[next_index:]
        if char == "\n":
            return True, prompt[index + 1 :]
        if not char.isspace():
            return True, prompt[index:]

    return True, ""


def parse_ooo_command(prompt: str) -> ParsedOooCommand | None:
    """Parse supported deterministic skill command prefixes from ``prompt``.

    The parser is intentionally runtime-neutral: it performs no logging, emits
    no messages, and makes no MCP calls. It only recognizes exact command-start
    invocations, normalizes the command prefix casing and separator whitespace,
    lowercases the skill name, and preserves the argument text after the
    command separator.
    """
    match = _SKILL_COMMAND_PREFIX_PATTERN.match(prompt)
    if match is None:
        return None

    skill_name = (match.group("ooo_skill") or match.group("slash_skill") or "").lower()
    if not skill_name:
        return None
    valid_remainder, remainder = _extract_command_remainder(prompt, match.end())
    if not valid_remainder:
        return None

    command_prefix = (
        f"ooo {skill_name}" if match.group("ooo_skill") is not None else f"/ouroboros:{skill_name}"
    )
    return ParsedOooCommand(
        skill_name=skill_name,
        command_prefix=command_prefix,
        remainder=remainder,
    )


__all__ = ["ParsedOooCommand", "parse_ooo_command"]
