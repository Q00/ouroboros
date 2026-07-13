"""Effort-first investment routing for the Agent-OS execution contract (RFC #1405).

The orchestrator decides an abstract reasoning-effort *level* per unit of work and
hands it to whichever runtime will execute that unit. Each runtime declares, via
:class:`~ouroboros.orchestrator.adapter.RuntimeCapabilities.reasoning_effort_support`,
whether it can ENFORCE the level through a native per-call knob (Claude Agent SDK
``effort``, Codex ``-c model_reasoning_effort``) or can only be *advised* of it.

This module is the single, pure decision point that sits between "what level do we
want" and "what each runtime can actually honor". Keeping it free of executor state
makes the policy testable in isolation and keeps the live executor a thin caller —
it lays ``parallel_executor`` on the capability contract instead of hard-coding a
backend-specific effort path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ouroboros.core.seed import InvestmentSpec
from ouroboros.orchestrator.adapter import ParamSupport

# Ordered weakest -> strongest. Shared vocabulary across the runtimes that expose
# an effort knob (Claude Agent SDK: low/medium/high/xhigh/max; Codex
# model_reasoning_effort: minimal/low/medium/high/xhigh). ``max`` is Claude-only
# and deliberately omitted from the ladder used for the notch rules so they never
# depend on a level a CLI runtime cannot accept.
EFFORT_LADDER: tuple[str, ...] = ("minimal", "low", "medium", "high", "xhigh")

# Default floor for the (legacy) one-notch-lower helper: never strip below "low".
DEFAULT_EFFORT_FLOOR = "low"

# Default ceiling for the retry raise rule: the strongest cross-runtime level.
DEFAULT_EFFORT_CEILING = EFFORT_LADDER[-1]

# Retry attempt at which a hard AC is escalated one notch. ``retry_attempt`` is 0
# on the initial dispatch, 1 on the first retry, 2 on the second — so a hard AC
# that has already burned one retry earns MORE reasoning, not less.
EFFORT_RAISE_RETRY_THRESHOLD = 2

# Effort modes recorded per unit so enforced rows can be told apart from advised
# ones — the distinction the deterministic frugality proof depends on.
EFFORT_MODE_ENFORCED = "enforced"
EFFORT_MODE_ADVISED = "advised"
EFFORT_MODE_NONE = "none"


def lower_one_notch(level: str, *, floor: str = DEFAULT_EFFORT_FLOOR) -> str:
    """Return ``level`` dropped one rung, never below ``floor``.

    Unknown levels (not on :data:`EFFORT_LADDER`) are returned unchanged — the
    caller chose a vocabulary this module does not model, so it is not this
    function's place to silently rewrite it.
    """
    if level not in EFFORT_LADDER:
        return level
    floor_index = EFFORT_LADDER.index(floor) if floor in EFFORT_LADDER else 0
    current_index = EFFORT_LADDER.index(level)
    return EFFORT_LADDER[max(floor_index, current_index - 1)]


def raise_one_notch(level: str, *, ceiling: str = DEFAULT_EFFORT_CEILING) -> str:
    """Return ``level`` lifted one rung, never above ``ceiling``.

    Unknown levels (not on :data:`EFFORT_LADDER`, e.g. Claude-only ``max``) are
    returned unchanged — the caller chose a vocabulary this module does not model,
    so it is not this function's place to silently rewrite it.
    """
    if level not in EFFORT_LADDER:
        return level
    ceiling_index = (
        EFFORT_LADDER.index(ceiling) if ceiling in EFFORT_LADDER else len(EFFORT_LADDER) - 1
    )
    current_index = EFFORT_LADDER.index(level)
    return EFFORT_LADDER[min(ceiling_index, current_index + 1)]


@dataclass(frozen=True)
class EffortDecision:
    """The effort level for one unit plus how the chosen runtime will honor it.

    Attributes:
        level: The reasoning-effort level to pass to ``execute_task``, or ``None``
            when no base effort is configured (the dormant default — no behavior
            change until an effort is wired in).
        mode: ``"enforced"`` when the runtime applies the level through a native
            per-call knob, ``"advised"`` when it cannot (the level is recorded but
            not guaranteed), or ``"none"`` when there is no level to route.
    """

    level: str | None
    mode: str

    @property
    def is_enforced(self) -> bool:
        return self.mode == EFFORT_MODE_ENFORCED and self.level is not None


@dataclass(frozen=True)
class InvestmentAssessment:
    """Normalized, auditable investment inputs used by effort policy."""

    difficulty: str
    stakes: str
    provenance: str
    confidence: str
    used_signals: tuple[str, ...]
    missing_signals: tuple[str, ...]
    can_cheapen: bool
    minimum_effort: str | None
    rationale: str

    def to_event_data(self) -> dict[str, Any]:
        """Return the exact policy inputs and outcome for event telemetry."""
        return {
            "difficulty": self.difficulty,
            "stakes": self.stakes,
            "provenance": self.provenance,
            "confidence": self.confidence,
            "used_signals": list(self.used_signals),
            "missing_signals": list(self.missing_signals),
            "can_cheapen": self.can_cheapen,
            "minimum_effort": self.minimum_effort,
            "rationale": self.rationale,
        }


def assess_investment(spec: InvestmentSpec | None) -> InvestmentAssessment:
    """Normalize optional AC metadata into a fail-safe policy assessment.

    Only complete low/low assessments from declared or measured, high-confidence
    inputs may authorize a one-notch reduction. Inferred or absent inputs are
    observe-only/raise-only. Any high axis requires at least high effort.
    """
    if spec is None:
        return InvestmentAssessment(
            difficulty="unknown",
            stakes="unknown",
            provenance="absent",
            confidence="low",
            used_signals=(),
            missing_signals=("difficulty", "stakes"),
            can_cheapen=False,
            minimum_effort=None,
            rationale="missing difficulty and stakes; base effort preserved",
        )

    difficulty = spec.difficulty or "unknown"
    stakes = spec.stakes or "unknown"
    missing_signals = tuple(
        signal
        for signal, value in (("difficulty", spec.difficulty), ("stakes", spec.stakes))
        if value is None
    )
    used_signals = tuple(
        signal
        for signal, value in (("difficulty", spec.difficulty), ("stakes", spec.stakes))
        if value is not None
    ) + ("provenance", "confidence")
    minimum_effort = "high" if "high" in {difficulty, stakes} else None
    can_cheapen = (
        not missing_signals
        and difficulty == "low"
        and stakes == "low"
        and spec.provenance in {"declared", "measured"}
        and spec.confidence == "high"
    )

    if missing_signals:
        rationale = f"missing {' and '.join(missing_signals)}; base effort preserved"
    elif minimum_effort is not None:
        high_axes = " and ".join(
            axis
            for axis, value in (("difficulty", difficulty), ("stakes", stakes))
            if value == "high"
        )
        rationale = f"high {high_axes} requires at least high effort"
    elif can_cheapen:
        rationale = (
            f"low difficulty and stakes with {spec.provenance} high-confidence evidence; "
            "one-notch reduction authorized"
        )
    elif spec.provenance in {"inferred", "absent"}:
        rationale = (
            f"{spec.provenance} provenance cannot authorize cheapening; base effort preserved"
        )
    elif spec.confidence != "high":
        rationale = (
            f"{spec.confidence} confidence cannot authorize cheapening; base effort preserved"
        )
    else:
        rationale = "assessment does not authorize cheapening; base effort preserved"

    return InvestmentAssessment(
        difficulty=difficulty,
        stakes=stakes,
        provenance=spec.provenance,
        confidence=spec.confidence,
        used_signals=used_signals,
        missing_signals=missing_signals,
        can_cheapen=can_cheapen,
        minimum_effort=minimum_effort,
        rationale=rationale,
    )


def _apply_investment_policy(level: str, assessment: InvestmentAssessment | None) -> str:
    if assessment is None:
        return level
    if assessment.can_cheapen and level in EFFORT_LADDER:
        current_index = EFFORT_LADDER.index(level)
        floor_index = EFFORT_LADDER.index(DEFAULT_EFFORT_FLOOR)
        if current_index > floor_index:
            level = EFFORT_LADDER[current_index - 1]
    if assessment.minimum_effort in EFFORT_LADDER and level in EFFORT_LADDER:
        level = EFFORT_LADDER[
            max(EFFORT_LADDER.index(level), EFFORT_LADDER.index(assessment.minimum_effort))
        ]
    return level


def decide_effort(
    reasoning_effort_support: ParamSupport,
    *,
    base_effort: str | None,
    is_decomposed_child: bool,
    retry_attempt: int = 0,
    ceiling: str = DEFAULT_EFFORT_CEILING,
    enforceable_levels: frozenset[str] | None = None,
    investment_assessment: InvestmentAssessment | None = None,
) -> EffortDecision:
    """Decide the per-unit effort level and whether the runtime will enforce it.

    Args:
        reasoning_effort_support: The chosen runtime's declared support, read from
            ``runtime.capabilities.reasoning_effort_support``.
        base_effort: The configured base level for full-strength units, or ``None``
            to leave effort routing dormant.
        is_decomposed_child: Whether this unit is a verified-MECE child. Retained as
            a first-class routing/proof flag, but **no longer lowers the level** (V5):
            a harder decomposed child inherits the parent tier unchanged — it needs at
            least as much reasoning as its parent, never less.
        retry_attempt: Same-runtime retry index for this unit (0 on the initial
            dispatch). From :data:`EFFORT_RAISE_RETRY_THRESHOLD` onward the level is
            raised one notch (capped at ``ceiling``): a hard AC that keeps failing
            earns MORE reasoning.
        ceiling: The strongest level the retry raise may reach.
        enforceable_levels: The runtime's enforceable vocabulary
            (``capabilities.enforceable_reasoning_efforts``). When provided, a level
            outside it is recorded as *advised* even on a NATIVE runtime, because the
            backend silently drops a level it does not accept (Codex ignores ``max``,
            Claude has no ``minimal``). ``None`` imposes no per-level restriction.
        investment_assessment: Optional normalized AC assessment. It may authorize
            one lower notch or impose a minimum effort before retry escalation.

    Returns:
        An :class:`EffortDecision`. ``mode`` is ``"enforced"`` only when the runtime
        declared ``NATIVE`` support **and** the chosen level is one it actually
        enforces, so a silently-dropped or advised level can never be mistaken for an
        enforced one — exactly the property the proof's enforced rows rely on.
    """
    # ``is_decomposed_child`` is intentionally not consulted for the level: V5
    # stopped lowering decomposed children so they inherit the parent tier. It
    # stays in the signature because live call sites pass it by keyword and the
    # frugality proof still records it as an admission flag.
    del is_decomposed_child

    if not base_effort:
        return EffortDecision(level=None, mode=EFFORT_MODE_NONE)

    level = _apply_investment_policy(base_effort, investment_assessment)
    if retry_attempt >= EFFORT_RAISE_RETRY_THRESHOLD:
        level = raise_one_notch(level, ceiling=ceiling)
    enforces_level = enforceable_levels is None or level in enforceable_levels
    mode = (
        EFFORT_MODE_ENFORCED
        if reasoning_effort_support is ParamSupport.NATIVE and enforces_level
        else EFFORT_MODE_ADVISED
    )
    return EffortDecision(level=level, mode=mode)


def resolve_execute_effort(
    adapter: object,
    *,
    base_effort: str | None,
    is_decomposed_child: bool,
    retry_attempt: int = 0,
    ceiling: str = DEFAULT_EFFORT_CEILING,
    investment_assessment: InvestmentAssessment | None = None,
) -> tuple[EffortDecision, dict[str, str]]:
    """Decide effort for one ``execute_task`` call and build its kwargs.

    The single place every live execute_task call site lays itself on the
    capability contract. Reads ``adapter.capabilities.reasoning_effort_support``
    (defaulting to IGNORED when an adapter declares no capabilities), decides the
    level, and returns the ``execute_task`` kwargs — which are **empty unless the
    runtime enforces effort**, so a runtime that does not accept the parameter is
    never handed it.

    ``investment_assessment`` is applied before ``retry_attempt``, so runtime
    failures can only raise later attempts (see :func:`decide_effort`).

    Returns:
        ``(decision, execute_kwargs)``. ``execute_kwargs`` is ``{"reasoning_effort":
        <level>}`` only when the chosen runtime declared NATIVE support, else ``{}``.
    """
    capabilities = getattr(adapter, "capabilities", None)
    support = (
        capabilities.reasoning_effort_support if capabilities is not None else ParamSupport.IGNORED
    )
    enforceable_levels = (
        getattr(capabilities, "enforceable_reasoning_efforts", None)
        if capabilities is not None
        else None
    )
    decision = decide_effort(
        support,
        base_effort=base_effort,
        is_decomposed_child=is_decomposed_child,
        retry_attempt=retry_attempt,
        ceiling=ceiling,
        enforceable_levels=enforceable_levels,
        investment_assessment=investment_assessment,
    )
    kwargs = {"reasoning_effort": decision.level} if decision.is_enforced else {}
    return decision, kwargs
