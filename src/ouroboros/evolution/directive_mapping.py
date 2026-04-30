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
