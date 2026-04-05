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
from ouroboros.openclaw.workflow import (
    ChannelRef,
    ChannelRepoRegistry,
    ChannelWorkflowManager,
    ChannelWorkflowRequest,
    WorkflowEntryPoint,
    WorkflowStage,
    detect_entry_point,
    extract_first_url,
    render_channel_summary,
    render_result_message,
    render_stage_message,
)


def _extract_seed_yaml(text: str) -> str:
    marker = "--- Seed YAML ---"
    if marker not in text:
        raise ValueError("generate_seed response did not include inline seed YAML")
    return text.split(marker, 1)[1].strip()


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
            repo = arguments.get("repo")
            if not isinstance(repo, str) or not repo.strip():
                return Result.err(
                    MCPToolError(
                        "repo is required for action=set_repo", tool_name=self.definition.name
                    )
                )
            self._repo_registry.set(channel, repo.strip())
            return self._ok(
                f"Default repo for channel {channel.key} set to `{repo.strip()}`.",
                {
                    "channel_key": channel.key,
                    "repo": repo.strip(),
                    "action": action,
                },
            )

        if action == "status":
            return self._ok(
                render_channel_summary(channel, self._workflow_manager, self._repo_registry),
                {"channel_key": channel.key, "action": action},
            )

        if action == "poll":
            return await self._poll_channel(channel)

        if action == "wait":
            return await self._wait_channel(
                channel,
                timeout_seconds=int(arguments.get("timeout_seconds", 30)),
            )

        return await self._handle_message(channel, arguments, user_id)

    async def _poll_channel(
        self,
        channel: ChannelRef,
    ) -> Result[MCPToolResult, MCPServerError]:
        active = self._workflow_manager.active_for_channel(channel)
        if active is None:
            return self._ok(
                render_channel_summary(channel, self._workflow_manager, self._repo_registry),
                {"channel_key": channel.key, "action": "poll", "active": False},
            )

        if active.stage != WorkflowStage.EXECUTING or not active.job_id:
            return self._ok(
                render_stage_message(active),
                {
                    "channel_key": channel.key,
                    "action": "poll",
                    "workflow_id": active.workflow_id,
                    "stage": active.stage,
                },
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
                {
                    "channel_key": channel.key,
                    "action": "poll",
                    "workflow_id": active.workflow_id,
                    "stage": active.stage,
                    "job_status": status,
                    "cursor": cursor,
                },
            )

        return await self._finalize_terminal_status(
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
                {"channel_key": channel.key, "action": "wait", "active": False},
            )

        if active.stage != WorkflowStage.EXECUTING or not active.job_id:
            return self._ok(
                render_stage_message(active),
                {
                    "channel_key": channel.key,
                    "action": "wait",
                    "workflow_id": active.workflow_id,
                    "stage": active.stage,
                },
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
                {
                    "channel_key": channel.key,
                    "action": "wait",
                    "workflow_id": active.workflow_id,
                    "stage": active.stage,
                    "job_status": status,
                    "cursor": cursor,
                    "changed": changed,
                },
            )

        return await self._finalize_terminal_status(
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

        mode = str(arguments.get("mode", "auto"))
        active = self._workflow_manager.active_for_channel(channel)
        if (
            active is not None
            and active.stage == WorkflowStage.INTERVIEWING
            and active.interview_session_id
            and mode == "answer"
        ):
            return await self._resume_interview(active, message.strip())

        if active is not None:
            repo = arguments.get("repo") or self._repo_registry.get(channel) or active.repo
            detection = detect_entry_point(
                message.strip(),
                seed_content=arguments.get("seed_content"),
                seed_path=arguments.get("seed_path"),
            )
            queued = self._workflow_manager.enqueue(
                ChannelWorkflowRequest(
                    channel=channel,
                    user_id=user_id,
                    message=message.strip(),
                    repo=str(repo),
                    seed_content=arguments.get("seed_content"),
                    seed_path=arguments.get("seed_path"),
                    entry_point=detection.entry_point,
                )
            )
            return self._ok(
                render_stage_message(queued),
                {
                    "channel_key": channel.key,
                    "workflow_id": queued.workflow_id,
                    "stage": queued.stage,
                    "entry_point": queued.entry_point,
                    "reason": detection.reason,
                },
            )

        repo_value = arguments.get("repo") or self._repo_registry.get(channel)
        if not isinstance(repo_value, str) or not repo_value.strip():
            return Result.err(
                MCPToolError(
                    "No repo provided and no default repo configured for this channel",
                    tool_name=self.definition.name,
                )
            )

        detection = detect_entry_point(
            message.strip(),
            seed_content=arguments.get("seed_content"),
            seed_path=arguments.get("seed_path"),
        )
        record = self._workflow_manager.enqueue(
            ChannelWorkflowRequest(
                channel=channel,
                user_id=user_id,
                message=message.strip(),
                repo=repo_value.strip(),
                seed_content=arguments.get("seed_content"),
                seed_path=arguments.get("seed_path"),
                entry_point=detection.entry_point,
            )
        )
        return await self._launch_workflow(record)

    async def _launch_workflow(
        self,
        record,
    ) -> Result[MCPToolResult, MCPServerError]:
        if record.entry_point == WorkflowEntryPoint.INTERVIEW:
            result = await self._interview_handler.handle(
                {"initial_context": record.request_message, "cwd": record.repo}
            )
            if result.is_err:
                self._workflow_manager.mark_failed(record.workflow_id, error=str(result.error))
                return Result.err(result.error)
            session_id = result.value.meta.get("session_id")
            if isinstance(session_id, str) and session_id:
                record = self._workflow_manager.set_interview_session(
                    record.workflow_id, session_id
                )
            return self._ok(
                result.value.content[0].text,
                {
                    "workflow_id": record.workflow_id,
                    "stage": record.stage,
                    "entry_point": record.entry_point,
                    "session_id": session_id,
                },
            )

        seed_content = record.seed_content or record.request_message
        execute_result = await self._start_execute_seed_handler.handle(
            {
                "seed_content": seed_content,
                "seed_path": record.seed_path,
                "cwd": record.repo,
            }
        )
        if execute_result.is_err:
            self._workflow_manager.mark_failed(record.workflow_id, error=str(execute_result.error))
            return Result.err(execute_result.error)
        meta = execute_result.value.meta
        record = self._workflow_manager.set_executing(
            record.workflow_id,
            job_id=meta.get("job_id"),
            session_id=meta.get("session_id"),
            execution_id=meta.get("execution_id"),
        )
        return self._ok(
            execute_result.value.content[0].text,
            {
                "workflow_id": record.workflow_id,
                "stage": record.stage,
                "entry_point": record.entry_point,
                "job_id": meta.get("job_id"),
                "session_id": meta.get("session_id"),
                "execution_id": meta.get("execution_id"),
            },
        )

    async def _maybe_launch_next_workflow(
        self,
        channel: ChannelRef,
        previous_workflow_id: str,
    ) -> Result[MCPToolResult, MCPServerError] | None:
        next_record = self._workflow_manager.active_for_channel(channel)
        if next_record is None or next_record.workflow_id == previous_workflow_id:
            return None

        if next_record.entry_point == WorkflowEntryPoint.INTERVIEW and next_record.interview_session_id:
            return None
        if next_record.entry_point == WorkflowEntryPoint.EXECUTION and next_record.job_id:
            return None

        return await self._launch_workflow(next_record)

    async def _finalize_terminal_status(
        self,
        *,
        channel: ChannelRef,
        active,
        status: str,
        fallback_text: str,
        action: str,
        cursor: int,
    ) -> Result[MCPToolResult, MCPServerError]:
        result_text = fallback_text
        if status == "completed":
            result_result = await self._job_result_handler.handle({"job_id": active.job_id})
            if result_result.is_ok:
                result_text = result_result.value.content[0].text
            pr_url = extract_first_url(result_text)
            completed = self._workflow_manager.mark_completed(
                active.workflow_id,
                pr_url=pr_url,
                final_result=result_text,
            )
            next_result = await self._maybe_launch_next_workflow(channel, completed.workflow_id)
            if next_result is not None and next_result.is_ok:
                return self._ok(
                    (
                        f"{render_result_message(completed)}\n\n"
                        "Started next queued workflow:\n\n"
                        f"{next_result.value.content[0].text}"
                    ),
                    {
                        "channel_key": channel.key,
                        "action": action,
                        "workflow_id": completed.workflow_id,
                        "stage": completed.stage,
                        "pr_url": pr_url,
                        "next_workflow_started": True,
                        "cursor": cursor,
                    },
                )
            return self._ok(
                render_result_message(completed),
                {
                    "channel_key": channel.key,
                    "action": action,
                    "workflow_id": completed.workflow_id,
                    "stage": completed.stage,
                    "pr_url": pr_url,
                    "cursor": cursor,
                },
            )

        failed = self._workflow_manager.mark_failed(active.workflow_id, error=result_text)
        next_result = await self._maybe_launch_next_workflow(channel, failed.workflow_id)
        if next_result is not None and next_result.is_ok:
            return self._ok(
                (
                    f"{render_result_message(failed)}\n\n"
                    "Started next queued workflow:\n\n"
                    f"{next_result.value.content[0].text}"
                ),
                {
                    "channel_key": channel.key,
                    "action": action,
                    "workflow_id": failed.workflow_id,
                    "stage": failed.stage,
                    "next_workflow_started": True,
                    "cursor": cursor,
                },
            )
        return self._ok(
            render_result_message(failed),
            {
                "channel_key": channel.key,
                "action": action,
                "workflow_id": failed.workflow_id,
                "stage": failed.stage,
                "cursor": cursor,
            },
        )

    async def _resume_interview(
        self,
        record,
        answer: str,
    ) -> Result[MCPToolResult, MCPServerError]:
        result = await self._interview_handler.handle(
            {"session_id": record.interview_session_id, "answer": answer}
        )
        if result.is_err:
            self._workflow_manager.mark_failed(record.workflow_id, error=str(result.error))
            return Result.err(result.error)

        meta = result.value.meta
        if meta.get("completed"):
            seed_result = await self._generate_seed_handler.handle(
                {
                    "session_id": record.interview_session_id,
                    "ambiguity_score": meta.get("ambiguity_score"),
                }
            )
            if seed_result.is_err:
                self._workflow_manager.mark_failed(record.workflow_id, error=str(seed_result.error))
                return Result.err(seed_result.error)

            seed_text = seed_result.value.content[0].text
            seed_yaml = _extract_seed_yaml(seed_text)
            self._workflow_manager.set_seed(
                record.workflow_id,
                seed_id=seed_result.value.meta.get("seed_id"),
                seed_content=seed_yaml,
            )
            execute_result = await self._start_execute_seed_handler.handle(
                {
                    "seed_content": seed_yaml,
                    "cwd": record.repo,
                }
            )
            if execute_result.is_err:
                self._workflow_manager.mark_failed(
                    record.workflow_id, error=str(execute_result.error)
                )
                return Result.err(execute_result.error)
            execute_meta = execute_result.value.meta
            executing = self._workflow_manager.set_executing(
                record.workflow_id,
                job_id=execute_meta.get("job_id"),
                session_id=execute_meta.get("session_id"),
                execution_id=execute_meta.get("execution_id"),
            )
            combined_text = (
                f"{seed_text}\n\n"
                f"{render_stage_message(executing)}\n\n"
                f"{execute_result.value.content[0].text}"
            )
            return self._ok(
                combined_text,
                {
                    "workflow_id": executing.workflow_id,
                    "stage": executing.stage,
                    "seed_id": seed_result.value.meta.get("seed_id"),
                    "job_id": execute_meta.get("job_id"),
                    "session_id": execute_meta.get("session_id"),
                    "execution_id": execute_meta.get("execution_id"),
                },
            )

        return self._ok(
            result.value.content[0].text,
            {
                "workflow_id": record.workflow_id,
                "stage": record.stage,
                "session_id": record.interview_session_id,
                "ambiguity_score": meta.get("ambiguity_score"),
                "seed_ready": meta.get("seed_ready"),
            },
        )

    @staticmethod
    def _ok(text: str, meta: dict[str, Any]) -> Result[MCPToolResult, MCPServerError]:
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=text),),
                is_error=False,
                meta=meta,
            )
        )
