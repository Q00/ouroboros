"""Directive vocabulary for control-plane decisions.

This module defines the shared type used to describe "what should happen next"
at any decision site across the Ouroboros workflow. Decision sites currently
distributed across `evaluation/`, `evolution/`, `resilience/`, `orchestrator/`,
and `observability/` will be migrated to this vocabulary in follow-up changes;
this module adds only the type and its metadata, with no caller modifications.

Design notes:
- The enum is additive. Each member has a deliberate precondition and effect.
- Exactly two members are terminal: ``CANCEL`` and ``CONVERGE``. Every other
  member implies the run continues.
- The vocabulary is intentionally small. New directives are only added when an
  existing one cannot cover the semantics without loss of meaning.

See the control-plane RFC and the Directive introduction issue for rationale
and the planned migration path.
"""

from __future__ import annotations

from enum import StrEnum


class Directive(StrEnum):
    """Control-plane decision emitted by a workflow site.

    Each directive is a single value that a decision site (an evaluator, an
    evolver, a resilience handler, etc.) can emit to the surrounding runtime.
    The runtime is responsible for acting on the directive; the site itself
    does not dispatch work.

    Members are named after the *action requested*, not the state that
    produced them. ``CONTINUE`` means "proceed", not "we are in a continuing
    state".
    """

    CONTINUE = "continue"
    """Proceed with the current plan. No change in phase or plan required."""

    EVALUATE = "evaluate"
    """Hand off the current artifacts to the evaluation pipeline."""

    EVOLVE = "evolve"
    """Emit a next-generation proposal. Used when an evaluation yields
    feedback that should influence the next seed generation rather than
    retrying the current one."""

    UNSTUCK = "unstuck"
    """Invoke a lateral-thinking persona. Used when stagnation is detected
    and a change in approach is required rather than a simple retry."""

    RETRY = "retry"
    """Re-execute the last unit under the same plan. The retry budget is
    owned by the resilience layer and must be respected by the consumer."""

    COMPACT = "compact"
    """Compress context before continuing. The consumer must preserve the
    event lineage; compaction affects working context, not persisted events."""

    WAIT = "wait"
    """Block on external input (user, upstream service, queued event). The
    consumer must not proceed until the awaited input is delivered."""

    CANCEL = "cancel"
    """Terminate this execution without claiming success. Terminal."""

    CONVERGE = "converge"
    """Terminal success. Used when the seed's acceptance threshold has been
    reached (e.g., ontology similarity satisfied, all ACs passed). Terminal."""

    @property
    def is_terminal(self) -> bool:
        """Return True if this directive ends the execution.

        Exactly the ``CANCEL`` and ``CONVERGE`` members are terminal.
        """
        return self in _TERMINAL_DIRECTIVES


_TERMINAL_DIRECTIVES: frozenset[Directive] = frozenset({Directive.CANCEL, Directive.CONVERGE})
"""The closed set of directives that end a run.

Maintained as a module-level constant so ``is_terminal`` does not allocate on
every access and so the terminal set can be referenced from tests and from
future invariants without inspecting individual enum members.
"""
