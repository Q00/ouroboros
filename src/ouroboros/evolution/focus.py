"""Deterministic working-set selection for focused evolution.

Loop engineering converges by shrinking the work that remains.  This module
turns the previous generation's verification evidence into an explicit AC
working set: failed, changed, challenged, regressed, and newly-added nodes stay
active; proven passing nodes are frozen and only reverified at the boundary.

The selector is deliberately independent of LLM output such as
``ReflectOutput.settled_ac_indices``.  A model may propose a change, but it does
not decide which already-passing nodes are safe to rerun.
"""

from __future__ import annotations

from dataclasses import dataclass
import inspect
import logging
from typing import Any

from ouroboros.core.lineage import EvaluationSummary
from ouroboros.core.seed import Seed
from ouroboros.evolution.evaluation_coverage import validate_seed_ac_coverage
from ouroboros.evolution.regression import RegressionReport
from ouroboros.evolution.wonder import WonderOutput

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EvolutionFocus:
    """The AC nodes open for mutation and the nodes frozen by evidence."""

    active_ac_indices: tuple[int, ...]
    frozen_ac_indices: tuple[int, ...]
    reason: str

    @property
    def is_scoped(self) -> bool:
        """Whether this generation can skip at least one proven node."""
        return bool(self.frozen_ac_indices)

    def externally_satisfied_acs(self) -> dict[int, dict[str, str]]:
        """Build the executor contract for nodes frozen by the prior gate."""
        return {
            index: {
                "reason": (
                    "frozen by prior PASS evidence; excluded from evolve and "
                    "reverified at the final boundary"
                )
            }
            for index in self.frozen_ac_indices
        }

    def log_selection(self, generation_number: int) -> None:
        """Emit the selected working set without burdening the orchestrator."""
        logger.info(
            "evolution.generation.focus_selected",
            extra={
                "generation": generation_number,
                "active_ac_indices": self.active_ac_indices,
                "frozen_ac_indices": self.frozen_ac_indices,
                "reason": self.reason,
            },
        )


def initial_evolution_focus(seed: Seed) -> EvolutionFocus:
    """Keep every AC active until a verifier has produced node evidence."""
    return EvolutionFocus(
        active_ac_indices=tuple(range(len(getattr(seed, "acceptance_criteria", ())))),
        frozen_ac_indices=(),
        reason="initial/full generation",
    )


def callable_accepts_keyword(
    callable_obj: Any,
    keyword: str,
    signature_getter: Any = inspect.signature,
) -> bool:
    """Return whether a callable supports a keyword or arbitrary keywords."""
    try:
        signature = signature_getter(callable_obj)
    except (TypeError, ValueError):
        return False
    return keyword in signature.parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


async def call_executor(
    executor: Any,
    seed: Seed,
    *,
    parallel: bool,
    execution_id: str | None,
    externally_satisfied_acs: dict[int, dict[str, Any]] | None = None,
) -> Any:
    """Invoke an executor while negotiating optional evolve context."""
    kwargs: dict[str, Any] = {"parallel": parallel}
    if execution_id is not None and callable_accepts_keyword(executor, "execution_id"):
        kwargs["execution_id"] = execution_id
    if externally_satisfied_acs and callable_accepts_keyword(executor, "externally_satisfied_acs"):
        kwargs["externally_satisfied_acs"] = externally_satisfied_acs
    return await executor(seed, **kwargs)


def add_active_focus(
    kwargs: dict[str, Any],
    callable_obj: Any,
    focus: EvolutionFocus,
) -> None:
    """Pass the active set only to focus-aware Wonder/Reflect implementations."""
    if focus.is_scoped and callable_accepts_keyword(callable_obj, "active_ac_indices"):
        kwargs["active_ac_indices"] = focus.active_ac_indices


def select_externally_satisfied_acs(
    *,
    scoped_reexecution: bool,
    focused_evolution: bool,
    focus: EvolutionFocus,
    settled_ac_indices: tuple[int, ...],
    reflect_ran_fresh: bool,
) -> dict[int, dict[str, str]] | None:
    """Map frozen nodes to the executor while retaining legacy opt-out behavior."""
    if not scoped_reexecution or not reflect_ran_fresh:
        return None
    if focused_evolution and focus.is_scoped:
        return focus.externally_satisfied_acs()
    if focused_evolution or not settled_ac_indices:
        return None
    return {
        index: {
            "reason": "satisficed: passed previously; not challenged or regressed",
        }
        for index in settled_ac_indices
    }


def select_evolution_focus(
    parent_seed: Seed,
    candidate_seed: Seed,
    evaluation: EvaluationSummary | None,
    *,
    wonder: WonderOutput | None = None,
    regression_report: RegressionReport | None = None,
    enabled: bool = True,
) -> EvolutionFocus:
    """Select the smallest evidence-backed AC working set.

    Scoping is fail-closed.  Without per-AC verdicts, or when every reported AC
    passed but the aggregate gate still did not end the lineage, every node
    remains active.  This avoids treating missing evidence as a PASS.
    """
    parent_acs = parent_seed.acceptance_criteria
    candidate_acs = candidate_seed.acceptance_criteria
    all_candidate = set(range(len(candidate_acs)))

    if not enabled or evaluation is None or not evaluation.ac_results:
        return EvolutionFocus(
            active_ac_indices=tuple(sorted(all_candidate)),
            frozen_ac_indices=(),
            reason="full generation: no per-AC PASS evidence available",
        )

    coverage = validate_seed_ac_coverage(parent_seed, evaluation)
    if not coverage.complete:
        return EvolutionFocus(
            active_ac_indices=tuple(sorted(all_candidate)),
            frozen_ac_indices=(),
            reason=f"full generation: {coverage.reason}",
        )

    passed = {
        result.ac_index
        for result in evaluation.ac_results
        if result.passed and 0 <= result.ac_index < len(parent_acs)
    }
    failed = {
        result.ac_index
        for result in evaluation.ac_results
        if not result.passed and 0 <= result.ac_index < len(parent_acs)
    }

    # If the aggregate run failed without naming a failed AC, the failure sits
    # outside the addressable node set (for example execution/validation).  A
    # node-focused retry would have no honest target, so keep the full graph open.
    if not failed:
        return EvolutionFocus(
            active_ac_indices=tuple(sorted(all_candidate)),
            frozen_ac_indices=(),
            reason="full generation: aggregate gate has no failed AC target",
        )

    challenged: set[int] = set()
    if wonder is not None:
        for question in wonder.grounded_questions:
            if question.kind == "challenge":
                challenged.update(question.ac_indices)

    regressed = (
        set(regression_report.regressed_ac_indices) if regression_report is not None else set()
    )
    changed = {
        index
        for index in range(min(len(parent_acs), len(candidate_acs)))
        # Display prose is not the authority boundary. Reopen whenever any
        # structured verifier, artifact, assertion, investment, or semantic
        # identity field changed beneath the previously passing verdict.
        if parent_acs[index] != candidate_acs[index]
    }
    added = set(range(len(parent_acs), len(candidate_acs)))
    reopened = challenged | regressed | changed | added

    frozen = {index for index in passed - reopened if index in all_candidate}
    active = all_candidate - frozen

    return EvolutionFocus(
        active_ac_indices=tuple(sorted(active)),
        frozen_ac_indices=tuple(sorted(frozen)),
        reason=(
            f"focused generation: {len(active)} active / {len(frozen)} frozen "
            "from prior per-AC gate"
        ),
    )
