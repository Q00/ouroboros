"""Convergence criteria for the evolutionary loop.

Determines when the loop should terminate. v1 uses 3 signals:
1. Ontology stability (similarity >= threshold)
2. Stagnation detection (unchanged ontology for N consecutive gens)
3. max_generations hard cap

v1.1 will add drift-trend and evaluation-satisfaction signals.
"""

from __future__ import annotations

from dataclasses import dataclass

from ouroboros.core.lineage import EvaluationSummary, OntologyDelta, OntologyLineage
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

    Convergence when ANY of:
    1. Ontology stability: similarity(Oₙ, Oₙ₋₁) >= threshold
    2. Stagnation: ontology similarity >= threshold for stagnation_window consecutive gens
    3. Repetitive feedback: wonder questions repeat across generations
    4. max_generations reached (forced termination)

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
    regression_gate_enabled: bool = True

    def evaluate(
        self,
        lineage: OntologyLineage,
        latest_wonder: WonderOutput | None = None,
        latest_evaluation: EvaluationSummary | None = None,
    ) -> ConvergenceSignal:
        """Check if the loop should terminate.

        Args:
            lineage: Current lineage with all generation records.
            latest_wonder: Latest wonder output (for repetitive feedback check).

        Returns:
            ConvergenceSignal with convergence status and reason.
        """
        num_gens = len(lineage.generations)
        current_gen = lineage.current_generation

        # Signal 4: Hard cap
        if num_gens >= self.max_generations:
            return ConvergenceSignal(
                converged=True,
                reason=f"Max generations reached ({self.max_generations})",
                ontology_similarity=self._latest_similarity(lineage),
                generation=current_gen,
            )

        # Need at least min_generations before checking other signals
        if num_gens < self.min_generations:
            return ConvergenceSignal(
                converged=False,
                reason=f"Below minimum generations ({num_gens}/{self.min_generations})",
                ontology_similarity=0.0,
                generation=current_gen,
            )

        # Signal 1: Ontology stability (latest two generations)
        latest_sim = self._latest_similarity(lineage)
        if latest_sim >= self.convergence_threshold:
            # Eval gate: block convergence if evaluation is unsatisfactory
            if self.eval_gate_enabled and latest_evaluation is not None:
                eval_blocks = not latest_evaluation.final_approved or (
                    latest_evaluation.score is not None
                    and latest_evaluation.score < self.eval_min_score
                )
                if eval_blocks:
                    return ConvergenceSignal(
                        converged=False,
                        reason=(
                            f"Ontology stable (similarity {latest_sim:.3f}) "
                            f"but evaluation unsatisfactory"
                        ),
                        ontology_similarity=latest_sim,
                        generation=current_gen,
                    )

            # Per-AC gate: block convergence if individual ACs are failing
            if (
                self.eval_gate_enabled
                and self.ac_gate_mode != "off"
                and latest_evaluation is not None
                and latest_evaluation.ac_results
            ):
                ac_block = self._check_ac_gate(latest_evaluation)
                if ac_block is not None:
                    failed_indices, reason = ac_block
                    return ConvergenceSignal(
                        converged=False,
                        reason=reason,
                        ontology_similarity=latest_sim,
                        generation=current_gen,
                        failed_acs=failed_indices,
                    )

            # Signal 5: Regression gate — block convergence if ACs regressed
            if self.regression_gate_enabled:
                regression_report = RegressionDetector().detect(lineage)
                if regression_report.has_regressions:
                    regressed = regression_report.regressed_ac_indices
                    display = ", ".join(str(i + 1) for i in regressed)
                    return ConvergenceSignal(
                        converged=False,
                        reason=(
                            f"Regression detected: {len(regressed)} AC(s) regressed (AC {display})"
                        ),
                        ontology_similarity=latest_sim,
                        generation=current_gen,
                        failed_acs=regressed,
                    )

            # False convergence gate: block if ontology never actually evolved.
            # When Wonder→Reflect repeatedly fails, the same seed is re-executed
            # and similarity stays at 1.0 — this is convergence through failure,
            # not through genuine stabilization.
            evolved_count = self._count_evolved_generations(lineage)
            if evolved_count == 0:
                return ConvergenceSignal(
                    converged=False,
                    reason=(
                        f"False convergence blocked: similarity {latest_sim:.3f} "
                        f"but ontology never evolved across {num_gens} generations "
                        f"(Wonder→Reflect may have failed)"
                    ),
                    ontology_similarity=latest_sim,
                    generation=current_gen,
                )

            return ConvergenceSignal(
                converged=True,
                reason=(
                    f"Ontology converged: similarity {latest_sim:.3f} "
                    f">= threshold {self.convergence_threshold}"
                ),
                ontology_similarity=latest_sim,
                generation=current_gen,
            )

        # Signal 2: Stagnation (unchanged for N consecutive gens)
        if num_gens >= self.stagnation_window:
            stagnant = self._check_stagnation(lineage)
            if stagnant:
                return ConvergenceSignal(
                    converged=True,
                    reason=(
                        f"Stagnation detected: ontology unchanged for "
                        f"{self.stagnation_window} consecutive generations"
                    ),
                    ontology_similarity=latest_sim,
                    generation=current_gen,
                )

        # Signal 2.5: Oscillation detection (A→B→A→B cycling)
        if self.enable_oscillation_detection and num_gens >= 3:
            oscillating = self._check_oscillation(lineage)
            if oscillating:
                return ConvergenceSignal(
                    converged=True,
                    reason=("Oscillation detected: ontology is cycling between similar states"),
                    ontology_similarity=latest_sim,
                    generation=current_gen,
                )

        # Signal 3: Repetitive wonder questions
        if latest_wonder and num_gens >= 3:
            repetitive = self._check_repetitive_feedback(lineage, latest_wonder)
            if repetitive:
                return ConvergenceSignal(
                    converged=True,
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

    def _latest_similarity(self, lineage: OntologyLineage) -> float:
        """Compute similarity between the last two generations."""
        if len(lineage.generations) < 2:
            return 0.0

        prev = lineage.generations[-2].ontology_snapshot
        curr = lineage.generations[-1].ontology_snapshot
        delta = OntologyDelta.compute(prev, curr)
        return delta.similarity

    def _count_evolved_generations(self, lineage: OntologyLineage) -> int:
        """Count how many consecutive generation pairs show actual ontology evolution.

        Returns the number of transitions where similarity < convergence_threshold,
        indicating Wonder→Reflect successfully mutated the ontology.
        A return of 0 means the ontology never changed — likely due to repeated
        Wonder/Reflect failures causing the same seed to be re-executed.
        """
        gens = lineage.generations
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

    def _check_stagnation(self, lineage: OntologyLineage) -> bool:
        """Check if ontology has been unchanged for stagnation_window gens."""
        gens = lineage.generations
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
        gens = lineage.generations

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

        # Check against last 2 generations' wonder questions
        repeat_count = 0
        for gen in lineage.generations[-3:]:
            if gen.wonder_questions:
                prev_set = set(gen.wonder_questions)
                overlap = len(latest_set & prev_set)
                if overlap >= len(latest_set) * 0.7:  # 70% overlap = repetitive
                    repeat_count += 1

        return repeat_count >= 2
