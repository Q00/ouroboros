from pathlib import Path

import pytest

from ouroboros.core.types import Result
from ouroboros.mcp.tools.authoring_handlers import GenerateSeedHandler, InterviewHandler
from ouroboros.mcp.tools.channel_workflow_handler import ChannelWorkflowHandler
from ouroboros.mcp.tools.execution_handlers import StartExecuteSeedHandler
from ouroboros.mcp.tools.job_handlers import JobResultHandler, JobStatusHandler, JobWaitHandler
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.openclaw.workflow import ChannelRepoRegistry, ChannelWorkflowManager


class FakeInterviewHandler(InterviewHandler):
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def handle(self, arguments: dict[str, str]):
        self.calls.append(arguments)
        if arguments.get("initial_context"):
            return Result.ok(
                MCPToolResult(
                    content=(
                        MCPContentItem(
                            type=ContentType.TEXT,
                            text="Session sess_1\n\n(ambiguity: 0.80) What should this do?",
                        ),
                    ),
                    is_error=False,
                    meta={"session_id": "sess_1", "ambiguity_score": 0.80, "seed_ready": False},
                )
            )
        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text='Interview completed. Session ID: sess_1\n\nGenerate a Seed with: session_id="sess_1"',
                    ),
                ),
                is_error=False,
                meta={"session_id": "sess_1", "completed": True, "ambiguity_score": 0.18},
            )
        )


class FakeGenerateSeedHandler(GenerateSeedHandler):
    def __init__(self) -> None:
        self.should_fail = False

    async def handle(self, arguments: dict[str, str]):
        if self.should_fail:
            return Result.err(Exception("seed generation failed"))
        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=(
                            "Seed Generated Successfully\n"
                            "=========================\n"
                            "Seed ID: seed_1\n"
                            "--- Seed YAML ---\n"
                            "goal: Demo\nacceptance_criteria:\n- one\nconstraints:\n- two\n"
                        ),
                    ),
                ),
                is_error=False,
                meta={"seed_id": "seed_1"},
            )
        )


class FakeStartExecuteSeedHandler(StartExecuteSeedHandler):
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []
        self.should_fail = False

    async def handle(self, arguments: dict[str, str]):
        self.calls.append(arguments)
        if self.should_fail:
            return Result.err(Exception("execution start failed"))
        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text="Started background execution.\n\nJob ID: job_1\nSession ID: orch_1\nExecution ID: exec_1",
                    ),
                ),
                is_error=False,
                meta={"job_id": "job_1", "session_id": "orch_1", "execution_id": "exec_1"},
            )
        )


class FakeJobStatusHandler(JobStatusHandler):
    def __init__(self) -> None:
        self.status = "completed"
        self.text = "Job status: completed"

    async def handle(self, arguments: dict[str, str]):
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=self.text),),
                is_error=False,
                meta={"job_id": "job_1", "status": self.status, "cursor": 1},
            )
        )


class FakeJobWaitHandler(JobWaitHandler):
    def __init__(self) -> None:
        self.status = "completed"
        self.text = "Job wait: completed"
        self.cursor = 1
        self.changed = True

    async def handle(self, arguments: dict[str, str]):
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=self.text),),
                is_error=False,
                meta={
                    "job_id": "job_1",
                    "status": self.status,
                    "cursor": self.cursor,
                    "changed": self.changed,
                },
            )
        )


class FakeJobResultHandler(JobResultHandler):
    def __init__(self) -> None:
        self.text = "Draft PR ready: https://example.com/pr/123"

    async def handle(self, arguments: dict[str, str]):
        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=self.text,
                    ),
                ),
                is_error=False,
                meta={"job_id": "job_1", "status": "completed"},
            )
        )


