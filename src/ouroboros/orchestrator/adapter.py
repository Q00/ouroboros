"""Claude Agent SDK adapter for Ouroboros orchestrator.

This module provides a wrapper around the Claude Agent SDK that:
- Normalizes SDK messages to internal AgentMessage format
- Handles streaming with async generators
- Maps SDK exceptions to Ouroboros error types
- Supports configurable tools and permission modes

Usage:
    adapter = ClaudeAgentAdapter(api_key="...")
    async for message in adapter.execute_task(
        prompt="Fix the bug in auth.py",
        tools=["Read", "Edit", "Bash"],
    ):
        print(message.content)
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
import math
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from ouroboros.core.errors import ProviderError
from ouroboros.core.retry import BASE_TRANSIENT_PATTERNS, is_transient_error
from ouroboros.core.types import Result
from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.backend_limits import resolve_backend_limits
from ouroboros.orchestrator.rate_limit import (
    DEFAULT_ANTHROPIC_RPM_CEILING,
    DEFAULT_ANTHROPIC_TPM_CEILING,
    RATE_LIMIT_HEARTBEAT_SECONDS,
    RATE_LIMIT_MAX_WAIT_SECONDS,
    RateLimitSnapshot,
    SharedRateLimitBucket,
    estimate_runtime_request_tokens,
)
from ouroboros.router.types import Resolved

if TYPE_CHECKING:
    from ouroboros.providers.base import CompletionConfig, CompletionResponse, Message

log = get_logger(__name__)


# =============================================================================
# Tool Detail Extraction
# =============================================================================

_TOOL_DETAIL_EXTRACTORS: dict[str, str] = {
    "Read": "file_path",
    "Glob": "pattern",
    "Grep": "pattern",
    "Edit": "file_path",
    "Write": "file_path",
    "Bash": "command",
    "WebFetch": "url",
    "WebSearch": "query",
    "NotebookEdit": "notebook_path",
}

_OPENCODE_PERSISTED_METADATA_KEYS = frozenset(
    {
        "ac_id",
        "ac_index",
        "attempt_number",
        "depth",
        "display_path",
        "execution_id",
        "identity_model",
        "legacy_node_id",
        "legacy_node_aliases",
        "legacy_parent_node_id",
        "legacy_parent_node_aliases",
        "legacy_session_scope_id",
        "legacy_session_scope_ids",
        "legacy_session_state_path",
        "legacy_session_state_paths",
        "level_number",
        "node_kind",
        "node_id",
        "ordinal",
        "parent_ac_index",
        "parent_node_id",
        "path",
        "recovery_discontinuity",
        "retry_attempt",
        "root_ac_index",
        "root_ac_number",
        "scope",
        "schema_version",
        "server_session_id",
        "session_attempt_id",
        "session_role",
        "session_scope_id",
        "session_state_path",
        "capability_graph",
        "control_plane",
        "sub_ac_index",
        "tool_catalog",
        "turn_id",
        "turn_number",
    }
)

_RUNTIME_TERMINAL_STATES = frozenset({"cancelled", "completed", "failed", "terminated"})
_RUNTIME_LIFECYCLE_STATE_BY_EVENT_TYPE = {
    "runtime.connected": "connecting",
    "runtime.ready": "ready",
    "session.bound": "ready",
    "session.created": "starting",
    "session.ready": "ready",
    "session.started": "running",
    "session.resumed": "running",
    "thread.started": "running",
    "result.completed": "running",
    "turn.completed": "running",
    "run.completed": "completed",
    "session.completed": "completed",
    "task.completed": "completed",
    "error": "failed",
    "run.failed": "failed",
    "session.failed": "failed",
    "task.failed": "failed",
}


@dataclass(frozen=True, slots=True)
class _RuntimeExecutionDispatch:
    """Execution dispatch state for a single runtime invocation."""

    backend: str
    runtime_handle: RuntimeHandle | None
    resume_session_id: str | None


@dataclass(frozen=True, slots=True)
class _RuntimeExecutionDispatchFailure:
    """Private execution-dispatch failure details for adapter logging."""

    public_message: str
    reason: str
    details: dict[str, Any] = field(default_factory=dict)


def _format_tool_detail(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Format a human-readable tool detail string.

    Args:
        tool_name: Name of the tool being called.
        tool_input: Raw input dict from ToolUseBlock.

    Returns:
        Formatted string like "Read: src/foo.py" or just "ToolName" if no detail.
    """
    key = _TOOL_DETAIL_EXTRACTORS.get(tool_name)
    if key:
        detail = str(tool_input.get(key, ""))
    elif tool_name.startswith("mcp__"):
        detail = next((str(v)[:80] for v in tool_input.values() if v), "")
    else:
        detail = ""
    if detail and len(detail) > 80:
        detail = detail[:77] + "..."
    return f"{tool_name}: {detail}" if detail else tool_name


_USAGE_TOKEN_KEYS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "total_tokens",
)


class _InvalidUsage:
    """Sentinel type preserving malformed usage across message normalization."""


_INVALID_USAGE = _InvalidUsage()


def _normalized_usage(obj: object) -> dict[str, int | float] | _InvalidUsage | None:
    """Extract an all-or-nothing dict of valid token counts from usage.

    Reads only the known token keys (:data:`_USAGE_TOKEN_KEYS`) from either a
    ``Mapping`` or an attribute-bearing object. If any *present* recognized key
    is non-numeric, boolean, negative, non-finite, or too large to convert to a
    finite float, the whole payload is rejected. Silently dropping one malformed
    component would undercount spend and can manufacture a false frugality PASS.
    Returns :data:`_INVALID_USAGE` for malformed recognized telemetry and
    ``None`` for an absent/empty measurement. Keeping those states distinct lets
    the leaf-level harvester invalidate the whole attempt even when a later
    message carries valid counters. Never raises.
    """
    if obj is None:
        return None
    if isinstance(obj, (str, bytes, bool, int, float, list, tuple, set)):
        return _INVALID_USAGE
    result: dict[str, int | float] = {}
    missing = object()
    for key in _USAGE_TOKEN_KEYS:
        if isinstance(obj, Mapping):
            if key not in obj:
                continue
            value = obj[key]
        else:
            try:
                value = getattr(obj, key, missing)
            except Exception:
                return _INVALID_USAGE
            if value is missing:
                continue
        if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
            return _INVALID_USAGE
        try:
            normalized = float(value)
        except (OverflowError, TypeError, ValueError):
            return _INVALID_USAGE
        if not math.isfinite(normalized) or normalized < 0:
            return _INVALID_USAGE
        result[key] = value
    return result or None


def _optional_str(value: object) -> str | None:
    """Return a string value when present, otherwise None."""
    return value if isinstance(value, str) and value else None


DELEGATED_EXECUTE_SEED_TOOL_NAMES: tuple[str, ...] = (
    "ouroboros_execute_seed",
    "ouroboros_start_execute_seed",
)
DELEGATED_EXECUTE_SEED_TOOL_MATCHER = (
    "mcp__plugin_ouroboros_ouroboros__ouroboros_execute_seed|"
    "mcp__plugin_ouroboros_ouroboros__ouroboros_start_execute_seed|"
    "mcp__ouroboros__ouroboros_execute_seed|"
    "mcp__ouroboros__ouroboros_start_execute_seed|"
    "ouroboros_execute_seed|"
    "ouroboros_start_execute_seed"
)

DELEGATED_PARENT_SESSION_ID_ARG = "_ooo_parent_claude_session_id"
DELEGATED_PARENT_TRANSCRIPT_PATH_ARG = "_ooo_parent_claude_transcript_path"
DELEGATED_PARENT_CWD_ARG = "_ooo_parent_claude_cwd"
DELEGATED_PARENT_PERMISSION_MODE_ARG = "_ooo_parent_claude_permission_mode"
DELEGATED_PARENT_EFFECTIVE_TOOLS_ARG = "_ooo_parent_effective_tools"


def _is_delegated_execute_seed_tool(tool_name: object) -> bool:
    """Return True for delegated execute-seed MCP tool calls."""
    if not isinstance(tool_name, str) or not tool_name:
        return False
    return any(
        tool_name == candidate or tool_name.endswith(f"__{candidate}")
        for candidate in DELEGATED_EXECUTE_SEED_TOOL_NAMES
    )


