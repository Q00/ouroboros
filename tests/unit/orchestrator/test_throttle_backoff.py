"""Throttled provider calls (HTTP 429) back off before the next attempt.

Covers the Codex CLI status parser, the structured ``http_status`` it puts on
runtime failures, the AC retry wait, the interview call retry, and the
investigator call retry.
"""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.auto import interview_recovery, investigator
from ouroboros.auto.adapters import HandlerError
from ouroboros.auto.investigator import RepoInvestigator
from ouroboros.auto.state import AutoPipelineState
from ouroboros.core.errors import ProviderError
from ouroboros.core.seed import AcceptanceCriterionSpec, OntologySchema, Seed, SeedMetadata
from ouroboros.core.types import Result
from ouroboros.orchestrator import retry_hints
from ouroboros.orchestrator.adapter import AgentMessage, TaskResult
from ouroboros.orchestrator.adaptive_concurrency import (
    BackendPressureKind,
    classify_backend_pressure,
)
from ouroboros.orchestrator.parallel_executor import ParallelACExecutor
from ouroboros.orchestrator.parallel_executor_models import ACExecutionOutcome, ACExecutionResult
from ouroboros.providers.codex_cli_adapter import CodexCliLLMAdapter, codex_last_http_status
from ouroboros.providers.throttle import is_throttled, throttle_backoff_seconds

STUDY_429 = (
    "exceeded retry limit, last status: 429 Too Many Requests, request id: a4d4d87829915087-NRT"
)


# -- the one Codex CLI parser -------------------------------------------------


@pytest.mark.parametrize(
    ("text", "status"),
    [
        (STUDY_429, 429),
        ("exceeded retry limit, last status: 502 Bad Gateway, request id: x", 502),
        ("Exceeded retry limit, LAST STATUS: 429", 429),
        ("429 Too Many Requests", None),
        ("rate limit reached for requests", None),
        ("the last status update was 2024", None),
        ("", None),
        (None, None),
    ],
)
def test_codex_last_http_status(text: str | None, status: int | None) -> None:
    assert codex_last_http_status(text) == status


def test_codex_failure_details_carry_http_status() -> None:
    details = CodexCliLLMAdapter._codex_failure_details(
        returncode=1, session_id=None, stderr="", stdout_errors=[], message=STUDY_429
    )
    assert details["http_status"] == 429
    other = CodexCliLLMAdapter._codex_failure_details(
        returncode=1, session_id=None, stderr="", stdout_errors=[], message="model not found"
    )
    assert "http_status" not in other


def test_structured_status_alone_classifies_as_concurrency_rejection() -> None:
    """The text fallback would not match this message; the field decides."""
    message = AgentMessage(
        type="result",
        content="exceeded retry limit, last status: 429",
        data={"subtype": "error", "error_type": "TurnFailed", "http_status": 429},
    )
    plain = AgentMessage(
        type="result",
        content="exceeded retry limit, last status: 429",
        data={"subtype": "error", "error_type": "TurnFailed"},
    )
    assert (
        classify_backend_pressure([message], provider_completed=True).kind
        is BackendPressureKind.CONCURRENCY_REJECTION
    )
    assert (
        classify_backend_pressure([plain], provider_completed=True).kind
        is not BackendPressureKind.CONCURRENCY_REJECTION
    )


@pytest.mark.asyncio
async def test_codex_runtime_turn_failed_carries_http_status() -> None:
    from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime

    runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
    messages = runtime._convert_event(
        {"type": "turn.failed", "error": {"message": STUDY_429}}, None
    )
    assert messages[-1].is_error
    assert messages[-1].data["http_status"] == 429


def test_is_throttled_reads_structured_fields_first() -> None:
    assert is_throttled(ProviderError("boom", status_code=429))
    assert is_throttled(ProviderError("boom", details={"http_status": 429}))
    assert is_throttled(HandlerError(f"ouroboros_interview failed: {STUDY_429}"))
    assert not is_throttled(ProviderError("boom", status_code=500))
    assert not is_throttled(HandlerError("ouroboros_interview failed: model not found"))
    assert not is_throttled(None)


