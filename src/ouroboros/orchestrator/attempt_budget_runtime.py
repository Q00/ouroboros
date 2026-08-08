"""Runtime-neutral enforcement and audit helpers for finite provider attempts."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from ouroboros.core.attempt_budget import (
    AttemptBudgetExhaustion,
    AttemptBudgetKind,
    AttemptBudgetProgress,
    conservative_timeout_deadline,
    scale_attempt_budget,
)
from ouroboros.core.seed import AcceptanceCriterionSpec
from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.adapter import AgentMessage, ParamSupport, RuntimeHandle
from ouroboros.orchestrator.decomposition_limits import has_durable_decomposition_replay
from ouroboros.orchestrator.decomposition_policy import (
    DecompositionTraceSummary,
    summarize_decomposition_trace,
)
from ouroboros.orchestrator.events import create_ac_attempt_budget_exhausted_event
from ouroboros.orchestrator.failure_taxonomy import FailureClass
from ouroboros.orchestrator.parallel_executor_models import ACExecutionOutcome, ACExecutionResult
from ouroboros.orchestrator.runtime_message_projection import project_runtime_message
from ouroboros.orchestrator.verifier import RetryAdmission

if TYPE_CHECKING:
    from ouroboros.orchestrator.execution_runtime_scope import (
        ACRuntimeIdentity,
        ExecutionNodeIdentity,
    )
    from ouroboros.orchestrator.leaf_dispatcher import LeafDispatchState
    from ouroboros.orchestrator.parallel_executor import ParallelACExecutor

log = get_logger(__name__)


def terminalize_bounded_route_exhaustion[T](value: T) -> T:
    """Turn an exhausted route result into a durable no-successor boundary."""
    if isinstance(value, ACExecutionResult) and value.attempt_budget_exhaustion is not None:
        return cast(T, replace(value, outcome=ACExecutionOutcome.BLOCKED))
    return value


class AttemptBudgetedMessageStream:
    """Stop one provider stream at its first portable step/time boundary."""

    def __init__(
        self,
        source: AsyncIterator[AgentMessage],
        *,
        max_agentic_steps: int,
        timeout_seconds: float,
        progress: AttemptBudgetProgress | None = None,
    ) -> None:
        if progress is not None:
            progress = AttemptBudgetProgress.from_contract_data(
                progress.to_contract_data(),
                max_agentic_steps=max_agentic_steps,
                timeout_seconds=timeout_seconds,
            )
        else:
            progress = AttemptBudgetProgress.capture(
                agentic_steps_consumed=0,
                elapsed_timeout_seconds=0,
                max_agentic_steps=max_agentic_steps,
                timeout_seconds=timeout_seconds,
            )
        self._source = source
        self._max_agentic_steps = max_agentic_steps
        self._timeout_seconds = timeout_seconds
        self._started_at: float | None = None
        self._progress_at_start = progress
        self._agentic_steps = progress.agentic_steps_consumed
        # This float is telemetry only. Deadlines and durable progress always
        # consume the exact integer remainder in ``_progress_at_start``.
        self._elapsed_before_start = progress.elapsed_timeout_seconds()
        self.exhaustion: AttemptBudgetExhaustion | None = None

    def __aiter__(self) -> AttemptBudgetedMessageStream:
        return self

    @property
    def elapsed_seconds(self) -> float:
        """Return monotonic time consumed since the first provider read."""

        return self._elapsed_before_start + self._elapsed_since_start

    @property
    def _elapsed_since_start(self) -> float:
        if self._started_at is None:
            return 0.0
        return max(0.0, asyncio.get_running_loop().time() - self._started_at)

    def progress(self) -> AttemptBudgetProgress:
        """Return conservative durable progress for a recoverable pause."""

        elapsed_progress = self._progress_at_start.consume_elapsed_seconds(
            self._elapsed_since_start
        )
        return AttemptBudgetProgress(
            max_agentic_steps=self._max_agentic_steps,
            timeout_microseconds=elapsed_progress.timeout_microseconds,
            agentic_steps_consumed=self._agentic_steps,
            remaining_timeout_microseconds=elapsed_progress.remaining_timeout_microseconds,
        )

    def _record_wall_clock_exhaustion(self) -> None:
        if self.exhaustion is None:
            self.exhaustion = AttemptBudgetExhaustion(
                kind=AttemptBudgetKind.WALL_CLOCK,
                limit=self._timeout_seconds,
                observed=self.elapsed_seconds,
            )

    @asynccontextmanager
    async def lifetime(self) -> AsyncIterator[None]:
        """Enforce one fixed deadline across provider reads and consumption."""

        if self._started_at is not None:
            raise RuntimeError("attempt-budget stream lifetime may only be entered once")
        loop = asyncio.get_running_loop()
        self._started_at = loop.time()
        fixed_deadline = conservative_timeout_deadline(
            started_at=self._started_at,
            remaining_seconds=self._progress_at_start.remaining_timeout_seconds,
        )
        if self._progress_at_start.remaining_timeout_microseconds <= 0:
            self._record_wall_clock_exhaustion()
            yield
            return

        deadline = asyncio.timeout_at(fixed_deadline)
        try:
            async with deadline:
                yield
        except TimeoutError:
            if not deadline.expired():
                raise
            self._record_wall_clock_exhaustion()

    async def __anext__(self) -> AgentMessage:
        if self.exhaustion is not None:
            raise StopAsyncIteration
        if self._started_at is None:
            raise RuntimeError("attempt-budget stream requires its lifetime context")

        message = await anext(self._source)

        if project_runtime_message(message).is_tool_call:
            self._agentic_steps += 1
            if self._agentic_steps > self._max_agentic_steps:
                self.exhaustion = AttemptBudgetExhaustion(
                    kind=AttemptBudgetKind.AGENTIC_STEPS,
                    limit=float(self._max_agentic_steps),
                    observed=float(self._agentic_steps),
                )
        return message


@asynccontextmanager
async def shielded_aclosing[T](
    stream: T,
    *,
    close_error_is_observe_only: Callable[[], bool] | None = None,
) -> AsyncIterator[T]:
    """Close an async provider without replacing an authoritative exhaustion."""

    close = getattr(stream, "aclose", None)
    try:
        yield stream
    except BaseException:
        if close is not None:
            close_task = asyncio.ensure_future(close())
            try:
                await asyncio.shield(close_task)
            except asyncio.CancelledError:
                try:
                    await close_task
                except Exception as exc:
                    log.warning(
                        "orchestrator.provider_stream.close_failed_during_cancellation",
                        error=str(exc),
                    )
                raise
            except Exception as exc:
                log.warning(
                    "orchestrator.provider_stream.close_failed_during_unwind",
                    error=str(exc),
                )
        raise
    else:
        if close is not None:
            close_task = asyncio.ensure_future(close())
            try:
                await asyncio.shield(close_task)
            except asyncio.CancelledError:
                try:
                    await close_task
                except Exception as exc:
                    log.warning(
                        "orchestrator.provider_stream.close_failed_during_cancellation",
                        error=str(exc),
                    )
                raise
            except Exception as exc:
                if close_error_is_observe_only is None or not close_error_is_observe_only():
                    raise
                log.warning(
                    "orchestrator.provider_stream.close_failed_after_exhaustion",
                    error=str(exc),
                )


@dataclass(slots=True)
class DirectAttemptBudget:
    """Resolved finite budget for one legacy whole-Seed provider call."""

    max_agentic_steps: int
    timeout_seconds: float
    root_ac_count: int
    attempts_started: int = 0
    resume_progress: AttemptBudgetProgress | None = None
    _last_stream: AttemptBudgetedMessageStream | None = field(
        default=None,
        init=False,
        repr=False,
    )

    @classmethod
    def from_execution_semantics(
        cls,
        execution_semantics: Mapping[str, Any],
        *,
        root_ac_count: int,
        resume_progress: AttemptBudgetProgress | None = None,
    ) -> DirectAttemptBudget:
        """Scale the validated per-AC contract by the whole-Seed root count."""

        bounded_root_count = max(1, root_ac_count)
        max_agentic_steps, timeout_seconds = scale_attempt_budget(
            max_iterations_per_ac=execution_semantics["max_iterations_per_ac"],
            timeout_seconds=execution_semantics["ac_attempt_timeout_seconds"],
            multiplier=bounded_root_count,
        )
        return cls(
            max_agentic_steps=max_agentic_steps,
            timeout_seconds=timeout_seconds,
            root_ac_count=bounded_root_count,
            resume_progress=resume_progress,
        )

    def decorate_prompt(self, prompt: str) -> str:
        """Tell the agent the same budget enforced by the stream owner."""

        return (
            f"{prompt}\n\nHard execution budget: finish this provider call within "
            f"{self.max_agentic_steps} tool-bearing agent turns and "
            f"{self.timeout_seconds:g} total wall-clock seconds. Do not start work "
            "that cannot finish inside this budget."
        )

    def wrap(self, source: AsyncIterator[AgentMessage]) -> AttemptBudgetedMessageStream:
        """Start and count one bounded provider stream."""

        self.attempts_started += 1
        stream = AttemptBudgetedMessageStream(
            source,
            max_agentic_steps=self.max_agentic_steps,
            timeout_seconds=self.timeout_seconds,
            progress=self.resume_progress,
        )
        self.resume_progress = None
        self._last_stream = stream
        return stream

    def progress(self) -> AttemptBudgetProgress:
        """Return the current stream's durable pause state."""

        if self._last_stream is None:
            raise RuntimeError("direct attempt budget has no started stream")
        return self._last_stream.progress()

    async def terminalize(
        self,
        *,
        stream: AttemptBudgetedMessageStream,
        event_store: Any,
        execution_id: str,
        session_id: str,
        runtime_handle: RuntimeHandle | None,
        context: str,
    ) -> str:
        """Persist and terminate one exhausted whole-Seed provider boundary."""

        exhaustion = stream.exhaustion
        if exhaustion is None:  # pragma: no cover - guarded by callers
            raise RuntimeError("direct attempt terminalization requires exhaustion data")
        await terminate_runtime_handle(
            runtime_handle,
            session_id=session_id,
            context=f"{context}_attempt_budget_exhausted",
        )
        event = create_ac_attempt_budget_exhausted_event(
            session_id=session_id,
            ac_index=None,
            ac_id=execution_id,
            attempt=self.attempts_started,
            budget_kind=exhaustion.kind.value,
            limit=exhaustion.limit,
            observed=exhaustion.observed,
            elapsed_seconds=stream.elapsed_seconds,
        )
        event.data.update(
            {
                "scope": "direct_seed",
                "root_ac_count": self.root_ac_count,
                "call_site": context,
            }
        )
        try:
            await event_store.append(event)
        except Exception as exc:
            log.warning(
                "orchestrator.runner.attempt_budget_event_persist_failed",
                execution_id=execution_id,
                session_id=session_id,
                context=context,
                event_type=event.type,
                error=str(exc),
            )
        log.warning(
            "orchestrator.runner.attempt_budget_exhausted",
            execution_id=execution_id,
            session_id=session_id,
            context=context,
            budget_kind=exhaustion.kind.value,
            budget_limit=exhaustion.limit,
            budget_observed=exhaustion.observed,
        )
        return (
            "Direct provider attempt budget exhausted: "
            f"{exhaustion.kind.value} limit={exhaustion.limit:g}, "
            f"observed={exhaustion.observed:g}. No recovery or route successor "
            "was authorized."
        )


