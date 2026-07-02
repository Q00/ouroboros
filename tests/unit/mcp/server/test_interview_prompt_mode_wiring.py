"""Wiring lock: the composition root pairs sealed adapters with the tool-less prompt.

PR #1541 gates the tool-less prompt variant inside ``InterviewHandler`` on
*handler-constructed* sealed adapters (``self.llm_adapter is None``). The MCP
composition root, however, injects the shared stage adapter — which is
catalog-sealed for envelope-capable backends (``allowed_tools=[]`` →
``--tools ""``) — so without wiring here the plugin MCP path would keep
sending the tool-advertising socratic-interviewer prompt into a subprocess
that has no tools, reproducing the phantom tool calls of #1537.

``create_ouroboros_server`` must therefore forward
``suppress_tool_use_prompt_cues`` to the ``InterviewHandler`` it wires
whenever the shared interview adapter is sealed, and must not force it when
the backend has no tool envelope at all.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from ouroboros.mcp.server.adapter import create_ouroboros_server


def _build_server() -> object:
    with (
        patch("ouroboros.providers.create_llm_adapter") as mock_create_llm_adapter,
        patch("ouroboros.orchestrator.create_agent_runtime") as mock_create_runtime,
    ):
        mock_create_llm_adapter.return_value = MagicMock()
        mock_create_runtime.return_value = MagicMock()
        return create_ouroboros_server()


def test_mcp_interview_handler_gets_toolless_prompt_for_sealed_envelope() -> None:
    """Envelope-capable backend ⇒ injected-adapter handler suppresses tool cues."""
    with patch(
        "ouroboros.backends.backend_supports_tool_envelope",
        return_value=True,
    ):
        server = _build_server()

    handler = server._tool_handlers["ouroboros_interview"]
    assert handler.suppress_tool_use_prompt_cues is True, (
        "the composition root injects the sealed shared adapter into "
        "InterviewHandler, so it must also select the tool-less prompt "
        "variant — the handler's own gate only covers self-constructed "
        "adapters (#1537 / PR #1541 follow-up)"
    )


def test_mcp_interview_handler_keeps_full_prompt_without_envelope() -> None:
    """No tool envelope ⇒ the adapter is not sealed ⇒ keep the full prompt."""
    with patch(
        "ouroboros.backends.backend_supports_tool_envelope",
        return_value=False,
    ):
        server = _build_server()

    handler = server._tool_handlers["ouroboros_interview"]
    assert handler.suppress_tool_use_prompt_cues is False, (
        "backends without a tool envelope run un-sealed adapters; forcing the "
        "tool-less prompt there would degrade brownfield question quality for "
        "no safety gain"
    )
