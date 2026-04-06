"""Runtime helpers for channel workflow orchestration."""

from __future__ import annotations

from dataclasses import dataclass

from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.tools.authoring_handlers import GenerateSeedHandler, InterviewHandler
from ouroboros.mcp.tools.execution_handlers import StartExecuteSeedHandler
from ouroboros.mcp.tools.job_handlers import JobResultHandler
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.openclaw.responses import build_channel_workflow_meta, extract_seed_yaml
from ouroboros.openclaw.workflow import (
    ChannelRef,
    ChannelWorkflowManager,
    WorkflowEntryPoint,
    extract_first_url,
    render_result_message,
)


@dataclass
class ChannelWorkflowRuntime:
    """Encapsulate interview/seed/execute terminal flow logic."""

    workflow_manager: ChannelWorkflowManager
    interview_handler: InterviewHandler
    generate_seed_handler: GenerateSeedHandler
    start_execute_seed_handler: StartExecuteSeedHandler
    job_result_handler: JobResultHandler

    async def launch_workflow(self, record) -> Result[MCPToolResult, MCPServerError]:
        """Launch either the interview path or direct execution path."""
        if record.entry_point == WorkflowEntryPoint.INTERVIEW:
            result = await self.interview_handler.handle(
                {"initial_context": record.request_message, "cwd": record.repo}
            )
            if result.is_err:
                self.workflow_manager.mark_failed(record.workflow_id, error=str(result.error))
                return Result.err(result.error)
            session_id = result.value.meta.get("session_id")
            if isinstance(session_id, str) and session_id:
                record = self.workflow_manager.set_interview_session(record.workflow_id, session_id)
            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text=result.value.content[0].text),),
                    is_error=False,
                    meta=build_channel_workflow_meta(
                        action="message",
                        workflow_id=record.workflow_id,
                        stage=record.stage,
                        entry_point=record.entry_point,
                        session_id=session_id,
                        repo=record.repo,
                    ),
                )
            )

        execute_arguments: dict[str, str] = {"cwd": record.repo}
        if record.seed_content:
            execute_arguments["seed_content"] = record.seed_content
        elif record.seed_path:
            execute_arguments["seed_path"] = record.seed_path
        else:
            execute_arguments["seed_content"] = record.request_message

        execute_result = await self.start_execute_seed_handler.handle(execute_arguments)
        if execute_result.is_err:
            self.workflow_manager.mark_failed(record.workflow_id, error=str(execute_result.error))
            return Result.err(execute_result.error)
        meta = execute_result.value.meta
        record = self.workflow_manager.set_executing(
            record.workflow_id,
            job_id=meta.get("job_id"),
            session_id=meta.get("session_id"),
            execution_id=meta.get("execution_id"),
        )
        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(type=ContentType.TEXT, text=execute_result.value.content[0].text),
                ),
                is_error=False,
                meta=build_channel_workflow_meta(
                    action="message",
                    workflow_id=record.workflow_id,
                    stage=record.stage,
                    entry_point=record.entry_point,
                    repo=record.repo,
                    job_id=meta.get("job_id"),
                    session_id=meta.get("session_id"),
                    execution_id=meta.get("execution_id"),
                ),
            )
        )

    async def maybe_launch_next_workflow(
        self,
        channel: ChannelRef,
        previous_workflow_id: str,
    ) -> Result[MCPToolResult, MCPServerError] | None:
        """Launch the next queued workflow when one becomes active."""
        next_record = self.workflow_manager.active_for_channel(channel)
        if next_record is None or next_record.workflow_id == previous_workflow_id:
            return None

        if next_record.entry_point == WorkflowEntryPoint.INTERVIEW and next_record.interview_session_id:
            return None
        if next_record.entry_point == WorkflowEntryPoint.EXECUTION and next_record.job_id:
            return None

        return await self.launch_workflow(next_record)

    async def finalize_terminal_status(
        self,
        *,
        channel: ChannelRef,
        active,
        status: str,
        fallback_text: str,
        action: str,
        cursor: int,
    ) -> Result[MCPToolResult, MCPServerError]:
        """Finalize a completed/failed execution and maybe start the next queued workflow."""
        result_text = fallback_text
        if status == "completed":
            result_result = await self.job_result_handler.handle({"job_id": active.job_id})
            if result_result.is_ok:
                result_text = result_result.value.content[0].text
            pr_url = extract_first_url(result_text)
            completed = self.workflow_manager.mark_completed(
                active.workflow_id,
                pr_url=pr_url,
                final_result=result_text,
            )
            next_result = await self.maybe_launch_next_workflow(channel, completed.workflow_id)
            if next_result is not None and next_result.is_ok:
                next_meta = dict(next_result.value.meta)
                next_meta.update(
                    {
                        "action": action,
                        "next_workflow_started": True,
                        "previous_workflow_id": completed.workflow_id,
                        "previous_stage": completed.stage,
                        "pr_url": pr_url,
                        "cursor": cursor,
                    }
                )
                return Result.ok(
                    MCPToolResult(
                        content=(
                            MCPContentItem(
                                type=ContentType.TEXT,
                                text=(
                                    f"{render_result_message(completed)}\n\n"
                                    "Started next queued workflow:\n\n"
                                    f"{next_result.value.content[0].text}"
                                ),
                            ),
                        ),
                        is_error=False,
                        meta=next_meta,
                    )
                )
            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text=render_result_message(completed)),),
                    is_error=False,
                    meta=build_channel_workflow_meta(
                        action=action,
                        channel_key=channel.key,
                        workflow_id=completed.workflow_id,
                        stage=completed.stage,
                        pr_url=pr_url,
                        cursor=cursor,
                    ),
                )
            )

        failed = self.workflow_manager.mark_failed(active.workflow_id, error=result_text)
        next_result = await self.maybe_launch_next_workflow(channel, failed.workflow_id)
        if next_result is not None and next_result.is_ok:
            next_meta = dict(next_result.value.meta)
            next_meta.update(
                {
                    "action": action,
                    "next_workflow_started": True,
                    "previous_workflow_id": failed.workflow_id,
                    "previous_stage": failed.stage,
                    "cursor": cursor,
                }
            )
            return Result.ok(
                MCPToolResult(
                    content=(
                        MCPContentItem(
                            type=ContentType.TEXT,
                            text=(
                                f"{render_result_message(failed)}\n\n"
                                "Started next queued workflow:\n\n"
                                f"{next_result.value.content[0].text}"
                            ),
                        ),
                    ),
                    is_error=False,
                    meta=next_meta,
                )
            )
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=render_result_message(failed)),),
                is_error=False,
                meta=build_channel_workflow_meta(
                    action=action,
                    channel_key=channel.key,
                    workflow_id=failed.workflow_id,
                    stage=failed.stage,
                    cursor=cursor,
                ),
            )
        )

    async def resume_interview(self, record, answer: str) -> Result[MCPToolResult, MCPServerError]:
        """Resume an interview and, if complete, hand off to seed/execution."""
        result = await self.interview_handler.handle(
            {"session_id": record.interview_session_id, "answer": answer}
        )
        if result.is_err:
            self.workflow_manager.mark_failed(record.workflow_id, error=str(result.error))
            return Result.err(result.error)

        meta = result.value.meta
        if meta.get("completed"):
            seed_result = await self.generate_seed_handler.handle(
                {
                    "session_id": record.interview_session_id,
                    "ambiguity_score": meta.get("ambiguity_score"),
                }
            )
            if seed_result.is_err:
                self.workflow_manager.mark_failed(record.workflow_id, error=str(seed_result.error))
                return Result.err(seed_result.error)

            seed_text = seed_result.value.content[0].text
            try:
                seed_yaml = extract_seed_yaml(seed_text)
            except ValueError as exc:
                self.workflow_manager.mark_failed(record.workflow_id, error=str(exc))
                return Result.err(
                    MCPToolError(str(exc), tool_name="ouroboros_channel_workflow")
                )
            self.workflow_manager.set_seed(
                record.workflow_id,
                seed_id=seed_result.value.meta.get("seed_id"),
                seed_content=seed_yaml,
            )
            execute_result = await self.start_execute_seed_handler.handle(
                {
                    "seed_content": seed_yaml,
                    "cwd": record.repo,
                }
            )
            if execute_result.is_err:
                self.workflow_manager.mark_failed(record.workflow_id, error=str(execute_result.error))
                return Result.err(execute_result.error)
            execute_meta = execute_result.value.meta
            executing = self.workflow_manager.set_executing(
                record.workflow_id,
                job_id=execute_meta.get("job_id"),
                session_id=execute_meta.get("session_id"),
                execution_id=execute_meta.get("execution_id"),
            )
            combined_text = f"{seed_text}\n\n{execute_result.value.content[0].text}"
            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text=combined_text),),
                    is_error=False,
                    meta=build_channel_workflow_meta(
                        action="message",
                        workflow_id=executing.workflow_id,
                        stage=executing.stage,
                        seed_id=seed_result.value.meta.get("seed_id"),
                        job_id=execute_meta.get("job_id"),
                        session_id=execute_meta.get("session_id"),
                        execution_id=execute_meta.get("execution_id"),
                        repo=record.repo,
                    ),
                )
            )

        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=result.value.content[0].text),),
                is_error=False,
                meta=build_channel_workflow_meta(
                    action="message",
                    workflow_id=record.workflow_id,
                    stage=record.stage,
                    session_id=record.interview_session_id,
                    ambiguity_score=meta.get("ambiguity_score"),
                    seed_ready=meta.get("seed_ready"),
                ),
            )
        )