async def terminate_runtime_handle(
    runtime_handle: RuntimeHandle | None,
    *,
    session_id: str,
    context: str,
) -> None:
    """Best-effort live runtime termination for controllable handles."""

    if runtime_handle is None or not runtime_handle.can_terminate:
        return
    try:
        terminated = await runtime_handle.terminate()
    except Exception as exc:
        log.warning(
            "orchestrator.runner.runtime_handle_terminate_failed",
            session_id=session_id,
            context=context,
            backend=runtime_handle.backend,
            error=str(exc),
        )
        return
    if terminated:
        log.info(
            "orchestrator.runner.runtime_handle_terminated",
            session_id=session_id,
            context=context,
            backend=runtime_handle.backend,
        )


def should_emit_progress_event(message: AgentMessage, messages_processed: int) -> bool:
    """Return whether one direct message should emit durable progress."""

    projected = project_runtime_message(message)
    runtime_backend = message.resume_handle.backend if message.resume_handle else None
    return (
        message.is_final
        or messages_processed % 10 == 0
        or projected.is_tool_call
        or projected.thinking is not None
        or message.type == "system"
        or runtime_backend == "opencode"
        or projected.is_tool_result
    )


def direct_bounded_route_runtime_active(
    *,
    max_decomposition_depth: int,
    model_router: Any,
    route_economics: Any,
    adapter: Any,
) -> bool:
    """Return whether a direct runner can authorize Routing D effects."""

    return bool(
        has_durable_decomposition_replay(max_decomposition_depth)
        and model_router is not None
        and route_economics is not None
        and getattr(
            getattr(adapter, "capabilities", None),
            "model_override_support",
            ParamSupport.IGNORED,
        )
        is ParamSupport.NATIVE
    )


