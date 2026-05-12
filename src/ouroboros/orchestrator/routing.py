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
        NotImplementedError: For DispatchRole.VERIFIER. Routing
            verifier dispatches requires a structured capability flag
            on ExecutionProfile (read-only-discovery vs subprocess-
            test-runner) that does not yet exist. Earlier rounds tried
            substring-matching `verifier_focus` text — that was
            rejected as fragile. The seam intentionally refuses to
            guess; add a `verifier_capability` field to
            ExecutionProfile (#881 follow-up) or plumb a custom
            verifier dispatcher before using VERIFIER routing.
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
        # ExecutionProfile does not yet expose a structured capability
        # flag describing what verifier envelope each profile actually
        # needs (read-only discovery vs subprocess test runner). The
        # earlier rounds tried two approximations:
        #   r3 — silently return (Read, Glob, Grep) for every profile.
        #        Wrong for code profile whose verifier_focus needs
        #        `pytest`.
        #   r4 — substring-match `verifier_focus` for subprocess markers.
        #        Fragile: prose changes break runtime behavior.
        # Until ExecutionProfile carries a structured `verifier_capability`
        # field, the honest answer at this seam is "I don't know what
        # this profile needs". Fail fast so the caller (#891 wiring
        # follow-ups) cannot accidentally route a verifier through an
        # envelope that may not match the profile's contract
        # (bot finding on #889 r5).
        msg = (
            f"DispatchRole.VERIFIER routing for profile "
            f"{profile.profile!r} is not implemented: ExecutionProfile "
            "does not yet expose a structured capability flag "
            "(read-only-discovery vs subprocess-test-runner), and the "
            "router will not infer it from free-form verifier_focus "
            "text. Add a `verifier_capability` field to ExecutionProfile "
            "(follow-up to #881) before wiring the verifier seam, or "
            "plumb a custom verifier dispatcher for this profile."
        )
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
