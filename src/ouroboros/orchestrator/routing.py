"""Adaptive model/tool routing (RFC v2 H5, #830).

H5 moves model and tool selection out of skills and into the harness.
The skill never picks a model. Each dispatch has a role (decomposer /
executor / verifier), and the harness chooses an appropriate tier and
tool set per role + profile.

Tiers are intentionally abstract strings rather than concrete model
IDs — the integration PR (PR 9) maps `ModelTier.HAIKU / SONNET / OPUS`
onto the adapter's current model knobs. Decoupling lets profile
authors think in cost/quality bands rather than vendor SKU drift.

Routing rules at this PR:
    decomposer  → HAIKU, no tools
    executor    → SONNET (default) or OPUS for FABRICATION_SUSPECTED
                  retries (the H7 ESCALATE_MODEL hook).
                  Tools come from profile.suggested_tools.
    verifier    → one tier above the executor.
                  Tools are hard-fixed to Read / Glob / Grep — these are
                  the only operations the H1 read-only contract can
                  guarantee at the routing layer. The router CANNOT
                  authorize Bash on the verifier seam: Bash can mutate
                  the workspace and the router can't inspect command
                  text. Code-style profiles whose verifier_focus needs
                  to execute the project's test command must route
                  that through a dedicated read-only test runner (a
                  separate follow-up PR), not via this generic
                  verifier-tool envelope.

This module is wiring-only. parallel_executor still uses its current
hardcoded adapter call until PR 9. The docstring previously claimed
AC-aware routing — that is intentionally deferred; `decide_route()`
takes role + profile + retry hint, no AC, until a profile actually
demands per-AC routing logic (bot non-blocking suggestion on r2).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ouroboros.orchestrator.profile_loader import ExecutionProfile


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

# Tools available to a verifier at the routing layer. Strictly read-only
# discovery tools — the H1 contract is that a verifier cannot mutate the
# workspace, and the router cannot inspect Bash command text to enforce
# that, so Bash is excluded structurally rather than delegated to prompt
# obedience (bot finding on PR #889 r3 reversed r2's keep-Bash position).
# Profiles whose verifier_focus needs subprocess test execution must
# route through a dedicated read-only test runner — see module docstring.
_VERIFIER_TOOLS: tuple[str, ...] = ("Read", "Glob", "Grep")


@dataclass(frozen=True)
class RouteDecision:
    """Resolved (tier, tools) for a single dispatch."""

    tier: ModelTier
    tools: tuple[str, ...]
    rationale: str


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
            previous attempt as FABRICATION_SUSPECTED. Bumps the
            executor and the verifier one tier up.

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
        verifier_tier = _bump_tier(executor_tier)
        return RouteDecision(
            tier=verifier_tier,
            # Hard-fixed read-only set. NOT derived from profile.
            # Granting Bash here would let a verifier mutate the
            # workspace via shell commands — the router cannot enforce
            # read-only-ness on Bash invocations, so it must not
            # authorize Bash at this seam. Subprocess-based verifier
            # workflows route through a separate read-only test runner.
            tools=_VERIFIER_TOOLS,
            rationale=(
                f"Verifier runs one tier above the executor on "
                f"{profile.profile!r} profile; toolset hard-fixed to "
                f"{list(_VERIFIER_TOOLS)} so the H1 read-only contract "
                "is enforced structurally, not by prompt obedience."
            ),
        )

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