def build_decomposition_trace_summary(
    *,
    result: ACExecutionResult,
    ac_spec: AcceptanceCriterionSpec | None,
    task_cwd: str | None,
    adapter_cwd: str | None,
) -> DecompositionTraceSummary:
    """Project one failed attempt into typed counts and enums only."""

    verdict = result.atomic_verifier_verdict
    attempted_tool_count = sum(
        1
        for message in result.messages
        if isinstance(message.tool_name, str) and bool(message.tool_name.strip())
    )
    evidence_field_count = (
        len(result.typed_evidence.data)
        if result.typed_evidence is not None and type(result.typed_evidence.data) is dict
        else 0
    )
    verified_artifact_count = 0
    remaining_artifact_count = 0
    if ac_spec is not None and ac_spec.expected_artifacts:
        cwd = Path(task_cwd or adapter_cwd or os.getcwd())
        for artifact in ac_spec.expected_artifacts[:8]:
            target = Path(artifact)
            if not target.is_absolute():
                target = cwd / target
            if target.exists():
                verified_artifact_count += 1
            else:
                remaining_artifact_count += 1
    try:
        failure_class = (
            FailureClass(verdict.failure_class).value
            if verdict is not None and verdict.failure_class is not None
            else "UNKNOWN"
        )
    except ValueError:
        failure_class = "UNKNOWN"
    retry_admission = (
        verdict.retry_admission.value
        if verdict is not None and isinstance(verdict.retry_admission, RetryAdmission)
        else "UNKNOWN"
    )
    lines = [
        f"attempt_message_count={len(result.messages)}",
        f"attempted_tool_count={attempted_tool_count}",
        f"attempt_budget_exhausted={result.attempt_budget_exhaustion is not None}",
        f"typed_evidence_present={result.typed_evidence is not None}",
        f"evidence_field_count={evidence_field_count}",
        f"verified_artifact_count={verified_artifact_count}",
        f"remaining_artifact_count={remaining_artifact_count}",
        f"failure_class={failure_class}",
        f"retry_admission={retry_admission}",
        f"verifier_reason_count={len(verdict.reasons) if verdict is not None else 0}",
        f"failure_detail_present={bool(result.error or result.final_message)}",
    ]
    if result.attempt_budget_exhaustion is not None:
        lines.extend(
            (
                f"attempt_budget_kind={result.attempt_budget_exhaustion.kind.value}",
                f"attempt_budget_limit={result.attempt_budget_exhaustion.limit:g}",
                f"attempt_budget_observed={result.attempt_budget_exhaustion.observed:g}",
            )
        )
    if ac_spec is not None:
        lines.append(f"verify_command_present={bool(ac_spec.verify_command)}")
        lines.append(f"output_assertion_present={bool(ac_spec.output_assertion)}")
    return summarize_decomposition_trace("\n".join(lines))


