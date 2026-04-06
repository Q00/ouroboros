"""OpenClaw-facing channel workflow primitives.

These modules provide the stateful orchestration layer needed for
message-based runtimes such as OpenClaw/Discord to drive the existing
Ouroboros interview -> seed -> execution pipeline.

OpenClaw natively supports MCP server registration and tool calls,
so the adapter/bridge/orchestrator/contracts/ux layers have been removed.
"""

from ouroboros.openclaw.workflow import (
    ChannelRef,
    ChannelRepoRegistry,
    ChannelWorkflowManager,
    ChannelWorkflowRecord,
    ChannelWorkflowRequest,
    EntryPointDetection,
    WorkflowEntryPoint,
    WorkflowStage,
    detect_entry_point,
    render_channel_summary,
    render_result_message,
    render_stage_message,
)

__all__ = [
    "ChannelRef",
    "ChannelRepoRegistry",
    "ChannelWorkflowManager",
    "ChannelWorkflowRecord",
    "ChannelWorkflowRequest",
    "EntryPointDetection",
    "WorkflowEntryPoint",
    "WorkflowStage",
    "detect_entry_point",
    "render_channel_summary",
    "render_result_message",
    "render_stage_message",
]
