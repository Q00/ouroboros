"""Wiring lock for the nested-MCP interview ``allowed_tools`` empty envelope.

The nested ``ouroboros_interview`` MCP-tool entrypoint
(``InterviewHandler.handle()``) MUST construct its question-generation
adapter with ``allowed_tools=[]`` (when the backend supports a tool
envelope) so the model cannot consume the single allowed turn on a
read-only tool-use block.

Background — https://github.com/Q00/ouroboros/issues/765
``max_turns=1`` was paired with the *read-only* policy envelope
(``_interview_allowed_tools``) by ``d7bbbf09`` ("Enforce read-only
policy envelopes for MCP handlers"). On modern Claude (Opus 4.x) the
model frequently chooses a ``Read``/``Grep``/``Glob`` tool call as its
first turn, immediately exhausting the budget and surfacing
``Reached maximum number of turns (1)`` before any final text streams.

This test pins the wiring so a future "policy consistency" sweep
cannot silently re-open the envelope and re-introduce the regression.
``PMInterviewHandler._get_engine`` (``pm_handler.py``) closes the
envelope the same way and is the prior-art reference.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ouroboros.bigbang.interview import InterviewState, InterviewStatus
from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError
from ouroboros.mcp.tools.authoring_handlers import InterviewHandler


@dataclass(slots=True)
class _FakeInterviewEngine:
    """Minimal injected engine — same shape as ``test_interview_strict_mcp_config_wiring``."""

    state_dir: Path
    saved_states: list[InterviewState] = field(default_factory=list)

    async def start_interview(
        self,
        initial_context: str,
        cwd: str | None = None,
        interview_id: str | None = None,
    ) -> Result[InterviewState, MCPServerError]:
        sid = interview_id or "interview_allowedtoolswiring001"
        state = InterviewState(
            interview_id=sid,
            initial_context=initial_context,
            status=InterviewStatus.IN_PROGRESS,
        )
        self.saved_states.append(state)
        return Result.ok(state)

    async def ask_next_question(self, state: InterviewState) -> Result[str, MCPServerError]:
        return Result.ok("What is the primary user goal?")

    async def save_state(self, state: InterviewState) -> Result[Path, MCPServerError]:
        path = self.state_dir / f"interview_{state.interview_id}.json"
        path.write_text("{}", encoding="utf-8")
        self.saved_states.append(state)
        return Result.ok(path)

    async def load_state(
        self, session_id: str
    ) -> Result[InterviewState, MCPServerError]:  # pragma: no cover
        raise NotImplementedError


@pytest.mark.asyncio
async def test_nested_mcp_interview_handler_pins_allowed_tools_to_empty_for_envelope_backends(
    tmp_path: Path,
) -> None:
    """``InterviewHandler.handle()`` MUST close the tool envelope on max_turns=1.

    Regression guard for https://github.com/Q00/ouroboros/issues/765 — a
    non-empty ``allowed_tools`` paired with ``max_turns=1`` lets the model
    burn the only allowed turn on a tool-use block.
    """
    engine = _FakeInterviewEngine(state_dir=tmp_path)
    handler = InterviewHandler(
        interview_engine=engine,
        agent_runtime_backend=None,
        opencode_mode=None,
        data_dir=tmp_path,
    )

    fake_adapter = MagicMock()

    with (
        patch(
            "ouroboros.mcp.tools.authoring_handlers.create_llm_adapter",
            return_value=fake_adapter,
        ) as mock_factory,
        patch(
            "ouroboros.mcp.tools.authoring_handlers.backend_supports_tool_envelope",
            return_value=True,
        ),
        patch(
            "ouroboros.mcp.tools.authoring_handlers.resolve_llm_backend",
            side_effect=lambda backend: backend or "claude",
        ),
    ):
        outcome = await handler.handle({"initial_context": "Build a CLI", "cwd": str(tmp_path)})

    assert outcome.is_ok, "handler must complete successfully on the happy path"
    assert mock_factory.called, "handler must construct an LLM adapter via the factory"

    factory_kwargs = mock_factory.call_args.kwargs
    assert factory_kwargs.get("max_turns") == 1, (
        "interview adapter is single-shot; this test pairs with the empty envelope below"
    )
    assert factory_kwargs.get("allowed_tools") == [], (
        "interview adapter MUST be wired with allowed_tools=[] when the backend "
        "supports a tool envelope. A non-empty envelope paired with max_turns=1 "
        "lets the model spend the only allowed turn on a Read/Grep/Glob tool call "
        "and the SDK raises 'Reached maximum number of turns (1)' before any "
        "final text can stream. See issue #765 / commit d7bbbf09 for history."
    )


@pytest.mark.asyncio
async def test_nested_mcp_interview_handler_passes_none_for_envelope_unaware_backends(
    tmp_path: Path,
) -> None:
    """Backends without a tool-envelope concept (e.g. hermes) must receive ``None``.

    Mirrors ``PMInterviewHandler._get_engine`` so the two handlers stay aligned.
    """
    engine = _FakeInterviewEngine(state_dir=tmp_path)
    handler = InterviewHandler(
        interview_engine=engine,
        agent_runtime_backend=None,
        opencode_mode=None,
        data_dir=tmp_path,
    )

    fake_adapter = MagicMock()

    with (
        patch(
            "ouroboros.mcp.tools.authoring_handlers.create_llm_adapter",
            return_value=fake_adapter,
        ) as mock_factory,
        patch(
            "ouroboros.mcp.tools.authoring_handlers.backend_supports_tool_envelope",
            return_value=False,
        ),
        patch(
            "ouroboros.mcp.tools.authoring_handlers.resolve_llm_backend",
            side_effect=lambda backend: backend or "hermes",
        ),
    ):
        outcome = await handler.handle({"initial_context": "Build a CLI", "cwd": str(tmp_path)})

    assert outcome.is_ok
    factory_kwargs = mock_factory.call_args.kwargs
    assert factory_kwargs.get("allowed_tools") is None, (
        "envelope-unaware backends must receive allowed_tools=None, not [], to "
        "preserve the pre-policy default behaviour for those runtimes"
    )
