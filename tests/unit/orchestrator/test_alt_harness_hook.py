"""Tests for the parallel-executor cross-harness redispatch hook (PR-X X1).

These exercise the narrow ``_maybe_redispatch_alt_harness`` shell: it must fire at
most once per AC, and fall back cleanly (return ``None``) whenever the feature is
off, no alternative exists, or the re-run itself *errors* to spawn — never making
the original failure worse. When the alternate backend actually runs and *fails*,
its failed result IS surfaced as the authoritative outcome (it ran in the shared
workspace and may have edited it), rather than being silently discarded. The live
alternate-runtime spawn is stubbed.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.orchestrator import cross_harness_redispatch as chr
from ouroboros.orchestrator.parallel_executor import ParallelACExecutor
from ouroboros.orchestrator.parallel_executor_models import ACExecutionResult


def _make_executor(*, enabled: bool) -> ParallelACExecutor:
    adapter = MagicMock()
    adapter.runtime_backend = "claude"
    adapter.working_directory = "/tmp/work"
    adapter.self_governs_rate_limit = True
    executor = ParallelACExecutor(
        adapter=adapter,
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        cross_harness_redispatch=enabled,
    )
    return executor


def _rerun_kwargs() -> dict[str, Any]:
    return {
        "ac_index": 0,
        "ac_content": "do the thing",
        "session_id": "sess",
        "tools": [],
        "tool_catalog": None,
        "system_prompt": "sys",
        "seed_goal": "goal",
        "depth": 0,
        "execution_id": "exec-1",
        "level_contexts": None,
        "sibling_acs": None,
        "execution_counters": None,
        "is_sub_ac": False,
        "parent_ac_index": None,
        "sub_ac_index": None,
        "node_identity": None,
    }


def _failed() -> ACExecutionResult:
    return ACExecutionResult(
        ac_index=0,
        ac_content="do the thing",
        success=False,
        error="Stalled (no activity for 90s)",
    )


def _succeeded() -> ACExecutionResult:
    return ACExecutionResult(ac_index=0, ac_content="do the thing", success=True)


@pytest.mark.asyncio
async def test_disabled_returns_none() -> None:
    executor = _make_executor(enabled=False)
    result = await executor._maybe_redispatch_alt_harness(
        result=_failed(),
        execution_context_id="exec-1",
        rerun_kwargs=_rerun_kwargs(),
        atomic_retry_attempt=0,
        stall_retries_exhausted=True,
    )
    assert result is None


@pytest.mark.asyncio
async def test_fires_once_then_cap_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    executor = _make_executor(enabled=True)
    monkeypatch.setattr(chr, "pick_alternative_runtime", lambda *_a, **_k: "codex")
    run_calls = {"n": 0}

    async def _fake_run(backend: str, **kwargs: Any) -> ACExecutionResult:
        run_calls["n"] += 1
        return _succeeded()

    executor._run_single_ac_on_backend = _fake_run  # type: ignore[method-assign]

    first = await executor._maybe_redispatch_alt_harness(
        result=_failed(),
        execution_context_id="exec-1",
        rerun_kwargs=_rerun_kwargs(),
        atomic_retry_attempt=0,
        stall_retries_exhausted=True,
    )
    assert first is not None and first.success is True
    assert run_calls["n"] == 1

    # Second call for the SAME AC hits the one-per-AC cap: no re-run.
    second = await executor._maybe_redispatch_alt_harness(
        result=_failed(),
        execution_context_id="exec-1",
        rerun_kwargs=_rerun_kwargs(),
        atomic_retry_attempt=0,
        stall_retries_exhausted=True,
    )
    assert second is None
    assert run_calls["n"] == 1


@pytest.mark.asyncio
async def test_no_alternative_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    executor = _make_executor(enabled=True)
    monkeypatch.setattr(chr, "pick_alternative_runtime", lambda *_a, **_k: None)
    result = await executor._maybe_redispatch_alt_harness(
        result=_failed(),
        execution_context_id="exec-1",
        rerun_kwargs=_rerun_kwargs(),
        atomic_retry_attempt=0,
        stall_retries_exhausted=True,
    )
    assert result is None


@pytest.mark.asyncio
async def test_rerun_error_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    executor = _make_executor(enabled=True)
    monkeypatch.setattr(chr, "pick_alternative_runtime", lambda *_a, **_k: "codex")

    async def _boom(backend: str, **kwargs: Any) -> ACExecutionResult:
        raise RuntimeError("fresh runtime failed to spawn")

    executor._run_single_ac_on_backend = _boom  # type: ignore[method-assign]
    result = await executor._maybe_redispatch_alt_harness(
        result=_failed(),
        execution_context_id="exec-1",
        rerun_kwargs=_rerun_kwargs(),
        atomic_retry_attempt=0,
        stall_retries_exhausted=True,
    )
    assert result is None


@pytest.mark.asyncio
async def test_alt_failure_is_surfaced_as_authoritative(monkeypatch: pytest.MonkeyPatch) -> None:
    """A FAILED alternate that ran in the workspace is surfaced, not discarded.

    Regression for the workspace-honesty contract: the alternate backend runs in
    the SAME cwd as the original executor, so when it fails AFTER touching the
    workspace the caller must receive the alternate's failed result — naming the
    from→to backends and flagging the possible workspace mutation — rather than
    silently keeping only the original same-runtime failure.
    """
    executor = _make_executor(enabled=True)
    monkeypatch.setattr(chr, "pick_alternative_runtime", lambda *_a, **_k: "codex")

    async def _fail_after_editing_workspace(backend: str, **kwargs: Any) -> ACExecutionResult:
        # Model an alternate that mutated the workspace, then still failed
        # verification — a distinct error from the original same-runtime failure.
        return ACExecutionResult(
            ac_index=0,
            ac_content="do the thing",
            success=False,
            error="codex edited files but verify_command exited 1",
            session_id="alt-sess",
        )

    executor._run_single_ac_on_backend = _fail_after_editing_workspace  # type: ignore[method-assign]
    result = await executor._maybe_redispatch_alt_harness(
        result=_failed(),
        execution_context_id="exec-1",
        rerun_kwargs=_rerun_kwargs(),
        atomic_retry_attempt=0,
        stall_retries_exhausted=True,
    )

    # The alternate's failure is now the authoritative result (not None, not the
    # original same-runtime error verbatim).
    assert result is not None
    assert result.success is False
    assert result.session_id == "alt-sess"
    # It names the alternate backend and flags the possible workspace mutation.
    assert "codex" in (result.error or "")
    assert "alt-harness" in (result.error or "")
    assert "may have modified" in (result.error or "")
    # The alternate's own failure detail is preserved as context, not erased.
    assert "codex edited files but verify_command exited 1" in (result.error or "")


@pytest.mark.asyncio
async def test_alt_success_result_returned_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful alternate is returned as the winning result, unannotated."""
    executor = _make_executor(enabled=True)
    monkeypatch.setattr(chr, "pick_alternative_runtime", lambda *_a, **_k: "codex")

    async def _win(backend: str, **kwargs: Any) -> ACExecutionResult:
        return ACExecutionResult(
            ac_index=0, ac_content="do the thing", success=True, session_id="alt-sess"
        )

    executor._run_single_ac_on_backend = _win  # type: ignore[method-assign]
    result = await executor._maybe_redispatch_alt_harness(
        result=_failed(),
        execution_context_id="exec-1",
        rerun_kwargs=_rerun_kwargs(),
        atomic_retry_attempt=0,
        stall_retries_exhausted=True,
    )
    assert result is not None
    assert result.success is True
    assert result.error is None
