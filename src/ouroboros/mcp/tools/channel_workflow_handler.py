"""MCP handler for channel-native OpenClaw workflow orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.tools.authoring_handlers import GenerateSeedHandler, InterviewHandler
from ouroboros.mcp.tools.execution_handlers import StartExecuteSeedHandler
from ouroboros.mcp.tools.job_handlers import JobResultHandler, JobStatusHandler, JobWaitHandler
from ouroboros.mcp.types import (
    ContentType,
    MCPContentItem,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
)
from ouroboros.openclaw.responses import build_channel_workflow_meta
from ouroboros.openclaw.runtime import ChannelWorkflowRuntime
from ouroboros.openclaw.workflow import (
    ChannelRef,
    ChannelRepoRegistry,
    ChannelWorkflowManager,
    ChannelWorkflowRequest,
    WorkflowStage,
    detect_entry_point,
    render_channel_summary,
    render_stage_message,
)


@dataclass
class ChannelWorkflowHandler:
    """Handle OpenClaw/Discord channel workflow orchestration."""

    workflow_manager: ChannelWorkflowManager | None = field(default=None, repr=False)
    repo_registry: ChannelRepoRegistry | None = field(default=None, repr=False)
    interview_handler: InterviewHandler | None = field(default=None, repr=False)
    generate_seed_handler: GenerateSeedHandler | None = field(default=None, repr=False)
    start_execute_seed_handler: StartExecuteSeedHandler | None = field(default=None, repr=False)
    job_status_handler: JobStatusHandler | None = field(default=None, repr=False)
    job_wait_handler: JobWaitHandler | None = field(default=None, repr=False)
    job_result_handler: JobResultHandler | None = field(default=None, repr=False)
    default_repo: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self._workflow_manager = self.workflow_manager or ChannelWorkflowManager()
        self._repo_registry = self.repo_registry or ChannelRepoRegistry()
        self._interview_handler = self.interview_handler or InterviewHandler()
        self._generate_seed_handler = self.generate_seed_handler or GenerateSeedHandler()
        self._start_execute_seed_handler = (
            self.start_execute_seed_handler or StartExecuteSeedHandler()
        )
        self._job_status_handler = self.job_status_handler or JobStatusHandler()
        self._job_wait_handler = self.job_wait_handler or JobWaitHandler()
        self._job_result_handler = self.job_result_handler or JobResultHandler()
        self._runtime = ChannelWorkflowRuntime(
            workflow_manager=self._workflow_manager,
            interview_handler=self._interview_handler,
            generate_seed_handler=self._generate_seed_handler,
            start_execute_seed_handler=self._start_execute_seed_handler,
            job_result_handler=self._job_result_handler,
        )

    @staticmethod
    def _meta(**kwargs: Any) -> dict[str, Any]:
        return build_channel_workflow_meta(**kwargs)

    @property
    def definition(self) -> MCPToolDefinition:
        return MCPToolDefinition(
            name="ouroboros_channel_workflow",
            description=(
                "Drive the Ouroboros workflow from a messaging channel such as "
                "OpenClaw/Discord. Supports per-channel queueing, default repo routing, "
                "input-detected stage entry, in-channel interview bridging, and "
                "execution status/result reporting."
            ),
            parameters=(
                MCPToolParameter(
                    name="channel_id",
                    type=ToolInputType.STRING,
                    description="Originating channel identifier",
                    required=True,
                ),
                MCPToolParameter(
                    name="guild_id",
                    type=ToolInputType.STRING,
                    description="Optional guild/server identifier",
                    required=False,
                ),
                MCPToolParameter(
                    name="user_id",
                    type=ToolInputType.STRING,
                    description="Optional user identifier of the caller",
                    required=False,
                ),
                MCPToolParameter(
                    name="message",
                    type=ToolInputType.STRING,
                    description="Channel message content or interview answer",
                    required=False,
                ),
                MCPToolParameter(
                    name="repo",
                    type=ToolInputType.STRING,
                    description="Optional explicit repo/path override",
                    required=False,
                ),
                MCPToolParameter(
                    name="seed_content",
                    type=ToolInputType.STRING,
                    description="Optional inline seed/spec payload to execute directly",
                    required=False,
                ),
                MCPToolParameter(
                    name="seed_path",
                    type=ToolInputType.STRING,
                    description="Optional seed path to execute directly",
                    required=False,
                ),
                MCPToolParameter(
                    name="action",
                    type=ToolInputType.STRING,
                    description="One of: message, set_repo, status, poll, wait",
                    required=False,
                    default="message",
                ),
                MCPToolParameter(
                    name="mode",
                    type=ToolInputType.STRING,
                    description="For action=message: auto, new, or answer",
                    required=False,
                    default="auto",
                ),
                MCPToolParameter(
                    name="timeout_seconds",
                    type=ToolInputType.INTEGER,
                    description="For action=wait: maximum seconds to wait for a workflow update",
                    required=False,
                    default=30,
                ),
                MCPToolParameter(
                    name="message_id",
                    type=ToolInputType.STRING,
                    description="Optional transport message identifier for dedupe",
                    required=False,
                ),
                MCPToolParameter(
                    name="event_id",
                    type=ToolInputType.STRING,
                    description="Optional transport event identifier for dedupe",
                    required=False,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        channel_id = arguments.get("channel_id")
        if not channel_id:
            return Result.err(
                MCPToolError("channel_id is required", tool_name=self.definition.name)
            )

        channel = ChannelRef(
            channel_id=str(channel_id),
            guild_id=(
                str(arguments["guild_id"]) if arguments.get("guild_id") is not None else None
            ),
        )
        action = str(arguments.get("action", "message"))
        user_id = str(arguments["user_id"]) if arguments.get("user_id") is not None else None

        if action == "set_repo":
            return self._handle_set_repo(channel, arguments)
        if action == "status":
            return self._ok(
                render_channel_summary(channel, self._workflow_manager, self._repo_registry),
                self._meta(action=action, channel_key=channel.key),
            )
        if action == "poll":
            return await self._poll_channel(channel)
        if action == "wait":
            return await self._wait_channel(
                channel,
                timeout_seconds=int(arguments.get("timeout_seconds", 30)),
            )
        return await self._handle_message(channel, arguments, user_id)

    def _handle_set_repo(
        self,
        channel: ChannelRef,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        repo = arguments.get("repo")
        if not isinstance(repo, str) or not repo.strip():
            return Result.err(
                MCPToolError("repo is required for action=set_repo", tool_name=self.definition.name)
            )
        self._repo_registry.set(channel, repo.strip())
        return self._ok(
            f"Default repo for channel {channel.key} set to `{repo.strip()}`.",
            self._meta(
                action="set_repo",
                channel_key=channel.key,
                repo=repo.strip(),
            ),
        )

    async def _poll_channel(
        self,
        channel: ChannelRef,
    ) -> Result[MCPToolResult, MCPServerError]:
        active = self._workflow_manager.active_for_channel(channel)
        if active is None:
            return self._ok(
                render_channel_summary(channel, self._workflow_manager, self._repo_registry),
                self._meta(action="poll", channel_key=channel.key, active=False),
            )

        if active.stage != WorkflowStage.EXECUTING or not active.job_id:
            return self._ok(
                render_stage_message(active),
                self._meta(
                    action="poll",
                    channel_key=channel.key,
                    workflow_id=active.workflow_id,
                    stage=active.stage,
                ),
            )

        status_result = await self._job_status_handler.handle({"job_id": active.job_id})
        if status_result.is_err:
            return Result.err(status_result.error)

        status_meta = status_result.value.meta
        status = status_meta["status"]
        cursor = int(status_meta.get("cursor", active.last_job_cursor))
        self._workflow_manager.set_job_cursor(active.workflow_id, cursor)
        if status in {"running", "queued", "cancel_requested"}:
            return self._ok(
                status_result.value.content[0].text,
                self._meta(
                    action="poll",
                    channel_key=channel.key,
                    workflow_id=active.workflow_id,
                    stage=active.stage,
                    job_status=status,
                    cursor=cursor,
                ),
            )

        return await self._runtime.finalize_terminal_status(
            channel=channel,
            active=active,
            status=status,
            fallback_text=status_result.value.content[0].text,
            action="poll",
            cursor=cursor,
        )

    async def _wait_channel(
        self,
        channel: ChannelRef,
        *,
        timeout_seconds: int,
    ) -> Result[MCPToolResult, MCPServerError]:
        active = self._workflow_manager.active_for_channel(channel)
        if active is None:
            return self._ok(
                render_channel_summary(channel, self._workflow_manager, self._repo_registry),
                self._meta(action="wait", channel_key=channel.key, active=False),
            )

        if active.stage != WorkflowStage.EXECUTING or not active.job_id:
            return self._ok(
                render_stage_message(active),
                self._meta(
                    action="wait",
                    channel_key=channel.key,
                    workflow_id=active.workflow_id,
                    stage=active.stage,
                ),
            )

        wait_result = await self._job_wait_handler.handle(
            {
                "job_id": active.job_id,
                "cursor": active.last_job_cursor,
                "timeout_seconds": timeout_seconds,
            }
        )
        if wait_result.is_err:
            return Result.err(wait_result.error)

        wait_meta = wait_result.value.meta
        status = wait_meta["status"]
        cursor = int(wait_meta.get("cursor", active.last_job_cursor))
        changed = bool(wait_meta.get("changed", False))
        self._workflow_manager.set_job_cursor(active.workflow_id, cursor)

        if status in {"running", "queued", "cancel_requested"}:
            return self._ok(
                wait_result.value.content[0].text,
                self._meta(
                    action="wait",
                    channel_key=channel.key,
                    workflow_id=active.workflow_id,
                    stage=active.stage,
                    job_status=status,
                    cursor=cursor,
                    changed=changed,
                ),
            )

        return await self._runtime.finalize_terminal_status(
            channel=channel,
            active=active,
            status=status,
            fallback_text=wait_result.value.content[0].text,
            action="wait",
            cursor=cursor,
        )

    async def _handle_message(
        self,
        channel: ChannelRef,
        arguments: dict[str, Any],
        user_id: str | None,
    ) -> Result[MCPToolResult, MCPServerError]:
        message = arguments.get("message")
        if not isinstance(message, str) or not message.strip():
            return Result.err(
                MCPToolError(
                    "message is required for action=message", tool_name=self.definition.name
                )
            )

        normalized_message = message.strip()
        mode = str(arguments.get("mode", "auto"))
        active = self._workflow_manager.active_for_channel(channel)
        repo = (
            arguments.get("repo")
            or self._repo_registry.get(channel)
            or (active.repo if active else None)
            or self.default_repo
        )
        if not isinstance(repo, str) or not repo.strip():
            return Result.err(
                MCPToolError(
                    "No repo provided and no default repo configured for this channel",
                    tool_name=self.definition.name,
                )
            )
        repo = repo.strip()

        message_id = (
            str(arguments["message_id"]) if arguments.get("message_id") is not None else None
        )
        event_id = str(arguments["event_id"]) if arguments.get("event_id") is not None else None
        raw_event_key = message_id or event_id
        event_key = f"{channel.key}:{raw_event_key}" if raw_event_key else None
        if event_key and self._workflow_manager.is_event_processed(event_key):
            label = active or self._workflow_manager.latest_for_channel(channel)
            if label is not None:
                return self._ok(
                    render_stage_message(label),
                    self._meta(
                        action="message",
                        channel_key=channel.key,
                        workflow_id=label.workflow_id,
                        stage=label.stage,
                        duplicate_delivery=True,
                        duplicate_of=label.workflow_id,
                    ),
                )

        detection = detect_entry_point(
            normalized_message,
            seed_content=arguments.get("seed_content"),
            seed_path=arguments.get("seed_path"),
        )
        duplicate = self._workflow_manager.find_inflight_duplicate(
            channel,
            user_id=user_id,
            message=normalized_message,
            repo=repo,
            entry_point=detection.entry_point,
            message_id=(
                str(arguments["message_id"]) if arguments.get("message_id") is not None else None
            ),
            event_id=(
                str(arguments["event_id"]) if arguments.get("event_id") is not None else None
            ),
        )
        if duplicate is not None:
            return self._ok(
                render_stage_message(duplicate),
                self._meta(
                    action="message",
                    channel_key=channel.key,
                    workflow_id=duplicate.workflow_id,
                    stage=duplicate.stage,
                    entry_point=duplicate.entry_point,
                    repo=duplicate.repo,
                    duplicate_delivery=True,
                    duplicate_of=duplicate.workflow_id,
                ),
            )

        if (
            active is not None
            and active.stage == WorkflowStage.INTERVIEWING
            and active.interview_session_id
            and mode != "new"
            and (active.user_id is None or user_id == active.user_id)
        ):
            result = await self._runtime.resume_interview(active, normalized_message)
            if result.is_ok and event_key:
                self._workflow_manager.mark_event_processed(event_key, active.workflow_id)
            return result

        if mode == "answer":
            return self._ok(
                "No active interview to answer. Start a new workflow first.",
                self._meta(
                    action="answer",
                    channel_key=channel.key,
                    stage="idle",
                    repo=repo,
                ),
                is_error=True,
            )

        record = self._workflow_manager.enqueue(
            ChannelWorkflowRequest(
                channel=channel,
                user_id=user_id,
                message=normalized_message,
                repo=repo,
                seed_content=arguments.get("seed_content"),
                seed_path=arguments.get("seed_path"),
                entry_point=detection.entry_point,
                message_id=(
                    str(arguments["message_id"])
                    if arguments.get("message_id") is not None
                    else None
                ),
                event_id=(
                    str(arguments["event_id"]) if arguments.get("event_id") is not None else None
                ),
            )
        )
        if event_key:
            self._workflow_manager.mark_event_processed(event_key, record.workflow_id)
        if active is not None:
            return self._ok(
                render_stage_message(record),
                self._meta(
                    action="message",
                    channel_key=channel.key,
                    workflow_id=record.workflow_id,
                    stage=record.stage,
                    entry_point=record.entry_point,
                    reason=detection.reason,
                    repo=record.repo,
                ),
            )
        return await self._runtime.launch_workflow(record)

    @staticmethod
    def _ok(
        text: str, meta: dict[str, Any], *, is_error: bool = False
    ) -> Result[MCPToolResult, MCPServerError]:
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=text),),
                is_error=is_error,
                meta=meta,
            )
        )
