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
    used = len(ac.strip())
    if used > budget.total_chars:
        # The caller violated the precondition. Don't try to salvage —
        # the orchestrator must split the AC further first.
        msg = (
            f"AC alone exceeds context budget "
            f"(ac={used} chars > total={budget.total_chars}); "
            "decompose further before dispatching."
        )
        raise ValueError(msg)

    sibling_lines: list[str] = []
    sibling_ceiling = budget.total_chars - budget.parent_summary_reserve
    for sib in siblings:
        line = sib.to_line()
        cost = len(line) + 1  # +1 for the newline join
        if used + cost > sibling_ceiling:
            break
        sibling_lines.append(line)
        used += cost

    remaining = budget.total_chars - used
    summary, truncated = _truncate(parent_summary.strip(), max(0, remaining))

    return ComposedContext(
        parent_summary=summary,
        sibling_lines=tuple(sibling_lines),
        ac=ac.strip(),
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