@pytest.fixture
def handler(tmp_path: Path) -> ChannelWorkflowHandler:
    interview = FakeInterviewHandler()
    generate = FakeGenerateSeedHandler()
    execute = FakeStartExecuteSeedHandler()
    job_status = FakeJobStatusHandler()
    job_wait = FakeJobWaitHandler()
    job_result = FakeJobResultHandler()
    tool = ChannelWorkflowHandler(
        workflow_manager=ChannelWorkflowManager(tmp_path / "state.json"),
        repo_registry=ChannelRepoRegistry(tmp_path / "repos.json"),
        interview_handler=interview,
        generate_seed_handler=generate,
        start_execute_seed_handler=execute,
        job_status_handler=job_status,
        job_wait_handler=job_wait,
        job_result_handler=job_result,
    )
    tool._fake_interview = interview  # type: ignore[attr-defined]
    tool._fake_generate = generate  # type: ignore[attr-defined]
    tool._fake_execute = execute  # type: ignore[attr-defined]
    tool._fake_job_status = job_status  # type: ignore[attr-defined]
    tool._fake_job_wait = job_wait  # type: ignore[attr-defined]
    tool._fake_job_result = job_result  # type: ignore[attr-defined]
    return tool


@pytest.mark.asyncio
async def test_set_repo_and_start_interview_flow(handler: ChannelWorkflowHandler) -> None:
    set_result = await handler.handle(
        {"action": "set_repo", "guild_id": "g1", "channel_id": "c1", "repo": "/repo/demo"}
    )
    assert set_result.is_ok

    result = await handler.handle(
        {
            "action": "message",
            "guild_id": "g1",
            "channel_id": "c1",
            "user_id": "u1",
            "message": "work on feature x",
        }
    )

    assert result.is_ok
    assert result.value.meta["stage"] == "interviewing"
    assert result.value.meta["session_id"] == "sess_1"


@pytest.mark.asyncio
async def test_same_channel_second_request_is_queued(handler: ChannelWorkflowHandler) -> None:
    await handler.handle(
        {"action": "set_repo", "guild_id": "g1", "channel_id": "c1", "repo": "/repo/demo"}
    )
    await handler.handle(
        {
            "action": "message",
            "guild_id": "g1",
            "channel_id": "c1",
            "user_id": "u1",
            "message": "work on feature x",
        }
    )

    result = await handler.handle(
        {
            "action": "message",
            "guild_id": "g1",
            "channel_id": "c1",
            "user_id": "u2",
            "message": "work on feature y",
        }
    )

    assert result.is_ok
    assert result.value.meta["stage"] == "queued"


@pytest.mark.asyncio
async def test_interview_completion_generates_seed_and_starts_execution(
    handler: ChannelWorkflowHandler,
) -> None:
    await handler.handle(
        {"action": "set_repo", "guild_id": "g1", "channel_id": "c1", "repo": "/repo/demo"}
    )
    await handler.handle(
        {
            "action": "message",
            "guild_id": "g1",
            "channel_id": "c1",
            "user_id": "u1",
            "message": "work on feature x",
        }
    )

    result = await handler.handle(
        {
            "action": "message",
            "guild_id": "g1",
            "channel_id": "c1",
            "user_id": "u1",
            "message": "done",
            "mode": "answer",
        }
    )

    assert result.is_ok
    assert result.value.meta["stage"] == "executing"
    assert result.value.meta["job_id"] == "job_1"


@pytest.mark.asyncio
async def test_seed_like_input_skips_interview(handler: ChannelWorkflowHandler) -> None:
    await handler.handle(
        {"action": "set_repo", "guild_id": "g1", "channel_id": "c2", "repo": "/repo/demo"}
    )
    result = await handler.handle(
        {
            "action": "message",
            "guild_id": "g1",
            "channel_id": "c2",
            "user_id": "u1",
            "message": "goal: Demo\nacceptance_criteria:\n- one\nconstraints:\n- two\n",
        }
    )

    assert result.is_ok
    assert result.value.meta["stage"] == "executing"


@pytest.mark.asyncio
async def test_poll_completes_execution_and_reports_draft_pr(
    handler: ChannelWorkflowHandler,
) -> None:
    await handler.handle(
        {"action": "set_repo", "guild_id": "g1", "channel_id": "c2", "repo": "/repo/demo"}
    )
    await handler.handle(
        {
            "action": "message",
            "guild_id": "g1",
            "channel_id": "c2",
            "user_id": "u1",
            "message": "goal: Demo\nacceptance_criteria:\n- one\nconstraints:\n- two\n",
        }
    )

    result = await handler.handle({"action": "poll", "guild_id": "g1", "channel_id": "c2"})

    assert result.is_ok
    assert "https://example.com/pr/123" in result.value.content[0].text


