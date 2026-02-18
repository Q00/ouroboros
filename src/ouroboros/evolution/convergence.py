"""Convergence criteria for the evolutionary loop.

Determines when the loop should terminate. v1 uses 3 signals:
1. Ontology stability (similarity >= threshold)
2. Stagnation detection (unchanged ontology for N consecutive gens)
3. max_generations hard cap

v1.1 will add drift-trend and evaluation-satisfaction signals.
"""

from __future__ import annotations

from dataclasses import dataclass

from ouroboros.core.lineage import OntologyDelta, OntologyLineage
from ouroboros.evolution.wonder import WonderOutput


@dataclass(frozen=True, slots=True)
class ConvergenceSignal:
    """Result of convergence evaluation."""

    converged: bool
    reason: str
    ontology_similarity: float
    generation: int


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

    def evaluate(
        self,
        lineage: OntologyLineage,
        latest_wonder: WonderOutput | None = None,
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
