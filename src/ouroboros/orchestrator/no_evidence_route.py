"""A code-task attempt that left no evidence is never complete on its own.

An attempt whose workspace has no diff against the ``HEAD`` the run started
from, and that ran no verify command, left no evidence that its criterion is
met (pytest-6197: the worker ran a check that already passed on the unchanged
code and stopped; scikit-learn-25973: the criterion asked only for a
diagnosis). Both were accepted.

The decision reads structured state only: ``Seed.task_type``, the verify gate
outcome (``None``, or a command that never started, is no verify result) and
``attempt_evidence.workspace_diff_evidence``. The first such attempt of a root
AC fails with a short factual note, so the executor's retry loop re-dispatches
it once. After that, or with no retry left, it takes the existing undecided
route (``verify_quarantine``, cause ``no_evidence``) and is marked
``BLOCKED``, so it neither succeeds nor spends more retries. Outside a Git
work tree, for a non-code task, or when a verify command ran, nothing changes.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ouroboros.core.seed import ac_text
from ouroboros.orchestrator.attempt_evidence import (
    NO_EVIDENCE_NOTE,
    workspace_diff_evidence,
    workspace_head,
    workspace_unchanged,
)
from ouroboros.orchestrator.parallel_executor_models import ACExecutionOutcome, ACExecutionResult
from ouroboros.orchestrator.verify_gate_outcome import _VerifyGateOutcome
from ouroboros.orchestrator.verify_quarantine import (
    quarantine_reason,
    quarantine_unverifiable_result,
)

if TYPE_CHECKING:
    from ouroboros.core.seed import Seed


class NoEvidenceRoute:
    """Per-run state of the no-evidence check: the starting ``HEAD`` and one redispatch per AC."""

    def __init__(self) -> None:
        # ``None`` outside a Git work tree (or before ``start``): the check is off.
        self.base_head: str | None = None
        self._redispatched: set[int] = set()

    async def start(self, cwd: str) -> None:
        """Record the workspace ``HEAD`` the run starts from."""
        self.base_head = await asyncio.to_thread(workspace_head, Path(cwd))

    async def route(
        self,
        executor: Any,
        *,
        seed: Seed,
        ac_index: int,
        result: ACExecutionResult,
        session_id: str,
        execution_id: str,
    ) -> ACExecutionResult:
        """Return ``result``, or its no-evidence form when the attempt left no evidence."""
        if not result.success or seed.task_type != "code" or self.base_head is None:
            return result
        outcome = result.verify_gate_outcome
        if outcome is not None and getattr(outcome, "cause", None) != "environment_unverifiable":
            return result
        if result.outcome not in (None, ACExecutionOutcome.SUCCEEDED):
            return result
        cwd = (
            executor._task_cwd
            or getattr(executor._adapter, "working_directory", None)
            or os.getcwd()
        )
        diff = await asyncio.to_thread(workspace_diff_evidence, Path(cwd))
        if not workspace_unchanged(diff, self.base_head):
            return result
        if (
            ac_index not in self._redispatched
            and result.retry_attempt < executor._ac_retry_attempts
        ):
            self._redispatched.add(ac_index)
            return replace(
                result,
                success=False,
                error=NO_EVIDENCE_NOTE,
                final_message=NO_EVIDENCE_NOTE,
                outcome=ACExecutionOutcome.FAILED,
            )
        spec = seed.acceptance_criteria[ac_index]
        quarantined = await quarantine_unverifiable_result(
            result=result,
            spec=spec,
            ac_content=ac_text(spec),
            ac_index=ac_index,
            gate_reason=NO_EVIDENCE_NOTE,
            verify_gate_outcome=_VerifyGateOutcome(
                passed=False,
                reason=NO_EVIDENCE_NOTE,
                output_tail="",
                environment_unverifiable=True,
                cause="no_evidence",
            ),
            session_id=session_id,
            execution_id=execution_id,
            emit_event=executor._safe_emit_event,
        )
        detail = quarantine_reason(NO_EVIDENCE_NOTE)
        return replace(
            quarantined,
            success=False,
            error=detail,
            final_message=detail,
            outcome=ACExecutionOutcome.BLOCKED,
        )


__all__ = ["NoEvidenceRoute"]
