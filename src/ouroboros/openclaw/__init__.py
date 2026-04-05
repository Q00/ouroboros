"""OpenClaw-facing channel workflow primitives.

These modules provide the stateful orchestration layer needed for
message-based runtimes such as OpenClaw/Discord to drive the existing
Ouroboros interview -> seed -> execution pipeline.
"""

from ouroboros.openclaw.adapter import (
    OpenClawAdapterResponse,
    OpenClawWorkflowAdapter,
)
from ouroboros.openclaw.bridge import (
    OpenClawTransport,
    OpenClawTransportBridge,
    event_from_payload,
)
from ouroboros.openclaw.contracts import (
    OpenClawChannelEvent,
    OpenClawWorkflowCommand,
)
from ouroboros.openclaw.orchestrator import (
    OpenClawReplySink,
    OpenClawWorkflowOrchestrator,
)
from ouroboros.openclaw.ux import ParsedChannelCommand, parse_channel_command
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
    "OpenClawAdapterResponse",
    "OpenClawChannelEvent",
    "OpenClawTransport",
    "OpenClawTransportBridge",
    "OpenClawReplySink",
    "OpenClawWorkflowAdapter",
    "OpenClawWorkflowCommand",
    "OpenClawWorkflowOrchestrator",
    "ParsedChannelCommand",
    "WorkflowEntryPoint",
    "WorkflowStage",
    "detect_entry_point",
    "event_from_payload",
    "parse_channel_command",
    "render_channel_summary",
    "render_result_message",
    "render_stage_message",
]
