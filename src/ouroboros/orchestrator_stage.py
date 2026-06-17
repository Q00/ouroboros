"""``Stage`` — closed enumeration of orchestrator pipeline stages.

Issue #519 — slice 1 of M4 / S3. The Agent OS architecture diagram
agreed in #476 assigns a different harness per pipeline stage:

* **interview** — Codex (clarification, ambiguity reduction)
* **execute** — OpenCode / OMX (the running of the AC tree)
* **evaluate** — Claude Code (Stage 1/2/3 verification)
* **reflect** — Hermes (Wonder/Reflect generation)

This module is the *binding-table primitive* the orchestrator reads to
pick a runtime per stage. The four stages above are the **closed**
initial vocabulary; adding a new stage is an explicit, justified PR
(per the narrow-membership rule the maintainer alignment in #476 Q1
applied to ``AgentRuntimeContext``). That stops the table from
sprawling into per-handler entries (``qa_judge``, ``unstuck`` …)
which belong inside an :class:`AgentProcess` (#518), not in the
binding table.

The module deliberately exposes nothing more than the enum and a
small resolution helper. The resolution rule itself is pinned by the
sub-thread:

::

    runtime = (
        runtime_profile.stages.get(stage)         # explicit per-stage
        or runtime_profile.default                # opt-in default
        or current_orchestrator_runtime_backend   # today's behaviour
    )

When a config has ``runtime_profile=None`` (or omits the block
entirely), :func:`resolve_runtime_for_stage` falls back to the
existing ``orchestrator.runtime_backend`` byte-for-byte — that is the
backwards-compat commitment carried forward from PR #505.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Final


class Stage(StrEnum):
    """Closed enumeration of pipeline stages routed by ``runtime_profile``.

    Adding a member requires (a) a stage name, (b) documentation of
    which workflow phase it covers, (c) a justification line in the
    PR body explaining why an existing stage cannot host the work.
    """

    INTERVIEW = "interview"
    EXECUTE = "execute"
    EVALUATE = "evaluate"
    REFLECT = "reflect"


VALID_STAGE_KEYS: Final[frozenset[str]] = frozenset(stage.value for stage in Stage)

INTERVIEW_LLM_ROLES: Final[frozenset[str]] = frozenset(
    {
        "interview",
        "clarification",
        "seed_generation",
        "pm_interview",
        "pm_document",
        "brownfield",
        "brownfield_explore",
        "question_classification",
        "ambiguity",
        "double_diamond",
        "agent_runtime_interview",
    }
)
EVALUATE_LLM_ROLES: Final[frozenset[str]] = frozenset(
    {
        "semantic_evaluation",
        "assertion_extraction",
        "mechanical_detection",
        "consensus",
        "consensus_advocate",
        "consensus_perspective",
        "consensus_vote",
        "consensus_judge",
        "qa",
        "dependency_analysis",
        "ontology_analysis",
        "agent_runtime_evaluation",
    }
)
REFLECT_LLM_ROLES: Final[frozenset[str]] = frozenset(
    {
        "wonder",
        "reflect",
        "lateral",
        "context_compression",
    }
)
# Execution-phase planning roles. They have an EXECUTE-stage backend but no
# dedicated stage *model* field, so model resolution falls through to the
# evaluate model (or an explicit legacy override).
EXECUTE_LLM_ROLES: Final[frozenset[str]] = frozenset(
    {
        "atomicity",
        "decomposition",
        "agent_runtime_implementation",
    }
)

LLM_ROLE_STAGE_MAP: Final[dict[str, Stage]] = {
    **dict.fromkeys(INTERVIEW_LLM_ROLES, Stage.INTERVIEW),
    **dict.fromkeys(EVALUATE_LLM_ROLES, Stage.EVALUATE),
    **dict.fromkeys(REFLECT_LLM_ROLES, Stage.REFLECT),
    **dict.fromkeys(EXECUTE_LLM_ROLES, Stage.EXECUTE),
}


class UnknownStageError(ValueError):
    """Raised when a runtime_profile.stages key is not a valid stage.

    The error message names the offending key and the valid set so
    operators see typos at startup rather than mid-workflow.
    """


class UnknownLLMRoleError(ValueError):
    """Raised when a logical internal-LLM role has no stage binding."""


def parse_stage(value: str) -> Stage:
    """Parse a string into a :class:`Stage`, raising on unknown values.

    Used at startup to validate ``runtime_profile.stages`` keys.
    Unknown keys raise :class:`UnknownStageError` so a typo in
    ``interveiw`` fails fast at config load.
    """
    if value not in VALID_STAGE_KEYS:
        valid_list = ", ".join(sorted(VALID_STAGE_KEYS))
        raise UnknownStageError(
            f"Unknown runtime_profile stage key: {value!r}. Valid keys are: {valid_list}.",
        )
    return Stage(value)


def normalize_llm_role(value: str) -> str:
    """Normalize a logical internal-LLM role key for table lookup."""
    return value.strip().lower().replace("-", "_")


def stage_for_llm_role(role: str) -> Stage:
    """Return the pipeline stage responsible for an internal-LLM role.

    The table is intentionally role-sized, not handler-sized: callers label
    a request as ``"qa"`` or ``"reflect"``, then stage routing decides which
    configured Agent/backend serves it.
    """
    normalized = normalize_llm_role(role)
    try:
        return LLM_ROLE_STAGE_MAP[normalized]
    except KeyError as exc:
        valid_list = ", ".join(sorted(LLM_ROLE_STAGE_MAP))
        raise UnknownLLMRoleError(
            f"Unknown internal LLM role: {role!r}. Valid roles are: {valid_list}.",
        ) from exc


def resolve_runtime_for_stage(
    stage: Stage,
    *,
    stages: dict[Stage, str] | None,
    default: str | None,
    fallback: str,
) -> str:
    """Return the runtime backend that should serve ``stage``.

    Resolution order locked in the #519 sub-thread:

    1. ``stages[stage]`` — explicit per-stage mapping wins.
    2. ``default`` — when set, the runtime_profile's own default.
    3. ``fallback`` — today's hard-coded ``orchestrator.runtime_backend``.

    Args:
        stage: The pipeline stage being resolved.
        stages: Optional explicit stage→runtime mapping. ``None`` means
            "no stage block configured".
        default: Optional ``runtime_profile.default``. ``None`` means
            "no runtime_profile default configured".
        fallback: The today-behaviour fallback (the orchestrator's
            top-level ``runtime_backend``). Always provided so the
            resolution function never returns ``None``.

    Returns:
        The runtime backend identifier (e.g. ``"codex"``, ``"opencode"``)
        that should serve the given stage.
    """
    if stages is not None:
        explicit = stages.get(stage)
        if explicit:
            return explicit
    if default:
        return default
    return fallback


def resolve_runtime_for_llm_role(
    role: str,
    *,
    stages: Mapping[Stage, str] | None,
    default: str | None,
    fallback: str,
) -> str:
    """Return the runtime/LLM backend that should serve ``role``.

    This is the public bridge between internal LLM call roles and the
    stage-level runtime profile. It deliberately delegates final resolution
    to :func:`resolve_runtime_for_stage` so the UI's "runs on X" label and
    the actual internal call backend share one rule.

    Fail-open by design: a role with no stage binding resolves to ``fallback``
    (the orchestrator runtime backend) rather than raising, so an out-of-tree
    or newly-introduced role degrades to a safe default instead of crashing a
    live request. Use :func:`stage_for_llm_role` directly when strict
    validation is wanted.
    """
    try:
        stage = stage_for_llm_role(role)
    except UnknownLLMRoleError:
        return fallback
    return resolve_runtime_for_stage(
        stage,
        stages=dict(stages) if stages is not None else None,
        default=default,
        fallback=fallback,
    )
