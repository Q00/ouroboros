"""Map :class:`StepAction` outcomes onto the :class:`Directive` vocabulary.

Issue #516 — slice 1 of #472. Per the maintainer alignment in #476 Q5,
the evolution loop is the first emission site that translates an
existing local enum (``StepAction``) into a :class:`Directive`. The
mapping is intentionally additive: ``StepAction`` itself is *not*
removed; existing callers continue to consume it. This module exposes a
pure function that the loop calls just before returning a
:class:`StepResult` so the directive event lands alongside the existing
``lineage.*`` events.

Implementation note: the function matches on the ``StrEnum`` value via
``str(action)`` so this module does **not** import from
:mod:`ouroboros.evolution.loop`. That avoids a circular import — the
loop module imports this one — while still accepting any ``StepAction``
instance because ``StrEnum`` instances stringify to their value.

The mapping is unambiguous for terminal outcomes; the only context-
dependent case is ``StepAction.FAILED`` where the resilience budget
decides whether the directive is :attr:`Directive.RETRY` or
:attr:`Directive.CANCEL`. The evolution loop itself does not own the
budget — when called without a budget hint it emits ``RETRY`` and the
resilience layer (Tier-1 M6 in #476) decides whether the loop runs
again.

``StepAction.CONTINUE`` is *not* mapped to a directive emission. A
``CONTINUE`` step is the no-op case ("proceed with the current plan");
emitting an event for every CONTINUE would flood the journal without
adding signal. The journal still has the underlying
``lineage.generation.completed`` event for replay purposes; only
*decision points* warrant a control-plane directive.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ouroboros.core.directive import Directive

if TYPE_CHECKING:  # pragma: no cover — type-only import, avoids cycle
    from ouroboros.evolution.loop import StepAction


def step_action_to_directive(
    action: StepAction | str,
    *,
    retry_budget_remaining: int = 1,
) -> Directive | None:
    """Translate a ``StepAction`` (or its string value) into a ``Directive``.

    Args:
        action: Outcome of a single :meth:`EvolutionaryLoop.evolve_step`
            invocation. Accepts the :class:`StepAction` enum or its
            string value (the enum is a ``StrEnum`` so they compare
            equal at runtime).
        retry_budget_remaining: Number of retries the resilience layer
            still authorizes for this lineage. Only consulted when
            ``action`` is ``failed``. The default of 1 preserves the
            "best-effort retry" stance until the resilience layer is
            wired in (`#475`).

    Returns:
        The :class:`Directive` to emit, or ``None`` if the outcome does
        not warrant a directive emission. ``continue`` is the only
        outcome that returns ``None``.

    Mapping table:

    ============================  ==================================
    ``StepAction``                ``Directive``
    ============================  ==================================
    ``CONTINUE``                  ``None`` (no emission)
    ``CONVERGED``                 ``CONVERGE``
    ``STAGNATED``                 ``UNSTUCK``
    ``EXHAUSTED``                 ``CANCEL``
    ``FAILED`` (budget > 0)       ``RETRY``
    ``FAILED`` (budget == 0)      ``CANCEL``
    ``INTERRUPTED``               ``CANCEL``
    ============================  ==================================
    """
    value = str(action)
    if value == "continue":
        return None
    if value == "converged":
        return Directive.CONVERGE
    if value == "stagnated":
        return Directive.UNSTUCK
    if value == "exhausted":
        return Directive.CANCEL
    if value == "failed":
        return Directive.RETRY if retry_budget_remaining > 0 else Directive.CANCEL
    if value == "interrupted":
        return Directive.CANCEL
    # Unknown action values are forward-compatible (e.g., a future
    # StepAction member that lands before this mapping is updated). The
    # caller treats ``None`` as "do not emit" so unknown values are
    # gracefully ignored rather than raising.
    return None


def is_terminal_directive(directive: Directive) -> bool:
    """Return ``True`` if *directive* ends the lineage chain.

    Aligns with the ``is_terminal`` payload field on
    ``control.directive.emitted`` so projectors (#514) can collapse
    timelines visually.
    """
    return directive in {Directive.CONVERGE, Directive.CANCEL}


# Canonical watchdog timeout kinds. Sourced from
# ``ouroboros.evolution.watchdog.GenerationProgressWatchdog._raise_timeout``
# — kept as a module-level constant so the mapping below and tests can
# share the alphabet without round-tripping a string back into the
# watchdog. Update both together when the watchdog grows a new
# threshold.
WATCHDOG_TIMEOUT_KINDS: frozenset[str] = frozenset(
    {
        "safety_timeout",
        "idle_timeout",
        "no_material_progress_timeout",
    }
)

# Watchdog-timeout → directive lookup. The mapping is conservative by
# design (see #578 maintainer comment):
#
# - ``safety_timeout`` is the hard absolute upper bound. Once exceeded
#   the runtime CANNOT continue regardless of whether work was
#   happening; the only safe directive is the terminal ``CANCEL``.
# - ``idle_timeout`` means the EventStore observed zero activity for
#   the threshold window. With no live signal, neither ``RETRY``
#   (re-run the same hung unit) nor ``UNSTUCK`` (lateral persona —
#   itself depends on receiving activity) can recover the lineage.
#   ``CANCEL`` is the defensive default; an operator can re-issue a
#   fresh attempt out-of-band if desired.
# - ``no_material_progress_timeout`` is the canonical "stuck doing
#   busywork" signal: events keep arriving but no material progress
#   accrues. That is exactly the precondition ``UNSTUCK`` exists for
#   (invoke a lateral persona to change approach), so we route the
#   directive accordingly.
#
# WAIT is intentionally absent: a watchdog firing means the runtime
# already waited past its budget. Returning WAIT would ask the runtime
# to wait longer, which is the failure mode the watchdog exists to
# prevent. RETRY is also absent — the watchdog acts at the
# *generation* boundary, not the *step* boundary; retry budgeting
# lives one layer down (``step_action_to_directive``).
_WATCHDOG_TIMEOUT_DIRECTIVES: dict[str, Directive] = {
    "safety_timeout": Directive.CANCEL,
    "idle_timeout": Directive.CANCEL,
    "no_material_progress_timeout": Directive.UNSTUCK,
}


def watchdog_timeout_to_directive(timeout_kind: str) -> Directive | None:
    """Translate a watchdog ``timeout_kind`` into a control ``Directive``.

    Issue #578 — Directive mapping for the RuntimeControls watchdog.
    Mirrors :func:`step_action_to_directive`: the loop already maps
    ``StepAction`` outcomes onto the shared ``Directive`` vocabulary,
    and the watchdog needs the same translation so its timeout
    decisions land on the control plane alongside step-level
    directives instead of as opaque local errors.

    Args:
        timeout_kind: The ``timeout_kind`` field carried on
            :class:`GenerationWatchdogTimeout` and on the
            ``lineage.generation.watchdog_decision`` event payload.
            Canonical values come from
            :data:`WATCHDOG_TIMEOUT_KINDS`.

    Returns:
        The :class:`Directive` to emit, or ``None`` when
        ``timeout_kind`` is unrecognized. Forward-compatible: a future
        watchdog threshold name that lands before this mapping is
        updated will silently be treated as "do not emit a directive",
        matching the no-op behaviour of
        :func:`step_action_to_directive` for unknown
        ``StepAction`` values.

    Mapping table:

    ====================================  =========================
    ``timeout_kind``                      ``Directive``
    ====================================  =========================
    ``safety_timeout``                    ``CANCEL``
    ``idle_timeout``                      ``CANCEL``
    ``no_material_progress_timeout``      ``UNSTUCK``
    *unknown*                             ``None`` (no emission)
    ====================================  =========================
    """
    return _WATCHDOG_TIMEOUT_DIRECTIVES.get(timeout_kind)
