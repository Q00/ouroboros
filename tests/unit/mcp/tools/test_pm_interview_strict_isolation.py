"""Wiring locks for #1768: PM interview strict-MCP isolation.

Two layers regressed together: ``PMInterviewHandler._get_engine()`` missed
the ``strict_mcp_config=True`` opt-in that ``InterviewHandler`` gained in
#765, and the production server assembly injected the shared stage adapter
into both interview handlers — which short-circuits the strict factory path
entirely (``self.llm_adapter or create_llm_adapter(...)``). Under a heavy
Claude Code harness the un-isolated question-generation subprocess then
front-loads every plugin MCP server and dies with ``Prompt is too long``
from round two.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from ouroboros.mcp.tools.authoring_handlers import InterviewHandler
from ouroboros.mcp.tools.pm_handler import PMInterviewHandler


class TestPMEngineStrictOptIn:
    def test_get_engine_requests_strict_mcp_config(self, tmp_path) -> None:
        handler = PMInterviewHandler(data_dir=tmp_path)

        with (
            patch("ouroboros.mcp.tools.pm_handler.create_llm_adapter") as create_adapter,
            patch("ouroboros.mcp.tools.pm_handler.PMInterviewEngine") as engine_cls,
        ):
            create_adapter.return_value = MagicMock()
            engine_cls.create.return_value = MagicMock()
            handler._get_engine()

        assert create_adapter.call_count == 1
        kwargs = create_adapter.call_args.kwargs
        assert kwargs.get("strict_mcp_config") is True


class TestProductionAssemblyLeavesHandlersIsolated:
    """The shared stage adapter must not be injected into interview handlers.

    With an injected adapter the strict factory branch never runs, so the
    per-handler opt-in is dead code on the MCP-server path — the exact
    production shape the issue reproduces.
    """

    def test_interview_handlers_have_no_injected_adapter(self) -> None:
        from ouroboros.mcp.server.adapter import create_ouroboros_server

        server = create_ouroboros_server()
        interview_handlers = [
            handler
            for handler in server._tool_handlers.values()
            if isinstance(handler, (InterviewHandler, PMInterviewHandler))
        ]
        assert interview_handlers, "no interview handlers registered"
        for handler in interview_handlers:
            assert handler.llm_adapter is None, (
                f"{type(handler).__name__} received the shared stage adapter; "
                "the strict-isolation factory path is unreachable"
            )

    def test_auto_path_interview_handler_has_no_injected_adapter(self) -> None:
        from ouroboros.mcp.server.adapter import create_ouroboros_server
        from ouroboros.mcp.tools.auto_handler import AutoHandler

        server = create_ouroboros_server()
        auto = next(
            (
                handler
                for handler in server._tool_handlers.values()
                if isinstance(handler, AutoHandler)
            ),
            None,
        )
        assert auto is not None, "AutoHandler not registered"
        assert auto.interview_handler.llm_adapter is None
