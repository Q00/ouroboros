"""Unit tests for ouroboros.providers.claude_code_adapter module.

Focused on verifying that allowed_tools / disallowed_tools are passed
correctly to the Claude Agent SDK.
"""

from unittest.mock import MagicMock, patch

import pytest

from ouroboros.providers.claude_code_adapter import ClaudeCodeAdapter


class TestClaudeCodeAdapterToolPermissions:
    """Verify that tool permission options are built correctly."""

    def _build_options_kwargs(self, adapter: ClaudeCodeAdapter) -> dict:
        """Extract the options_kwargs dict that _execute_single_request would build.

        We replicate just the tool-permission logic so we can assert on
        the dictionary without actually calling the SDK.
        """
        import os

        dangerous_tools = ["Write", "Edit", "Bash", "Task", "NotebookEdit"]

        if adapter._allowed_tools:
            all_tools = [
                "Read", "Write", "Edit", "Bash", "WebFetch", "WebSearch",
                "Glob", "Grep", "Task", "NotebookEdit", "TodoRead",
                "TodoWrite", "LS",
            ]
            disallowed = [t for t in all_tools if t not in adapter._allowed_tools]
        else:
            disallowed = dangerous_tools

        options_kwargs: dict = {
            "disallowed_tools": disallowed,
            "max_turns": adapter._max_turns,
            "permission_mode": adapter._permission_mode,
            "cwd": os.getcwd(),
            "cli_path": adapter._cli_path,
        }
        if adapter._allowed_tools:
            options_kwargs["allowed_tools"] = adapter._allowed_tools

        return options_kwargs

    def test_permissive_mode_omits_allowed_tools(self) -> None:
        """When no allowed_tools specified, the key is omitted entirely.

        An empty list would tell the SDK 'allow nothing', silently blocking
        Read/Glob/Grep that the interviewer prompt claims are available.
        """
        adapter = ClaudeCodeAdapter(max_turns=3)

        opts = self._build_options_kwargs(adapter)

        assert "allowed_tools" not in opts
        assert "Write" in opts["disallowed_tools"]
        assert "Bash" in opts["disallowed_tools"]
        # Read-only tools should NOT be disallowed
        assert "Read" not in opts["disallowed_tools"]
        assert "Glob" not in opts["disallowed_tools"]
        assert "Grep" not in opts["disallowed_tools"]

    def test_strict_mode_sets_allowed_tools(self) -> None:
        """When allowed_tools is specified, it is included in options."""
        adapter = ClaudeCodeAdapter(
            max_turns=3,
            allowed_tools=["Read", "Glob", "Grep"],
        )

        opts = self._build_options_kwargs(adapter)

        assert opts["allowed_tools"] == ["Read", "Glob", "Grep"]
        # Disallowed should be everything NOT in allowed_tools
        assert "Write" in opts["disallowed_tools"]
        assert "Bash" in opts["disallowed_tools"]
        assert "Read" not in opts["disallowed_tools"]
        assert "Glob" not in opts["disallowed_tools"]
        assert "Grep" not in opts["disallowed_tools"]

    @pytest.mark.asyncio
    async def test_execute_request_omits_allowed_tools_in_permissive_mode(self) -> None:
        """Integration: _execute_single_request does not pass allowed_tools=[] to SDK."""
        adapter = ClaudeCodeAdapter(max_turns=1)

        # Mock the SDK imports and query function
        mock_result_msg = MagicMock()
        type(mock_result_msg).__name__ = "ResultMessage"
        mock_result_msg.result = "What framework are you using?"
        mock_result_msg.is_error = False

        mock_options_cls = MagicMock()
        captured_kwargs: dict = {}

        def capture_options(**kwargs):
            captured_kwargs.update(kwargs)
            return MagicMock()

        mock_options_cls.side_effect = capture_options

        async def mock_query_gen(**kwargs):
            yield mock_result_msg

        with patch.dict("sys.modules", {
            "claude_agent_sdk": MagicMock(
                ClaudeAgentOptions=mock_options_cls,
                query=mock_query_gen,
            ),
            "claude_agent_sdk._errors": MagicMock(
                MessageParseError=type("MessageParseError", (Exception,), {}),
            ),
        }):
            from ouroboros.providers.base import CompletionConfig

            config = CompletionConfig(model="claude-opus-4-6")
            await adapter._execute_single_request("test prompt", config)

            # The key assertion: allowed_tools should NOT be in the kwargs
            assert "allowed_tools" not in captured_kwargs
            assert "disallowed_tools" in captured_kwargs
