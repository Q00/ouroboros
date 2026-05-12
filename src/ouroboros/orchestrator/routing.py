"""Adaptive model/tool routing (RFC v2 H5, #830).

H5 moves model and tool selection out of skills and into the harness.
The skill never picks a model. Each dispatch has a role (decomposer /
executor / verifier), and the harness chooses an appropriate tier and
tool set per role + profile.

Tiers are intentionally abstract strings rather than concrete model
IDs — the integration PR (PR 9) maps `ModelTier.HAIKU / SONNET / OPUS`
onto the adapter's current model knobs. Decoupling lets profile
authors think in cost/quality bands rather than vendor SKU drift.

Implementation status at this PR:

  DECOMPOSER (implemented)
    Tier HAIKU. Tools = (). Decomposition is structured-output
    planning; HAIKU keeps the per-AC fixed cost low and tools are
    intentionally empty.

  EXECUTOR (implemented)
    Tier SONNET by default. Escalates to OPUS when
    `fabrication_retry=True` per the H7 ESCALATE_MODEL hook.
    Tools come from `profile.suggested_tools` verbatim.

  VERIFIER (implemented)
    Routing verifier dispatches is based on the structured
    `ExecutionProfile.verifier_capability` field. Read-only discovery
    verifiers receive file-discovery tools only. Subprocess-test-runner
    verifiers still receive read-only discovery tools at the agent-tool
    layer and set `requires_external_test_runner=True`, so callers know
    to provide a harness-owned bounded runner instead of raw Bash.

This module is wiring-only. `parallel_executor` still uses its current
hardcoded adapter call. `decide_route()` takes role + profile + retry
hint — no AC text — until a profile demands per-AC routing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ouroboros.orchestrator.profile_loader import ExecutionProfile, VerifierCapability


class DispatchRole(StrEnum):
    """Which leg of the verifier loop this dispatch serves."""

    DECOMPOSER = "DECOMPOSER"
    EXECUTOR = "EXECUTOR"
    VERIFIER = "VERIFIER"


class ModelTier(StrEnum):
    """Abstract cost/quality tier — the adapter layer maps to concrete IDs."""

    HAIKU = "HAIKU"
    SONNET = "SONNET"
    OPUS = "OPUS"


_TIER_ORDER: tuple[ModelTier, ...] = (ModelTier.HAIKU, ModelTier.SONNET, ModelTier.OPUS)

# Tools available to verifier envelopes at the routing layer.
_VERIFIER_READ_ONLY_TOOLS: tuple[str, ...] = ("Read", "Glob", "Grep")
_VERIFIER_TEST_RUNNER_TOOLS: tuple[str, ...] = _VERIFIER_READ_ONLY_TOOLS


@dataclass(frozen=True)
class RouteDecision:
    """Resolved (tier, tools) for a single dispatch."""

    tier: ModelTier
    tools: tuple[str, ...]
    rationale: str
    requires_external_test_runner: bool = False


def _bump_tier(tier: ModelTier, *, steps: int = 1) -> ModelTier:
    """Return the tier `steps` levels above `tier`, capped at OPUS."""
    idx = _TIER_ORDER.index(tier)
    return _TIER_ORDER[min(idx + steps, len(_TIER_ORDER) - 1)]


def _executor_tier(
    profile: ExecutionProfile,  # noqa: ARG001 — reserved for per-profile overrides
    *,
    fabrication_retry: bool,
) -> ModelTier:
    base = ModelTier.SONNET
    if fabrication_retry:
        return _bump_tier(base)
    return base


def decide_route(
    *,
    role: DispatchRole,
    profile: ExecutionProfile,
    fabrication_retry: bool = False,
) -> RouteDecision:
    """Choose a tier and tool set for a single dispatch.

    Args:
        role: Which loop leg this dispatch is for.
        profile: Active ExecutionProfile (suggested_tools is the source
            of truth for the executor's tool set).
        fabrication_retry: True when retrying after H7 classified the
            previous attempt as FABRICATION_SUSPECTED. Escalates the
            executor one tier up (SONNET → OPUS). The verifier always
            runs one tier above the executor and is capped at OPUS, so
            in practice the verifier tier is unchanged on retry once
            the executor reaches OPUS — the cap, not the bump, defines
            the verifier on retry.

    Returns:
        RouteDecision with the chosen tier, the resolved tool tuple,
        and a one-line rationale for logs.

    Raises:
        TypeError: If `role` is not a `DispatchRole` member. The public
            routing seam fails fast on unknown inputs (e.g. a raw
            string from config/JSON) rather than silently falling
            through to the verifier branch with the wrong tools.
    """
    if not isinstance(role, DispatchRole):
        msg = (
            f"decide_route(role=...) requires a DispatchRole member, "
            f"got {role!r} of type {type(role).__name__}. "
            f"Valid roles: {[r.name for r in DispatchRole]}."
        )
        raise TypeError(msg)

    if role is DispatchRole.DECOMPOSER:
        return RouteDecision(
            tier=ModelTier.HAIKU,
            tools=(),
            rationale=(
                "Decomposition is structured-output planning; "
                "HAIKU keeps the per-AC fixed cost low."
            ),
        )

    if role is DispatchRole.EXECUTOR:
        tier = _executor_tier(profile, fabrication_retry=fabrication_retry)
        return RouteDecision(
            tier=tier,
            tools=profile.suggested_tools,
            rationale=(
                "Executor: SONNET by default; escalate to OPUS on "
                "FABRICATION_SUSPECTED retry per H7."
                if not fabrication_retry
                else "Executor: escalated one tier after FABRICATION_SUSPECTED."
            ),
        )

    if role is DispatchRole.VERIFIER:
        executor_tier = _executor_tier(profile, fabrication_retry=fabrication_retry)
        tier = _bump_tier(executor_tier)
        if profile.verifier_capability is VerifierCapability.READ_ONLY_DISCOVERY:
            return RouteDecision(
                tier=tier,
                tools=_VERIFIER_READ_ONLY_TOOLS,
                rationale=(
                    "Verifier: one tier above executor with read-only "
                    "discovery tools from verifier_capability."
                ),
            )
        if profile.verifier_capability is VerifierCapability.SUBPROCESS_TEST_RUNNER:
            return RouteDecision(
                tier=tier,
                tools=_VERIFIER_TEST_RUNNER_TOOLS,
                rationale=(
                    "Verifier: one tier above executor; profile requires a "
                    "harness-owned read-only subprocess test runner, so raw "
                    "Bash is not granted as an agent tool."
                ),
                requires_external_test_runner=True,
            )
        msg = f"Unhandled verifier_capability: {profile.verifier_capability!r}"
        raise NotImplementedError(msg)

    # Exhaustive — every DispatchRole member handled above. Reached only
    # if a new role is added without updating decide_route.
    msg = f"Unhandled DispatchRole: {role!r}"
    raise NotImplementedError(msg)


__all__ = [
    "DispatchRole",
    "ModelTier",
    "RouteDecision",
    "decide_route",
]
