"""Authoritative Seed-wide acceptance-verdict coverage checks.

Per-AC consumers must not infer completeness from a non-empty tuple.  The
evaluator contract is complete only when every current Seed criterion appears
exactly once, at the expected index, with the expected human-readable content.
"""

from __future__ import annotations

from dataclasses import dataclass

from ouroboros.core.lineage import EvaluationSummary
from ouroboros.core.seed import Seed, ac_texts


@dataclass(frozen=True, slots=True)
class ACVerdictCoverage:
    """Result of validating one evaluation against its Seed contract."""

    complete: bool
    reason: str
    expected_indices: tuple[int, ...]
    reported_indices: tuple[int, ...]


def validate_seed_ac_coverage(
    seed: Seed,
    evaluation: EvaluationSummary,
) -> ACVerdictCoverage:
    """Require complete, unique, in-range verdicts for the current Seed."""
    expected_acs = ac_texts(seed.acceptance_criteria)
    expected_indices = tuple(range(len(expected_acs)))
    reported_indices = tuple(result.ac_index for result in evaluation.ac_results)

    if len(reported_indices) != len(set(reported_indices)):
        return ACVerdictCoverage(
            complete=False,
            reason="duplicate per-AC verdict indices",
            expected_indices=expected_indices,
            reported_indices=reported_indices,
        )

    out_of_range = tuple(
        index for index in reported_indices if index < 0 or index >= len(expected_acs)
    )
    if out_of_range:
        return ACVerdictCoverage(
            complete=False,
            reason=f"out-of-range per-AC verdict indices: {list(out_of_range)}",
            expected_indices=expected_indices,
            reported_indices=reported_indices,
        )

    if set(reported_indices) != set(expected_indices):
        missing = sorted(set(expected_indices) - set(reported_indices))
        return ACVerdictCoverage(
            complete=False,
            reason=f"missing per-AC verdict indices: {missing}",
            expected_indices=expected_indices,
            reported_indices=reported_indices,
        )

    mismatched = tuple(
        result.ac_index
        for result in evaluation.ac_results
        if result.ac_content.strip() != expected_acs[result.ac_index].strip()
    )
    if mismatched:
        return ACVerdictCoverage(
            complete=False,
            reason=f"per-AC verdict content differs from Seed at indices: {list(mismatched)}",
            expected_indices=expected_indices,
            reported_indices=reported_indices,
        )

    identity_mismatched = tuple(
        result.ac_index
        for result in evaluation.ac_results
        if result.semantic_ac_key != seed.acceptance_criteria[result.ac_index].semantic_ac_key
        and (
            seed.acceptance_criteria[result.ac_index].has_success_contract
            or seed.acceptance_criteria[result.ac_index].investment is not None
            or result.semantic_ac_key is not None
        )
    )
    if identity_mismatched:
        return ACVerdictCoverage(
            complete=False,
            reason=(
                "per-AC verdict semantic identity differs from Seed at indices: "
                f"{list(identity_mismatched)}"
            ),
            expected_indices=expected_indices,
            reported_indices=reported_indices,
        )

    return ACVerdictCoverage(
        complete=True,
        reason="complete Seed-wide per-AC verdict coverage",
        expected_indices=expected_indices,
        reported_indices=reported_indices,
    )


__all__ = ["ACVerdictCoverage", "validate_seed_ac_coverage"]
