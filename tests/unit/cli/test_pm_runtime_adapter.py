"""Tests for PM CLI adapter selection."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import typer

from ouroboros.cli.commands.pm import _run_pm_interview


def test_run_pm_interview_uses_factory_for_interview_adapter() -> None:
    """PM should honor the shared backend factory instead of hardcoding LiteLLM."""
    sentinel_adapter = object()
    engine = SimpleNamespace(
        load_state=AsyncMock(return_value=SimpleNamespace(is_err=True, error="boom")),
    )

    with (
        patch("ouroboros.cli.commands.pm.create_llm_adapter", return_value=sentinel_adapter) as mock_factory,
        patch("ouroboros.bigbang.pm_interview.PMInterviewEngine.create", return_value=engine) as mock_create,
    ):
        try:
            asyncio.run(
                _run_pm_interview(
                    resume_id="session-123",
                    model="default",
                    debug=False,
                    output_dir=None,
                )
            )
        except typer.Exit:
            pass
        else:
            raise AssertionError("Expected typer.Exit when mocked load_state returns an error")

    mock_factory.assert_called_once_with(use_case="interview")
    mock_create.assert_called_once_with(llm_adapter=sentinel_adapter, model="default")