@pytest.mark.asyncio
async def test_wait_completes_execution_and_reports_draft_pr(
    handler: ChannelWorkflowHandler,
) -> None:
    await handler.handle(
        {"action": "set_repo", "guild_id": "g1", "channel_id": "cw", "repo": "/repo/demo"}
    )
    await handler.handle(
        {
            "action": "message",
            "guild_id": "g1",
            "channel_id": "cw",
            "user_id": "u1",
            "message": "goal: Demo\nacceptance_criteria:\n- one\nconstraints:\n- two\n",
        }
    )

    result = await handler.handle(
        {
            "action": "wait",
            "guild_id": "g1",
            "channel_id": "cw",
            "timeout_seconds": 5,
        }
    )

    assert result.is_ok
    assert result.value.meta["action"] == "wait"
    assert "https://example.com/pr/123" in result.value.content[0].text


@pytest.mark.asyncio
async def test_message_without_default_repo_returns_error(handler: ChannelWorkflowHandler) -> None:
    result = await handler.handle(
        {
            "action": "message",
            "guild_id": "g1",
            "channel_id": "missing",
            "user_id": "u1",
            "message": "work on feature x",
        }
    )

    assert result.is_err
    assert "default repo" in str(result.error)


@pytest.mark.asyncio
async def test_status_reports_default_repo_and_queue(handler: ChannelWorkflowHandler) -> None:
    await handler.handle(
        {"action": "set_repo", "guild_id": "g1", "channel_id": "c3", "repo": "/repo/demo"}
    )
    await handler.handle(
        {
            "action": "message",
            "guild_id": "g1",
            "channel_id": "c3",
            "user_id": "u1",
            "message": "work on feature x",
        }
    )
    await handler.handle(
        {
            "action": "message",
            "guild_id": "g1",
            "channel_id": "c3",
            "user_id": "u2",
            "message": "work on feature y",
        }
    )

    result = await handler.handle({"action": "status", "guild_id": "g1", "channel_id": "c3"})

    assert result.is_ok
    text = result.value.content[0].text
    assert "Default repo: /repo/demo" in text
    assert "Queued workflows: 1" in text


@pytest.mark.asyncio
async def test_poll_advances_queue_after_completion(handler: ChannelWorkflowHandler) -> None:
    await handler.handle(
        {"action": "set_repo", "guild_id": "g1", "channel_id": "c4", "repo": "/repo/demo"}
    )
    await handler.handle(
        {
            "action": "message",
            "guild_id": "g1",
            "channel_id": "c4",
            "user_id": "u1",
            "message": "goal: Demo\nacceptance_criteria:\n- one\nconstraints:\n- two\n",
        }
    )
    queued = await handler.handle(
        {
            "action": "message",
            "guild_id": "g1",
            "channel_id": "c4",
            "user_id": "u2",
            "message": "work on feature y",
        }
    )
    assert queued.is_ok
    assert queued.value.meta["stage"] == "queued"

    poll = await handler.handle({"action": "poll", "guild_id": "g1", "channel_id": "c4"})

    assert poll.is_ok
    assert poll.value.meta["next_workflow_started"] is True
    assert "Started next queued workflow" in poll.value.content[0].text
    status = await handler.handle({"action": "status", "guild_id": "g1", "channel_id": "c4"})
    assert status.is_ok
    text = status.value.content[0].text
    assert "Queued workflows: 0" in text
    assert "Active workflow:" in text
    assert "interviewing" in text


@pytest.mark.asyncio
async def test_interview_completion_surfaces_seed_generation_failure(
    handler: ChannelWorkflowHandler,
) -> None:
    handler._fake_generate.should_fail = True  # type: ignore[attr-defined]
    await handler.handle(
        {"action": "set_repo", "guild_id": "g1", "channel_id": "c5", "repo": "/repo/demo"}
    )
    await handler.handle(
        {
            "action": "message",
            "guild_id": "g1",
            "channel_id": "c5",
            "user_id": "u1",
            "message": "work on feature x",
        }
    )

    result = await handler.handle(
        {
            "action": "message",
            "guild_id": "g1",
            "channel_id": "c5",
            "user_id": "u1",
            "message": "done",
            "mode": "answer",
        }
    )

    assert result.is_err
    status = await handler.handle({"action": "status", "guild_id": "g1", "channel_id": "c5"})
    assert status.is_ok
    assert "failed" in status.value.content[0].text.lower()