def _build_delegated_tool_context_update(
    hook_input: dict[str, Any],
    effective_tools: list[str],
) -> dict[str, Any] | None:
    """Inject parent Claude runtime metadata into delegated execute-seed tool input."""
    tool_name = hook_input.get("tool_name")
    if not _is_delegated_execute_seed_tool(tool_name):
        return None

    tool_input = hook_input.get("tool_input")
    if not isinstance(tool_input, dict):
        return None

    updated_input = dict(tool_input)
    updated_input[DELEGATED_PARENT_SESSION_ID_ARG] = hook_input.get("session_id")
    updated_input[DELEGATED_PARENT_TRANSCRIPT_PATH_ARG] = hook_input.get("transcript_path")
    updated_input[DELEGATED_PARENT_CWD_ARG] = hook_input.get("cwd")
    updated_input[DELEGATED_PARENT_PERMISSION_MODE_ARG] = hook_input.get("permission_mode")
    updated_input[DELEGATED_PARENT_EFFECTIVE_TOOLS_ARG] = list(effective_tools)
    return {
        "hookEventName": "PreToolUse",
        "updatedInput": updated_input,
    }


def _clone_runtime_handle_data(value: object) -> Any:
    """Clone persisted runtime payload data without retaining mutable aliases."""
    if isinstance(value, dict):
        return {key: _clone_runtime_handle_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clone_runtime_handle_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_clone_runtime_handle_data(item) for item in value)
    return value


# Keep this boundary map limited to canonical selectors and legacy spellings
# already exercised by current runtimes or persisted RuntimeHandle payloads.
_RUNTIME_HANDLE_BACKEND_ALIASES = {
    "claude": "claude",
    "claude_code": "claude",
    "claude_mcp": "claude_mcp",
    "codex": "codex_cli",
    "codex_cli": "codex_cli",
    "codex_mcp": "codex_mcp",
    "opencode": "opencode",
    "opencode_cli": "opencode",
    "hermes": "hermes_cli",
    "hermes_cli": "hermes_cli",
    "kiro": "kiro",
    "kiro_cli": "kiro",
    "copilot": "copilot_cli",
    "copilot_cli": "copilot_cli",
    "goose": "goose",
    "goose_cli": "goose",
    "pi": "pi",
    "pi_cli": "pi",
    "gjc": "gjc",
    "gjc_cli": "gjc",
    "gemini": "gemini_cli",
    "gemini_cli": "gemini_cli",
    "grok": "grok_cli",
    "grok_cli": "grok_cli",
    "antigravity": "antigravity_cli",
    "antigravity_cli": "antigravity_cli",
}


def _normalize_runtime_handle_selector(
    selector: object,
    *,
    field_name: str,
) -> str | None:
    """Normalize a boundary selector value onto the RuntimeHandle backend contract."""
    if selector is None:
        return None
    if not isinstance(selector, str):
        msg = f"RuntimeHandle {field_name} selector must be a string, got {type(selector).__name__}"
        raise ValueError(msg)

    normalized = selector.strip().lower()
    if not normalized:
        return None

    canonical = _RUNTIME_HANDLE_BACKEND_ALIASES.get(normalized)
    if canonical is None:
        msg = f"Unsupported RuntimeHandle {field_name} selector: {selector}"
        raise ValueError(msg)
    return canonical


def _resolve_runtime_handle_backend(
    *,
    backend: object,
    provider: object = None,
) -> str:
    """Resolve backend/provider boundary selectors to the canonical backend value."""
    normalized_backend = _normalize_runtime_handle_selector(backend, field_name="backend")
    normalized_provider = _normalize_runtime_handle_selector(provider, field_name="provider")

    if normalized_backend is None and normalized_provider is None:
        msg = "RuntimeHandle selector cannot be determined"
        raise ValueError(msg)
    if (
        normalized_backend is not None
        and normalized_provider is not None
        and normalized_backend != normalized_provider
    ):
        msg = "RuntimeHandle backend/provider conflict"
        raise ValueError(msg)

    # At least one is non-None (guarded above); `or` selects the non-None value.
    return normalized_backend or normalized_provider  # type: ignore[return-value]


def _runtime_handle_lifecycle_state(
    runtime_event_type: str | None,
    *,
    has_session_id: bool,
) -> str:
    """Map a runtime event type onto a stable lifecycle state label."""
    if runtime_event_type is None:
        return "running" if has_session_id else "initialized"

    normalized = runtime_event_type.strip().lower()
    if not normalized:
        return "running" if has_session_id else "initialized"

    direct_match = _RUNTIME_LIFECYCLE_STATE_BY_EVENT_TYPE.get(normalized)
    if direct_match is not None:
        return direct_match

    if "permission" in normalized or "approval" in normalized:
        return "awaiting_permission"
    if "cancelled" in normalized or "canceled" in normalized:
        return "cancelled"
    if "terminated" in normalized:
        return "terminated"
    if "failed" in normalized:
        return "failed"
    if "completed" in normalized and not normalized.startswith(("message.", "result.", "turn.")):
        return "completed"
    if any(
        token in normalized
        for token in ("connected", "created", "bound", "ready", "resumed", "started")
    ):
        return "running"
    return "running" if has_session_id else "initialized"


def runtime_handle_tool_catalog(
    runtime_handle: RuntimeHandle | None,
) -> dict[str, Any] | list[dict[str, Any]] | None:
    """Return a copy of the serialized startup tool catalog when present.

    Accepts both the legacy ``list`` format and the ``dict`` format produced
    by :func:`serialize_tool_catalog` when ``inherited_capabilities`` are
    present.
    """
    if runtime_handle is None:
        return None

    tool_catalog = runtime_handle.metadata.get("tool_catalog")
    if isinstance(tool_catalog, list):
        return list(tool_catalog)
    if isinstance(tool_catalog, dict):
        return dict(tool_catalog)
    return None


def runtime_handle_capability_graph(
    runtime_handle: RuntimeHandle | None,
) -> list[dict[str, Any]] | None:
    """Return a copy of the serialized capability graph when present."""
    if runtime_handle is None:
        return None

    capability_graph = runtime_handle.metadata.get("capability_graph")
    if not isinstance(capability_graph, list):
        return None
    return list(capability_graph)


def runtime_handle_control_plane(
    runtime_handle: RuntimeHandle | None,
) -> list[dict[str, Any]] | None:
    """Return a copy of serialized control-plane hints when present."""
    if runtime_handle is None:
        return None

    control_plane = runtime_handle.metadata.get("control_plane")
    if not isinstance(control_plane, list):
        return None
    return list(control_plane)


type RuntimeHandleObserver = Callable[["RuntimeHandle"], Awaitable[dict[str, Any]]]
type RuntimeHandleTerminator = Callable[["RuntimeHandle"], Awaitable[bool]]


# =============================================================================
# Data Models
# =============================================================================