@dataclass(slots=True)
class AtomicAttemptTerminalizer:
    """Close route-drift and budget boundaries for one atomic dispatch."""

    executor: ParallelACExecutor
    dispatch_state: LeafDispatchState
    seal_dispatch: Callable[..., Awaitable[None]]
    start_time: datetime
    runtime_identity: ACRuntimeIdentity
    node_identity: ExecutionNodeIdentity | None
    execution_context_id: str
    session_id: str
    ac_index: int
    ac_content: str
    retry_attempt: int
    depth: int
    route_candidate: Any
    route_drift_result: Callable[[], ACExecutionResult]

    async def route_drift(self, dispatch_id: str) -> ACExecutionResult:
        """Seal a provider entry whose live route admission became stale."""

        await self.seal_dispatch(
            dispatch_id,
            reason="live route authority changed before provider entry",
        )
        await self.executor._emit_ac_runtime_event(
            event_type="execution.session.failed",
            runtime_identity=self.runtime_identity,
            ac_content=self.ac_content,
            runtime_handle=self.dispatch_state.runtime_handle,
            execution_id=self.execution_context_id,
            session_id=self.dispatch_state.ac_session_id,
            orchestrator_session_id=self.session_id,
            success=False,
            error="route admission blocked: live route state changed before provider entry",
        )
        return self.route_drift_result()

    async def attempt_budget(self, dispatch_id: str) -> ACExecutionResult:
        """Seal, audit, and fail one exhausted atomic provider attempt."""

        exhaustion = self.dispatch_state.attempt_budget_exhaustion
        if exhaustion is None:  # pragma: no cover - guarded by callers
            raise RuntimeError("atomic attempt terminalization requires exhaustion data")
        duration = (datetime.now(UTC) - self.start_time).total_seconds()
        log.warning(
            "parallel_executor.ac.attempt_budget_exhausted",
            ac_index=self.ac_index,
            depth=self.depth,
            budget_kind=exhaustion.kind.value,
            budget_limit=exhaustion.limit,
            budget_observed=exhaustion.observed,
            message_count=self.dispatch_state.message_count,
        )
        await self.seal_dispatch(
            dispatch_id,
            reason=f"provider attempt exhausted {exhaustion.kind.value} budget",
        )
        budget_event = create_ac_attempt_budget_exhausted_event(
            session_id=self.session_id,
            ac_index=self.ac_index,
            ac_id=self.runtime_identity.ac_id,
            attempt=self.runtime_identity.attempt_number,
            budget_kind=exhaustion.kind.value,
            limit=exhaustion.limit,
            observed=exhaustion.observed,
            elapsed_seconds=duration,
        )
        if self.node_identity is not None:
            budget_event.data.update(self.node_identity.to_event_metadata())
        await self.executor._safe_emit_event(budget_event)
        error = (
            "Atomic attempt budget exhausted: "
            f"{exhaustion.kind.value} limit={exhaustion.limit:g}, "
            f"observed={exhaustion.observed:g}."
        )
        await self.executor._emit_ac_runtime_event(
            event_type="execution.session.failed",
            runtime_identity=self.runtime_identity,
            ac_content=self.ac_content,
            runtime_handle=self.dispatch_state.runtime_handle,
            execution_id=self.execution_context_id,
            session_id=self.dispatch_state.ac_session_id,
            orchestrator_session_id=self.session_id,
            success=False,
            error=error,
        )
        return ACExecutionResult(
            ac_index=self.ac_index,
            ac_content=self.ac_content,
            success=False,
            messages=tuple(self.dispatch_state.messages),
            error=error,
            duration_seconds=duration,
            session_id=self.dispatch_state.ac_session_id,
            retry_attempt=self.retry_attempt,
            depth=self.depth,
            route_candidate=self.route_candidate,
            attempt_budget_exhaustion=exhaustion,
        )


__all__ = [
    "AttemptBudgetedMessageStream",
    "AtomicAttemptTerminalizer",
    "DirectAttemptBudget",
    "build_decomposition_trace_summary",
    "direct_bounded_route_runtime_active",
    "should_emit_progress_event",
    "shielded_aclosing",
    "terminalize_bounded_route_exhaustion",
    "terminate_runtime_handle",
]
