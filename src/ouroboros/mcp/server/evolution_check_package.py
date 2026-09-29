"""The check package of each evolve generation, from preparation to its verdicts.

Every evolve generation is a run: before its worker starts, its check package
is constructed on that generation's base, frozen, and admitted, exactly as for
``ooo run`` (``CheckPackageRun``). The evolve loop names each generation's run with one
deterministic execution id, given to both its executor and its evaluator; the
evaluator reads that run's recorded decision, and no other run's, and lets it
decide the criteria it covered (``apply_package_decisions``); the source-scan
verifier decides the rest.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import structlog

from ouroboros.boundary.decision import recorded_criterion_decisions
from ouroboros.boundary.ledger import BoundaryOrderError
from ouroboros.boundary.run_control import CheckPackageRun
from ouroboros.core.types import Result
from ouroboros.mcp.server.spec_verification_adapter import apply_package_decisions
from ouroboros.orchestrator.runner import OrchestratorError
from ouroboros.persistence.event_store import EventStore

log = structlog.get_logger(__name__)


@dataclass
class GenerationCheckPackages:
    """Prepares each generation's package and reads its decision back for evaluation."""

    event_store: EventStore

    async def execute(
        self,
        runner: Any,
        seed: Any,
        *,
        execution_id: str | None,
        worker_dir: Path,
        runtime_backend: str,
        model: str | None,
        parallel: bool,
        externally_satisfied_acs: dict[int, dict[str, Any]] | None,
    ) -> Any:
        """Prepare the generation's package, run the worker, and close the package record."""
        run_id = execution_id or f"exec_{uuid4().hex[:12]}"
        check_package = CheckPackageRun.resolve()
        try:
            await check_package.prepare(
                runner,
                seed,
                event_store=self.event_store,
                execution_id=run_id,
                worker_dir=worker_dir,
                runtime_backend=runtime_backend,
                model=model,
                resume=False,
            )
        except BoundaryOrderError as exc:
            return Result.err(
                OrchestratorError(f"Check package refused the worker start: {exc.message}")
            )
        result = await runner.execute_seed(
            seed=seed,
            execution_id=run_id,
            parallel=parallel,
            externally_satisfied_acs=externally_satisfied_acs,
        )
        value = result.value if getattr(result, "is_ok", False) else None
        terminal = "completed" if getattr(value, "success", False) else "failed"
        if value is not None and not value.success and value.summary.get("cancelled"):
            terminal = "cancelled"
        check_package.finish(terminal)
        return result

    async def decide(self, summary: Any, seed: Any, execution_id: str | None) -> Any:
        """``summary`` with the named run's recorded package verdicts on what they covered.

        ``execution_id`` is the generation's run as the evolve loop named it, in
        this process or after a resume; with no run named there is no decision.
        """
        if execution_id is None:
            return summary
        try:
            decisions = await recorded_criterion_decisions(self.event_store, execution_id, seed)
        except Exception as exc:  # noqa: BLE001 - an unreadable decision is no evidence
            log.warning("evolution.recorded_decision_unreadable", error=str(exc))
            return summary
        return apply_package_decisions(summary, decisions, seed)
