"""PRE/POST phase wrappers for leaf prompts (RFC v2 H3, #830).

H3 reframes "execution phases" from per-skill markdown (`phase-task-start`,
`phase-task-end`) into harness-injected wrappers. The orchestrator
prepends a [PRE] block (restate AC, list assumed preconditions, name the
evidence schema) and appends a [POST] block (emit evidence record, do
not declare DONE — harness adjudicates).

This module is pure prompt-composition surface. parallel_executor and
execution_strategy still pass through their existing system-prompt
fragments; PR 9 wires `wrap_prompt` into the dispatch path.

Why wrap at the harness layer instead of in skill markdown:
  - The wrappers reference the active ExecutionProfile.evidence_schema
    directly, so the format stays in lockstep with the H2 validator and
    can never drift.
  - Skills cannot opt out, so the "do not self-declare DONE" rule from
    H1 is mechanical, not rhetorical.
  - Per-domain skills shrink because they no longer carry phase prose.
"""

from __future__ import annotations

from dataclasses import dataclass

from ouroboros.orchestrator.profile_loader import EvidenceSchema, ExecutionProfile

_DEFAULT_PRE_HEADER = "[PRE — harness-injected; restate before any action]"
_DEFAULT_POST_HEADER = "[POST — harness-injected; emit and stop]"


@dataclass(frozen=True)
class WrappedPrompt:
    """Composed leaf prompt with provenance of each segment."""

    pre: str
    body: str
    post: str

    def render(self) -> str:
        """Join the three segments with double-newline separators."""
        return f"{self.pre}\n\n{self.body}\n\n{self.post}"


def _format_required(schema: EvidenceSchema) -> str:
    if not schema.required:
        return "(profile declares no required evidence fields)"
    return ", ".join(schema.required)


def _format_rejected(schema: EvidenceSchema) -> str:
    if not schema.rejected_if:
        return "(profile declares no automatic rejection rules)"
    return "; ".join(schema.rejected_if)


def _indent_ac(ac: str, indent: str = "  ") -> str:
    """Apply a constant indent to every line of an AC.

    Seed ACs are free-form text and may contain embedded newlines.
    Indenting only the first line lets subsequent lines run flush-left
    and visually escape the "Acceptance criterion to satisfy" section,
    merging with the harness instructions that follow (bot finding on
    #886 r2). Apply the indent to every non-empty line so the AC's
    section boundary is preserved.
    """
    return "\n".join(indent + line if line else line for line in ac.splitlines())


def build_pre_block(profile: ExecutionProfile, ac: str) -> str:
    """Compose the PRE wrapper for a single leaf dispatch.

    The leaf must restate the AC in its own words and surface its
    assumed preconditions before touching any tool. This is the verifier
    hook for SCOPE_CREEP — if the restatement drifts from the AC, the
    verifier catches it on the first read.

    The AC is passed through verbatim (no .strip()): seed ACs are free-
    form text and may legitimately carry leading indentation (indented
    code blocks, nested bullets, YAML snippets) or trailing whitespace.
    Stripping would corrupt formatting-sensitive ACs before they reach
    the leaf executor (bot finding on #886 r3).
    """
    return (
        f"{_DEFAULT_PRE_HEADER}\n"
        f"Active profile: {profile.profile!r} (axis: {profile.axis}).\n"
        f"Acceptance criterion to satisfy:\n{_indent_ac(ac)}\n\n"
        "Before touching any tool, restate this AC in one sentence and "
        "list every precondition you are assuming (paths, commands, "
        "external services). Do not begin execution if any precondition "
        "is unverified — surface the blocker instead."
    )


def build_post_block(profile: ExecutionProfile) -> str:
    """Compose the POST wrapper for a single leaf dispatch.

    Encodes the H1/H2 contract: emit a fenced JSON evidence record with
    the schema's required keys, and never self-declare DONE — the
    harness adjudicates via the verifier loop.
    """
    schema = profile.evidence_schema
    return (
        f"{_DEFAULT_POST_HEADER}\n"
        "When you finish, emit a single fenced JSON block on its own "
        "line, then stop. Required fields for this profile: "
        f"{_format_required(schema)}.\n"
        f"Automatic rejection rules: {_format_rejected(schema)}.\n\n"
        "Do not declare the task DONE in prose — the harness adjudicates "
        "via an external verifier pass. Your job ends when the evidence "
        "block is emitted."
    )


def wrap_prompt(profile: ExecutionProfile, ac: str, body: str) -> WrappedPrompt:
    """Compose a profile-aware [PRE] + body + [POST] leaf prompt.

    The `body` is the skill-/strategy-specific task instructions the
    existing dispatch path supplies. It is passed through verbatim —
    no stripping, no normalization — so formatting-sensitive content
    (indented code blocks, JSON examples, deliberately blank-prefixed
    markdown) survives intact (bot finding on #886 r2).
    """
    return WrappedPrompt(
        pre=build_pre_block(profile, ac),
        body=body,
        post=build_post_block(profile),
    )


__all__ = [
    "WrappedPrompt",
    "build_post_block",
    "build_pre_block",
    "wrap_prompt",
]
