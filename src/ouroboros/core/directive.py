"""Directive vocabulary for the Phase 2 control plane.

This module defines the shared value type that every decision site uses to
describe "what should happen next." In the Agent OS framing introduced by
the Phase-2 RFC, ``Directive`` members act as runtime-level syscalls: a
small, stable alphabet through which decision sites (evaluator, evolver,
resilience handlers, orchestrator, job manager) express control flow
without each inventing its own signalling.

Positioning within the Agent OS layers described by the RFC:

- Capability layer   — answers *what can this environment do?*
- Policy layer       — answers *who may use which capability?*
- **Directive layer  — answers what should happen next?*   (this module)*
- Event journal      — answers *why did the system move?*  (paired with the
                       ``control.directive.emitted`` event factory)

Design invariants:

- The enum is additive. Each member has a deliberate precondition and
  effect; additions require a PR-level justification.
- Exactly two members are terminal: ``CANCEL`` and ``CONVERGE``. Every
  other member implies the run continues.
- The vocabulary is intentionally small. New directives are introduced
  only when an existing one cannot carry the semantics without loss.
- Directives describe **workflow control**. They do not describe *capability
  policy* (whether a tool is visible/executable). Those concerns stay in
  the policy layer and in ``policy.*`` events.

Migration posture (mapping, not replacement):

Existing local enums — notably ``StepAction`` in the evolution loop, and
the terminal branches in ``evaluation/`` and ``resilience/`` — do not
disappear when this vocabulary lands. The first reference migration maps
``StepAction`` onto ``Directive`` at the adapter boundary::

    StepAction.STAGNATED  -> Directive.UNSTUCK
    StepAction.CONVERGED  -> Directive.CONVERGE
    StepAction.FAILED     -> Directive.RETRY  or Directive.CANCEL   (budget-dependent)

Later migrations follow the same pattern so callers can be converted one
at a time without flag days. This PR adds only the type and its
invariants; no caller is modified here.
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
