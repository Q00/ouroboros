"""Unit tests for deterministic Codex command dispatch."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import AsyncMock, patch

import pytest

from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPTimeoutError, MCPToolError
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.orchestrator.adapter import ResolvedWorkerCwd, RuntimeHandle
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime
from ouroboros.orchestrator.command_dispatcher import (
    CodexCommandDispatcher,
    create_codex_command_dispatcher,
)
from ouroboros.router.types import Resolved


class TestCodexCommandDispatcher:
    """Tests for the in-process dispatcher used by Codex runtimes."""

    def test_server_receives_the_dispatchers_resolved_runtime_workspace(
        self,
        tmp_path: Path,
    ) -> None:
        project = tmp_path / "runtime-project"
        project.mkdir()
        dispatcher = CodexCommandDispatcher(cwd=project)
        server = object()

        with patch(
            "ouroboros.mcp.server.adapter.create_ouroboros_server",
            return_value=server,
        ) as create_server:
            assert dispatcher._get_server() is server

        assert create_server.call_args.kwargs["project_dir"] == str(project.resolve())

    def test_stable_identity_tracks_dispatch_helper_implementation_drift(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Portable dispatcher identity must bind tool-authority implementation code."""
        original = CodexCommandDispatcher(cwd="/tmp/project").stable_identity_contract()

        def replacement_build_tool_arguments(self, intercept, current_handle):  # noqa: ANN001, ARG001
            return {"changed": True}

        monkeypatch.setattr(
            CodexCommandDispatcher,
            "_build_tool_arguments",
            replacement_build_tool_arguments,
        )

        changed = CodexCommandDispatcher(cwd="/tmp/project").stable_identity_contract()

        assert original["kind"] == changed["kind"]
        assert original["implementation_sha256"]
        assert original["implementation_sha256"] != changed["implementation_sha256"]

    def test_stable_identity_tracks_mcp_call_tool_implementation_drift(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Portable dispatcher identity must bind the MCP adapter effect boundary."""
        from ouroboros.mcp.server.adapter import MCPServerAdapter

        original = CodexCommandDispatcher(cwd="/tmp/project").stable_identity_contract()

        async def replacement_call_tool(self, name, arguments):  # noqa: ANN001, ARG001
            return Result.err(RuntimeError("changed"))

        monkeypatch.setattr(MCPServerAdapter, "call_tool", replacement_call_tool)

        changed = CodexCommandDispatcher(cwd="/tmp/project").stable_identity_contract()

        assert original["kind"] == changed["kind"]
        assert original["implementation_sha256"]
        assert original["implementation_sha256"] != changed["implementation_sha256"]

    def test_stable_identity_tracks_mcp_server_factory_implementation_drift(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Portable dispatcher identity must bind server factory composition."""
        import ouroboros.mcp.server.adapter as server_adapter

        original = CodexCommandDispatcher(cwd="/tmp/project").stable_identity_contract()

        def replacement_create_server(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
            raise RuntimeError("changed")

        monkeypatch.setattr(server_adapter, "create_ouroboros_server", replacement_create_server)

        changed = CodexCommandDispatcher(cwd="/tmp/project").stable_identity_contract()

        assert original["kind"] == changed["kind"]
        assert original["implementation_sha256"]
        assert original["implementation_sha256"] != changed["implementation_sha256"]

    def test_stable_identity_tracks_mcp_server_factory_default_drift(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Portable identity must bind behavior-affecting factory defaults."""
        from ouroboros.mcp.server.adapter import create_ouroboros_server

        original = CodexCommandDispatcher(cwd="/tmp/project").stable_identity_contract()
        assert create_ouroboros_server.__kwdefaults__ is not None
        monkeypatch.setitem(create_ouroboros_server.__kwdefaults__, "durable_jobs", False)

        changed = CodexCommandDispatcher(cwd="/tmp/project").stable_identity_contract()

        assert original["implementation_sha256"] != changed["implementation_sha256"]

    def test_stable_identity_tracks_worker_cwd_failure_helper_drift(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Portable identity must bind the dispatcher's fail-closed helper."""
        from ouroboros.orchestrator import command_dispatcher

        original = CodexCommandDispatcher(cwd="/tmp/project").stable_identity_contract()

        def replacement_worker_cwd_failure_message(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
            return None

        monkeypatch.setattr(
            command_dispatcher,
            "worker_cwd_failure_message",
            replacement_worker_cwd_failure_message,
        )

        changed = CodexCommandDispatcher(cwd="/tmp/project").stable_identity_contract()

        assert original["implementation_sha256"] != changed["implementation_sha256"]

    def test_stable_identity_tracks_orchestrator_resume_semantics_drift(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Portable identity must bind behavior behind registered resume handlers."""
        from ouroboros.orchestrator.runner import OrchestratorRunner

        original = CodexCommandDispatcher(cwd="/tmp/project").stable_identity_contract()

        async def replacement_resume_session(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ARG001
            return None

        monkeypatch.setattr(OrchestratorRunner, "resume_session", replacement_resume_session)

        changed = CodexCommandDispatcher(cwd="/tmp/project").stable_identity_contract()

        assert original["implementation_sha256"] != changed["implementation_sha256"]

    def test_stable_identity_is_stable_across_fresh_interpreters(self) -> None:
        """Portable dispatcher identity must not include process-local code repr addresses."""
        script = (
            "import json;"
            "from ouroboros.orchestrator.command_dispatcher import CodexCommandDispatcher;"
            "print(json.dumps(CodexCommandDispatcher(cwd='/tmp/project').stable_identity_contract(), sort_keys=True))"
        )

        first = subprocess.check_output([sys.executable, "-c", script], text=True).strip()
        second = subprocess.check_output([sys.executable, "-c", script], text=True).strip()

        assert json.loads(first)["implementation_sha256"]
        assert first == second

    def test_stable_identity_tracks_dispatcher_global_semantics(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Behavior-affecting globals must be part of portable dispatcher identity."""
        from ouroboros.orchestrator import command_dispatcher

        dispatcher = CodexCommandDispatcher(cwd="/tmp/project")
        original = dispatcher.stable_identity_contract()

        monkeypatch.setattr(
            command_dispatcher,
            "_INTERVIEW_SESSION_METADATA_KEY",
            "changed_session_metadata_key",
        )

        changed = dispatcher.stable_identity_contract()

        assert original["implementation_sha256"] != changed["implementation_sha256"]

    @staticmethod
    def _write_skill(
        skills_dir: Path,
        skill_name: str,
        frontmatter_lines: list[str],
    ) -> None:
        skill_dir = skills_dir / skill_name
        skill_dir.mkdir(parents=True)
        frontmatter = "\n".join(frontmatter_lines)
        (skill_dir / "SKILL.md").write_text(
            f"---\n{frontmatter}\n---\n\n# {skill_name}\n",
            encoding="utf-8",
        )

    @staticmethod
    def _make_intercept(
        skills_dir: Path,
        skill_name: str,
        *,
        mcp_tool: str,
        mcp_args: dict[str, object],
        prompt: str,
        first_argument: str | None,
    ) -> Resolved:
        return Resolved(
            skill_name=skill_name,
            command_prefix=f"ooo {skill_name}",
            prompt=prompt,
            skill_path=skills_dir / skill_name / "SKILL.md",
            mcp_tool=mcp_tool,
            mcp_args=mcp_args,
            first_argument=first_argument,
        )

    @pytest.mark.asyncio
    async def test_unresolved_cwd_blocks_server_creation(self, tmp_path: Path) -> None:
        dispatcher = CodexCommandDispatcher(cwd=ResolvedWorkerCwd(None))
        intercept = self._make_intercept(
            tmp_path,
            "run",
            mcp_tool="ouroboros_execute_seed",
            mcp_args={"seed_path": "seed.yaml"},
            prompt="ooo run seed.yaml",
            first_argument="seed.yaml",
        )

        with patch(
            "ouroboros.mcp.server.adapter.create_ouroboros_server",
            side_effect=AssertionError("server must not be created without a resolved cwd"),
        ):
            messages = await dispatcher.dispatch(intercept)

        assert messages is not None
        assert len(messages) == 1
        assert messages[0].data["error_type"] == "WorkerCwdUnavailable"

    @pytest.mark.asyncio
    async def test_dispatches_ooo_run_before_codex_exec(self, tmp_path: Path) -> None:
        """`ooo run` should resolve through the dispatcher before Codex model execution."""
        self._write_skill(
            tmp_path,
            "run",
            [
                "name: run",
                'description: "Execute a Seed specification through the workflow engine"',
                "mcp_tool: ouroboros_execute_seed",
                "mcp_args:",
                '  seed_path: "$1"',
                '  cwd: "$CWD"',
            ],
        )
        fake_server = AsyncMock()
        fake_server.call_tool = AsyncMock(
            return_value=Result.ok(
                MCPToolResult(
                    content=(
                        MCPContentItem(
                            type=ContentType.TEXT,
                            text="Seed Execution SUCCESS",
                        ),
                    ),
                    meta={"session_id": "sess-123"},
                )
            )
        )
        with (
            patch(
                "ouroboros.mcp.server.adapter.create_ouroboros_server",
                return_value=fake_server,
            ),
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec"
            ) as mock_exec,
        ):
            runtime = CodexCliRuntime(
                cli_path="codex",
                cwd=tmp_path,
                skills_dir=tmp_path,
                skill_dispatcher=create_codex_command_dispatcher(
                    cwd=tmp_path,
                    runtime_backend="codex",
                ),
            )
            messages = [message async for message in runtime.execute_task("ooo run seed.yaml")]

        fake_server.call_tool.assert_awaited_once_with(
            "ouroboros_execute_seed",
            {"seed_path": "seed.yaml", "cwd": str(tmp_path)},
        )
        mock_exec.assert_not_called()
        assert [message.content for message in messages] == [
            "Calling tool: ouroboros_execute_seed",
            "Seed Execution SUCCESS",
        ]
        assert messages[-1].data["session_id"] == "sess-123"

    @pytest.mark.asyncio
    async def test_dispatches_ooo_interview_with_session_reuse(self, tmp_path: Path) -> None:
        """`ooo interview` should resume the stored interview session and return its MCP result."""
        self._write_skill(
            tmp_path,
            "interview",
            [
                "name: interview",
                'description: "Socratic interview to crystallize vague requirements"',
                "mcp_tool: ouroboros_interview",
                "mcp_args:",
                '  initial_context: "$1"',
                '  cwd: "$CWD"',
            ],
        )
        fake_server = AsyncMock()
        fake_server.call_tool = AsyncMock(
            return_value=Result.ok(
                MCPToolResult(
                    content=(
                        MCPContentItem(
                            type=ContentType.TEXT,
                            text="Session interview-123\n\nWhat database do you want?",
                        ),
                    ),
                    meta={"session_id": "interview-123"},
                    is_error=True,
                )
            )
        )
        resume_handle = RuntimeHandle(
            backend="codex_cli",
            native_session_id="thread-123",
            metadata={"ouroboros_interview_session_id": "interview-123"},
        )

        with (
            patch(
                "ouroboros.mcp.server.adapter.create_ouroboros_server",
                return_value=fake_server,
            ),
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec"
            ) as mock_exec,
        ):
            runtime = CodexCliRuntime(
                cli_path="codex",
                cwd=tmp_path,
                skills_dir=tmp_path,
                skill_dispatcher=create_codex_command_dispatcher(
                    cwd=tmp_path,
                    runtime_backend="codex",
                ),
            )
            messages = [
                message
                async for message in runtime.execute_task(
                    'ooo interview "Use PostgreSQL"',
                    resume_handle=resume_handle,
                )
            ]

        call_args = fake_server.call_tool.call_args
        assert call_args[0][0] == "ouroboros_interview"
        actual_args = call_args[0][1]
        # Resume must drop initial_context so InterviewHandler branches on
        # session_id instead of restarting the interview, while preserving
        # cwd and overlaying session_id + answer.
        assert actual_args["session_id"] == "interview-123"
        assert actual_args["answer"] == "Use PostgreSQL"
        assert "initial_context" not in actual_args
        assert "cwd" in actual_args
        mock_exec.assert_not_called()
        assert messages[-1].data["subtype"] == "error"
        assert messages[-1].data["tool_error"] is True
        assert messages[-1].resume_handle is not None
        assert messages[-1].resume_handle.native_session_id == "thread-123"
        assert (
            messages[-1].resume_handle.metadata["ouroboros_interview_session_id"] == "interview-123"
        )

    @pytest.mark.asyncio
    async def test_interview_idk_answer_sequence_preserves_pending_turn_and_calibration(
        self,
        tmp_path: Path,
    ) -> None:
        """An idk control turn must not consume the pending interview answer slot."""
        interview = self._make_intercept(
            tmp_path,
            "interview",
            mcp_tool="ouroboros_interview",
            mcp_args={"initial_context": "Design payment failure handling"},
            prompt="ooo interview Design payment failure handling",
            first_argument="Design payment failure handling",
        )
        idk = self._make_intercept(
            tmp_path,
            "idk",
            mcp_tool="ouroboros_interview",
            mcp_args={"calibration_input": "I do not know idempotency; I built REST APIs"},
            prompt="ooo idk I do not know idempotency; I built REST APIs",
            first_argument="I do not know idempotency; I built REST APIs",
        )
        answer = self._make_intercept(
            tmp_path,
            "interview",
            mcp_tool="ouroboros_interview",
            mcp_args={"initial_context": "Retry once"},
            prompt="ooo interview Retry once",
            first_argument="Retry once",
        )
        calibration = {
            "level": "foundational",
            "confidence": "high",
            "evidence": "I do not know idempotency; I built REST APIs",
            "unknown_terms": [],
        }
        fake_server = AsyncMock()
        fake_server.call_tool = AsyncMock(
            side_effect=(
                Result.ok(
                    MCPToolResult(
                        content=(
                            MCPContentItem(
                                type=ContentType.TEXT, text="Should retries duplicate a charge?"
                            ),
                        ),
                        meta={"session_id": "interview-123"},
                    )
                ),
                Result.ok(
                    MCPToolResult(
                        content=(
                            MCPContentItem(
                                type=ContentType.TEXT, text="Same question, in plain language"
                            ),
                        ),
                        meta={
                            "session_id": "interview-123",
                            "interview_calibration": calibration,
                            "pending_question": "Should retries duplicate a charge?",
                            "pending_question_preserved": True,
                        },
                    )
                ),
                Result.ok(
                    MCPToolResult(
                        content=(MCPContentItem(type=ContentType.TEXT, text="Next question"),),
                        meta={"session_id": "interview-123"},
                    )
                ),
            )
        )
        dispatcher = create_codex_command_dispatcher(cwd=tmp_path, runtime_backend="codex")

        with patch(
            "ouroboros.mcp.server.adapter.create_ouroboros_server",
            return_value=fake_server,
        ):
            started = await dispatcher(interview, None)
            assert started is not None
            calibrated = await dispatcher(idk, started[-1].resume_handle)
            assert calibrated is not None
            completed_turn = await dispatcher(answer, calibrated[-1].resume_handle)

        assert completed_turn is not None
        calls = fake_server.call_tool.await_args_list
        calibration_args = calls[1].args[1]
        assert calibration_args["session_id"] == "interview-123"
        assert "answer" not in calibration_args
        assert calibration_args["calibration_input"].startswith("I do not know")
        answer_args = calls[2].args[1]
        assert answer_args == {
            "session_id": "interview-123",
            "answer": "Retry once",
            "interview_calibration": calibration,
        }
        assert (
            calibrated[-1].resume_handle.metadata["ouroboros_interview_calibration"] == calibration
        )

    @pytest.mark.asyncio
    async def test_dispatch_returns_recoverable_messages_when_call_tool_fails(
        self,
        tmp_path: Path,
    ) -> None:
        """MCP server Result errors should surface as recoverable dispatcher output."""
        self._write_skill(
            tmp_path,
            "run",
            [
                "name: run",
                'description: "Execute a Seed specification through the workflow engine"',
                "mcp_tool: ouroboros_execute_seed",
                "mcp_args:",
                '  seed_path: "$1"',
                '  cwd: "$CWD"',
            ],
        )
        intercept = self._make_intercept(
            tmp_path,
            "run",
            mcp_tool="ouroboros_execute_seed",
            mcp_args={"seed_path": "seed.yaml", "cwd": str(tmp_path)},
            prompt="ooo run seed.yaml",
            first_argument="seed.yaml",
        )
        fake_server = AsyncMock()
        fake_server.call_tool = AsyncMock(
            return_value=Result.err(
                MCPToolError(
                    "Seed tool unavailable",
                    tool_name="ouroboros_execute_seed",
                )
            )
        )
        dispatcher = create_codex_command_dispatcher(cwd=tmp_path, runtime_backend="codex")

        with patch(
            "ouroboros.mcp.server.adapter.create_ouroboros_server",
            return_value=fake_server,
        ):
            messages = await dispatcher(intercept, None)

        assert messages is not None
        assert messages[0].tool_name == "ouroboros_execute_seed"
        assert messages[0].data["tool_input"] == {
            "seed_path": "seed.yaml",
            "cwd": str(tmp_path),
        }
        assert messages[1].is_error is True
        assert messages[1].data["recoverable"] is True
        assert messages[1].data["error_type"] == "MCPToolError"
        assert messages[1].content == "Seed tool unavailable"

    @pytest.mark.asyncio
    async def test_dispatch_returns_recoverable_messages_when_call_tool_raises(
        self,
        tmp_path: Path,
    ) -> None:
        """Transport exceptions should be surfaced as recoverable dispatcher output."""
        self._write_skill(
            tmp_path,
            "run",
            [
                "name: run",
                'description: "Execute a Seed specification through the workflow engine"',
                "mcp_tool: ouroboros_execute_seed",
                "mcp_args:",
                '  seed_path: "$1"',
            ],
        )
        intercept = self._make_intercept(
            tmp_path,
            "run",
            mcp_tool="ouroboros_execute_seed",
            mcp_args={"seed_path": "seed.yaml"},
            prompt="ooo run seed.yaml",
            first_argument="seed.yaml",
        )
        resume_handle = RuntimeHandle(backend="codex_cli", native_session_id="thread-123")
        fake_server = AsyncMock()
        fake_server.call_tool = AsyncMock(
            side_effect=MCPTimeoutError(
                "Tool call timed out",
                server_name="ouroboros-codex-dispatch",
            )
        )
        dispatcher = create_codex_command_dispatcher(cwd=tmp_path, runtime_backend="codex")

        with patch(
            "ouroboros.mcp.server.adapter.create_ouroboros_server",
            return_value=fake_server,
        ):
            messages = await dispatcher(intercept, resume_handle)

        assert messages is not None
        assert messages[0].resume_handle == resume_handle
        assert messages[1].resume_handle == resume_handle
        assert messages[1].is_error is True
        assert messages[1].data["recoverable"] is True
        assert messages[1].data["is_retriable"] is True
        assert messages[1].data["error_type"] == "MCPTimeoutError"
        assert (
            messages[1].content
            == "Tool call timed out server=ouroboros-codex-dispatch retriable=True"
        )

    @pytest.mark.asyncio
    async def test_dispatch_builds_opencode_resume_handle_for_interview_sessions(
        self,
        tmp_path: Path,
    ) -> None:
        """Interview dispatch should persist the selected runtime backend."""
        intercept = self._make_intercept(
            tmp_path,
            "interview",
            mcp_tool="ouroboros_interview",
            mcp_args={"initial_context": "Build a REST API"},
            prompt='ooo interview "Build a REST API"',
            first_argument="Build a REST API",
        )
        fake_server = AsyncMock()
        fake_server.call_tool = AsyncMock(
            return_value=Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="Question 1"),),
                    meta={"session_id": "interview-123"},
                )
            )
        )
        dispatcher = create_codex_command_dispatcher(cwd=tmp_path, runtime_backend="opencode")

        with patch(
            "ouroboros.mcp.server.adapter.create_ouroboros_server",
            return_value=fake_server,
        ):
            messages = await dispatcher(intercept, None)

        assert messages is not None
        assert messages[1].resume_handle is not None
        assert messages[1].resume_handle.backend == "opencode"
        assert messages[1].resume_handle.cwd == str(tmp_path)
        assert (
            messages[1].resume_handle.metadata["ouroboros_interview_session_id"] == "interview-123"
        )
