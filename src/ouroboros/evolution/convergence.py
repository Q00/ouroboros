"""Convergence criteria for the evolutionary loop.

Determines when the loop should terminate. v1 uses 3 signals:
1. Ontology stability (similarity >= threshold)
2. Stagnation detection (unchanged ontology for N consecutive gens)
3. max_generations hard cap

v1.1 will add drift-trend and evaluation-satisfaction signals.
"""

from __future__ import annotations

from dataclasses import dataclass

from ouroboros.core.lineage import (
    EvaluationSummary,
    GenerationPhase,
    GenerationRecord,
    OntologyDelta,
    OntologyLineage,
    TerminationReason,
)
from ouroboros.evolution.regression import RegressionDetector
from ouroboros.evolution.wonder import WonderOutput


@dataclass(frozen=True, slots=True)
class ConvergenceSignal:
    """Result of convergence evaluation.

    When converged=True, termination_reason must be set to indicate why.
    When converged=False, termination_reason is None (loop continues).

    Note: ``converged`` means "the loop should terminate", NOT "the ontology
    has converged". EXHAUSTED, STAGNATED, OSCILLATED and REPETITIVE all
    set converged=True despite not representing true ontological convergence.
    """

    converged: bool
    reason: str
    ontology_similarity: float
    generation: int
    failed_acs: tuple[int, ...] = ()
    termination_reason: TerminationReason | None = None
    blocking_gate: str | None = None

    def __post_init__(self) -> None:
        if self.converged and self.termination_reason is None:
            raise ValueError("converged=True requires termination_reason to be set")


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
    regression_gate_enabled: bool = False
    validation_gate_enabled: bool = False
    ontology_completeness_gate_enabled: bool = False
    ontology_min_fields: int = 3
    wonder_gate_enabled: bool = False
    wonder_novelty_threshold: float = 0.5
    drift_trend_gate_enabled: bool = False
    drift_trend_window: int = 3

    def evaluate(
        self,
        lineage: OntologyLineage,
        latest_wonder: WonderOutput | None = None,
        latest_evaluation: EvaluationSummary | None = None,
        validation_output: str | None = None,
        validation_passed: bool | None = None,
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
                termination_reason=TerminationReason.EXHAUSTED,
            )

        # Need at least min_generations completed before checking other signals
        if num_completed < self.min_generations:
            return ConvergenceSignal(
                converged=False,
                reason=f"Below minimum generations ({num_completed}/{self.min_generations})",
                ontology_similarity=0.0,
                generation=current_gen,
            )

        # Signal 1: Ontology stability (latest two generations)
        latest_sim = self._latest_similarity(lineage)
        if latest_sim >= self.convergence_threshold:
            # Safety: if ontology has been stable for stagnation_window consecutive
            # generations but gates keep blocking, force stagnation termination.
            # Without this, gates (e.g., completeness) can block indefinitely
            # since the stagnation check below only runs when similarity < threshold.
            if self._check_stagnation(lineage):
                return ConvergenceSignal(
                    converged=True,
                    reason=(
                        f"Stagnation detected: ontology unchanged for "
                        f"{self.stagnation_window} consecutive generations "
                        f"(convergence gates could not be satisfied)"
                    ),
                    ontology_similarity=latest_sim,
                    generation=current_gen,
                    termination_reason=TerminationReason.STAGNATED,
                )

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
                        blocking_gate="eval",
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
                        blocking_gate="ac",
                    )

            # Signal 5: Regression gate — block convergence if ACs regressed
            if self.regression_gate_enabled:
                # Pass only completed generations to regression detector
                completed_lineage = lineage.model_copy(update={"generations": completed})
                regression_report = RegressionDetector().detect(completed_lineage)
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
                        blocking_gate="regression",
                    )

            # Evolution gate: withhold convergence if ontology never actually evolved.
            # When ontology never changes, either Reflect is conservatively
            # preserving a well-performing ontology, or Wonder/Reflect encountered
            # errors. Either way, withhold convergence until genuine evolution occurs.
            evolved_count = self._count_evolved_generations(lineage)
            if evolved_count == 0:
                return ConvergenceSignal(
                    converged=False,
                    reason=(
                        f"Convergence withheld: similarity {latest_sim:.3f} "
                        f"but ontology unchanged across {num_completed} generations "
                        f"(evolution required before convergence is accepted)"
                    ),
                    ontology_similarity=latest_sim,
                    generation=current_gen,
                    blocking_gate="evolution",
                )

            # Ontology completeness gate: block convergence if ontology is structurally thin
            if self.ontology_completeness_gate_enabled:
                completeness_block = self._check_ontology_completeness(lineage)
                if completeness_block is not None:
                    return ConvergenceSignal(
                        converged=False,
                        reason=completeness_block,
                        ontology_similarity=latest_sim,
                        generation=current_gen,
                        blocking_gate="completeness",
                    )

            # Wonder gate: block convergence if Wonder found significant novel questions.
            # When wonder_gate is enabled but no wonder output is available, block
            # convergence — "unable to generate questions" is not the same as
            # "no questions remain" (Ouroboros principle: questions must be answered).
            if self.wonder_gate_enabled and latest_wonder is None:
                return ConvergenceSignal(
                    converged=False,
                    reason=(
                        "Wonder gate: wonder output unavailable — cannot confirm "
                        "no questions remain (wonder_gate_enabled=True)"
                    ),
                    ontology_similarity=latest_sim,
                    generation=current_gen,
                    blocking_gate="wonder",
                )
            if self.wonder_gate_enabled and latest_wonder is not None:
                wonder_block = self._check_wonder_gate(lineage, latest_wonder)
                if wonder_block is not None:
                    return ConvergenceSignal(
                        converged=False,
                        reason=wonder_block,
                        ontology_similarity=latest_sim,
                        generation=current_gen,
                        blocking_gate="wonder",
                    )

            # Drift trend gate: block if drift_score is monotonically increasing
            if self.drift_trend_gate_enabled:
                drift_block = self._check_drift_trend_gate(lineage)
                if drift_block is not None:
                    return ConvergenceSignal(
                        converged=False,
                        reason=drift_block,
                        ontology_similarity=latest_sim,
                        generation=current_gen,
                        blocking_gate="drift_trend",
                    )

            # Validation gate: block convergence if validation was skipped or failed
            if self.validation_gate_enabled and validation_output:
                # Use explicit bool flag when available; fall back to string matching
                if validation_passed is not None:
                    gate_blocked = not validation_passed
                else:
                    gate_blocked = (
                        "skipped" in validation_output.lower()
                        or "error" in validation_output.lower()
                    )
                if gate_blocked:
                    return ConvergenceSignal(
                        converged=False,
                        reason=(f"Validation gate blocked: {validation_output}"),
                        ontology_similarity=latest_sim,
                        generation=current_gen,
                        blocking_gate="validation",
                    )

            return ConvergenceSignal(
                converged=True,
                reason=(
                    f"Ontology converged: similarity {latest_sim:.3f} "
                    f">= threshold {self.convergence_threshold}"
                ),
                ontology_similarity=latest_sim,
                generation=current_gen,
                termination_reason=TerminationReason.CONVERGED,
            )

        # Signal 2: Stagnation (unchanged for N consecutive gens)
        if num_completed >= self.stagnation_window:
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
                    termination_reason=TerminationReason.STAGNATED,
                )

        # Signal 2.5: Oscillation detection (A→B→A→B cycling)
        if self.enable_oscillation_detection and num_completed >= 3:
            oscillating = self._check_oscillation(lineage)
            if oscillating:
                return ConvergenceSignal(
                    converged=True,
                    reason="Oscillation detected: ontology is cycling between similar states",
                    ontology_similarity=latest_sim,
                    generation=current_gen,
                    termination_reason=TerminationReason.OSCILLATED,
                )

        # Signal 3: Repetitive wonder questions
        if latest_wonder and num_completed >= 3:
            repetitive = self._check_repetitive_feedback(lineage, latest_wonder)
            if repetitive:
                return ConvergenceSignal(
                    converged=True,
                    reason="Repetitive feedback: wonder questions are repeating across generations",
                    ontology_similarity=latest_sim,
                    generation=current_gen,
                    termination_reason=TerminationReason.REPETITIVE,
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

    def _check_drift_trend_gate(self, lineage: OntologyLineage) -> str | None:
        """Block convergence if drift_score shows monotonic increase.

        Checks if drift_score has increased in every consecutive pair within
        the recent window. This indicates the ontology is consistently moving
        away from the goal.

        Generations with missing evaluation or drift_score (None) are skipped.
        If fewer than 2 valid scores remain, the gate passes.
        """
        gens = self._completed_generations(lineage)
        if len(gens) < self.drift_trend_window:
            return None

        recent = gens[-self.drift_trend_window :]
        scores = [
            g.evaluation_summary.drift_score
            for g in recent
            if g.evaluation_summary is not None and g.evaluation_summary.drift_score is not None
        ]

        if len(scores) < 2:
            return None

        increases = sum(1 for i in range(1, len(scores)) if scores[i] > scores[i - 1])
        if increases >= len(scores) - 1:
            return (
                f"Drift trend gate: drift_score monotonically increasing over "
                f"{len(scores)} generations ({scores[0]:.3f} → {scores[-1]:.3f})"
            )

        return None

    def _check_wonder_gate(
        self, lineage: OntologyLineage, latest_wonder: WonderOutput
    ) -> str | None:
        """Block convergence if Wonder found significant novel questions.

        Compares latest wonder questions against all previous generations.
        If novelty ratio >= threshold, ontology may still benefit from evolution.

        Returns blocking reason if novel questions exceed threshold, None if OK.
        """
        if not latest_wonder.questions:
            return None

        # Exclude the latest generation — its questions are the ones being
        # evaluated via latest_wonder, so including them would make every
        # question appear "already seen" and the gate would never block.
        prev_questions: set[str] = set()
        for gen in lineage.generations[:-1]:
            prev_questions.update(gen.wonder_questions)

        novel = [q for q in latest_wonder.questions if q not in prev_questions]
        if not latest_wonder.questions:
            return None
        novelty_ratio = len(novel) / len(latest_wonder.questions)

        if novelty_ratio >= self.wonder_novelty_threshold:
            return (
                f"Wonder gate: {len(novel)}/{len(latest_wonder.questions)} novel questions "
                f"(novelty {novelty_ratio:.0%} >= {self.wonder_novelty_threshold:.0%} threshold)"
            )

        return None

    def _check_ontology_completeness(self, lineage: OntologyLineage) -> str | None:
        """Check if ontology meets minimum structural completeness.

        Returns blocking reason if incomplete, None if OK.
        Checks: (1) minimum field count, (2) description quality.
        """
        if not lineage.generations:
            return None

        ontology = lineage.generations[-1].ontology_snapshot

        # Check 1: Minimum field count
        if self.ontology_min_fields > 0 and len(ontology.fields) < self.ontology_min_fields:
            return (
                f"Ontology completeness gate: {len(ontology.fields)} fields "
                f"(minimum {self.ontology_min_fields} required)"
            )

        # Check 2: Trivially short or name-echoing descriptions
        if ontology.fields:
            trivial_count = sum(
                1
                for f in ontology.fields
                if len(f.description.strip()) < 10
                or f.description.strip().lower() == f.name.strip().lower()
            )
            if trivial_count > len(ontology.fields) // 2:
                return (
                    f"Ontology completeness gate: {trivial_count}/{len(ontology.fields)} "
                    f"fields have trivial descriptions"
                )

        return None

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
