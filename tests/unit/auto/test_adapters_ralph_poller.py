"""Focused tests for AutoPipeline Ralph handler adapters."""

from __future__ import annotations

from typing import Any

import pytest

from ouroboros.auto import adapters
from ouroboros.auto.adapters import HandlerRalphPoller


class _FakeJobManager:
    def __init__(self) -> None:
        self._event_store = object()


class _FakeRalphHandler:
    def __init__(self) -> None:
        self._job_manager = _FakeJobManager()


@pytest.mark.asyncio
async def test_handler_ralph_poller_propagates_terminal_generation_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminal job metadata must restore auto state's Ralph generation on resume."""
    handler = _FakeRalphHandler()
    poller = HandlerRalphPoller(handler)  # type: ignore[arg-type]

    async def wait_for_terminal(_job_manager: Any, job_id: str) -> dict[str, Any]:
        assert job_id == "job_ralph_existing"
        return {
            "status": "completed",
            "stop_reason": "qa passed",
            "lineage_id": "lineage-1",
            "iterations": 7,
        }

    monkeypatch.setattr(adapters, "_wait_for_job_terminal", wait_for_terminal)

    result = await poller(job_id="job_ralph_existing")

    assert poller.job_event_store is handler._job_manager._event_store
    assert result == {
        "job_id": "job_ralph_existing",
        "lineage_id": "lineage-1",
        "dispatch_mode": "job",
        "terminal_status": "completed",
        "stop_reason": "qa passed",
        "current_generation": 7,
    }


@pytest.mark.asyncio
async def test_handler_ralph_poller_prefers_generations_over_iterations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resume metadata must preserve lineage generation, not loop iteration count."""
    handler = _FakeRalphHandler()
    poller = HandlerRalphPoller(handler)  # type: ignore[arg-type]

    async def wait_for_terminal(_job_manager: Any, job_id: str) -> dict[str, Any]:
        assert job_id == "job_ralph_existing"
        return {
            "status": "completed",
            "stop_reason": "qa passed",
            "lineage_id": "lineage-1",
            "iterations": 2,
            "generations": [9, 10],
        }

    monkeypatch.setattr(adapters, "_wait_for_job_terminal", wait_for_terminal)

    result = await poller(job_id="job_ralph_existing")

    assert result["current_generation"] == 10
