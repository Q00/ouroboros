"""Wiring lock: sealed no-tools envelope pairs with the tool-less prompt.

The nested ``ouroboros_interview`` question generator runs with
``allowed_tools=[]`` — its subprocess catalog is emptied via ``tools=[]``
(``--tools ""``). The full socratic-interviewer prompt advertises tool use,
so pairing it with a sealed envelope tempts tool-happy models into phantom
tool calls that burn the single ``max_turns=1`` turn (#1537). When the
handler constructs the sealed adapter itself, the per-call engine MUST use
the tool-less prompt variant (``suppress_tool_use_prompt_cues=True``).

Injected adapters (tests, custom wiring) keep the caller's prompt mode —
the handler cannot know whether an injected adapter is sealed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

import pytest

from ouroboros.bigbang.interview import InterviewState, InterviewStatus
from ouroboros.core.types import Result
from ouroboros.mcp.tools.authoring_handlers import InterviewHandler


class _RecordingEngine:
    """Stands in for ``InterviewEngine`` via patching.

    A real class (not a MagicMock instance) so the handler's
    ``isinstance(InterviewEngine, type)`` clone-branch guard passes and the
    per-call engine construction kwargs can be recorded.
    """

    constructed: ClassVar[list[dict[str, Any]]] = []

    def __init__(
        self,
        llm_adapter: Any = None,
        state_dir: Path | None = None,
        model: str | None = None,
        suppress_tool_use_prompt_cues: bool = False,
    ) -> None:
        type(self).constructed.append(
            {
                "llm_adapter": llm_adapter,
                "state_dir": state_dir,
                "model": model,
                "suppress_tool_use_prompt_cues": suppress_tool_use_prompt_cues,
            }
        )
        self.llm_adapter = llm_adapter
        self.state_dir = state_dir
        self.model = model
        self.suppress_tool_use_prompt_cues = suppress_tool_use_prompt_cues
        self.temperature: float | None = None
        self.max_tokens: int | None = None

    async def start_interview(
        self,
        initial_context: str,
        cwd: str | None = None,
        interview_id: str | None = None,
    ) -> Result[InterviewState, Any]:
        state = InterviewState(
            interview_id=interview_id or "interview_toolless0001",
            initial_context=initial_context,
            status=InterviewStatus.IN_PROGRESS,
        )
        return Result.ok(state)

    async def ask_next_question(self, state: InterviewState) -> Result[str, Any]:
        return Result.ok("What is the primary user goal?")

    async def save_state(self, state: InterviewState) -> Result[Path, Any]:
        assert self.state_dir is not None
        path = self.state_dir / f"interview_{state.interview_id}.json"
        path.write_text("{}", encoding="utf-8")
        return Result.ok(path)


async def _run_handler(tmp_path: Path, *, injected_adapter: Any) -> list[dict[str, Any]]:
    _RecordingEngine.constructed = []
    template = _RecordingEngine(state_dir=tmp_path, model="claude-sonnet-4-6")
    handler = InterviewHandler(
        interview_engine=template,  # type: ignore[arg-type]
        llm_adapter=injected_adapter,
        agent_runtime_backend=None,
        opencode_mode=None,
        data_dir=tmp_path,
    )

    with (
        patch(
            "ouroboros.mcp.tools.authoring_handlers.InterviewEngine",
            _RecordingEngine,
        ),
        patch(
            "ouroboros.mcp.tools.authoring_handlers.create_llm_adapter",
            return_value=MagicMock(),
        ),
        patch(
            "ouroboros.mcp.tools.authoring_handlers.backend_supports_tool_envelope",
            return_value=True,
        ),
    ):
        outcome = await handler.handle({"initial_context": "Build a CLI", "cwd": str(tmp_path)})

    assert outcome.is_ok, "handler must complete successfully on the happy path"
    # constructed[0] is the template built above; the handler's per-call
    # clone is the last construction.
    clones = _RecordingEngine.constructed[1:]
    assert clones, "handler must build a per-call engine from the template"
    return clones


@pytest.mark.asyncio
async def test_sealed_envelope_uses_toolless_prompt_variant(tmp_path: Path) -> None:
    """Handler-constructed sealed adapters must suppress tool-use prompt cues."""
    clones = await _run_handler(tmp_path, injected_adapter=None)
    assert clones[-1]["suppress_tool_use_prompt_cues"] is True, (
        "a question generator whose tool catalog is emptied (allowed_tools=[]) "
        "must not receive the tool-advertising socratic-interviewer prompt — "
        "that pairing produces phantom tool calls on max_turns=1 (#1537)"
    )


@pytest.mark.asyncio
async def test_injected_adapter_keeps_caller_prompt_mode(tmp_path: Path) -> None:
    """Injected adapters are opaque — the handler must not force suppression."""
    clones = await _run_handler(tmp_path, injected_adapter=MagicMock())
    assert clones[-1]["suppress_tool_use_prompt_cues"] is False, (
        "the handler cannot know whether an injected adapter is sealed, so it "
        "must preserve the caller's prompt mode"
    )
