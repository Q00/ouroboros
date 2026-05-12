"""Per-dispatch context budget governance (RFC v2 H6, #830).

H6 keeps the harness in control of what flows into each leaf dispatch.
Without governance, the layered PRE/POST wrappers (H3) plus profile
metadata plus sibling status plus parent summary plus the AC text plus
the body can balloon a single dispatch well past a healthy budget.

This module gives the dispatch path a single, deterministic place to
compose those segments, summarise sibling status to status lines
(never bodies), and trim parent context when the budget is tight.

The budget is measured in characters here rather than tokens. Char
budgets are deterministic, profile-author-readable, and good enough
for the structural guardrail H6 is about — tightening to tokenizer
counts can come later when there's evidence the char approximation
misses something load-bearing.

This PR is wiring-only. parallel_executor still composes context
ad-hoc until PR 9 routes context assembly through compose_context.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

DEFAULT_TOTAL_CHARS: int = 12_000
DEFAULT_PARENT_SUMMARY_RESERVE: int = 1_500
_TRUNCATED_SUFFIX: str = "\n…[truncated by context governor]"

# Fixed overheads that ComposedContext.render() introduces. The budget
# must charge against these so the rendered output stays under the
# advertised hard limit.
_AC_HEADER: str = "## AC\n"
_PARENT_HEADER: str = "## Parent context\n"
_SIBLING_HEADER: str = "## Sibling status\n"
_SECTION_JOINER: str = "\n\n"
# Module-level singleton so callers can pass-through "use defaults" without
# constructing the frozen dataclass in argument defaults (ruff B008).
_DEFAULT_BUDGET: ContextBudget


@dataclass(frozen=True)
class ContextBudget:
    """Per-dispatch budget knobs.

    Attributes:
        total_chars: Hard upper bound on the composed context size.
        parent_summary_reserve: Minimum chars guaranteed for the
            parent summary even when the total budget is tight.
    """

    total_chars: int = DEFAULT_TOTAL_CHARS
    parent_summary_reserve: int = DEFAULT_PARENT_SUMMARY_RESERVE

    def __post_init__(self) -> None:
        if self.total_chars <= 0:
            msg = f"total_chars must be positive, got {self.total_chars}"
            raise ValueError(msg)
        if self.parent_summary_reserve < 0:
            msg = f"parent_summary_reserve must be >= 0, got {self.parent_summary_reserve}"
            raise ValueError(msg)
        if self.parent_summary_reserve > self.total_chars:
            msg = (
                f"parent_summary_reserve ({self.parent_summary_reserve}) "
                f"cannot exceed total_chars ({self.total_chars})"
            )
            raise ValueError(msg)


@dataclass(frozen=True)
class SiblingStatus:
    """One-line status of a sibling AC.

    Carries the minimum signal the leaf needs (id, accepted) without
    pulling in the sibling's body or evidence record.
    """

    sibling_id: str
    accepted: bool
    headline: str = ""

    def to_line(self) -> str:
        marker = "✓" if self.accepted else "✗"
        head = f": {self.headline.strip()}" if self.headline else ""
        return f"{marker} {self.sibling_id}{head}"


@dataclass(frozen=True)
class ComposedContext:
    """Output of compose_context — the dispatch path renders this."""

    parent_summary: str
    sibling_lines: tuple[str, ...]
    ac: str
    truncated: bool

    def render(self) -> str:
        parts: list[str] = []
        if self.parent_summary:
            parts.append(f"## Parent context\n{self.parent_summary}")
        if self.sibling_lines:
            parts.append(
                "## Sibling status\n" + "\n".join(self.sibling_lines),
            )
        parts.append(f"## AC\n{self.ac}")
        return "\n\n".join(parts)


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    """Hard char-truncate `text` to `limit`, marking when it happened."""
    if len(text) <= limit:
        return text, False
    if limit <= len(_TRUNCATED_SUFFIX):
        return text[:limit], True
    head = text[: limit - len(_TRUNCATED_SUFFIX)]
    return head + _TRUNCATED_SUFFIX, True


def compose_context(
    *,
    ac: str,
    parent_summary: str = "",
    siblings: Iterable[SiblingStatus] = (),
    budget: ContextBudget | None = None,
) -> ComposedContext:
    """Assemble a single dispatch's context under the budget.

    Order of operations:
        1. AC text is non-negotiable; it goes in verbatim. If the AC
           alone exceeds the budget, callers must have decomposed it
           further before reaching this point — that is the
           orchestrator's responsibility, not this module's.
        2. Sibling lines are appended one at a time until they would
           push the running total past (budget - parent_summary_reserve).
           Status lines stay terse by construction (no bodies).
        3. Parent summary is truncated to whatever space remains.

    The result is a `ComposedContext` whose `.render()` produces the
    final string. `truncated` is True when the parent summary was
    char-truncated; siblings dropped silently due to budget pressure
    do not set the flag (the dropped lines were never load-bearing).
    """
    if budget is None:
        budget = _DEFAULT_BUDGET

    # The AC text is contract-bound to flow through verbatim — leading
    # indentation, trailing newlines, and whitespace-significant
    # fenced/code-block content carry prompt semantics the dispatch
    # path must not rewrite. The summary is the only place where we
    # apply .strip() (it's free prose).
    parent_stripped = parent_summary.strip()

    # The AC section is non-negotiable. Charge the header against the
    # budget here so the rendered output (which always carries the
    # "## AC\n" prefix) stays under total_chars.
    ac_section_cost = len(_AC_HEADER) + len(ac)
    if ac_section_cost > budget.total_chars:
        msg = (
            f"AC alone exceeds context budget "
            f"(ac={len(ac)} chars + header={len(_AC_HEADER)} "
            f"> total={budget.total_chars}); decompose further before "
            "dispatching."
        )
        raise ValueError(msg)
    used = ac_section_cost

    # Sibling section bookkeeping: include the header + section-joiner
    # cost only if at least one sibling line lands in the output.
    sibling_lines: list[str] = []
    sibling_overhead = len(_SIBLING_HEADER) + len(_SECTION_JOINER)
    sibling_inner = 0  # bytes inside the section (lines + newline joins).
    # The parent-summary reserve is a floor only when there's a parent
    # summary to place. With no parent, withholding the reserve from
    # siblings would silently discard sibling status that fits in the
    # total budget (bot finding on #890 r2). When a parent IS present,
    # the reserve must also account for the parent section's own
    # render overhead ("## Parent context\n" + "\n\n"); otherwise
    # siblings can eat into bytes meant for the parent header, and
    # the actual parent_summary content lands below the reserved size
    # (bot finding on #890 r3).
    parent_overhead = len(_PARENT_HEADER) + len(_SECTION_JOINER)
    sibling_ceiling = budget.total_chars
    if parent_stripped:
        sibling_ceiling -= budget.parent_summary_reserve + parent_overhead
    for sib in siblings:
        line = sib.to_line()
        # `\n` joiner between sibling lines, only after the first.
        joiner_cost = 1 if sibling_lines else 0
        prospective_section = sibling_overhead + sibling_inner + joiner_cost + len(line)
        if used + prospective_section > sibling_ceiling:
            break
        sibling_inner += joiner_cost + len(line)
        sibling_lines.append(line)
    if sibling_lines:
        used += sibling_overhead + sibling_inner

    # Parent summary section: header + joiner only count if the summary
    # actually lands. Without enough room for the header itself, the
    # summary is dropped entirely and the flag is set so callers can
    # log it. (parent_overhead defined above when computing
    # sibling_ceiling.)
    remaining_for_parent = budget.total_chars - used
    if parent_stripped and remaining_for_parent > parent_overhead:
        summary_budget = remaining_for_parent - parent_overhead
        summary, truncated = _truncate(parent_stripped, summary_budget)
    elif parent_stripped:
        # Not enough headroom even for the parent header — drop it.
        summary, truncated = "", True
    else:
        summary, truncated = "", False

    return ComposedContext(
        parent_summary=summary,
        sibling_lines=tuple(sibling_lines),
        ac=ac,
        truncated=truncated,
    )


_DEFAULT_BUDGET = ContextBudget()


__all__ = [
    "DEFAULT_PARENT_SUMMARY_RESERVE",
    "DEFAULT_TOTAL_CHARS",
    "ComposedContext",
    "ContextBudget",
    "SiblingStatus",
    "compose_context",
]
