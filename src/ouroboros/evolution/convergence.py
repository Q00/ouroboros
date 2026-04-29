"""Convergence criteria for the evolutionary loop.

Determines when the loop should terminate.

Idea-first convergence treats ontology as a conceptual lens. The primary stop
condition is alignment with the human-origin Seed contract as represented by
evaluation approval, score, AC results, drift, and reward-hacking risk. Ontology
stability is only a secondary signal that the lens no longer needs motion.
"""

from __future__ import annotations

from dataclasses import dataclass

from ouroboros.core.lineage import (
    EvaluationSummary,
    GenerationPhase,
    GenerationRecord,
    OntologyDelta,
    OntologyLineage,
)
from ouroboros.evolution.regression import RegressionDetector
from ouroboros.evolution.wonder import WonderOutput


@dataclass(frozen=True, slots=True)
class ConvergenceSignal:
    """Result of convergence evaluation."""

    converged: bool
    reason: str
    ontology_similarity: float
    generation: int
    failed_acs: tuple[int, ...] = ()


@dataclass
class ConvergenceCriteria:
    """Evaluates whether the evolutionary loop should terminate.

    Convergence when:
    1. max_generations reached (forced termination), OR
    2. evaluation contract gate passes and ontology is stable, OR
    3. evaluation contract gate passes and Wonder reports no substantive gap.

    Stagnation, oscillation, and repetitive Wonder are not successful
    convergence. They return converged=False with reason strings that loop
    routing can map to STAGNATED.

    Must have run at least min_generations before checking signals 1-3.
    """

    convergence_threshold: float = 0.95
    stagnation_window: int = 3
    min_generations: int = 2
    max_generations: int = 30
    enable_oscillation_detection: bool = True
    eval_gate_enabled: bool = False
    eval_min_score: float = 0.7
    ac_gate_mode: str = "all"  # "all" | "ratio" | "off"
    ac_min_pass_ratio: float = 1.0  # for "ratio" mode
    drift_max_score: float = 0.30
    reward_hacking_max_risk: float = 0.30
    regression_gate_enabled: bool = True
    validation_gate_enabled: bool = True

    def evaluate(
        self,
        lineage: OntologyLineage,
        latest_wonder: WonderOutput | None = None,
        latest_evaluation: EvaluationSummary | None = None,
        validation_output: str | None = None,
    ) -> ConvergenceSignal:
        """Check if the loop should terminate.

        Args:
            lineage: Current lineage with all generation records.
            latest_wonder: Latest wonder output (for repetitive feedback check).

        Returns:
            ConvergenceSignal with convergence status and reason.
        """
        completed = self._completed_generations(lineage)
        num_completed = len(completed)
        current_gen = lineage.current_generation

        # Signal 4: Hard cap (only count completed generations)
        if num_completed >= self.max_generations:
            return ConvergenceSignal(
                converged=True,
                reason=f"Max generations reached ({self.max_generations})",
                ontology_similarity=self._latest_similarity(lineage),
                generation=current_gen,
            )

        # Need at least min_generations completed before checking other signals
        if num_completed < self.min_generations:
            return ConvergenceSignal(
                converged=False,
                reason=f"Below minimum generations ({num_completed}/{self.min_generations})",
                ontology_similarity=0.0,
                generation=current_gen,
            )

        latest_sim = self._latest_similarity(lineage)

        blocking_signal: tuple[tuple[int, ...], str] | None = None

        if self.eval_gate_enabled:
            if latest_evaluation is None:
                blocking_signal = ((), "Evaluation gate: no evaluation summary available")
            else:
                blocking_signal = self._check_evaluation_contract_gate(latest_evaluation)

        if blocking_signal is None and self.validation_gate_enabled and validation_output:
            if self._validation_blocks_convergence(validation_output):
                blocking_signal = ((), f"Validation gate blocked: {validation_output}")

        if blocking_signal is None and self.regression_gate_enabled:
            completed_lineage = lineage.model_copy(update={"generations": completed})
            regression_report = RegressionDetector().detect(completed_lineage)
            if regression_report.has_regressions:
                regressed = regression_report.regressed_ac_indices
                display = ", ".join(str(i + 1) for i in regressed)
                blocking_signal = (
                    regressed,
                    f"Regression detected: {len(regressed)} AC(s) regressed (AC {display})",
                )

        wonder_has_gap = latest_wonder is not None and latest_wonder.should_continue

        # Stagnation is not successful convergence unless the Idea contract gate
        # already passed and Wonder has no substantive gap; then zero ontology
        # mutation may be the correct outcome.
        if (
            blocking_signal is not None or wonder_has_gap
        ) and num_completed >= self.stagnation_window:
            stagnant = self._check_stagnation(lineage)
            if stagnant:
                failed_acs, block_reason = blocking_signal or ((), "")
                block_suffix = f" while blocked by {block_reason}" if block_reason else ""
                return ConvergenceSignal(
                    converged=False,
                    reason=(
                        f"Stagnation detected: ontology unchanged for "
                        f"{self.stagnation_window} consecutive generations"
                        f"{block_suffix}"
                    ),
                    ontology_similarity=latest_sim,
                    generation=current_gen,
                    failed_acs=failed_acs,
                )

        if blocking_signal is not None:
            failed_acs, reason = blocking_signal
            return ConvergenceSignal(
                converged=False,
                reason=reason,
                ontology_similarity=latest_sim,
                generation=current_gen,
                failed_acs=failed_acs,
            )

        # Signal 1: Idea contract satisfied and Wonder reports no substantive gap.
        # Mirror loop.py's contradictory-Wonder override: should_continue=False is
        # only a true "no gap" signal when there are also no remaining questions
        # or ontology tensions. Otherwise the loop would short-circuit while
        # leaving unresolved gaps on the table.
        if (
            self.eval_gate_enabled
            and latest_evaluation is not None
            and latest_wonder is not None
            and latest_wonder.should_continue is False
            and not latest_wonder.questions
            and not latest_wonder.ontology_tensions
        ):
            return ConvergenceSignal(
                converged=True,
                reason="Idea contract converged: evaluation passed and Wonder found no gap",
                ontology_similarity=latest_sim,
                generation=current_gen,
            )

        # Signal 2: Ontology stability (latest two generations) as a secondary stop signal.
        if latest_sim >= self.convergence_threshold and not wonder_has_gap:
            if self.eval_gate_enabled and latest_evaluation is None:
                return ConvergenceSignal(
                    converged=False,
                    reason="Evaluation gate: no evaluation summary available",
                    ontology_similarity=latest_sim,
                    generation=current_gen,
                )
            if not self.eval_gate_enabled:
                return ConvergenceSignal(
                    converged=True,
                    reason=(
                        f"Ontology converged: similarity {latest_sim:.3f} "
                        f">= threshold {self.convergence_threshold}"
                    ),
                    ontology_similarity=latest_sim,
                    generation=current_gen,
                )
            return ConvergenceSignal(
                converged=True,
                reason=(
                    f"Idea contract converged with stable ontology lens: "
                    f"similarity {latest_sim:.3f} >= threshold {self.convergence_threshold}"
                ),
                ontology_similarity=latest_sim,
                generation=current_gen,
            )

        # Signal 4: Oscillation detection (A→B→A→B cycling)
        if self.enable_oscillation_detection and num_completed >= 3:
            oscillating = self._check_oscillation(lineage)
            if oscillating:
                return ConvergenceSignal(
                    converged=False,
                    reason=("Oscillation detected: ontology is cycling between similar states"),
                    ontology_similarity=latest_sim,
                    generation=current_gen,
                )

        # Signal 5: Repetitive wonder questions
        if latest_wonder and num_completed >= 3:
            repetitive = self._check_repetitive_feedback(lineage, latest_wonder)
            if repetitive:
                return ConvergenceSignal(
                    converged=False,
                    reason="Repetitive feedback: wonder questions are repeating across generations",
                    ontology_similarity=latest_sim,
                    generation=current_gen,
                )

        # Not converged
        return ConvergenceSignal(
            converged=False,
            reason=f"Continuing: similarity {latest_sim:.3f} < {self.convergence_threshold}",
            ontology_similarity=latest_sim,
            generation=current_gen,
        )

    def _completed_generations(self, lineage: OntologyLineage) -> tuple[GenerationRecord, ...]:
        """Return only completed generations for convergence calculations."""
        return tuple(g for g in lineage.generations if g.phase == GenerationPhase.COMPLETED)

    def _latest_similarity(self, lineage: OntologyLineage) -> float:
        """Compute similarity between the last two completed generations."""
        gens = self._completed_generations(lineage)
        if len(gens) < 2:
            return 0.0

        prev = gens[-2].ontology_snapshot
        curr = gens[-1].ontology_snapshot
        delta = OntologyDelta.compute(prev, curr)
        return delta.similarity

    def _count_evolved_generations(self, lineage: OntologyLineage) -> int:
        """Count how many generation pairs show actual ontology evolution.

        Returns the number of transitions where similarity < convergence_threshold,
        indicating Wonder→Reflect successfully mutated the ontology.
        A return of 0 means the ontology never changed -- either because Reflect
        conservatively preserved a well-performing ontology, or because
        Wonder/Reflect encountered errors preventing mutation.
        """
        gens = self._completed_generations(lineage)
        if len(gens) < 2:
            return 0

        count = 0
        for i in range(1, len(gens)):
            delta = OntologyDelta.compute(
                gens[i - 1].ontology_snapshot,
                gens[i].ontology_snapshot,
            )
            if delta.similarity < self.convergence_threshold:
                count += 1

        return count

    def _check_ac_gate(
        self,
        evaluation: EvaluationSummary,
    ) -> tuple[tuple[int, ...], str] | None:
        """Check per-AC gate. Returns (failed_ac_indices, reason) if blocked, None if OK."""
        if not evaluation.ac_results:
            return None

        failed = tuple(ac.ac_index for ac in evaluation.ac_results if not ac.passed)
        if not failed:
            return None

        total = len(evaluation.ac_results)
        passed = total - len(failed)
        ratio = passed / total if total > 0 else 0.0

        if self.ac_gate_mode == "all":
            failed_display = ", ".join(str(i + 1) for i in failed)
            return failed, (
                f"Per-AC gate (mode=all): {len(failed)} AC(s) still failing (AC {failed_display})"
            )
        elif self.ac_gate_mode == "ratio":
            if ratio < self.ac_min_pass_ratio:
                return failed, (
                    f"Per-AC gate (mode=ratio): pass ratio {ratio:.2f} "
                    f"< required {self.ac_min_pass_ratio:.2f}"
                )

        return None

    def _check_evaluation_contract_gate(
        self,
        evaluation: EvaluationSummary,
    ) -> tuple[tuple[int, ...], str] | None:
        """Check Idea/Seed contract alignment before ontology stop signals."""
        if self.ac_gate_mode != "off" and evaluation.ac_results:
            ac_block = self._check_ac_gate(evaluation)
            if ac_block is not None:
                return ac_block

        if not evaluation.final_approved:
            return (), "Evaluation gate: final approval is false"

        if evaluation.score is not None and evaluation.score < self.eval_min_score:
            return (), (
                f"Evaluation gate: score {evaluation.score:.2f} "
                f"< required {self.eval_min_score:.2f}"
            )

        if evaluation.drift_score is not None and evaluation.drift_score > self.drift_max_score:
            return (), (
                f"Evaluation gate: drift score {evaluation.drift_score:.2f} "
                f"> allowed {self.drift_max_score:.2f}"
            )

        if (
            evaluation.reward_hacking_risk is not None
            and evaluation.reward_hacking_risk > self.reward_hacking_max_risk
        ):
            return (), (
                f"Evaluation gate: reward hacking risk {evaluation.reward_hacking_risk:.2f} "
                f"> allowed {self.reward_hacking_max_risk:.2f}"
            )

        return None

    def _validation_blocks_convergence(self, validation_output: str) -> bool:
        """Return True when validation output should block convergence.

        Code validation can be intentionally skipped for non-code tasks such as
        research or analysis. Those tasks are still judged through the semantic
        Seed contract gate; a skipped pytest/import pass is not a failure.
        """
        normalized = validation_output.lower()
        if "does not require code validation" in normalized:
            return False
        return "skipped" in normalized or "error" in normalized

    def _check_stagnation(self, lineage: OntologyLineage) -> bool:
        """Check if ontology has been unchanged for stagnation_window gens."""
        gens = self._completed_generations(lineage)
        if len(gens) < self.stagnation_window:
            return False

        window = gens[-self.stagnation_window :]
        for i in range(1, len(window)):
            delta = OntologyDelta.compute(
                window[i - 1].ontology_snapshot,
                window[i].ontology_snapshot,
            )
            if delta.similarity < self.convergence_threshold:
                return False

        return True

    def _check_oscillation(self, lineage: OntologyLineage) -> bool:
        """Detect oscillation: N~N-2 AND N-1~N-3 (full period-2 verification)."""
        gens = self._completed_generations(lineage)

        # Period-2 full check: A→B→A→B — verify BOTH half-periods
        if len(gens) >= 4:
            sim_n_n2 = OntologyDelta.compute(
                gens[-3].ontology_snapshot, gens[-1].ontology_snapshot
            ).similarity
            sim_n1_n3 = OntologyDelta.compute(
                gens[-4].ontology_snapshot, gens[-2].ontology_snapshot
            ).similarity
            if sim_n_n2 >= self.convergence_threshold and sim_n1_n3 >= self.convergence_threshold:
                return True

        # Simpler period-2 check: only 3 gens available, check N~N-2
        elif len(gens) >= 3:
            sim = OntologyDelta.compute(
                gens[-3].ontology_snapshot, gens[-1].ontology_snapshot
            ).similarity
            if sim >= self.convergence_threshold:
                return True

        return False

    def _check_repetitive_feedback(
        self,
        lineage: OntologyLineage,
        latest_wonder: WonderOutput,
    ) -> bool:
        """Check if wonder questions are repeating across generations."""
        if not latest_wonder.questions:
            return False

        latest_set = set(latest_wonder.questions)

        # Check against last 2 completed generations' wonder questions
        repeat_count = 0
        completed = self._completed_generations(lineage)
        for gen in completed[-3:]:
            if gen.wonder_questions:
                prev_set = set(gen.wonder_questions)
                overlap = len(latest_set & prev_set)
                if overlap >= len(latest_set) * 0.7:  # 70% overlap = repetitive
                    repeat_count += 1

        return repeat_count >= 2
