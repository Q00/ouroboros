"""Gate-anchored decomposition trust attestation.

The RLM thesis (:mod:`ouroboros.orchestrator.model_routing`) is that a
decomposed child may run on a cheaper model tier because decomposition made it
easy enough. Before this module, the only signal admitting that discount was
``DecompositionDecisionRecord.trustworthy`` — a JSON-shape/coverage-claim
heuristic computed from the decomposer's OWN unverified proposal, before any
child ever ran. It can only prove the split *looked* well-formed on paper; it
cannot prove the split actually worked.

This module is the deterministic, LLM-free judge that replaces that paper
proof with a real one. Ouroboros already has a single oracle it trusts for "did
this AC actually succeed": the AC's own verify gate
(``parallel_executor._run_ac_verify_gate`` / ``_VerifyGateOutcome``). Rather
than inventing a parallel evidence-manifest/coverage-claim judge (the
file-overlap-based attestation approach an earlier design review considered
and rejected as a strawman), this module reuses that SAME oracle twice:

* once per sibling — did each child pass its own verify gate?
* once for the parent — re-run *after* every child has finished, does the
  parent's own verify gate now pass over the union of all children's combined
  effect on the shared workspace?

If a sibling clobbered another's work, or the split left a gap the parent's
own contract cares about, the parent's re-run gate fails and the round is
untrustworthy — with no separate MECE heuristic required. A decomposition
round earns the ``TRUSTWORTHY`` verdict only when both axes are backed by a
REAL, evaluated verify-gate outcome and both said "passed". Any axis this
module cannot evaluate (no verify command at all — common for many ACs, and
therefore the case this module leans hardest on getting right) resolves to
``INDETERMINATE``, which is NOT trustworthy: false positives (an untrustworthy
round graded trustworthy) are the one failure mode this module must never
produce, so ambiguity always resolves closed, never open.

This module is pure: no LLM calls, no subprocess execution, no filesystem
access, and no import from ``ouroboros.orchestrator`` (mirroring
``ouroboros.harness.traceguard_validator``). Callers construct
:class:`SiblingVerifyOutcome`/:class:`ParentVerifyOutcome` from whatever the
orchestrator's existing verify-gate machinery already produced (or re-ran) and
hand them to :func:`attest_decomposition`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DecompositionTrustAxis(StrEnum):
    """Which axis of the gate-anchored check the verdict turned on."""

    SIBLING_GATE = "sibling_gate"
    PARENT_GATE = "parent_gate"


class DecompositionTrustVerdict(StrEnum):
    """The three possible outcomes of a gate-anchored attestation."""

    TRUSTWORTHY = "trustworthy"
    UNTRUSTWORTHY = "untrustworthy"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True, slots=True)
class SiblingVerifyOutcome:
    """One sibling sub-AC's own verify-gate result, as seen by the judge.

    Attributes:
        sibling_id: Stable identifier for the sibling (e.g. its child index or
            node id) so a failure can be attributed for logging/observability.
        has_verify_command: Whether this sibling carries an evaluable success
            contract (``verify_command`` or ``expected_artifacts``). ``False``
            means this axis cannot be evaluated for this sibling at all.
        passed: Whether the sibling's own verify gate passed. Must be ``None``
            when ``has_verify_command`` is ``False`` — there is no gate result
            to report. Never optimistically defaulted to ``True``.
        reason: Human-readable detail (gate failure reason, or why the gate
            could not be evaluated), for logging/observability.
    """

    sibling_id: str
    has_verify_command: bool
    passed: bool | None
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.has_verify_command and self.passed is not None:
            msg = "passed must be None when has_verify_command is False"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ParentVerifyOutcome:
    """The parent AC's own verify-gate result, re-evaluated after children finish.

    Attributes:
        has_verify_command: Whether the parent carries an evaluable success
            contract. ``False`` means the parent gate cannot be re-checked at
            all — the common "no verify command" case this module treats as
            fail-closed ``INDETERMINATE``.
        passed: Whether the re-run parent gate passed. Must be ``None`` when
            ``has_verify_command`` is ``False``.
        reason: Human-readable detail, for logging/observability.
    """

    has_verify_command: bool
    passed: bool | None
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.has_verify_command and self.passed is not None:
            msg = "passed must be None when has_verify_command is False"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class DecompositionAttestation:
    """The gate-anchored trust verdict for one finished decomposition round.

    Attributes:
        node_id: The decomposition round's node id (the parent AC's node),
            matching ``ExecutionNodeIdentity.node_id``.
        verdict: The three-way verdict. Only ``TRUSTWORTHY`` admits the
            child-tier discount for this root AC's next retry.
        failed_axis: Which axis produced a non-trustworthy verdict, or
            ``None`` when ``verdict`` is ``TRUSTWORTHY``.
        failed_sibling_id: The sibling responsible, when ``failed_axis`` is
            ``SIBLING_GATE``. ``None`` otherwise (including for
            ``PARENT_GATE``, which has no single sibling to blame).
        reason: Human-readable summary for logging/observability.
    """

    node_id: str
    verdict: DecompositionTrustVerdict
    failed_axis: DecompositionTrustAxis | None
    failed_sibling_id: str | None
    reason: str

    @property
    def trustworthy(self) -> bool:
        """Whether this round's trust verdict admits the child-tier discount."""
        return self.verdict is DecompositionTrustVerdict.TRUSTWORTHY

    def to_event_data(self) -> dict[str, object]:
        """Serialize for durable event emission / observability."""
        return {
            "node_id": self.node_id,
            "verdict": self.verdict.value,
            "trustworthy": self.trustworthy,
            "failed_axis": self.failed_axis.value if self.failed_axis is not None else None,
            "failed_sibling_id": self.failed_sibling_id,
            "reason": self.reason,
        }