@pytest.mark.asyncio
async def test_seed_execution_start_failure_is_recorded(handler: ChannelWorkflowHandler) -> None:
    handler._fake_execute.should_fail = True  # type: ignore[attr-defined]
    await handler.handle(
        {"action": "set_repo", "guild_id": "g1", "channel_id": "c6", "repo": "/repo/demo"}
    )

    result = await handler.handle(
        {
            "action": "message",
            "guild_id": "g1",
            "channel_id": "c6",
            "user_id": "u1",
            "message": "goal: Demo\nacceptance_criteria:\n- one\nconstraints:\n- two\n",
        }
    )

    assert result.is_err
    status = await handler.handle({"action": "status", "guild_id": "g1", "channel_id": "c6"})
    assert status.is_ok
    assert "failed" in status.value.content[0].text.lower()


@pytest.mark.asyncio
async def test_poll_running_job_returns_status_without_completion(
    handler: ChannelWorkflowHandler,
) -> None:
    handler._fake_job_status.status = "running"  # type: ignore[attr-defined]
    handler._fake_job_status.text = "Job status: running"  # type: ignore[attr-defined]
    await handler.handle(
        {"action": "set_repo", "guild_id": "g1", "channel_id": "c7", "repo": "/repo/demo"}
    )
    await handler.handle(
        {
            "action": "message",
            "guild_id": "g1",
            "channel_id": "c7",
            "user_id": "u1",
            "message": "goal: Demo\nacceptance_criteria:\n- one\nconstraints:\n- two\n",
        }
    )

    result = await handler.handle({"action": "poll", "guild_id": "g1", "channel_id": "c7"})

    assert result.is_ok
    assert result.value.meta["job_status"] == "running"
    assert "Job status: running" in result.value.content[0].text


@pytest.mark.asyncio
async def test_wait_running_job_returns_status_without_completion(
    handler: ChannelWorkflowHandler,
) -> None:
    handler._fake_job_wait.status = "running"  # type: ignore[attr-defined]
    handler._fake_job_wait.text = "Job wait: running"  # type: ignore[attr-defined]
    handler._fake_job_wait.cursor = 3  # type: ignore[attr-defined]
    handler._fake_job_wait.changed = False  # type: ignore[attr-defined]
    await handler.handle(
        {"action": "set_repo", "guild_id": "g1", "channel_id": "c7w", "repo": "/repo/demo"}
    )
    await handler.handle(
        {
            "action": "message",
            "guild_id": "g1",
            "channel_id": "c7w",
            "user_id": "u1",
            "message": "goal: Demo\nacceptance_criteria:\n- one\nconstraints:\n- two\n",
        }
    )

    result = await handler.handle(
        {"action": "wait", "guild_id": "g1", "channel_id": "c7w", "timeout_seconds": 5}
    )

    assert result.is_ok
    assert result.value.meta["job_status"] == "running"
    assert result.value.meta["cursor"] == 3
    assert result.value.meta["changed"] is False
    assert "Job wait: running" in result.value.content[0].text


@pytest.mark.asyncio
async def test_poll_failed_job_reports_failure_and_starts_next_queued(
    handler: ChannelWorkflowHandler,
) -> None:
    handler._fake_job_status.status = "failed"  # type: ignore[attr-defined]
    handler._fake_job_status.text = "Job status: failed"  # type: ignore[attr-defined]
    await handler.handle(
        {"action": "set_repo", "guild_id": "g1", "channel_id": "c8", "repo": "/repo/demo"}
    )
    await handler.handle(
        {
            "action": "message",
            "guild_id": "g1",
            "channel_id": "c8",
            "user_id": "u1",
            "message": "goal: Demo\nacceptance_criteria:\n- one\nconstraints:\n- two\n",
        }
    )
    await handler.handle(
        {
            "action": "message",
            "guild_id": "g1",
            "channel_id": "c8",
            "user_id": "u2",
            "message": "work on feature y",
        }
    )

    result = await handler.handle({"action": "poll", "guild_id": "g1", "channel_id": "c8"})

    assert result.is_ok
    assert result.value.meta["next_workflow_started"] is True
    assert "failed" in result.value.content[0].text.lower()