@dataclass(frozen=True, slots=True)
class RuntimeHandle:
    """Backend-neutral resume handle for agent runtimes.

    Attributes:
        backend: Runtime backend identifier (for example, "claude" or "codex_cli").
        kind: Handle kind for future extensibility.
        native_session_id: Backend-native session identifier when available.
        conversation_id: Durable conversation/thread identifier when applicable.
        previous_response_id: Last response identifier for turn-chaining APIs.
        transcript_path: Optional transcript path for CLI-based runtimes.
        cwd: Working directory used for execution.
        approval_mode: Runtime approval/sandbox mode if available.
        updated_at: ISO timestamp when the handle was last updated.
        metadata: Backend-specific extension data.
    """

    backend: str
    kind: str = "agent_runtime"
    native_session_id: str | None = None
    conversation_id: str | None = None
    previous_response_id: str | None = None
    transcript_path: str | None = None
    cwd: str | None = None
    approval_mode: str | None = None
    updated_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    _observe_callback: RuntimeHandleObserver | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _terminate_callback: RuntimeHandleTerminator | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        """Normalize legacy backend aliases onto the canonical backend contract."""
        object.__setattr__(
            self,
            "backend",
            _resolve_runtime_handle_backend(backend=self.backend),
        )

    @property
    def server_session_id(self) -> str | None:
        """Return the server-side session identifier when present."""
        return _optional_str(self.metadata.get("server_session_id"))

    @property
    def ac_id(self) -> str | None:
        """Return the stable AC identity when present."""
        return _optional_str(self.metadata.get("ac_id"))

    @property
    def session_scope_id(self) -> str | None:
        """Return the stable AC-scoped session owner identifier when present."""
        return _optional_str(self.metadata.get("session_scope_id"))

    @property
    def session_attempt_id(self) -> str | None:
        """Return the per-attempt implementation-session identifier when present."""
        return _optional_str(self.metadata.get("session_attempt_id"))

    @property
    def resume_session_id(self) -> str | None:
        """Return the identifier the runtime should use to reconnect/resume."""
        if self.native_session_id:
            return self.native_session_id
        return self.server_session_id

    @property
    def control_session_id(self) -> str | None:
        """Return the preferred identifier for live runtime observation/control."""
        if self.server_session_id:
            return self.server_session_id
        return self.native_session_id

    @property
    def runtime_event_type(self) -> str | None:
        """Return the latest normalized runtime event type when present."""
        return _optional_str(self.metadata.get("runtime_event_type"))

    @property
    def lifecycle_state(self) -> str:
        """Return the current runtime lifecycle state inferred from handle state."""
        return _runtime_handle_lifecycle_state(
            self.runtime_event_type,
            has_session_id=self.control_session_id is not None
            or self.resume_session_id is not None,
        )

    @property
    def is_terminal(self) -> bool:
        """Return True when the handle reports a terminal lifecycle state."""
        return self.lifecycle_state in _RUNTIME_TERMINAL_STATES

    @property
    def can_resume(self) -> bool:
        """Return True when the handle carries enough data to reconnect."""
        return self.resume_session_id is not None

    @property
    def can_observe(self) -> bool:
        """Return True when the handle can describe or observe runtime state."""
        return (
            self._observe_callback is not None
            or self.control_session_id is not None
            or self.resume_session_id is not None
        )

    @property
    def can_terminate(self) -> bool:
        """Return True when the handle can actively terminate the live runtime."""
        return self._terminate_callback is not None and not self.is_terminal

    def bind_controls(
        self,
        *,
        observe_callback: RuntimeHandleObserver | None = None,
        terminate_callback: RuntimeHandleTerminator | None = None,
    ) -> RuntimeHandle:
        """Attach live observe/terminate callbacks without affecting persistence."""
        return replace(
            self,
            _observe_callback=observe_callback,
            _terminate_callback=terminate_callback,
        )

    def snapshot(self) -> dict[str, Any]:
        """Return a serializable snapshot of lifecycle and control state."""
        return {
            "backend": self.backend,
            "kind": self.kind,
            "native_session_id": self.native_session_id,
            "server_session_id": self.server_session_id,
            "resume_session_id": self.resume_session_id,
            "control_session_id": self.control_session_id,
            "cwd": self.cwd,
            "approval_mode": self.approval_mode,
            "updated_at": self.updated_at,
            "runtime_event_type": self.runtime_event_type,
            "lifecycle_state": self.lifecycle_state,
            "can_resume": self.can_resume,
            "can_observe": self.can_observe,
            "can_terminate": self.can_terminate,
            "metadata": dict(self.metadata),
        }

    async def observe(self) -> dict[str, Any]:
        """Return the latest observable runtime state for this handle."""
        if self._observe_callback is not None:
            return await self._observe_callback(self)
        return self.snapshot()

    async def terminate(self) -> bool:
        """Terminate the live runtime when a control callback is attached."""
        if not self.can_terminate or self._terminate_callback is None:
            return False
        return await self._terminate_callback(self)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the handle for progress persistence using the canonical backend key."""
        return {
            "backend": self.backend,
            "kind": self.kind,
            "native_session_id": self.native_session_id,
            "conversation_id": self.conversation_id,
            "previous_response_id": self.previous_response_id,
            "transcript_path": self.transcript_path,
            "cwd": self.cwd,
            "approval_mode": self.approval_mode,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
        }

    def to_persisted_dict(self) -> dict[str, Any]:
        """Serialize the handle for event/session persistence.

        OpenCode runtime sessions persist only the reconnectable session handle
        plus AC ownership metadata so stored events remain minimal and resume-safe.
        """
        if self.backend != "opencode":
            return self.to_dict()

        metadata = {
            key: value
            for key, value in self.metadata.items()
            if key in _OPENCODE_PERSISTED_METADATA_KEYS
        }
        return {
            "backend": self.backend,
            "kind": self.kind,
            "native_session_id": self.native_session_id,
            "cwd": self.cwd,
            "approval_mode": self.approval_mode,
            "metadata": metadata,
        }

    def to_session_state_dict(self) -> dict[str, Any]:
        """Serialize only the runtime state required to resume a session later.

        OpenCode sessions persist a smaller payload than other runtimes so the
        event-sourced session tracker keeps only reconnect identifiers plus the
        scope metadata needed to rebind the execution attempt on resume.
        """
        return self.to_persisted_dict()

    @classmethod
    def from_dict(cls, value: object) -> RuntimeHandle | None:
        """Deserialize a runtime handle from persisted progress data."""
        if not isinstance(value, dict):
            return None

        backend = _resolve_runtime_handle_backend(
            backend=value.get("backend"),
            provider=value.get("provider"),
        )

        metadata = value.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        else:
            metadata = _clone_runtime_handle_data(metadata)

        return cls(
            backend=backend,
            kind=str(value.get("kind", "agent_runtime")),
            native_session_id=_optional_str(value.get("native_session_id")),
            conversation_id=_optional_str(value.get("conversation_id")),
            previous_response_id=_optional_str(value.get("previous_response_id")),
            transcript_path=_optional_str(value.get("transcript_path")),
            cwd=_optional_str(value.get("cwd")),
            approval_mode=_optional_str(value.get("approval_mode")),
            updated_at=_optional_str(value.get("updated_at")),
            metadata=metadata,
        )


@dataclass(frozen=True, slots=True)
class AgentMessage:
    """Normalized message from Claude Agent SDK.

    Attributes:
        type: Message type ("assistant", "user", "tool", "result", "system").
        content: Human-readable content.
        tool_name: Name of tool being called (if type="tool").
        data: Additional message data.
        resume_handle: Backend-neutral runtime resume handle, if available.
    """

    type: str
    content: str
    tool_name: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    resume_handle: RuntimeHandle | None = None

    @property
    def is_final(self) -> bool:
        """Return True if this is the final result message."""
        return self.type == "result"

    @property
    def is_error(self) -> bool:
        """Return True if this message indicates an error."""
        return self.data.get("subtype") == "error"


type SkillDispatchHandler = Callable[
    [Resolved, RuntimeHandle | None],
    Awaitable[tuple[AgentMessage, ...] | None],
]


@dataclass(frozen=True, slots=True)
class TaskResult:
    """Result of executing a task via Claude Agent.

    Attributes:
        success: Whether the task completed successfully.
        final_message: The final result message content.
        messages: All messages from the execution.
        session_id: Claude Agent session ID for resumption.
        resume_handle: Backend-neutral resume handle for resumption.
    """

    success: bool
    final_message: str
    messages: tuple[AgentMessage, ...]
    session_id: str | None = None
    resume_handle: RuntimeHandle | None = None


class ParamSupport(StrEnum):
    """How a runtime honors a given execution parameter.

    Used for observability only: the orchestrator surfaces non-``NATIVE``
    handling so an operator can see when a parameter is not honored in the form
    they supplied it. It does **not** change what is passed to the runtime.

    Values:
        NATIVE: The runtime honors the parameter directly (e.g. a separate
            system-prompt field, a real tool allow-list, a permission flag).
        TRANSLATED: The runtime honors it only through a lossy adaptation —
            e.g. embedding the system prompt into the user message, or mapping
            a permission mode onto coarser CLI flags. The intent is partially
            preserved but the supplied form is not.
        IGNORED: The runtime silently drops the parameter.
    """

    NATIVE = "native"
    TRANSLATED = "translated"
    IGNORED = "ignored"


class SubagentOrchestration(StrEnum):
    """How (and whether) the orchestrator can fan work out to sub-agents.

    This is a property of the (runtime × backend) PAIR, declared per
    ``AgentRuntime`` instance via ``RuntimeCapabilities`` — NOT of the backend
    name alone. The same backend can present different modes under different
    runtimes: ``codex`` driven by ``codex exec`` is INTERNAL, but the same
    ``codex`` driven as ``codex mcp-server`` (``codex``/``codex-reply`` →
    addressable threads) is EXTERNAL_LEADER_DRIVEN.

    The two EXTERNAL modes are distinguished by *who drives the child*, because
    that determines the dispatch mechanism and is the seam that generalizes
    across providers:

    Values:
        NONE: No sub-agent mechanism the orchestrator can use. Work runs
            in-process (ouroboros's own ``anyio`` parallel AC executor). Default.
        INTERNAL: The backend self-parallelizes *inside its own session* but
            exposes no child-orchestration surface to an external driver.
            Verified for ``codex exec`` / ``codex mcp-server``: the native
            ``multi_agent_v1`` / ``codex_app`` team tools live only inside the
            interactive session, so a delegated call may spin up its own helpers
            but ouroboros cannot drive those children. In-process fan-out; may
            *nudge* internal parallelism via developer-instructions.
        EXTERNAL_HOST_BRIDGE: A host bridge spawns native sub-agents on the
            orchestrator's behalf, driven OUT-OF-BAND via the ``_subagent``
            envelope — ouroboros emits a payload and the host (the OpenCode
            plugin) spawns a child session. ouroboros does NOT call the child
            directly. Feeds ``should_dispatch_via_plugin``; eligible only when
            the per-deployment bridge is enabled (``opencode_mode=plugin``).
        EXTERNAL_LEADER_DRIVEN: ouroboros IS the leader and drives addressable
            worker sessions DIRECTLY through the runtime's own resumable-session
            surface — it spawns N sessions, holds their handles, and continues
            each. This is the provider-NEUTRAL worker-pool mode: any runtime that
            yields a resumable, addressable session qualifies. Verified surfaces:
            Codex (``codex``→threadId→``codex-reply`` via ``codex mcp-server``);
            Claude (``claude --resume <id>`` / ``claude mcp serve`` /
            ``--print --output-format stream-json``); future Gemini etc. The
            worker pool reads this mode via :func:`is_leader_driven_worker` and
            drives the runtime through the ``AgentRuntime`` seam + the threaded
            ``RuntimeHandle.native_session_id`` — NO ``_subagent`` envelope, so
            it must NEVER reach ``should_dispatch_via_plugin``.
    """

    NONE = "none"
    INTERNAL = "internal"
    EXTERNAL_HOST_BRIDGE = "external_host_bridge"
    EXTERNAL_LEADER_DRIVEN = "external_leader_driven"


# Canonical backend → sub-agent-orchestration mapping. Single source of truth so
# the per-runtime ``capabilities`` declarations and the static dispatch gate
# (``should_dispatch_via_plugin``) can never drift. This map is the COARSE
# backend-name fallback; the authoritative value is each runtime's declared
# ``capabilities.subagent_orchestration`` (which can be finer — e.g. a
# ``codex mcp-server`` worker runtime declares EXTERNAL_LEADER_DRIVEN even though
# the backend name ``codex`` maps to INTERNAL here). Backends absent default to
# NONE. Leader-driven worker runtimes deliberately stay ABSENT (→ NONE) so they
# never trip the host-bridge plugin gate; they are recognized at the runtime
# layer via :func:`is_leader_driven_worker`.
_OPENCODE_BACKENDS: frozenset[str] = frozenset({"opencode", "opencode_cli"})
_CODEX_BACKENDS: frozenset[str] = frozenset({"codex", "codex_cli"})


def subagent_orchestration_for_backend(backend: str | None) -> SubagentOrchestration:
    """Return the coarse backend-name :class:`SubagentOrchestration` fallback.

    Prefer a runtime's declared ``capabilities.subagent_orchestration`` where a
    runtime instance is available; this name-keyed map cannot distinguish
    ``codex exec`` from ``codex mcp-server`` and is only the static fallback for
    the envelope dispatch gate.
    """
    normalized = (backend or "").strip().lower()
    if normalized in _OPENCODE_BACKENDS:
        return SubagentOrchestration.EXTERNAL_HOST_BRIDGE
    if normalized in _CODEX_BACKENDS:
        return SubagentOrchestration.INTERNAL
    return SubagentOrchestration.NONE


def is_leader_driven_worker(capabilities: RuntimeCapabilities) -> bool:
    """Return whether ouroboros can drive this runtime as a direct worker-pool member.

    Provider-neutral seam: any runtime declaring
    ``SubagentOrchestration.EXTERNAL_LEADER_DRIVEN`` is eligible to be spawned and
    addressed directly by the leader (via ``execute_task`` + a resumable
    ``RuntimeHandle``), regardless of backend name. This is what a future
    worker-pool scheduler reads — NOT a backend-name check — so adding Claude,
    Gemini, or a codex-mcp runtime is "declare the mode + supply a thin
    transport", never a bespoke dispatch path.
    """
    return capabilities.subagent_orchestration is SubagentOrchestration.EXTERNAL_LEADER_DRIVEN


def is_host_bridge_dispatch(capabilities: RuntimeCapabilities) -> bool:
    """Return whether this runtime dispatches sub-agents via the host-bridge envelope."""
    return capabilities.subagent_orchestration is SubagentOrchestration.EXTERNAL_HOST_BRIDGE


@dataclass(frozen=True, slots=True)
class RuntimeCapabilities:
    """Declarative feature contract surfaced by an ``AgentRuntime``.

    Added to move backend differences from implicit "silent degradation" to
    explicit metadata the orchestrator can branch on. Upstream code should
    prefer ``runtime.capabilities.<feature>`` over backend-name checks.

    Attributes:
        skill_dispatch: Runtime honors ``ooo <skill>`` / ``/ouroboros:<skill>``
            prefixes by invoking the matching MCP tool instead of passing
            the prompt through to the underlying CLI.
        targeted_resume: Runtime can resume a specific session by id
            (as opposed to "resume most recent" or no-resume).
        structured_output: Runtime emits structured JSONL events
            (tool calls, thread ids, per-item events). ``False`` means
            plain-text stdout lines only.
        system_prompt_support: How the runtime honors the ``system_prompt``
            execution parameter (see :class:`ParamSupport`).
        tool_restriction_support: How the runtime honors the ``tools``
            allow-list passed to ``execute_task``.
        permission_mode_support: How the runtime honors ``permission_mode``.

    The three ``*_support`` fields default to :attr:`ParamSupport.NATIVE` so
    existing runtimes and ``FULL_CAPABILITIES`` are unchanged; a runtime opts in
    to a non-native value only when its handling is demonstrably lossy.
    """

    skill_dispatch: bool
    targeted_resume: bool
    structured_output: bool
    system_prompt_support: ParamSupport = ParamSupport.NATIVE
    tool_restriction_support: ParamSupport = ParamSupport.NATIVE
    permission_mode_support: ParamSupport = ParamSupport.NATIVE
    # The effort-first investment lever (RFC #1405): how the runtime honors the
    # ``reasoning_effort`` execution parameter. Unlike the three fields above it
    # defaults to IGNORED, because most agent runtimes have no per-call effort
    # knob — a runtime must opt in to NATIVE (or TRANSLATED) only when it can
    # actually route the level to its backend (e.g. Claude Agent SDK ``effort``,
    # Codex ``-c model_reasoning_effort``). This lets the orchestrator tell
    # enforced rows apart from advised ones instead of assuming uniform support.
    reasoning_effort_support: ParamSupport = ParamSupport.IGNORED
    # The reasoning-effort vocabulary this runtime actually enforces. A NATIVE
    # runtime still only honors the levels its backend accepts (Codex drops
    # anything outside ``model_reasoning_effort``'s allow-list; Claude has no
    # ``minimal``), so a level outside this set is recorded as *advised*, not
    # enforced — keeping the proof's enforced rows truthful. ``None`` means "no
    # per-level restriction" (any level is enforced when support is NATIVE).
    enforceable_reasoning_efforts: frozenset[str] | None = None
    # The per-call model-tier lever for frugality routing (RFC #1405 sibling): how
    # the runtime honors a ``model`` override handed to ``execute_task`` for a
    # single call. Like ``reasoning_effort_support`` it defaults to IGNORED — most
    # runtimes pin one model at construction and cannot re-target per call. A
    # runtime opts in to NATIVE only when it can verifiably route a per-call model
    # id to its backend (Claude SDK ``model`` option, claude-worker/codex
    # ``--model`` argv). This lets the orchestrator route a cheaper model to a
    # decomposed child and escalate on retry only where the choice is enforced.
    model_override_support: ParamSupport = ParamSupport.IGNORED
    # How the orchestrator may fan work out to sub-agents on this backend (see
    # :class:`SubagentOrchestration`). Defaults to NONE so a backend opts in to
    # INTERNAL/EXTERNAL only when its surface demonstrably supports it — this is
    # the capability the sub-agent dispatch gate reads instead of a backend name.
    subagent_orchestration: SubagentOrchestration = SubagentOrchestration.NONE


# Default capability profile for first-class backends (Claude, Codex).
# New backends should declare capabilities explicitly; silently inheriting
# this default would recreate the gap this dataclass exists to prevent.
FULL_CAPABILITIES = RuntimeCapabilities(
    skill_dispatch=True,
    targeted_resume=True,
    structured_output=True,
)

# Reasoning-effort levels the Claude Agent SDK ``effort`` option accepts. Used to
# declare Claude's enforceable vocabulary so a level it cannot honor (e.g. the
# Codex-only ``minimal``) is recorded as advised rather than falsely enforced.
CLAUDE_REASONING_EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})


class AgentRuntime(Protocol):
    """Protocol for autonomous agent runtimes used by the orchestrator."""

    @property
    def runtime_backend(self) -> str:
        """Canonical backend identifier (e.g. ``"claude"``, ``"codex_cli"``)."""
        ...

    @property
    def capabilities(self) -> RuntimeCapabilities:
        """Feature contract surfaced by this runtime.

        Default: ``FULL_CAPABILITIES`` (skill_dispatch + targeted_resume +
        structured_output all True). Runtimes override this property to
        declare a narrower surface — see ``KiroAgentAdapter`` for the
        canonical example. Providing a default implementation keeps
        pre-existing runtime adapters (Codex, Hermes, OpenCode, Gemini)
        structurally compatible with the Protocol without forcing a
        change to each one in this PR; callers can still branch on
        capability flags rather than backend names.
        """
        return FULL_CAPABILITIES

    @property
    def llm_backend(self) -> str | None:
        """LLM backend name for dependency analyzer wiring.

        Added in v0.28.6. Legacy runtime implementations without this property
        are handled via ``getattr()`` fallback at call sites - they degrade to
        structured-only dependency analysis. New implementations SHOULD define
        this to enable LLM-assisted dependency inference.

        Returns the canonical LLM backend identifier (e.g. ``"claude"``,
        ``"codex"``, ``"opencode"``, ``"litellm"``) used for non-runtime LLM
        tasks, or ``None`` to fall back to ``runtime_backend``.
        """
        ...

    @property
    def working_directory(self) -> str | None:
        """Working directory for task execution, or ``None`` if unset."""
        ...

    @property
    def permission_mode(self) -> str | None:
        """Active permission mode (e.g. ``"acceptEdits"``), or ``None``."""
        ...

    def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,  # Deprecated: use resume_handle instead
    ) -> AsyncIterator[AgentMessage]:
        """Execute a task and stream normalized messages.

        Implementations are async generators (``async def`` with ``yield``).
        The Protocol signature omits ``async`` so that structural subtyping
        correctly matches async-generator methods returning ``AsyncIterator``.
        """
        ...

    async def execute_task_to_result(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,  # Deprecated: use resume_handle instead
    ) -> Result[TaskResult, ProviderError]:
        """Execute a task and return the collected final result."""
        ...


# =============================================================================
# Adapter
# =============================================================================


# Default tools for code execution tasks
DEFAULT_TOOLS: list[str] = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]

# Retry configuration for transient SDK errors
MAX_RETRIES: int = 3
RETRY_WAIT_INITIAL: float = 1.0  # seconds
RETRY_WAIT_MAX: float = 10.0  # seconds

# Error patterns that indicate transient failures worth retrying.
#
# Derived from the canonical transient core in ``core.retry`` plus the one
# execution-specific signal (``"exit code 1"`` — the SDK CLI process failing).
# Adopting the shared core fixes a real divergence: this adapter previously did
# NOT retry on ``"overloaded"`` (Anthropic 529), the most common transient error
# under load, even though the completion adapters did. The shared tuple closes
# that gap without dropping any pattern this adapter already matched.
_CLAUDE_EXECUTION_RETRYABLE_EXTRA_PATTERNS = (
    "exit code 1",  # SDK CLI process failed
)
TRANSIENT_ERROR_PATTERNS: tuple[str, ...] = (
    *BASE_TRANSIENT_PATTERNS,
    *_CLAUDE_EXECUTION_RETRYABLE_EXTRA_PATTERNS,
)


class ClaudeAgentAdapter:
    """Adapter for Claude Agent SDK with streaming support.

    This adapter wraps the Claude Agent SDK's query() function to provide:
    - Async generator interface for message streaming
    - Normalized message format (AgentMessage)
    - Error handling with Result type
    - Configurable tools and permission modes

    Example:
        adapter = ClaudeAgentAdapter(permission_mode="acceptEdits")

        async for message in adapter.execute_task(
            prompt="Review and fix bugs in auth.py",
            tools=["Read", "Edit", "Bash"],
        ):
            if message.type == "assistant":
                print(f"Claude: {message.content[:100]}")
            elif message.type == "tool":
                print(f"Using tool: {message.tool_name}")
    """

    _runtime_handle_backend = "claude"
    _runtime_backend = "claude"
    _provider_name = "claude"

    #: This adapter runs its own shared RPM/TPM bucket
    #: (:meth:`_build_rate_limit_bucket`), so the parallel executor must NOT add a
    #: second dispatch-level rate gate for it (that would double-limit Claude).
    self_governs_rate_limit = True

    def __init__(
        self,
        api_key: str | None = None,
        permission_mode: str = "acceptEdits",
        model: str | None = None,
        cwd: str | Path | None = None,
        cli_path: str | Path | None = None,
    ) -> None:
        """Initialize Claude Agent adapter.

        Args:
            api_key: Anthropic API key. If not provided, uses ANTHROPIC_API_KEY
                    environment variable or Claude Code CLI authentication.
            permission_mode: Permission mode for tool execution.
                - "acceptEdits": Auto-approve file edits
                - "bypassPermissions": Run without prompts (CI/CD)
                - "default": Require canUseTool callback
            model: Claude model to use (e.g., "claude-sonnet-4-6").
                If not provided, uses the SDK default.
            cwd: Working directory for tool execution and resume metadata.
            cli_path: Optional Claude CLI path to pass through to the SDK.
        """
        self._api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self._permission_mode = permission_mode
        self._model = model
        self._cwd = str(Path(cwd).expanduser()) if cwd is not None else os.getcwd()
        self._cli_path = str(Path(cli_path).expanduser()) if cli_path is not None else None
        self._rate_limit_bucket = self._build_rate_limit_bucket()

        log.info(
            "orchestrator.adapter.initialized",
            permission_mode=permission_mode,
            has_api_key=bool(self._api_key),
            cwd=self._cwd,
            cli_path=self._cli_path,
            shared_rate_limit_enabled=self._rate_limit_bucket.enabled,
        )

    # -- AgentRuntime protocol properties ----------------------------------

    @property
    def runtime_backend(self) -> str:
        return self._runtime_handle_backend

    @property
    def llm_backend(self) -> str | None:
        return self._runtime_handle_backend  # "claude" → resolved to "claude_code" by factory

    @property
    def working_directory(self) -> str | None:
        return self._cwd

    @property
    def permission_mode(self) -> str | None:
        return self._permission_mode

    @property
    def capabilities(self) -> RuntimeCapabilities:
        # The Claude runtime drives the Claude Agent SDK, which honors the
        # per-call ``effort`` and ``model`` options natively (see execute_task),
        # so it opts in to NATIVE reasoning-effort AND model-override support.
        # Other capabilities match the first-class default.
        return replace(
            FULL_CAPABILITIES,
            reasoning_effort_support=ParamSupport.NATIVE,
            enforceable_reasoning_efforts=CLAUDE_REASONING_EFFORT_LEVELS,
            model_override_support=ParamSupport.NATIVE,
        )

    def _is_transient_error(self, error: Exception) -> bool:
        """Check if an error is transient and worth retrying.

        Args:
            error: The exception to check.

        Returns:
            True if the error appears to be transient.
        """
        return is_transient_error(
            str(error),
            extra_patterns=_CLAUDE_EXECUTION_RETRYABLE_EXTRA_PATTERNS,
        )

    @staticmethod
    def _parse_optional_positive_int(
        env_name: str,
        *,
        default: int,
    ) -> int | None:
        """Parse an optional positive integer env var; 0 disables the limit."""
        raw_value = os.environ.get(env_name, "").strip()
        if not raw_value:
            return default

        try:
            parsed = int(raw_value)
        except ValueError:
            log.warning(
                "orchestrator.adapter.invalid_rate_limit_env",
                env_name=env_name,
                raw_value=raw_value,
            )
            return default

        if parsed <= 0:
            return None
        return parsed

    def _build_rate_limit_bucket(self) -> SharedRateLimitBucket:
        """Create the shared rate-limit bucket for orchestrator workers.

        RPM/TPM defaults are sourced from the backend-limits registry (the
        single source of truth for per-backend constraints) and may be
        overridden per deployment via the ``OUROBOROS_ANTHROPIC_*`` env knobs
        (``0`` disables that limit). For the native Claude backend this resolves
        to the same Anthropic ceilings as before.
        """
        limits = resolve_backend_limits(self._runtime_backend)
        rpm_default = limits.requests_per_minute or DEFAULT_ANTHROPIC_RPM_CEILING
        tpm_default = limits.tokens_per_minute or DEFAULT_ANTHROPIC_TPM_CEILING
        return SharedRateLimitBucket(
            runtime_backend=self._runtime_backend,
            request_limit=self._parse_optional_positive_int(
                "OUROBOROS_ANTHROPIC_RPM_CEILING",
                default=rpm_default,
            ),
            token_limit=self._parse_optional_positive_int(
                "OUROBOROS_ANTHROPIC_TPM_CEILING",
                default=tpm_default,
            ),
        )

    @staticmethod
    def _rate_limit_snapshot_data(snapshot: RateLimitSnapshot) -> dict[str, Any]:
        """Serialize a shared-budget snapshot into message metadata."""
        return {
            "runtime_backend": snapshot.runtime_backend,
            "requests_in_window": snapshot.requests_in_window,
            "request_limit": snapshot.request_limit,
            "tokens_in_window": snapshot.tokens_in_window,
            "token_limit": snapshot.token_limit,
        }

    async def _wait_for_shared_rate_limit_budget(
        self,
        *,
        estimated_tokens: int,
        attempt: int,
        max_wait_seconds: float = RATE_LIMIT_MAX_WAIT_SECONDS,
    ) -> AsyncIterator[AgentMessage]:
        """Yield heartbeat messages while waiting for shared budget headroom."""
        if not self._rate_limit_bucket.enabled:
            return

        total_waited = 0.0
        while True:
            wait_seconds, snapshot = await self._rate_limit_bucket.acquire(estimated_tokens)
            if wait_seconds <= 0:
                return

            if total_waited >= max_wait_seconds:
                # Reserve the capacity anyway — otherwise concurrent timeout-fallbacks
                # would all bypass the bucket simultaneously, causing an N× RPM burst
                # to hit the upstream API (worse than starvation per review).
                snapshot = await self._rate_limit_bucket.force_reserve(estimated_tokens)
                log.warning(
                    "orchestrator.adapter.rate_limit_timeout_force_reserve",
                    total_waited=total_waited,
                    max_wait_seconds=max_wait_seconds,
                    estimated_tokens=estimated_tokens,
                    **self._rate_limit_snapshot_data(snapshot),
                )
                yield AgentMessage(
                    type="system",
                    content=(
                        f"Shared rate limit budget wait exceeded {max_wait_seconds:.0f}s; "
                        "proceeding with force-reserved capacity."
                    ),
                    data={
                        "subtype": "rate_limit_timeout_force_reserve",
                        "total_waited": total_waited,
                        "max_wait_seconds": max_wait_seconds,
                        "source": "shared_rate_limit_bucket",
                        **self._rate_limit_snapshot_data(snapshot),
                    },
                )
                return

            sleep_seconds = min(wait_seconds, RATE_LIMIT_HEARTBEAT_SECONDS)
            yield AgentMessage(
                type="system",
                content=(
                    "Shared Anthropic budget saturated; waiting "
                    f"{sleep_seconds:.1f}s before retrying worker dispatch."
                ),
                data={
                    "subtype": "rate_limit_backoff",
                    "backoff_seconds": sleep_seconds,
                    "retry_attempt": attempt,
                    "total_waited": total_waited,
                    "max_wait_seconds": max_wait_seconds,
                    "source": "shared_rate_limit_bucket",
                    **self._rate_limit_snapshot_data(snapshot),
                },
            )
            await asyncio.sleep(sleep_seconds)
            total_waited += sleep_seconds

    @staticmethod
    def _transient_backoff_subtype(error: Exception) -> str:
        """Classify transient backoff messages for observability."""
        error_text = str(error).lower()
        if "429" in error_text or "rate" in error_text or "concurrency" in error_text:
            return "rate_limit_backoff"
        return "transient_backoff"

    def _build_runtime_handle(
        self,
        native_session_id: str | None,
        current_handle: RuntimeHandle | None = None,
        *,
        approval_mode: str | None = None,
    ) -> RuntimeHandle | None:
        """Build a normalized runtime handle for the current Claude session."""
        dispatch = self._dispatch_execution_runtime(
            current_handle=current_handle,
            resume_session_id=native_session_id,
            prefer_current_handle_session_id=False,
        )
        if isinstance(dispatch, _RuntimeExecutionDispatchFailure):
            return None

        if dispatch.resume_session_id is None:
            return None

        current_runtime_handle = dispatch.runtime_handle
        if current_runtime_handle is not None:
            return replace(
                current_runtime_handle,
                backend=dispatch.backend,
                kind=current_runtime_handle.kind or "agent_runtime",
                native_session_id=dispatch.resume_session_id,
                cwd=current_runtime_handle.cwd or self._cwd,
                approval_mode=current_runtime_handle.approval_mode or self._permission_mode,
                updated_at=datetime.now(UTC).isoformat(),
                metadata=_clone_runtime_handle_data(current_runtime_handle.metadata),
            )

        return RuntimeHandle(
            backend=dispatch.backend,
            kind="agent_runtime",
            native_session_id=dispatch.resume_session_id,
            cwd=self._cwd,
            approval_mode=approval_mode or self._permission_mode,
            updated_at=datetime.now(UTC).isoformat(),
            metadata={},
        )

    def _dispatch_execution_runtime(
        self,
        *,
        current_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
        prefer_current_handle_session_id: bool = True,
    ) -> _RuntimeExecutionDispatch | _RuntimeExecutionDispatchFailure:
        """Resolve the single execution path for this adapter invocation."""
        runtime_handle = current_handle
        resolved_backend = self._runtime_handle_backend

        if runtime_handle is not None:
            try:
                normalized_backend = _resolve_runtime_handle_backend(
                    backend=runtime_handle.backend,
                )
            except ValueError as exc:
                return _RuntimeExecutionDispatchFailure(
                    public_message=(
                        "Task execution failed: runtime handle is incompatible with this runtime."
                    ),
                    reason="unknown_runtime_backend",
                    details={
                        "backend": runtime_handle.backend,
                        "error": str(exc),
                    },
                )
            if normalized_backend != self._runtime_handle_backend:
                return _RuntimeExecutionDispatchFailure(
                    public_message=(
                        "Task execution failed: runtime handle is incompatible with this runtime."
                    ),
                    reason="unsupported_runtime_backend",
                    details={
                        "backend": runtime_handle.backend,
                        "normalized_backend": normalized_backend,
                        "expected_backend": self._runtime_handle_backend,
                    },
                )
            # __post_init__ already canonicalizes backend on construction,
            # so runtime_handle.backend == normalized_backend is guaranteed here.
            resolved_backend = normalized_backend

        resolved_resume_session_id = resume_session_id
        if (
            prefer_current_handle_session_id
            and runtime_handle is not None
            and runtime_handle.native_session_id
        ):
            resolved_resume_session_id = runtime_handle.native_session_id

        return _RuntimeExecutionDispatch(
            backend=resolved_backend,
            runtime_handle=runtime_handle,
            resume_session_id=resolved_resume_session_id,
        )

    def _execution_dispatch_error_message(
        self,
        failure: _RuntimeExecutionDispatchFailure,
    ) -> AgentMessage:
        """Project a private dispatch failure into the existing result-message surface."""
        log.error(
            "orchestrator.adapter.execution_dispatch_failed",
            reason=failure.reason,
            **failure.details,
        )
        return AgentMessage(
            type="result",
            content=failure.public_message,
            data={
                "subtype": "error",
                "error_type": "RuntimeHandleError",
            },
        )

    async def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
        reasoning_effort: str | None = None,
        model: str | None = None,
    ) -> AsyncIterator[AgentMessage]:
        """Execute a task and yield progress messages.

        This is an async generator that streams messages as Claude works.
        Use async for to consume messages in real-time.

        Args:
            prompt: The task for Claude to perform.
            tools: List of tools Claude can use. Defaults to DEFAULT_TOOLS.
            system_prompt: Optional custom system prompt.
            resume_handle: Backend-neutral handle to resume from.
            resume_session_id: Legacy Claude session ID to resume from.
            model: Per-call model override from model-tier routing. When set it
                WINS over the constructor pin (``self._model``); when ``None``
                (the default) behavior is byte-identical to before.

        Yields:
            AgentMessage for each SDK message (assistant reasoning, tool calls, results).

        Raises:
            ProviderError: If SDK initialization fails.
        """
        effective_tools = tools or DEFAULT_TOOLS

        log.info(
            "orchestrator.adapter.task_started",
            prompt_preview=prompt[:100],
            tools=effective_tools,
            has_system_prompt=bool(system_prompt),
            resume_backend=resume_handle.backend if resume_handle else None,
            resume_session_id=resume_session_id,
        )

        dispatch = self._dispatch_execution_runtime(
            current_handle=resume_handle,
            resume_session_id=resume_session_id,
        )
        if isinstance(dispatch, _RuntimeExecutionDispatchFailure):
            yield self._execution_dispatch_error_message(dispatch)
            return

        try:
            # Lazy import to avoid loading SDK at module import time
            from claude_agent_sdk import ClaudeAgentOptions, query
            from claude_agent_sdk.types import HookMatcher
        except ImportError as e:
            log.error(
                "orchestrator.adapter.sdk_not_installed",
                error=str(e),
            )
            yield AgentMessage(
                type="result",
                content="Claude Agent SDK is not installed. Run: pip install claude-agent-sdk",
                data={"subtype": "error"},
            )
            return

        # Retry loop for transient errors
        attempt = 0
        last_error: Exception | None = None
        current_runtime_handle = dispatch.runtime_handle
        current_session_id = dispatch.resume_session_id
        estimated_tokens = estimate_runtime_request_tokens(prompt, system_prompt=system_prompt)

        while attempt < MAX_RETRIES:
            attempt += 1
            try:
                async for budget_message in self._wait_for_shared_rate_limit_budget(
                    estimated_tokens=estimated_tokens,
                    attempt=attempt,
                ):
                    yield budget_message

                effective_permission_mode = (
                    current_runtime_handle.approval_mode
                    if current_runtime_handle and current_runtime_handle.approval_mode
                    else self._permission_mode
                )

                # Build options
                options_kwargs: dict[str, Any] = {
                    "allowed_tools": effective_tools,
                    "permission_mode": effective_permission_mode,
                    "cwd": self._cwd,
                }

                async def _delegated_tool_context_hook(
                    hook_input: dict[str, Any],
                    _tool_name: str | None,
                    _context: dict[str, Any],
                ) -> dict[str, Any] | None:
                    return _build_delegated_tool_context_update(hook_input, effective_tools)

                options_kwargs["hooks"] = {
                    "PreToolUse": [
                        HookMatcher(
                            matcher=DELEGATED_EXECUTE_SEED_TOOL_MATCHER,
                            hooks=[_delegated_tool_context_hook],
                        )
                    ]
                }

                # Per-call model-tier override (RFC #1405 sibling) wins over the
                # constructor pin; ``model is None`` falls back to ``self._model``
                # so existing call sites are byte-identical. The Claude Agent SDK
                # honors ``model`` natively per call, which is what makes
                # ``model_override_support = NATIVE`` truthful for this runtime.
                effective_model = model or self._model
                if effective_model:
                    options_kwargs["model"] = effective_model

                if reasoning_effort:
                    # Effort-first investment dial (RFC #1405). The Claude Agent
                    # SDK honors ``effort`` natively per call (low/medium/high/
                    # xhigh/max), so the level the orchestrator chose is ENFORCED
                    # here rather than merely advised in the prompt. This is what
                    # makes ``reasoning_effort_support = NATIVE`` truthful for the
                    # Claude runtime.
                    options_kwargs["effort"] = reasoning_effort

                if self._cli_path:
                    options_kwargs["cli_path"] = self._cli_path

                if system_prompt:
                    options_kwargs["system_prompt"] = system_prompt

                if current_session_id:
                    options_kwargs["resume"] = current_session_id
                    if current_runtime_handle and current_runtime_handle.metadata.get(
                        "fork_session"
                    ):
                        options_kwargs["fork_session"] = True

                options = ClaudeAgentOptions(**options_kwargs)

                # Stream messages from SDK
                session_id: str | None = None
                async for sdk_message in query(prompt=prompt, options=options):
                    agent_message = self._convert_message(sdk_message)

                    # Capture session ID from init message
                    session_id = getattr(sdk_message, "session_id", None) or agent_message.data.get(
                        "session_id"
                    )
                    if session_id and (
                        session_id != current_session_id or current_runtime_handle is None
                    ):
                        current_session_id = session_id  # Save for potential retry
                        current_runtime_handle = self._build_runtime_handle(
                            session_id,
                            current_runtime_handle,
                            approval_mode=effective_permission_mode,
                        )

                    if current_runtime_handle:
                        data = agent_message.data
                        if current_session_id and data.get("session_id") != current_session_id:
                            data = {**data, "session_id": current_session_id}
                        agent_message = replace(
                            agent_message,
                            data=data,
                            resume_handle=current_runtime_handle,
                        )

                    yield agent_message

                    if agent_message.is_final:
                        log.info(
                            "orchestrator.adapter.task_completed",
                            success=not agent_message.is_error,
                            session_id=session_id,
                        )

                # Success - exit retry loop
                return

            except Exception as e:
                last_error = e
                if self._is_transient_error(e) and attempt < MAX_RETRIES:
                    wait_time = min(
                        RETRY_WAIT_INITIAL * (2 ** (attempt - 1)),
                        RETRY_WAIT_MAX,
                    )
                    yield AgentMessage(
                        type="system",
                        content=(
                            f"Transient backend backoff for {wait_time:.1f}s before retrying: {e!s}"
                        ),
                        data={
                            "subtype": self._transient_backoff_subtype(e),
                            "backoff_seconds": wait_time,
                            "retry_attempt": attempt,
                        },
                    )
                    log.warning(
                        "orchestrator.adapter.transient_error_retry",
                        error=str(e),
                        attempt=attempt,
                        max_retries=MAX_RETRIES,
                        wait_seconds=wait_time,
                        will_resume=bool(current_session_id),
                    )
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    # Non-transient error or max retries reached
                    log.exception(
                        "orchestrator.adapter.task_failed",
                        error=str(e),
                        attempts=attempt,
                    )
                    data = {
                        "subtype": "error",
                        "error_type": type(e).__name__,
                    }
                    if current_session_id:
                        data["session_id"] = current_session_id
                    yield AgentMessage(
                        type="result",
                        content=f"Task execution failed: {e!s}",
                        data=data,
                        resume_handle=current_runtime_handle,
                    )
                    return

        # Max retries exhausted (shouldn't normally reach here)
        if last_error:
            log.error(
                "orchestrator.adapter.max_retries_exhausted",
                error=str(last_error),
                attempts=MAX_RETRIES,
            )
            yield AgentMessage(
                type="result",
                content=f"Task failed after {MAX_RETRIES} retries: {last_error!s}",
                data={
                    "subtype": "error",
                    "error_type": type(last_error).__name__,
                    **({"session_id": current_session_id} if current_session_id else {}),
                },
                resume_handle=current_runtime_handle,
            )

    def _convert_message(self, sdk_message: Any) -> AgentMessage:
        """Convert SDK message to internal AgentMessage format.

        Args:
            sdk_message: Message from Claude Agent SDK.

        Returns:
            Normalized AgentMessage.
        """
        # SDK uses class names, not 'type' attribute
        class_name = type(sdk_message).__name__

        log.debug(
            "orchestrator.adapter.message_received",
            class_name=class_name,
            sdk_message=str(sdk_message)[:500],
        )

        # Extract content based on message class
        content = ""
        tool_name = None
        data: dict[str, Any] = {}
        msg_type = "unknown"

        if class_name == "AssistantMessage":
            msg_type = "assistant"
            # Assistant message with content blocks -- iterate ALL blocks
            content_blocks = getattr(sdk_message, "content", [])
            text_parts: list[str] = []

            for block in content_blocks:
                block_type = type(block).__name__

                if block_type == "TextBlock" and hasattr(block, "text"):
                    text_parts.append(block.text)

                elif block_type == "ToolUseBlock" and hasattr(block, "name"):
                    tool_name = block.name
                    tool_input = getattr(block, "input", {}) or {}
                    data["tool_input"] = tool_input
                    data["tool_detail"] = _format_tool_detail(tool_name, tool_input)
                    tool_call_id = getattr(block, "id", None) or getattr(block, "tool_use_id", None)
                    if isinstance(tool_call_id, str) and tool_call_id.strip():
                        # Preserve the SDK correlation id so later evidence
                        # validation can pair a mutation request with its actual
                        # (possibly failed) ToolResultBlock.
                        data["tool_call_id"] = tool_call_id.strip()

                elif block_type == "ThinkingBlock":
                    thinking = getattr(block, "thinking", "") or getattr(block, "text", "")
                    if thinking:
                        data["thinking"] = thinking.strip()

            if text_parts:
                content = "\n".join(text_parts)
            elif tool_name:
                content = f"Calling tool: {data.get('tool_detail', tool_name)}"

        elif class_name == "ResultMessage":
            msg_type = "result"
            # Final result message
            content = getattr(sdk_message, "result", "") or ""
            data["subtype"] = getattr(sdk_message, "subtype", "success")
            data["is_error"] = getattr(sdk_message, "is_error", False)
            data["session_id"] = getattr(sdk_message, "session_id", None)
            # Surface token usage so a later executor can attribute per-AC spend.
            # The SDK ResultMessage carries ``usage`` (a dict) and
            # ``total_cost_usd``; normalize defensively and omit anything
            # malformed (the deterministic proof treats such as missing).
            usage = _normalized_usage(getattr(sdk_message, "usage", None))
            if usage is _INVALID_USAGE:
                data["usage_invalid"] = True
            elif usage is not None:
                data["usage"] = usage
            total_cost_usd = getattr(sdk_message, "total_cost_usd", None)
            if (
                total_cost_usd is not None
                and not isinstance(total_cost_usd, bool)
                and isinstance(total_cost_usd, (int, float))
                and math.isfinite(total_cost_usd)
            ):
                data["total_cost_usd"] = total_cost_usd
            log.info(
                "orchestrator.adapter.result_message",
                result_content=content[:200] if content else "empty",
                subtype=data["subtype"],
                is_error=data["is_error"],
            )

        elif class_name == "SystemMessage":
            msg_type = "system"
            subtype = getattr(sdk_message, "subtype", "")
            msg_data = getattr(sdk_message, "data", {})
            if subtype == "init":
                session_id = msg_data.get("session_id")
                content = f"Session initialized: {session_id}"
                data["session_id"] = session_id
            else:
                content = f"System: {subtype}"
            data["subtype"] = subtype

        elif class_name == "UserMessage":
            msg_type = "user"
            # Tool result message. Claude's SDK reports ToolResultBlock on a
            # UserMessage and, historically, this adapter kept only its prose.
            # Preserve the call id + structured error bit so failed Edit/Write
            # calls can never become accepted mutation evidence.
            content_blocks = getattr(sdk_message, "content", [])
            for block in content_blocks:
                if hasattr(block, "content"):
                    content = str(block.content)[:500]
                block_type = type(block).__name__
                tool_call_id = getattr(block, "tool_use_id", None) or getattr(block, "id", None)
                is_tool_result = block_type == "ToolResultBlock" or isinstance(tool_call_id, str)
                if not is_tool_result:
                    continue
                msg_type = "tool_result"
                data["subtype"] = "tool_result"
                if isinstance(tool_call_id, str) and tool_call_id.strip():
                    data["tool_call_id"] = tool_call_id.strip()
                result_tool_name = getattr(block, "tool_name", None) or getattr(block, "name", None)
                if isinstance(result_tool_name, str) and result_tool_name.strip():
                    tool_name = result_tool_name.strip()
                raw_is_error = getattr(block, "is_error", None)
                if isinstance(raw_is_error, bool):
                    data["is_error"] = raw_is_error
                if isinstance(raw_is_error, bool):
                    data["tool_result"] = {
                        "content": [],
                        "text_content": content,
                        "is_error": raw_is_error,
                        "meta": {
                            **(
                                {"tool_call_id": tool_call_id.strip()}
                                if isinstance(tool_call_id, str) and tool_call_id.strip()
                                else {}
                            )
                        },
                    }
                break

        else:
            # Unknown message type
            content = str(sdk_message)
            data["raw_class"] = class_name

        return AgentMessage(
            type=msg_type,
            content=content,
            tool_name=tool_name,
            data=data,
        )

    async def complete(
        self,
        messages: list[Message],
        config: CompletionConfig,
    ) -> Result[CompletionResponse, ProviderError]:
        """LLMAdapter-compatible completion interface.

        Bridges ClaudeAgentAdapter to the LLMAdapter protocol so it can be
        used by InterviewEngine and other components that expect complete().

        Args:
            messages: Conversation messages (system, user, assistant).
            config: Completion configuration (model, temperature, etc.).

        Returns:
            Result containing CompletionResponse or ProviderError.
        """
        from ouroboros.providers.base import (
            CompletionResponse,
            MessageRole,
            UsageInfo,
        )

        # Extract system prompt from messages
        system_msgs = [m for m in messages if m.role == MessageRole.SYSTEM]
        non_system_msgs = [m for m in messages if m.role != MessageRole.SYSTEM]
        system_prompt = system_msgs[0].content if system_msgs else None

        # Build prompt from non-system messages.
        # For the first interview round, conversation_history is empty
        # so we must provide a minimal user prompt to prevent execute_task()
        # from early-returning. The system_prompt carries the full context.
        prompt_parts: list[str] = []
        for m in non_system_msgs:
            prompt_parts.append(f"[{m.role.value}]\n{m.content}")
        prompt = "\n\n".join(prompt_parts) if prompt_parts else "Proceed."

        # Allow read-only tools so the LLM can explore the codebase
        # when generating interview questions for brownfield projects.
        tools = ["Read", "Glob", "Grep"]
        assistant_texts: list[str] = []
        error_content: str | None = None

        async for message in self.execute_task(
            prompt=prompt,
            tools=tools,
            system_prompt=system_prompt,
        ):
            if message.type == "assistant" and message.content:
                assistant_texts.append(message.content)
            elif message.is_final and message.is_error:
                error_content = message.content

        if error_content:
            return Result.err(
                ProviderError(
                    message=error_content,
                    details={"assistant_texts": assistant_texts},
                )
            )

        # Use the last assistant message as the primary content
        content = assistant_texts[-1] if assistant_texts else ""

        if not content:
            return Result.err(
                ProviderError(
                    message="Empty response from Claude Agent SDK",
                    details={"message_count": len(assistant_texts)},
                )
            )

        return Result.ok(
            CompletionResponse(
                content=content,
                model=self._model or "claude-agent-sdk",
                usage=UsageInfo(
                    prompt_tokens=0,
                    completion_tokens=0,
                    total_tokens=0,
                ),
                finish_reason="stop",
            )
        )

    async def execute_task_to_result(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
    ) -> Result[TaskResult, ProviderError]:
        """Execute a task and collect all messages into a TaskResult.

        This is a convenience method that collects all messages from
        execute_task() into a single TaskResult. Use this when you don't
        need streaming progress updates.

        Args:
            prompt: The task for Claude to perform.
            tools: List of tools Claude can use. Defaults to DEFAULT_TOOLS.
            system_prompt: Optional custom system prompt.
            resume_handle: Backend-neutral handle to resume from.
            resume_session_id: Legacy Claude session ID to resume from.

        Returns:
            Result containing TaskResult on success, ProviderError on failure.
        """
        messages: list[AgentMessage] = []
        final_message = ""
        success = True
        session_id: str | None = None
        final_resume_handle = resume_handle

        async for message in self.execute_task(
            prompt=prompt,
            tools=tools,
            system_prompt=system_prompt,
            resume_handle=resume_handle,
            resume_session_id=resume_session_id,
        ):
            messages.append(message)

            if message.resume_handle is not None:
                final_resume_handle = message.resume_handle

            if message.is_final:
                final_message = message.content
                success = not message.is_error
                session_id = message.data.get("session_id")
                if session_id and final_resume_handle is None:
                    final_resume_handle = self._build_runtime_handle(session_id)

        if not success:
            return Result.err(
                ProviderError(
                    message=final_message,
                    details={"messages": [m.content for m in messages]},
                )
            )

        if session_id is None and final_resume_handle is not None:
            session_id = final_resume_handle.native_session_id

        return Result.ok(
            TaskResult(
                success=success,
                final_message=final_message,
                messages=tuple(messages),
                session_id=session_id,
                resume_handle=final_resume_handle,
            )
        )


ClaudeCodeRuntime = ClaudeAgentAdapter


__all__ = [
    "AgentRuntime",
    "AgentMessage",
    "ClaudeAgentAdapter",
    "ClaudeCodeRuntime",
    "DEFAULT_TOOLS",
    "FULL_CAPABILITIES",
    "ParamSupport",
    "RuntimeCapabilities",
    "RuntimeHandle",
    "SkillDispatchHandler",
    "SubagentOrchestration",
    "TaskResult",
    "is_host_bridge_dispatch",
    "is_leader_driven_worker",
    "subagent_orchestration_for_backend",
    "runtime_handle_tool_catalog",
    "runtime_handle_capability_graph",
    "runtime_handle_control_plane",
]