def test_backoff_doubles_and_is_capped() -> None:
    assert [throttle_backoff_seconds(n) for n in range(1, 7)] == [30, 60, 120, 240, 300, 300]


# -- AC retries wait out a throttle ------------------------------------------


def _throttled_result(idx: int = 0, *, retry_after: int | None = None) -> ACExecutionResult:
    data: dict[str, Any] = {"subtype": "error", "error_type": "TurnFailed", "http_status": 429}
    if retry_after is not None:
        data["retry_after_seconds"] = retry_after
    return ACExecutionResult(
        ac_index=idx,
        ac_content="ac",
        success=False,
        error=STUDY_429,
        outcome=ACExecutionOutcome.FAILED,
        messages=(AgentMessage(type="result", content=STUDY_429, data=data),),
    )


def test_throttle_retry_delay_schedule() -> None:
    plain_failure = ACExecutionResult(
        ac_index=0, ac_content="ac", success=False, error="tests fail"
    )
    assert retry_hints.throttle_retry_delay(plain_failure, 1) == 0
    assert retry_hints.throttle_retry_delay(_throttled_result(), 1) == 60
    assert retry_hints.throttle_retry_delay(_throttled_result(), 2) == 120
    assert retry_hints.throttle_retry_delay(_throttled_result(), 5) == 300
    assert retry_hints.throttle_retry_delay(_throttled_result(retry_after=400), 1) == 400
    assert retry_hints.throttle_retry_delay(_throttled_result(retry_after=1000), 1) == 600


@pytest.mark.asyncio
async def test_ac_retry_waits_before_redispatching_a_throttled_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(retry_hints, "_throttle_sleep", fake_sleep)
    adapter = MagicMock()
    adapter.runtime_backend = "codex"
    adapter.self_governs_rate_limit = True
    adapter.working_directory = str(tmp_path)
    adapter.permission_mode = "acceptEdits"
    executor = ParallelACExecutor(
        adapter=adapter,
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        run_verify_commands=False,
        ac_retry_attempts=2,
    )
    seed = Seed(
        goal="g",
        acceptance_criteria=(AcceptanceCriterionSpec(description="ac"),),
        ontology_schema=OntologySchema(name="n", description="d"),
        metadata=SeedMetadata(ambiguity_score=0.05),
    )
    dispatches: list[float] = []

    async def fake_batch(**kwargs: Any) -> list[ACExecutionResult]:
        dispatches.append(len(waits))
        if len(dispatches) < 3:
            return [_throttled_result(idx) for idx in kwargs["batch_indices"]]
        return [
            ACExecutionResult(ac_index=idx, ac_content="ac", success=True)
            for idx in kwargs["batch_indices"]
        ]

    executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]

    results = await executor._run_batch_with_verify_and_retry(
        seed=seed,
        batch_executable=[0],
        session_id="s",
        execution_id="e",
        tools=["Read"],
        tool_catalog=None,
        system_prompt="system",
        level_contexts=[],
        ac_retry_attempts={0: 0},
        execution_counters=None,
    )

    assert waits == [60, 120]
    assert dispatches == [0, 1, 2]  # each re-dispatch came after one more wait
    assert isinstance(results[0], ACExecutionResult) and results[0].success


# -- interview calls ----------------------------------------------------------


@pytest.mark.asyncio
async def test_interview_call_backs_off_on_429_beyond_the_transient_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(interview_recovery, "_throttle_sleep", fake_sleep)
    state = AutoPipelineState(goal="g", cwd=".")
    state.deadline_at = time.monotonic() + 100.0
    state.deadline_at_epoch = time.time() + 100.0
    calls = 0

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls <= 4:
            raise HandlerError(f"ouroboros_interview failed: {STUDY_429}")
        return "question"

    result = await interview_recovery.with_transient_retry(
        flaky, state, tool_name="interview.answer", timeout_seconds=5
    )

    assert result == "question"
    assert calls == 5  # more than the 3 transient attempts
    assert waits == [30, 60, 120, 240]


