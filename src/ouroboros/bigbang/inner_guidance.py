"""Inner-interviewer guidance contract for steering wrappers.

Any wrapper that composes ``InterviewEngine`` and prepends steering to its
system prompt must honor one rule: **the interview layer always has
priority**. Whatever guidance an unwrapped inner build retains completely
must also survive the steered build completely — steering yields otherwise.

This module owns both sides of that rule, so future wrappers (QA steering,
team-convention steering, …) get the guarantees without re-deriving them:

- Declaration: ``INNER_GUIDANCE_INVARIANTS`` — named, explained invariants
  that resolve their full marker texts per build.
- Enforcement: ``reserve_steering_extension`` + ``compose_steered_prompt``
  — the budget-extension composition in which steering rides in a reserved
  extension on the normal path (the inner build stays byte-identical to an
  unwrapped engine's designed-budget build) and falls back to atomic
  paragraph shedding when a caller-supplied cap cannot host both.

``interview.py`` itself is never modified; the extension is reserved via
instance attributes on the wrapper-owned engine only.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import re

from ouroboros.bigbang.interview import InterviewEngine, InterviewState


@dataclass(frozen=True)
class InnerGuidanceInvariant:
    """One piece of inner-interviewer guidance steering must never evict.

    Each invariant resolves to the marker texts to check for the current
    engine and state — resolution happens on every build so guidance always
    reflects the currently loaded prompts (operator prompt reloads via
    ``agents.loader.clear_cache()`` are picked up immediately; there is
    deliberately no cache at this layer).

    Attributes:
        name: Stable identifier for logging and tests.
        why: What interview behavior the guidance carries.
        resolve: Returns the full marker texts for (inner, state); empty
            strings are ignored. Full text matters — a heading or fragment
            surviving while its tail is cut would still defeat the policy.
    """

    name: str
    why: str
    resolve: Callable[[InterviewEngine, InterviewState], tuple[str, ...]]


def _current_inner_base_prompt_sections(_inner: InterviewEngine) -> tuple[str, ...]:
    """Sections of the inner interviewer base prompts, resolved per call.

    The inner builder truncates its base prompt from the end, so every
    ``##`` section the unconstrained build retains completely must also
    survive the steered build completely. Sections are collected from both
    base prompt variants; the parity filter against the actual unconstrained
    build decides which apply at runtime. Reads go through the agent loader
    (which owns caching and its ``clear_cache()`` invalidation), so operator
    prompt reloads are honored.
    """
    from ouroboros.agents.loader import load_agent_prompt
    from ouroboros.bigbang.interview import _TOOLLESS_INTERVIEW_BASE_PROMPT

    texts = [_TOOLLESS_INTERVIEW_BASE_PROMPT]
    try:
        texts.append(load_agent_prompt("socratic-interviewer"))
    except Exception:  # noqa: BLE001 - agent file is optional at runtime
        pass
    sections: list[str] = []
    for text in texts:
        sections.extend(s for s in re.split(r"(?=\n## )", text) if s.strip())
    return tuple(sections)


INNER_GUIDANCE_INVARIANTS: tuple[InnerGuidanceInvariant, ...] = (
    InnerGuidanceInvariant(
        name="initial-context",
        why="The user's own requirements text embedded in the header outranks steering",
        resolve=lambda inner, state: (
            (
                inner._initial_context_for_system_prompt(state.initial_context)
                if state.initial_context
                else ""
            ),
        ),
    ),
    InnerGuidanceInvariant(
        name="answer-prefix-legend",
        why=(
            "Interpretation contract for [from-code]/[from-user]/"
            "[from-research]/[from-data] answers"
        ),
        # Full final legend lines (both builder variants) PLUS the compact
        # under-pressure form (bot-review round-31). The header truncates
        # from the end, so the complete LAST line surviving implies the
        # whole legend above it is intact — and since round-29 the last
        # line is [from-data]; a bare fragment check would let the legend's
        # semantics be silently cut mid-line.
        resolve=lambda _inner, _state: (
            "- [from-data]: Data evidence from metrics/DB/warehouse queries "
            "(factual at query time, may be stale — treat as description, "
            "not decision).",
            "- [from-data]: Caller-supplied data evidence (factual at query time, may be stale).",
            "Prefixes: [from-code]=existing-system fact; [from-user]=human "
            "decision; [from-research]=external fact; [from-data]=point-in-time "
            "data description (not a decision, may be stale).",
        ),
    ),
    InnerGuidanceInvariant(
        name="brownfield-intent-hint",
        why="Keeps brownfield questions on intent and decisions, not code discovery",
        resolve=lambda _inner, _state: ("not on discovering what exists.",),
    ),
    InnerGuidanceInvariant(
        name="ambiguity-snapshot",
        why='Carries the "Weakest area" feedback steering philosophies govern',
        resolve=lambda inner, state: (inner._build_ambiguity_snapshot_prompt(state),),
    ),
    InnerGuidanceInvariant(
        name="perspective-panel",
        why="Breadth-recap / closure-mode / seed-ready interview safeguards",
        resolve=lambda inner, state: (inner._build_perspective_panel_prompt(state),),
    ),
    InnerGuidanceInvariant(
        name="base-prompt-sections",
        why="Interviewer role and context boundary rules (never promise implementation)",
        resolve=lambda inner, _state: _current_inner_base_prompt_sections(inner),
    ),
)


def required_guidance(
    inner: InterviewEngine,
    state: InterviewState,
    baseline: str,
    *,
    extra_candidates: tuple[str, ...] = (),
) -> list[str]:
    """Marker texts a steered build must preserve for this round.

    An invariant applies only when the unwrapped ``baseline`` build retains
    its full text — guidance the inner engine itself trims under the same
    budget is not the steering's debt.

    Args:
        inner: The wrapped engine.
        state: Current interview state.
        baseline: The unwrapped build at the designed budget.
        extra_candidates: Caller-known texts to protect under the same
            baseline filter (e.g. an effective initial-context override
            that only the composing wrapper can see).
    """
    candidates = [
        text for invariant in INNER_GUIDANCE_INVARIANTS for text in invariant.resolve(inner, state)
    ]
    candidates.extend(extra_candidates)
    return [text for text in candidates if text and text in baseline]


def reserve_steering_extension(inner: InterviewEngine, steering: str) -> None:
    """Widen a wrapper-owned engine's budgets by exactly the steering size.

    Instance attributes only — the ``InterviewEngine`` class defaults and
    every other instance (dev interviews) are untouched. Derived from the
    class attributes, so repeated calls never stack the extension. The
    engine then computes budgets and trims history against the widened
    numbers while its wire ceiling (``_MAX_TOTAL_PROMPT_CHARS``) stays
    enforced by the engine itself, so on the normal path the steering rides
    entirely inside the reserved extension and displaces nothing.
    """
    inner_cls = type(inner)
    extension = len(steering) + 2  # steering + "\n\n" separator
    inner._MAX_SYSTEM_PROMPT_CHARS = inner_cls._MAX_SYSTEM_PROMPT_CHARS + extension
    inner._MIN_SYSTEM_PROMPT_CHARS = inner_cls._MIN_SYSTEM_PROMPT_CHARS + extension


def fit_steering_paragraphs(
    steering: str,
    budget: int,
    *,
    shed_last_marker: str | None = None,
) -> str:
    """Fit whole steering paragraphs into ``budget`` characters.

    Paragraphs are included atomically: one that does not fit (with its
    trailing ``"\\n\\n"`` separator) is skipped whole, so a reduced budget
    can only narrow the steering policy — never cut a sentence in half or
    strip the exclusion clause off a paragraph and invert its meaning.

    Selection follows the shedding priority, not document order: the
    paragraph containing ``shed_last_marker`` (the wrapper's core policy)
    is placed first, then the remaining paragraphs in document order as
    budget allows. The fitted paragraphs are emitted in their original
    document order.

    Returns the fitted block ending with ``"\\n\\n"``, or ``""`` when no
    paragraph fits.
    """
    paragraphs = [p for p in steering.split("\n\n") if p.strip()]
    by_priority = sorted(
        range(len(paragraphs)),
        key=lambda i: (
            0 if shed_last_marker is not None and shed_last_marker in paragraphs[i] else 1,
            i,
        ),
    )
    fitted_len = 0
    chosen: set[int] = set()
    for index in by_priority:
        cost = len(paragraphs[index]) + 2  # paragraph + "\n\n" separator
        if fitted_len + cost <= budget:
            chosen.add(index)
            fitted_len += cost
    if not chosen:
        return ""
    return "\n\n".join(paragraphs[i] for i in sorted(chosen)) + "\n\n"


def shed_one_paragraph(block: str, *, shed_last_marker: str | None = None) -> str:
    """Drop the lowest-priority paragraph from a fitted steering block.

    Paragraphs are shed from the end, except the one containing
    ``shed_last_marker``, which is kept until nothing else remains.
    """
    paragraphs = [p for p in block.split("\n\n") if p.strip()]
    if len(paragraphs) <= 1:
        return ""
    for index in range(len(paragraphs) - 1, -1, -1):
        if shed_last_marker is None or shed_last_marker not in paragraphs[index]:
            del paragraphs[index]
            break
    else:
        paragraphs.pop()
    return "\n\n".join(paragraphs) + "\n\n"


def compose_steered_prompt(
    *,
    inner: InterviewEngine,
    build: Callable[..., str],
    steering: str,
    state: InterviewState,
    initial_context: str | None = None,
    max_chars: int | None = None,
    shed_last_marker: str | None = None,
) -> str:
    """Compose ``steering`` above an inner system prompt, interview-first.

    Requires ``reserve_steering_extension(inner, steering)`` to have been
    called so the engine's budgets carry the earmarked extension.

    Normal path: the caller's cap includes the reserved extension, so the
    inner ``build`` keeps the engine's *designed* budget — byte-identical
    to an unwrapped engine's output — and the full steering rides in the
    extension. The unwrapped comparison point is always the designed cap,
    never the widened one: the extension is earmarked for steering and
    must not raise the bar the inner build is held to.

    Tight path (caller-supplied small cap, or wire pressure in the
    history-decline zone): steering is fitted atomically and shed one
    paragraph at a time — the ``shed_last_marker`` paragraph last — until
    every ``INNER_GUIDANCE_INVARIANTS`` marker retained by the unwrapped
    baseline also survives the steered build, or no steering remains.

    Args:
        inner: The wrapper-owned engine (budget attributes are read here).
        build: The *unwrapped* system prompt builder to delegate to.
        steering: Full steering text (without trailing separator).
        state: Current interview state.
        initial_context: Optional prompt-safe context override.
        max_chars: Optional cap; defaults to the widened engine cap.
        shed_last_marker: Substring identifying the steering paragraph that
            carries the wrapper's core policy; it outlives the others.

    Returns:
        The composed system prompt, never longer than the effective cap.
    """
    inner_cls = type(inner)
    cap = max_chars or inner._MAX_SYSTEM_PROMPT_CHARS
    steering_block = steering + "\n\n"
    inner_budget = cap - len(steering_block)
    baseline_cap = min(inner_cls._MAX_SYSTEM_PROMPT_CHARS, cap)

    if inner_budget >= baseline_cap:
        base = build(state, initial_context=initial_context, max_chars=inner_budget)
        return steering_block + base

    baseline = build(state, initial_context=initial_context, max_chars=baseline_cap)
    # The effective context may be a caller-supplied override (e.g. the
    # prompt-safe summary for oversized contexts) that state-based
    # invariants cannot see; protect it under the same baseline filter.
    effective_context = initial_context if initial_context is not None else state.initial_context
    context_override = (
        inner._initial_context_for_system_prompt(effective_context) if effective_context else ""
    )
    required_markers = required_guidance(
        inner, state, baseline, extra_candidates=(context_override,)
    )

    if inner_budget >= inner_cls._MIN_SYSTEM_PROMPT_CHARS:
        base = build(state, initial_context=initial_context, max_chars=inner_budget)
        if all(marker in base for marker in required_markers):
            return steering_block + base

    steering_block = fit_steering_paragraphs(
        steering,
        budget=max(0, cap - min(cap, inner_cls._MIN_SYSTEM_PROMPT_CHARS)),
        shed_last_marker=shed_last_marker,
    )
    while True:
        # Never pass 0 down: the inner builder treats falsy max_chars as
        # "use the default cap", which would blow the budget again.
        inner_budget = max(1, cap - len(steering_block))
        base = (
            build(state, initial_context=initial_context, max_chars=inner_budget)
            if steering_block
            else baseline
        )
        if not steering_block or all(marker in base for marker in required_markers):
            # Hard cap as final safety net (degenerate tiny-cap case).
            return (steering_block + base)[:cap]
        steering_block = shed_one_paragraph(steering_block, shed_last_marker=shed_last_marker)
