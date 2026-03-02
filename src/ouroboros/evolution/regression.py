"""RegressionDetector — detects AC regressions across generations.

Computes regressions from existing lineage history without new storage.
A regression occurs when an AC that passed in a prior generation
now fails in the latest generation.
"""

from __future__ import annotations

from pydantic import BaseModel

from ouroboros.core.lineage import OntologyLineage


class ACRegression(BaseModel, frozen=True):
    """A detected regression for a single acceptance criterion."""

    ac_index: int
    ac_text: str
    passed_in_generation: int
    failed_in_generation: int
    consecutive_failures: int = 1


class RegressionReport(BaseModel, frozen=True):
    """Summary of regressions in the latest generation."""

    regressions: tuple[ACRegression, ...] = ()

    @property
    def has_regressions(self) -> bool:
        return len(self.regressions) > 0

    @property
    def regressed_ac_indices(self) -> tuple[int, ...]:
        return tuple(r.ac_index for r in self.regressions)


class RegressionDetector:
    """Detects AC regressions by comparing latest generation to history.

    Algorithm:
        For each AC in latest generation:
          If NOT passed AND passed in any prior generation:
            → REGRESSION detected
            Count consecutive failures from latest backwards
            Record last-passed generation
    """

    def detect(self, lineage: OntologyLineage) -> RegressionReport:
        """Detect regressions in the latest generation.

        Args:
            lineage: Full lineage with generation history.

        Returns:
            RegressionReport with detected regressions.
        """
        if len(lineage.generations) < 2:
            return RegressionReport()

        latest = lineage.generations[-1]
        if not latest.evaluation_summary or not latest.evaluation_summary.ac_results:
            return RegressionReport()

        # Build per-AC history: ac_index → list of (gen_number, passed)
        ac_history: dict[int, list[tuple[int, bool]]] = {}
        for gen in lineage.generations:
            if not gen.evaluation_summary or not gen.evaluation_summary.ac_results:
                continue
            for ac in gen.evaluation_summary.ac_results:
                ac_history.setdefault(ac.ac_index, []).append((gen.generation_number, ac.passed))

        regressions: list[ACRegression] = []
        for ac in latest.evaluation_summary.ac_results:
            if ac.passed:
                continue

            history = ac_history.get(ac.ac_index, [])
            if not history:
                continue

            # Find last generation where this AC passed
            last_passed_gen = None
            for gen_num, passed in reversed(history):
                if passed:
                    last_passed_gen = gen_num
                    break

            if last_passed_gen is None:
                # Never passed — not a regression, just a persistent failure
                continue

            # Count consecutive failures from latest backwards
            consecutive = 0
            for _gen_num, passed in reversed(history):
                if not passed:
                    consecutive += 1
                else:
                    break

            regressions.append(
                ACRegression(
                    ac_index=ac.ac_index,
                    ac_text=ac.ac_content,
                    passed_in_generation=last_passed_gen,
                    failed_in_generation=latest.generation_number,
                    consecutive_failures=consecutive,
                )
            )

        return RegressionReport(regressions=tuple(regressions))