@pytest.mark.asyncio
async def test_interview_throttle_waits_stop_at_the_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(interview_recovery, "_throttle_sleep", fake_sleep)
    monkeypatch.setattr(interview_recovery, "_INTERVIEW_TRANSIENT_BACKOFF_SECONDS", (0.0, 0.0))
    state = AutoPipelineState(goal="g", cwd=".")

    async def always_throttled() -> str:
        raise HandlerError(f"ouroboros_interview failed: {STUDY_429}")

    with pytest.raises(HandlerError):
        await interview_recovery.with_transient_retry(
            always_throttled, state, tool_name="interview.answer", timeout_seconds=5
        )
    assert waits == [30, 60, 120, 240, 300]
    assert sum(waits) <= 900


@pytest.mark.asyncio
async def test_interview_non_throttle_failure_keeps_three_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(interview_recovery, "_INTERVIEW_TRANSIENT_BACKOFF_SECONDS", (0.0, 0.0))
    state = AutoPipelineState(goal="g", cwd=".")
    calls = 0

    async def broken() -> str:
        nonlocal calls
        calls += 1
        raise HandlerError("ouroboros_interview failed: model not found")

    with pytest.raises(HandlerError):
        await interview_recovery.with_transient_retry(
            broken, state, tool_name="interview.answer", timeout_seconds=5
        )
    assert calls == 3


@pytest.mark.asyncio
async def test_interview_throttle_wait_does_not_spend_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    async def short_sleep(seconds: float) -> None:
        await asyncio.sleep(0.05)

    monkeypatch.setattr(interview_recovery, "_throttle_sleep", short_sleep)
    state = AutoPipelineState(goal="g", cwd=".")
    state.deadline_at = time.monotonic() + 100.0
    before = state.deadline_at
    calls = 0

    async def once_throttled() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise HandlerError(f"ouroboros_interview failed: {STUDY_429}")
        return "ok"

    await interview_recovery.with_transient_retry(
        once_throttled, state, tool_name="interview.answer", timeout_seconds=5
    )
    assert state.deadline_at - before >= 0.04


# -- investigator calls -------------------------------------------------------


class _ThrottlingRuntime:
    _runtime_backend = "codex"

    def __init__(self, replies: list[Any]) -> None:
        self._replies = replies

    def _build_permission_args(self) -> list[str]:
        return ["--sandbox", "read-only"]

    async def execute_task_to_result(self, prompt: str, **_kw: Any) -> Any:
        return self._replies.pop(0)


@pytest.mark.asyncio
async def test_investigator_reissues_a_throttled_call_without_using_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(investigator, "_throttle_sleep", fake_sleep)
    (tmp_path / "a.py").write_text("def handler():\n    return 'value ok'\n", encoding="utf-8")
    reply = json.dumps(
        {
            "kind": "current_behavior",
            "status": "found",
            "answer": "handler returns a fixed value.",
            "evidence": [
                {"path": "a.py", "start_line": 1, "end_line": 2, "quote": "return 'value ok'"}
            ],
        }
    )
    replies: list[Any] = [
        Result.err(ProviderError(STUDY_429)),
        Result.err(ProviderError(STUDY_429)),
        Result.ok(TaskResult(success=True, final_message=reply, messages=())),
    ]
    inv = RepoInvestigator(
        runtime_backend="codex", runtime_factory=lambda **_k: _ThrottlingRuntime(replies)
    )

    result = await inv.investigate("goal", "What does handler return?", tmp_path)

    assert result is not None and result.found
    assert inv.calls_made == 1
    assert waits == [30, 60]
    assert result.throttle_waited_seconds == 90