def attest_decomposition(
    *,
    node_id: str,
    siblings: tuple[SiblingVerifyOutcome, ...],
    parent: ParentVerifyOutcome,
) -> DecompositionAttestation:
    """Judge one finished decomposition round against the gate-anchored rule.

    A round is ``TRUSTWORTHY`` iff every sibling passed its own verify gate
    AND the parent's own verify gate, re-run over the current workspace state
    after all children finished, also passes. Any sibling or the parent
    without an evaluable verify gate collapses the whole round to
    ``INDETERMINATE`` (not trustworthy) rather than being skipped or
    optimistically assumed to pass — an unproven axis must never be read as a
    passing one.

    Siblings are checked in order so the FIRST failing/indeterminate sibling
    is the one attributed in the verdict (deterministic, stable output for a
    given input order).

    Args:
        node_id: The decomposition round's node id.
        siblings: Every sibling's own verify-gate outcome. An empty tuple
            (a decomposition round with no recorded children) is itself
            indeterminate — there is nothing to attest.
        parent: The parent's own verify-gate outcome, re-run after all
            siblings finished.

    Returns:
        A frozen :class:`DecompositionAttestation`.
    """
    if not siblings:
        return DecompositionAttestation(
            node_id=node_id,
            verdict=DecompositionTrustVerdict.INDETERMINATE,
            failed_axis=DecompositionTrustAxis.SIBLING_GATE,
            failed_sibling_id=None,
            reason="no sibling verify-gate evidence recorded for this round",
        )

    for sibling in siblings:
        if not sibling.has_verify_command or sibling.passed is None:
            return DecompositionAttestation(
                node_id=node_id,
                verdict=DecompositionTrustVerdict.INDETERMINATE,
                failed_axis=DecompositionTrustAxis.SIBLING_GATE,
                failed_sibling_id=sibling.sibling_id,
                reason=(
                    f"sibling {sibling.sibling_id!r} has no evaluable verify gate; "
                    "cannot attest this round"
                ),
            )
        if sibling.passed is False:
            return DecompositionAttestation(
                node_id=node_id,
                verdict=DecompositionTrustVerdict.UNTRUSTWORTHY,
                failed_axis=DecompositionTrustAxis.SIBLING_GATE,
                failed_sibling_id=sibling.sibling_id,
                reason=(
                    f"sibling {sibling.sibling_id!r} failed its own verify gate: {sibling.reason}"
                ),
            )

    if not parent.has_verify_command or parent.passed is None:
        return DecompositionAttestation(
            node_id=node_id,
            verdict=DecompositionTrustVerdict.INDETERMINATE,
            failed_axis=DecompositionTrustAxis.PARENT_GATE,
            failed_sibling_id=None,
            reason=(
                "parent AC has no evaluable verify gate; cannot re-check collective "
                "sufficiency after decomposition"
            ),
        )
    if parent.passed is False:
        return DecompositionAttestation(
            node_id=node_id,
            verdict=DecompositionTrustVerdict.UNTRUSTWORTHY,
            failed_axis=DecompositionTrustAxis.PARENT_GATE,
            failed_sibling_id=None,
            reason=f"parent verify gate failed after decomposition: {parent.reason}",
        )

    return DecompositionAttestation(
        node_id=node_id,
        verdict=DecompositionTrustVerdict.TRUSTWORTHY,
        failed_axis=None,
        failed_sibling_id=None,
        reason=(
            "every sibling passed its own verify gate and the parent's verify gate "
            "re-confirmed collective sufficiency"
        ),
    )


__all__ = [
    "DecompositionAttestation",
    "DecompositionTrustAxis",
    "DecompositionTrustVerdict",
    "ParentVerifyOutcome",
    "SiblingVerifyOutcome",
    "attest_decomposition",
]
