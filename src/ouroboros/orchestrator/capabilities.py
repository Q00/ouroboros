"""Engine-owned capability graph derived from tool catalog state."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
import hashlib
import os
from pathlib import Path
import stat
from typing import Any
import unicodedata

import yaml

from ouroboros.mcp.types import MCPToolDefinition
from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.mcp_tools import (
    SessionToolCatalog,
    SessionToolCatalogEntry,
    ToolCatalogSourceMetadata,
)
from ouroboros.orchestrator.skill_tool_mapping import get_packaged_skill_context_keys

log = get_logger(__name__)


class CapabilityMutationClass(StrEnum):
    """How a capability can mutate state."""

    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"
    DESTRUCTIVE = "destructive"


class CapabilityParallelSafety(StrEnum):
    """How safely a capability can be used in parallel."""

    SAFE = "safe"
    SERIALIZED = "serialized"
    ISOLATED_SESSION_REQUIRED = "isolated_session_required"


class CapabilityInterruptibility(StrEnum):
    """How safely a running capability can be interrupted."""

    NONE = "none"
    SOFT = "soft"
    HARD = "hard"


class CapabilityApprovalClass(StrEnum):
    """Approval sensitivity for a capability."""

    DEFAULT = "default"
    ELEVATED = "elevated"
    BYPASS_FORBIDDEN = "bypass_forbidden"


class CapabilityOrigin(StrEnum):
    """Engine-level provenance classes for capabilities."""

    BUILTIN = "builtin"
    ATTACHED_MCP = "attached_mcp"
    PROVIDER_NATIVE = "provider_native"
    FUTURE_RUNTIME = "future_runtime"


class CapabilityScope(StrEnum):
    """Where a capability conceptually belongs."""

    KERNEL = "kernel"
    SIDECAR = "sidecar"
    ATTACHMENT = "attachment"
    SHELL_ONLY = "shell_only"


@dataclass(frozen=True, slots=True)
class CapabilitySemantics:
    """Engine semantics attached to a tool capability."""

    mutation_class: CapabilityMutationClass
    parallel_safety: CapabilityParallelSafety
    interruptibility: CapabilityInterruptibility
    approval_class: CapabilityApprovalClass
    origin: CapabilityOrigin
    scope: CapabilityScope


@dataclass(frozen=True, slots=True)
class CapabilityToolMetadata:
    """Tool-specific capability metadata used by runtime orchestration."""

    input_schema: Mapping[str, Any]
    mutation_class: str
    execution_mode: str
    companions: tuple[str, ...]
    required_context_keys: tuple[str, ...]
    mutation_targets: tuple[str, ...]
    state_mutations: tuple[Mapping[str, Any], ...]
    side_effects: tuple[str, ...]
    retry: Mapping[str, Any]
    interrupt: Mapping[str, Any]
    cancel: Mapping[str, Any]
    fallback_used: bool = False
    orchestration: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _OuroborosToolCapabilitySpec:
    """Explicit capability metadata spec for one Ouroboros-owned MCP tool."""

    execution_mode: str
    companions: tuple[str, ...]
    side_effects: tuple[str, ...]
    retry: Mapping[str, Any]
    interrupt: Mapping[str, Any]
    mutation_class: CapabilityMutationClass | None = None


@dataclass(frozen=True, slots=True)
class LateralPersonaMetadata:
    """Structured metadata for one lateral persona subagent lane."""

    persona_id: str
    role: str
    prompt: Mapping[str, Any]
    response_payload_ref: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-safe capability metadata."""
        return {
            "persona_id": self.persona_id,
            "role": self.role,
            "prompt": dict(self.prompt),
            "response_payload_ref": dict(self.response_payload_ref),
        }


@dataclass(frozen=True, slots=True)
class LateralPersonaPanelMetadata:
    """Structured metadata for lateral multi-persona orchestration."""

    panel_id: str
    mcp_tool: str
    dispatch_modes: tuple[str, ...]
    parallel_preference: str
    sequential_fallback: Mapping[str, Any]
    personas: tuple[LateralPersonaMetadata, ...]
    request_model_schema: Mapping[str, Any]
    response_payload_refs: Mapping[str, Any]
    runtime_instruction: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-safe capability metadata."""
        return {
            "panel_id": self.panel_id,
            "mcp_tool": self.mcp_tool,
            "dispatch_modes": list(self.dispatch_modes),
            "parallel_preference": self.parallel_preference,
            "sequential_fallback": dict(self.sequential_fallback),
            "personas": [persona.to_dict() for persona in self.personas],
            "request_model_schema": dict(self.request_model_schema),
            "response_payload_refs": dict(self.response_payload_refs),
            "runtime_instruction": self.runtime_instruction,
        }


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    """Capability wrapper around a normalized tool definition."""

    stable_id: str
    name: str
    original_name: str
    description: str
    server_name: str | None
    source_kind: str
    source_name: str
    semantics: CapabilitySemantics
    metadata: CapabilityToolMetadata | None = None


@dataclass(frozen=True, slots=True)
class CapabilityGraph:
    """Deterministic engine-owned capability graph."""

    capabilities: tuple[CapabilityDescriptor, ...] = field(default_factory=tuple)

    def names(self) -> tuple[str, ...]:
        """Return capability names in graph order."""
        return tuple(descriptor.name for descriptor in self.capabilities)

    def by_name(self) -> Mapping[str, CapabilityDescriptor]:
        """Return capability descriptors keyed by normalized tool name."""
        return {descriptor.name: descriptor for descriptor in self.capabilities}


def stable_code_investigation_question_identity(question: str) -> str:
    """Return a deterministic identity for an interview-originating question."""
    normalized = " ".join(unicodedata.normalize("NFKC", question).strip().split())
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"interview-question:{digest}"


_BUILTIN_SEMANTICS: dict[str, CapabilitySemantics] = {
    "Read": CapabilitySemantics(
        mutation_class=CapabilityMutationClass.READ_ONLY,
        parallel_safety=CapabilityParallelSafety.SAFE,
        interruptibility=CapabilityInterruptibility.NONE,
        approval_class=CapabilityApprovalClass.DEFAULT,
        origin=CapabilityOrigin.BUILTIN,
        scope=CapabilityScope.KERNEL,
    ),
    "Glob": CapabilitySemantics(
        mutation_class=CapabilityMutationClass.READ_ONLY,
        parallel_safety=CapabilityParallelSafety.SAFE,
        interruptibility=CapabilityInterruptibility.NONE,
        approval_class=CapabilityApprovalClass.DEFAULT,
        origin=CapabilityOrigin.BUILTIN,
        scope=CapabilityScope.KERNEL,
    ),
    "Grep": CapabilitySemantics(
        mutation_class=CapabilityMutationClass.READ_ONLY,
        parallel_safety=CapabilityParallelSafety.SAFE,
        interruptibility=CapabilityInterruptibility.NONE,
        approval_class=CapabilityApprovalClass.DEFAULT,
        origin=CapabilityOrigin.BUILTIN,
        scope=CapabilityScope.KERNEL,
    ),
    "WebFetch": CapabilitySemantics(
        mutation_class=CapabilityMutationClass.READ_ONLY,
        parallel_safety=CapabilityParallelSafety.SAFE,
        interruptibility=CapabilityInterruptibility.NONE,
        approval_class=CapabilityApprovalClass.DEFAULT,
        origin=CapabilityOrigin.BUILTIN,
        scope=CapabilityScope.SIDECAR,
    ),
    "WebSearch": CapabilitySemantics(
        mutation_class=CapabilityMutationClass.READ_ONLY,
        parallel_safety=CapabilityParallelSafety.SAFE,
        interruptibility=CapabilityInterruptibility.NONE,
        approval_class=CapabilityApprovalClass.DEFAULT,
        origin=CapabilityOrigin.BUILTIN,
        scope=CapabilityScope.SIDECAR,
    ),
    "Edit": CapabilitySemantics(
        mutation_class=CapabilityMutationClass.WORKSPACE_WRITE,
        parallel_safety=CapabilityParallelSafety.SERIALIZED,
        interruptibility=CapabilityInterruptibility.SOFT,
        approval_class=CapabilityApprovalClass.DEFAULT,
        origin=CapabilityOrigin.BUILTIN,
        scope=CapabilityScope.KERNEL,
    ),
    "Write": CapabilitySemantics(
        mutation_class=CapabilityMutationClass.WORKSPACE_WRITE,
        parallel_safety=CapabilityParallelSafety.SERIALIZED,
        interruptibility=CapabilityInterruptibility.SOFT,
        approval_class=CapabilityApprovalClass.ELEVATED,
        origin=CapabilityOrigin.BUILTIN,
        scope=CapabilityScope.KERNEL,
    ),
    "NotebookEdit": CapabilitySemantics(
        mutation_class=CapabilityMutationClass.WORKSPACE_WRITE,
        parallel_safety=CapabilityParallelSafety.SERIALIZED,
        interruptibility=CapabilityInterruptibility.SOFT,
        approval_class=CapabilityApprovalClass.ELEVATED,
        origin=CapabilityOrigin.BUILTIN,
        scope=CapabilityScope.SIDECAR,
    ),
    "Bash": CapabilitySemantics(
        mutation_class=CapabilityMutationClass.EXTERNAL_SIDE_EFFECT,
        parallel_safety=CapabilityParallelSafety.ISOLATED_SESSION_REQUIRED,
        interruptibility=CapabilityInterruptibility.HARD,
        approval_class=CapabilityApprovalClass.ELEVATED,
        origin=CapabilityOrigin.BUILTIN,
        scope=CapabilityScope.SHELL_ONLY,
    ),
}

# Pessimistic default classification for any capability whose real semantics
# cannot yet be inferred (inherited delegations, unmapped attached MCP tools).
# Intentionally EXTERNAL_SIDE_EFFECT + SERIALIZED + ELEVATED so an unknown
# tool never quietly widens a role envelope.
_DEFAULT_ATTACHED_SEMANTICS = CapabilitySemantics(
    mutation_class=CapabilityMutationClass.EXTERNAL_SIDE_EFFECT,
    parallel_safety=CapabilityParallelSafety.SERIALIZED,
    interruptibility=CapabilityInterruptibility.SOFT,
    approval_class=CapabilityApprovalClass.ELEVATED,
    origin=CapabilityOrigin.ATTACHED_MCP,
    scope=CapabilityScope.ATTACHMENT,
)

_OUROBOROS_COMPANION_FAMILIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "auto",
        (
            "ouroboros_auto",
            "ouroboros_start_auto",
        ),
    ),
    (
        "execute",
        (
            "ouroboros_execute_seed",
            "ouroboros_start_execute_seed",
            "ouroboros_cancel_execution",
        ),
    ),
    (
        "job",
        (
            "ouroboros_start_auto",
            "ouroboros_start_execute_seed",
            "ouroboros_start_evaluate",
            "ouroboros_start_evolve_step",
            "ouroboros_start_ralph",
            "ouroboros_job_status",
            "ouroboros_job_wait",
            "ouroboros_job_result",
            "ouroboros_cancel_job",
        ),
    ),
    (
        "session",
        (
            "ouroboros_session_status",
            "ouroboros_query_events",
            "ouroboros_query_projection",
            "ouroboros_ac_tree_hud",
        ),
    ),
    (
        "authoring",
        (
            "ouroboros_interview",
            "ouroboros_generate_seed",
            "ouroboros_pm_interview",
            "ouroboros_brownfield",
        ),
    ),
    (
        "evaluation",
        (
            "ouroboros_evaluate",
            "ouroboros_start_evaluate",
            "ouroboros_checklist_verify",
            "ouroboros_measure_drift",
            "ouroboros_qa",
        ),
    ),
    (
        "evolution",
        (
            "ouroboros_evolve_step",
            "ouroboros_start_evolve_step",
            "ouroboros_lineage_status",
            "ouroboros_evolve_rewind",
            "ouroboros_ralph",
            "ouroboros_start_ralph",
        ),
    ),
    (
        "subagent_orchestration",
        (
            "ouroboros_interview",
            "ouroboros_lateral_think",
        ),
    ),
)

_OUROBOROS_BACKGROUND_TOOLS = frozenset(
    {
        "ouroboros_start_auto",
        "ouroboros_start_execute_seed",
        "ouroboros_start_evaluate",
        "ouroboros_start_evolve_step",
        "ouroboros_ralph",
        "ouroboros_start_ralph",
    }
)
_OUROBOROS_STATUS_TOOLS = frozenset(
    {
        "ouroboros_session_status",
        "ouroboros_job_status",
        "ouroboros_job_wait",
        "ouroboros_job_result",
        "ouroboros_ac_tree_hud",
        "ouroboros_query_events",
        "ouroboros_query_projection",
        "ouroboros_lineage_status",
    }
)
_OUROBOROS_CANCEL_TOOLS = frozenset(
    {
        "ouroboros_cancel_job",
        "ouroboros_cancel_execution",
    }
)
_OUROBOROS_WORKSPACE_WRITE_TOOLS = frozenset(
    {
        "ouroboros_auto",
        "ouroboros_start_auto",
        "ouroboros_execute_seed",
        "ouroboros_start_execute_seed",
        "ouroboros_evolve_step",
        "ouroboros_start_evolve_step",
        "ouroboros_ralph",
        "ouroboros_start_ralph",
        "ouroboros_generate_seed",
        "ouroboros_checklist_verify",
        "ouroboros_brownfield",
    }
)
_OUROBOROS_SUBAGENT_TOOLS = frozenset(
    {
        "ouroboros_interview",
        "ouroboros_lateral_think",
        "ouroboros_pm_interview",
    }
)
_OUROBOROS_DEFAULT_EXECUTION_MODE = "blocking"
_OUROBOROS_DEFAULT_RETRY_METADATA: Mapping[str, Any] = {
    "supported": True,
    "mode": "handler_owned",
}
_OUROBOROS_JOB_POLL_RETRY_METADATA: Mapping[str, Any] = {
    "supported": True,
    "mode": "job_poll",
}
_OUROBOROS_UNSUPPORTED_RETRY_METADATA: Mapping[str, Any] = {
    "supported": False,
    "mode": "unsupported",
}
_OUROBOROS_DEFAULT_INTERRUPT_METADATA: Mapping[str, Any] = {
    "supported": True,
    "mode": "soft",
}
_OUROBOROS_BLOCKING_INTERRUPT_METADATA: Mapping[str, Any] = {
    "supported": True,
    "mode": "soft",
    "execution_mode": "blocking",
    "blocking_semantics": "synchronous_handler",
    "resumable": False,
    "background_companions": (),
    "target_context_keys": (),
}
_OUROBOROS_BACKGROUND_INTERRUPT_METADATA: Mapping[str, Any] = {
    "supported": True,
    "mode": "resumable_background_job",
    "resumable": True,
    "cancellable": True,
    "resume_companions": (
        "ouroboros_job_status",
        "ouroboros_job_wait",
        "ouroboros_job_result",
    ),
    "cancel_companions": ("ouroboros_cancel_job",),
    "target_context_keys": ("job_id",),
}
_OUROBOROS_TERMINAL_CONTROL_INTERRUPT_METADATA_BY_TOOL: Mapping[str, Mapping[str, Any]] = {
    "ouroboros_cancel_job": {
        "supported": True,
        "mode": "terminal_control",
        "terminal_action": "cancel",
        "target_type": "background_job",
        "target_context_keys": ("job_id",),
        "directive_semantics": "request_terminal_job_cancellation",
        "terminal_statuses": ("cancelled",),
        "idempotent": True,
    },
    "ouroboros_cancel_execution": {
        "supported": True,
        "mode": "terminal_control",
        "terminal_action": "cancel",
        "target_type": "execution_session",
        "target_context_keys": ("execution_id",),
        "directive_semantics": "request_terminal_execution_cancellation",
        "terminal_statuses": ("cancelled",),
        "idempotent": True,
    },
}
_OUROBOROS_UNSUPPORTED_INTERRUPT_METADATA: Mapping[str, Any] = {
    "supported": False,
    "mode": "unsupported",
}
_OUROBOROS_READ_ONLY_INTERRUPT_METADATA: Mapping[str, Any] = {
    "supported": True,
    "mode": "read_only_non_mutating",
    "mutation_semantics": "no_state_mutation",
    "resumable": False,
    "target_context_keys": (),
}
_OUROBOROS_UNSUPPORTED_CANCEL_METADATA: Mapping[str, Any] = {
    "supported": False,
    "mode": "unsupported",
    "companions": (),
    "target_context_keys": (),
}
_OUROBOROS_SIDE_EFFECT_FREE_METADATA: tuple[str, ...] = ()
_OUROBOROS_MUTATION_TARGETS_BY_SIDE_EFFECT: Mapping[str, tuple[str, ...]] = {
    "background_job_start": ("background_job",),
    "checkpoint_store_write": ("checkpoint_store",),
    "event_store_write": ("event_store",),
    "runtime_control": ("runtime",),
    "session_state_write": ("session_state",),
    "side_effect_free": (),
    "subagent_dispatch": ("subagent",),
    "workspace_write": ("workspace",),
}
_OUROBOROS_STATE_MUTATIONS_BY_TOOL: Mapping[str, tuple[Mapping[str, Any], ...]] = {
    "ouroboros_auto": (
        {
            "target": "auto_session_state",
            "operation": "run_auto_pipeline_to_seed_and_execution",
            "side_effect": "event_store_write",
            "context_keys": ("goal", "resume", "cwd"),
        },
        {
            "target": "workspace",
            "operation": "apply_auto_generated_execution_changes",
            "side_effect": "workspace_write",
            "context_keys": ("cwd",),
        },
    ),
    "ouroboros_start_auto": (
        {
            "target": "auto_session_state",
            "operation": "enqueue_background_auto_pipeline",
            "side_effect": "event_store_write",
            "context_keys": ("goal", "resume", "cwd"),
        },
        {
            "target": "workspace",
            "operation": "apply_background_auto_execution_changes",
            "side_effect": "workspace_write",
            "context_keys": ("cwd",),
        },
    ),
    "ouroboros_execute_seed": (
        {
            "target": "execution_session",
            "operation": "run_seed_execution_session",
            "side_effect": "event_store_write",
            "context_keys": ("seed_path", "seed_content", "session_id", "cwd"),
        },
        {
            "target": "workspace",
            "operation": "apply_seed_execution_changes",
            "side_effect": "workspace_write",
            "context_keys": ("cwd",),
        },
    ),
    "ouroboros_start_execute_seed": (
        {
            "target": "execution_session",
            "operation": "enqueue_background_seed_execution",
            "side_effect": "event_store_write",
            "context_keys": ("seed_content", "session_id", "cwd"),
        },
        {
            "target": "workspace",
            "operation": "apply_background_seed_execution_changes",
            "side_effect": "workspace_write",
            "context_keys": ("cwd",),
        },
    ),
    "ouroboros_evaluate": (
        {
            "target": "session_state",
            "operation": "append_evaluation_result",
            "side_effect": "session_state_write",
            "context_keys": ("session_id",),
        },
    ),
    "ouroboros_evolve_step": (
        {
            "target": "lineage_state",
            "operation": "append_evolution_generation_result",
            "side_effect": "event_store_write",
            "context_keys": ("lineage_id", "seed_content", "execution_id"),
        },
        {
            "target": "workspace",
            "operation": "apply_evolution_generation_changes",
            "side_effect": "workspace_write",
            "context_keys": ("project_dir",),
        },
    ),
    "ouroboros_evolve_rewind": (
        {
            "target": "lineage_state",
            "operation": "truncate_generations_after_target",
            "side_effect": "session_state_write",
            "context_keys": ("lineage_id", "to_generation"),
        },
    ),
    "ouroboros_start_evolve_step": (
        {
            "target": "lineage_state",
            "operation": "enqueue_background_evolution_generation",
            "side_effect": "event_store_write",
            "context_keys": ("lineage_id", "seed_content", "execution_id"),
        },
        {
            "target": "workspace",
            "operation": "apply_background_evolution_generation_changes",
            "side_effect": "workspace_write",
            "context_keys": ("project_dir",),
        },
    ),
    "ouroboros_interview": (
        {
            "target": "interview_state",
            "operation": "create_or_update_or_complete_interview_session",
            "side_effect": "session_state_write",
            "context_keys": (
                "initial_context",
                "session_id",
                "answer",
                "last_question",
            ),
        },
        {
            "target": "subagent_dispatch_log",
            "operation": "record_interview_subagent_dispatch",
            "side_effect": "subagent_dispatch",
            "context_keys": ("session_id",),
        },
    ),
    "ouroboros_lateral_think": (
        {
            "target": "lateral_panel_state",
            "operation": "dispatch_persona_panel_and_synthesize_findings",
            "side_effect": "subagent_dispatch",
            "context_keys": (
                "problem_context",
                "current_approach",
                "failed_attempts",
            ),
        },
        {
            "target": "session_state",
            "operation": "record_lateral_review_result",
            "side_effect": "session_state_write",
            "context_keys": ("problem_context",),
        },
    ),
    "ouroboros_measure_drift": (
        {
            "target": "session_state",
            "operation": "append_drift_measurement",
            "side_effect": "session_state_write",
            "context_keys": ("session_id",),
        },
    ),
    "ouroboros_pm_interview": (
        {
            "target": "interview_state",
            "operation": "create_or_update_pm_interview_session",
            "side_effect": "session_state_write",
            "context_keys": (
                "initial_context",
                "session_id",
                "answer",
                "last_question",
            ),
        },
        {
            "target": "pm_meta_state",
            "operation": "persist_pm_session_metadata",
            "side_effect": "session_state_write",
            "context_keys": ("session_id", "cwd", "selected_repos"),
        },
        {
            "target": "subagent_dispatch_log",
            "operation": "record_pm_interview_subagent_dispatch",
            "side_effect": "subagent_dispatch",
            "context_keys": ("session_id",),
        },
    ),
    "ouroboros_qa": (
        {
            "target": "qa_session_state",
            "operation": "append_qa_iteration_verdict",
            "side_effect": "session_state_write",
            "context_keys": ("qa_session_id", "iteration_history"),
        },
    ),
    "ouroboros_ralph": (
        {
            "target": "ralph_loop_state",
            "operation": "run_evolution_loop_until_terminal_condition",
            "side_effect": "event_store_write",
            "context_keys": ("lineage_id", "execution_id"),
        },
        {
            "target": "workspace",
            "operation": "apply_ralph_loop_generation_changes",
            "side_effect": "workspace_write",
            "context_keys": ("project_dir",),
        },
    ),
    "ouroboros_start_ralph": (
        {
            "target": "ralph_loop_state",
            "operation": "enqueue_background_evolution_loop",
            "side_effect": "event_store_write",
            "context_keys": ("lineage_id", "execution_id"),
        },
        {
            "target": "workspace",
            "operation": "apply_background_ralph_loop_generation_changes",
            "side_effect": "workspace_write",
            "context_keys": ("project_dir",),
        },
    ),
    "ouroboros_cancel_execution": (
        {
            "target": "execution_session",
            "operation": "mark_execution_session_cancelled",
            "side_effect": "session_state_write",
            "context_keys": ("execution_id", "reason"),
        },
        {
            "target": "event_store",
            "operation": "append_session_cancelled_event",
            "side_effect": "event_store_write",
            "context_keys": ("execution_id", "reason"),
        },
        {
            "target": "runtime",
            "operation": "signal_execution_runner_to_stop_via_cancellation_event",
            "side_effect": "runtime_control",
            "context_keys": ("execution_id",),
        },
    ),
    "ouroboros_cancel_job": (
        {
            "target": "background_job",
            "operation": "mark_background_job_cancel_requested",
            "side_effect": "event_store_write",
            "context_keys": ("job_id",),
        },
        {
            "target": "checkpoint_store",
            "operation": "persist_durable_agent_process_cancel_signal",
            "side_effect": "checkpoint_store_write",
            "context_keys": ("job_id",),
        },
        {
            "target": "runtime",
            "operation": "cancel_live_background_job_tasks",
            "side_effect": "runtime_control",
            "context_keys": ("job_id",),
        },
        {
            "target": "session_state",
            "operation": "mark_linked_execution_session_cancelled_when_needed",
            "side_effect": "session_state_write",
            "context_keys": ("job_id",),
        },
    ),
}
_OUROBOROS_BACKGROUND_JOB_CANCEL_METADATA: Mapping[str, Any] = {
    "supported": True,
    "mode": "background_job",
    "companions": ("ouroboros_cancel_job",),
    "target_context_keys": ("job_id",),
}
_OUROBOROS_EXECUTION_SESSION_CANCEL_METADATA: Mapping[str, Any] = {
    "supported": True,
    "mode": "execution_session",
    "companions": ("ouroboros_cancel_execution",),
    "target_context_keys": ("execution_id",),
}
_OUROBOROS_BACKGROUND_JOB_CANCEL_CONTROL_METADATA: Mapping[str, Any] = {
    "supported": True,
    "mode": "background_job_control",
    "companions": (
        "ouroboros_job_status",
        "ouroboros_job_wait",
        "ouroboros_job_result",
    ),
    "target_context_keys": ("job_id",),
}
_OUROBOROS_EXECUTION_SESSION_CANCEL_CONTROL_METADATA: Mapping[str, Any] = {
    "supported": True,
    "mode": "execution_session_control",
    "companions": (
        "ouroboros_execute_seed",
        "ouroboros_start_execute_seed",
    ),
    "target_context_keys": ("execution_id",),
}

_OUROBOROS_TOOL_CAPABILITY_SPECS: Mapping[str, _OuroborosToolCapabilitySpec] = {
    "ouroboros_execute_seed": _OuroborosToolCapabilitySpec(
        execution_mode="blocking",
        companions=(
            "ouroboros_start_execute_seed",
            "ouroboros_cancel_execution",
            "ouroboros_job_status",
            "ouroboros_job_wait",
            "ouroboros_job_result",
            "ouroboros_cancel_job",
        ),
        side_effects=("workspace_write", "event_store_write"),
        retry=_OUROBOROS_DEFAULT_RETRY_METADATA,
        interrupt=_OUROBOROS_BLOCKING_INTERRUPT_METADATA,
    ),
    "ouroboros_start_execute_seed": _OuroborosToolCapabilitySpec(
        execution_mode="background",
        companions=(
            "ouroboros_execute_seed",
            "ouroboros_cancel_execution",
            "ouroboros_start_evaluate",
            "ouroboros_start_evolve_step",
            "ouroboros_start_ralph",
            "ouroboros_job_status",
            "ouroboros_job_wait",
            "ouroboros_job_result",
            "ouroboros_cancel_job",
        ),
        side_effects=("workspace_write", "event_store_write"),
        retry=_OUROBOROS_JOB_POLL_RETRY_METADATA,
        interrupt=_OUROBOROS_BACKGROUND_INTERRUPT_METADATA,
    ),
    "ouroboros_auto": _OuroborosToolCapabilitySpec(
        execution_mode="blocking",
        companions=("ouroboros_start_auto",),
        side_effects=("workspace_write", "event_store_write"),
        retry=_OUROBOROS_DEFAULT_RETRY_METADATA,
        interrupt=_OUROBOROS_BLOCKING_INTERRUPT_METADATA,
    ),
    "ouroboros_start_auto": _OuroborosToolCapabilitySpec(
        execution_mode="background",
        companions=(
            "ouroboros_auto",
            "ouroboros_job_status",
            "ouroboros_job_wait",
            "ouroboros_job_result",
            "ouroboros_cancel_job",
        ),
        side_effects=("workspace_write", "event_store_write"),
        retry=_OUROBOROS_JOB_POLL_RETRY_METADATA,
        interrupt=_OUROBOROS_BACKGROUND_INTERRUPT_METADATA,
    ),
    "ouroboros_session_status": _OuroborosToolCapabilitySpec(
        execution_mode="status",
        companions=(
            "ouroboros_query_events",
            "ouroboros_query_projection",
            "ouroboros_ac_tree_hud",
        ),
        side_effects=_OUROBOROS_SIDE_EFFECT_FREE_METADATA,
        retry=_OUROBOROS_DEFAULT_RETRY_METADATA,
        interrupt=_OUROBOROS_READ_ONLY_INTERRUPT_METADATA,
        mutation_class=CapabilityMutationClass.READ_ONLY,
    ),
    "ouroboros_job_status": _OuroborosToolCapabilitySpec(
        execution_mode="status",
        companions=(
            "ouroboros_start_auto",
            "ouroboros_start_execute_seed",
            "ouroboros_start_evaluate",
            "ouroboros_start_evolve_step",
            "ouroboros_start_ralph",
            "ouroboros_job_wait",
            "ouroboros_job_result",
            "ouroboros_cancel_job",
        ),
        side_effects=_OUROBOROS_SIDE_EFFECT_FREE_METADATA,
        retry=_OUROBOROS_DEFAULT_RETRY_METADATA,
        interrupt=_OUROBOROS_READ_ONLY_INTERRUPT_METADATA,
        mutation_class=CapabilityMutationClass.READ_ONLY,
    ),
    "ouroboros_job_wait": _OuroborosToolCapabilitySpec(
        execution_mode="status",
        companions=(
            "ouroboros_start_auto",
            "ouroboros_start_execute_seed",
            "ouroboros_start_evaluate",
            "ouroboros_start_evolve_step",
            "ouroboros_start_ralph",
            "ouroboros_job_status",
            "ouroboros_job_result",
            "ouroboros_cancel_job",
        ),
        side_effects=_OUROBOROS_SIDE_EFFECT_FREE_METADATA,
        retry=_OUROBOROS_DEFAULT_RETRY_METADATA,
        interrupt=_OUROBOROS_READ_ONLY_INTERRUPT_METADATA,
        mutation_class=CapabilityMutationClass.READ_ONLY,
    ),
    "ouroboros_job_result": _OuroborosToolCapabilitySpec(
        execution_mode="status",
        companions=(
            "ouroboros_start_auto",
            "ouroboros_start_execute_seed",
            "ouroboros_start_evaluate",
            "ouroboros_start_evolve_step",
            "ouroboros_start_ralph",
            "ouroboros_job_status",
            "ouroboros_job_wait",
            "ouroboros_cancel_job",
        ),
        side_effects=_OUROBOROS_SIDE_EFFECT_FREE_METADATA,
        retry=_OUROBOROS_DEFAULT_RETRY_METADATA,
        interrupt=_OUROBOROS_READ_ONLY_INTERRUPT_METADATA,
        mutation_class=CapabilityMutationClass.READ_ONLY,
    ),
    "ouroboros_ac_tree_hud": _OuroborosToolCapabilitySpec(
        execution_mode="status",
        companions=(
            "ouroboros_session_status",
            "ouroboros_query_events",
            "ouroboros_query_projection",
        ),
        side_effects=_OUROBOROS_SIDE_EFFECT_FREE_METADATA,
        retry=_OUROBOROS_DEFAULT_RETRY_METADATA,
        interrupt=_OUROBOROS_READ_ONLY_INTERRUPT_METADATA,
        mutation_class=CapabilityMutationClass.READ_ONLY,
    ),
    "ouroboros_cancel_job": _OuroborosToolCapabilitySpec(
        execution_mode="cancel",
        companions=(
            "ouroboros_start_auto",
            "ouroboros_start_execute_seed",
            "ouroboros_start_evaluate",
            "ouroboros_start_evolve_step",
            "ouroboros_start_ralph",
            "ouroboros_job_status",
            "ouroboros_job_wait",
            "ouroboros_job_result",
        ),
        side_effects=(
            "runtime_control",
            "event_store_write",
            "checkpoint_store_write",
            "session_state_write",
        ),
        retry=_OUROBOROS_UNSUPPORTED_RETRY_METADATA,
        interrupt=_OUROBOROS_TERMINAL_CONTROL_INTERRUPT_METADATA_BY_TOOL["ouroboros_cancel_job"],
    ),
    "ouroboros_query_events": _OuroborosToolCapabilitySpec(
        execution_mode="status",
        companions=(
            "ouroboros_session_status",
            "ouroboros_query_projection",
            "ouroboros_ac_tree_hud",
        ),
        side_effects=_OUROBOROS_SIDE_EFFECT_FREE_METADATA,
        retry=_OUROBOROS_DEFAULT_RETRY_METADATA,
        interrupt=_OUROBOROS_READ_ONLY_INTERRUPT_METADATA,
        mutation_class=CapabilityMutationClass.READ_ONLY,
    ),
    "ouroboros_query_projection": _OuroborosToolCapabilitySpec(
        execution_mode="status",
        companions=(
            "ouroboros_session_status",
            "ouroboros_query_events",
            "ouroboros_ac_tree_hud",
        ),
        side_effects=_OUROBOROS_SIDE_EFFECT_FREE_METADATA,
        retry=_OUROBOROS_DEFAULT_RETRY_METADATA,
        interrupt=_OUROBOROS_READ_ONLY_INTERRUPT_METADATA,
        mutation_class=CapabilityMutationClass.READ_ONLY,
    ),
    "ouroboros_generate_seed": _OuroborosToolCapabilitySpec(
        execution_mode="blocking",
        companions=(
            "ouroboros_interview",
            "ouroboros_pm_interview",
            "ouroboros_brownfield",
        ),
        side_effects=("workspace_write", "event_store_write"),
        retry=_OUROBOROS_DEFAULT_RETRY_METADATA,
        interrupt=_OUROBOROS_BLOCKING_INTERRUPT_METADATA,
    ),
    "ouroboros_measure_drift": _OuroborosToolCapabilitySpec(
        execution_mode="blocking",
        companions=(
            "ouroboros_evaluate",
            "ouroboros_start_evaluate",
            "ouroboros_checklist_verify",
            "ouroboros_qa",
        ),
        side_effects=("session_state_write",),
        retry=_OUROBOROS_DEFAULT_RETRY_METADATA,
        interrupt=_OUROBOROS_BLOCKING_INTERRUPT_METADATA,
    ),
    "ouroboros_interview": _OuroborosToolCapabilitySpec(
        execution_mode="subagent_orchestration",
        companions=(
            "ouroboros_generate_seed",
            "ouroboros_pm_interview",
            "ouroboros_brownfield",
            "ouroboros_lateral_think",
        ),
        side_effects=("subagent_dispatch", "session_state_write"),
        retry=_OUROBOROS_DEFAULT_RETRY_METADATA,
        interrupt=_OUROBOROS_DEFAULT_INTERRUPT_METADATA,
    ),
    "ouroboros_evaluate": _OuroborosToolCapabilitySpec(
        execution_mode="blocking",
        companions=(
            "ouroboros_start_evaluate",
            "ouroboros_job_status",
            "ouroboros_job_wait",
            "ouroboros_job_result",
            "ouroboros_cancel_job",
            "ouroboros_checklist_verify",
            "ouroboros_measure_drift",
            "ouroboros_qa",
        ),
        side_effects=("session_state_write",),
        retry=_OUROBOROS_DEFAULT_RETRY_METADATA,
        interrupt=_OUROBOROS_BLOCKING_INTERRUPT_METADATA,
    ),
    "ouroboros_start_evaluate": _OuroborosToolCapabilitySpec(
        execution_mode="background",
        companions=(
            "ouroboros_start_execute_seed",
            "ouroboros_start_evolve_step",
            "ouroboros_start_ralph",
            "ouroboros_job_status",
            "ouroboros_job_wait",
            "ouroboros_job_result",
            "ouroboros_cancel_job",
            "ouroboros_evaluate",
            "ouroboros_checklist_verify",
            "ouroboros_measure_drift",
            "ouroboros_qa",
        ),
        side_effects=("background_job_start", "event_store_write"),
        retry=_OUROBOROS_JOB_POLL_RETRY_METADATA,
        interrupt=_OUROBOROS_BACKGROUND_INTERRUPT_METADATA,
    ),
    "ouroboros_checklist_verify": _OuroborosToolCapabilitySpec(
        execution_mode="blocking",
        companions=(
            "ouroboros_evaluate",
            "ouroboros_start_evaluate",
            "ouroboros_measure_drift",
            "ouroboros_qa",
        ),
        side_effects=("workspace_write", "event_store_write"),
        retry=_OUROBOROS_DEFAULT_RETRY_METADATA,
        interrupt=_OUROBOROS_BLOCKING_INTERRUPT_METADATA,
    ),
    "ouroboros_lateral_think": _OuroborosToolCapabilitySpec(
        execution_mode="subagent_orchestration",
        companions=("ouroboros_interview",),
        side_effects=("subagent_dispatch", "session_state_write"),
        retry=_OUROBOROS_DEFAULT_RETRY_METADATA,
        interrupt=_OUROBOROS_DEFAULT_INTERRUPT_METADATA,
    ),
    "ouroboros_evolve_step": _OuroborosToolCapabilitySpec(
        execution_mode="blocking",
        companions=(
            "ouroboros_start_evolve_step",
            "ouroboros_lineage_status",
            "ouroboros_evolve_rewind",
            "ouroboros_ralph",
            "ouroboros_start_ralph",
            "ouroboros_job_status",
            "ouroboros_job_wait",
            "ouroboros_job_result",
            "ouroboros_cancel_job",
        ),
        side_effects=("workspace_write", "event_store_write"),
        retry=_OUROBOROS_DEFAULT_RETRY_METADATA,
        interrupt=_OUROBOROS_BLOCKING_INTERRUPT_METADATA,
    ),
    "ouroboros_start_evolve_step": _OuroborosToolCapabilitySpec(
        execution_mode="background",
        companions=(
            "ouroboros_start_execute_seed",
            "ouroboros_start_evaluate",
            "ouroboros_start_ralph",
            "ouroboros_job_status",
            "ouroboros_job_wait",
            "ouroboros_job_result",
            "ouroboros_cancel_job",
            "ouroboros_evolve_step",
            "ouroboros_lineage_status",
            "ouroboros_evolve_rewind",
            "ouroboros_ralph",
        ),
        side_effects=("workspace_write", "event_store_write"),
        retry=_OUROBOROS_JOB_POLL_RETRY_METADATA,
        interrupt=_OUROBOROS_BACKGROUND_INTERRUPT_METADATA,
    ),
    "ouroboros_ralph": _OuroborosToolCapabilitySpec(
        execution_mode="background",
        companions=(
            "ouroboros_evolve_step",
            "ouroboros_start_evolve_step",
            "ouroboros_lineage_status",
            "ouroboros_evolve_rewind",
            "ouroboros_start_ralph",
            "ouroboros_job_status",
            "ouroboros_job_wait",
            "ouroboros_job_result",
            "ouroboros_cancel_job",
        ),
        side_effects=("workspace_write", "event_store_write"),
        retry=_OUROBOROS_JOB_POLL_RETRY_METADATA,
        interrupt=_OUROBOROS_BACKGROUND_INTERRUPT_METADATA,
    ),
    "ouroboros_start_ralph": _OuroborosToolCapabilitySpec(
        execution_mode="background",
        companions=(
            "ouroboros_start_execute_seed",
            "ouroboros_start_evaluate",
            "ouroboros_start_evolve_step",
            "ouroboros_job_status",
            "ouroboros_job_wait",
            "ouroboros_job_result",
            "ouroboros_cancel_job",
            "ouroboros_evolve_step",
            "ouroboros_lineage_status",
            "ouroboros_evolve_rewind",
            "ouroboros_ralph",
        ),
        side_effects=("workspace_write", "event_store_write"),
        retry=_OUROBOROS_JOB_POLL_RETRY_METADATA,
        interrupt=_OUROBOROS_BACKGROUND_INTERRUPT_METADATA,
    ),
    "ouroboros_lineage_status": _OuroborosToolCapabilitySpec(
        execution_mode="status",
        companions=(
            "ouroboros_evolve_step",
            "ouroboros_start_evolve_step",
            "ouroboros_evolve_rewind",
            "ouroboros_ralph",
            "ouroboros_start_ralph",
        ),
        side_effects=_OUROBOROS_SIDE_EFFECT_FREE_METADATA,
        retry=_OUROBOROS_DEFAULT_RETRY_METADATA,
        interrupt=_OUROBOROS_READ_ONLY_INTERRUPT_METADATA,
        mutation_class=CapabilityMutationClass.READ_ONLY,
    ),
    "ouroboros_evolve_rewind": _OuroborosToolCapabilitySpec(
        execution_mode="blocking",
        companions=(
            "ouroboros_evolve_step",
            "ouroboros_start_evolve_step",
            "ouroboros_lineage_status",
            "ouroboros_ralph",
            "ouroboros_start_ralph",
        ),
        side_effects=("session_state_write",),
        retry=_OUROBOROS_DEFAULT_RETRY_METADATA,
        interrupt=_OUROBOROS_BLOCKING_INTERRUPT_METADATA,
    ),
    "ouroboros_cancel_execution": _OuroborosToolCapabilitySpec(
        execution_mode="cancel",
        companions=("ouroboros_execute_seed", "ouroboros_start_execute_seed"),
        side_effects=(
            "runtime_control",
            "event_store_write",
            "session_state_write",
        ),
        retry=_OUROBOROS_UNSUPPORTED_RETRY_METADATA,
        interrupt=_OUROBOROS_TERMINAL_CONTROL_INTERRUPT_METADATA_BY_TOOL[
            "ouroboros_cancel_execution"
        ],
    ),
    "ouroboros_brownfield": _OuroborosToolCapabilitySpec(
        execution_mode="blocking",
        companions=(
            "ouroboros_interview",
            "ouroboros_generate_seed",
            "ouroboros_pm_interview",
        ),
        side_effects=("workspace_write", "event_store_write"),
        retry=_OUROBOROS_DEFAULT_RETRY_METADATA,
        interrupt=_OUROBOROS_BLOCKING_INTERRUPT_METADATA,
    ),
    "ouroboros_pm_interview": _OuroborosToolCapabilitySpec(
        execution_mode="subagent_orchestration",
        companions=(
            "ouroboros_interview",
            "ouroboros_generate_seed",
            "ouroboros_brownfield",
        ),
        side_effects=("subagent_dispatch", "session_state_write"),
        retry=_OUROBOROS_DEFAULT_RETRY_METADATA,
        interrupt=_OUROBOROS_DEFAULT_INTERRUPT_METADATA,
    ),
    "ouroboros_qa": _OuroborosToolCapabilitySpec(
        execution_mode="blocking",
        companions=(
            "ouroboros_evaluate",
            "ouroboros_start_evaluate",
            "ouroboros_checklist_verify",
            "ouroboros_measure_drift",
        ),
        side_effects=("session_state_write",),
        retry=_OUROBOROS_DEFAULT_RETRY_METADATA,
        interrupt=_OUROBOROS_BLOCKING_INTERRUPT_METADATA,
    ),
}

_OUROBOROS_CANCEL_METADATA: Mapping[str, Mapping[str, Any]] = {
    "ouroboros_ac_tree_hud": _OUROBOROS_UNSUPPORTED_CANCEL_METADATA,
    "ouroboros_auto": _OUROBOROS_UNSUPPORTED_CANCEL_METADATA,
    "ouroboros_brownfield": _OUROBOROS_UNSUPPORTED_CANCEL_METADATA,
    "ouroboros_cancel_execution": _OUROBOROS_EXECUTION_SESSION_CANCEL_CONTROL_METADATA,
    "ouroboros_cancel_job": _OUROBOROS_BACKGROUND_JOB_CANCEL_CONTROL_METADATA,
    "ouroboros_checklist_verify": _OUROBOROS_UNSUPPORTED_CANCEL_METADATA,
    "ouroboros_evaluate": _OUROBOROS_UNSUPPORTED_CANCEL_METADATA,
    "ouroboros_evolve_rewind": _OUROBOROS_UNSUPPORTED_CANCEL_METADATA,
    "ouroboros_evolve_step": _OUROBOROS_UNSUPPORTED_CANCEL_METADATA,
    "ouroboros_execute_seed": _OUROBOROS_EXECUTION_SESSION_CANCEL_METADATA,
    "ouroboros_generate_seed": _OUROBOROS_UNSUPPORTED_CANCEL_METADATA,
    "ouroboros_interview": _OUROBOROS_UNSUPPORTED_CANCEL_METADATA,
    "ouroboros_job_result": _OUROBOROS_UNSUPPORTED_CANCEL_METADATA,
    "ouroboros_job_status": _OUROBOROS_UNSUPPORTED_CANCEL_METADATA,
    "ouroboros_job_wait": _OUROBOROS_UNSUPPORTED_CANCEL_METADATA,
    "ouroboros_lateral_think": _OUROBOROS_UNSUPPORTED_CANCEL_METADATA,
    "ouroboros_lineage_status": _OUROBOROS_UNSUPPORTED_CANCEL_METADATA,
    "ouroboros_measure_drift": _OUROBOROS_UNSUPPORTED_CANCEL_METADATA,
    "ouroboros_pm_interview": _OUROBOROS_UNSUPPORTED_CANCEL_METADATA,
    "ouroboros_qa": _OUROBOROS_UNSUPPORTED_CANCEL_METADATA,
    "ouroboros_query_events": _OUROBOROS_UNSUPPORTED_CANCEL_METADATA,
    "ouroboros_query_projection": _OUROBOROS_UNSUPPORTED_CANCEL_METADATA,
    "ouroboros_ralph": _OUROBOROS_BACKGROUND_JOB_CANCEL_METADATA,
    "ouroboros_session_status": _OUROBOROS_UNSUPPORTED_CANCEL_METADATA,
    "ouroboros_start_auto": _OUROBOROS_BACKGROUND_JOB_CANCEL_METADATA,
    "ouroboros_start_evaluate": _OUROBOROS_BACKGROUND_JOB_CANCEL_METADATA,
    "ouroboros_start_evolve_step": _OUROBOROS_BACKGROUND_JOB_CANCEL_METADATA,
    "ouroboros_start_execute_seed": _OUROBOROS_BACKGROUND_JOB_CANCEL_METADATA,
    "ouroboros_start_ralph": _OUROBOROS_BACKGROUND_JOB_CANCEL_METADATA,
}

_OUROBOROS_BACKGROUND_BLOCKING_COMPANIONS: Mapping[str, str] = {
    "ouroboros_start_auto": "ouroboros_auto",
    "ouroboros_start_evaluate": "ouroboros_evaluate",
    "ouroboros_start_evolve_step": "ouroboros_evolve_step",
    "ouroboros_start_execute_seed": "ouroboros_execute_seed",
}

_OUROBOROS_BACKGROUND_LIFECYCLE_ROLE_TOOLS: Mapping[str, str] = {
    "status": "ouroboros_job_status",
    "wait": "ouroboros_job_wait",
    "result": "ouroboros_job_result",
    "cancel": "ouroboros_cancel_job",
}
_OUROBOROS_JOB_LIFECYCLE_SIBLING_ORDER = (
    "status",
    "wait",
    "result",
    "cancel",
)


@lru_cache(maxsize=1)
def _ouroboros_tool_definitions_by_name() -> Mapping[str, MCPToolDefinition]:
    """Return the owned MCP tool definitions without invoking handlers."""
    from ouroboros.mcp.tools.definitions import get_ouroboros_tools

    return {handler.definition.name: handler.definition for handler in get_ouroboros_tools()}


def _is_ouroboros_owned_tool(tool: MCPToolDefinition) -> bool:
    return tool.name in _ouroboros_tool_definitions_by_name()


def _is_ouroboros_owned_tool_name(tool_name: str) -> bool:
    return tool_name in _ouroboros_tool_definitions_by_name()


def _spec_for_ouroboros_tool_name(name: str) -> _OuroborosToolCapabilitySpec:
    try:
        return _OUROBOROS_TOOL_CAPABILITY_SPECS[name]
    except KeyError as exc:
        raise RuntimeError(
            "Ouroboros MCP capability registry has no explicit spec for "
            f"{name}; retry metadata must be defined per owned tool"
        ) from exc


def _ouroboros_companions_for_tool(name: str) -> tuple[str, ...]:
    return _spec_for_ouroboros_tool_name(name).companions


def _available_ouroboros_tool_names() -> frozenset[str]:
    return frozenset(_ouroboros_tool_definitions_by_name())


def _filter_available_ouroboros_tools(tool_names: Sequence[str]) -> tuple[str, ...]:
    available = _available_ouroboros_tool_names()
    return tuple(tool_name for tool_name in tool_names if tool_name in available)


def _available_job_lifecycle_sibling_roles() -> Mapping[str, str]:
    """Return available generic job lifecycle siblings in stable role order."""
    definitions = _ouroboros_tool_definitions_by_name()
    sibling_roles = {
        role: tool_name
        for role, tool_name in _OUROBOROS_BACKGROUND_LIFECYCLE_ROLE_TOOLS.items()
        if tool_name in definitions
    }
    return {
        role: sibling_roles[role]
        for role in _OUROBOROS_JOB_LIFECYCLE_SIBLING_ORDER
        if role in sibling_roles
    }


def _derived_ouroboros_companions_for_tool(name: str) -> tuple[str, ...]:
    """Return catalog-derived companions layered on top of explicit specs."""
    companions = list(_filter_available_ouroboros_tools(_ouroboros_companions_for_tool(name)))
    sibling_roles = _available_job_lifecycle_sibling_roles()
    if name in sibling_roles.values():
        for sibling_name in sibling_roles.values():
            if sibling_name != name and sibling_name not in companions:
                companions.append(sibling_name)
    return tuple(companions)


def _ouroboros_execution_mode(name: str) -> str:
    return _spec_for_ouroboros_tool_name(name).execution_mode


def extract_capability_input_schema(tool: MCPToolDefinition) -> dict[str, Any]:
    """Convert an MCP tool definition into capability input_schema metadata."""
    schema = tool.to_input_schema()
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    return {
        "type": schema.get("type", "object"),
        "properties": {
            str(name): dict(property_schema)
            if isinstance(property_schema, Mapping)
            else property_schema
            for name, property_schema in (
                properties.items() if isinstance(properties, Mapping) else ()
            )
        },
        "required": list(required) if isinstance(required, Sequence) else [],
    }


def _input_schema_for_ouroboros_tool(tool: MCPToolDefinition) -> Mapping[str, Any]:
    """Return the owned-tool input schema, defaulting to the live descriptor."""
    canonical_tool = _ouroboros_tool_definitions_by_name().get(tool.name, tool)
    return extract_capability_input_schema(canonical_tool)


def mcp_tool_required_parameter_keys(tool: MCPToolDefinition) -> tuple[str, ...]:
    """Return required input parameter keys from an MCP tool definition.

    This is the schema-derived extraction point for unknown or external MCP
    tools. Ouroboros-owned tools use explicit context metadata, but tests still
    compare that metadata against this source-of-truth extraction.
    """
    return tuple(parameter.name for parameter in tool.parameters if parameter.required)


def _required_parameter_names(tool: MCPToolDefinition) -> tuple[str, ...]:
    return mcp_tool_required_parameter_keys(tool)


def _merge_required_context_keys(
    required_parameter_keys: Sequence[str],
    observed_skill_context_keys: Sequence[str],
) -> tuple[str, ...]:
    """Return definition-required plus skill-observed context keys.

    Required MCP parameters stay first in definition order. Skill usage keys
    are appended in discovery order only when they add new runtime context.
    """
    merged: list[str] = []
    for raw_key in (*required_parameter_keys, *observed_skill_context_keys):
        key = raw_key.strip() if isinstance(raw_key, str) else ""
        if key and key not in merged:
            merged.append(key)
    return tuple(merged)


def _required_context_keys_for_ouroboros_tool(tool: MCPToolDefinition) -> tuple[str, ...]:
    """Return owned-tool context keys from MCP definitions and skill usage."""
    canonical_tool = _ouroboros_tool_definitions_by_name().get(tool.name, tool)
    skill_context_keys = get_packaged_skill_context_keys().get(tool.name, ())
    return _merge_required_context_keys(
        mcp_tool_required_parameter_keys(canonical_tool),
        skill_context_keys,
    )


def _side_effects_for_ouroboros_tool(name: str) -> tuple[str, ...]:
    return _spec_for_ouroboros_tool_name(name).side_effects


def _resolved_side_effects_for_ouroboros_tool(name: str) -> tuple[str, ...]:
    side_effects = _side_effects_for_ouroboros_tool(name)
    return side_effects or ("session_state_write",)


def _mutation_targets_for_side_effects(side_effects: Sequence[str]) -> tuple[str, ...]:
    targets: list[str] = []
    for side_effect in side_effects:
        for target in _OUROBOROS_MUTATION_TARGETS_BY_SIDE_EFFECT.get(
            side_effect,
            ("unknown",),
        ):
            if target and target not in targets:
                targets.append(target)
    return tuple(targets)


def _mutation_targets_for_state_mutations(
    base_targets: Sequence[str],
    state_mutations: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    targets = list(base_targets)
    for mutation in state_mutations:
        target = mutation.get("target")
        if isinstance(target, str) and target and target not in targets:
            targets.append(target)
    return tuple(targets)


def _state_mutations_for_ouroboros_tool(name: str) -> tuple[Mapping[str, Any], ...]:
    """Return explicit state mutation records for an owned MCP tool."""
    mutations = _OUROBOROS_STATE_MUTATIONS_BY_TOOL.get(name, ())
    return tuple(
        {
            **dict(mutation),
            "context_keys": tuple(mutation.get("context_keys", ())),
        }
        for mutation in mutations
    )


def _retry_metadata_for_ouroboros_tool(name: str) -> Mapping[str, Any]:
    return dict(_spec_for_ouroboros_tool_name(name).retry)


def _interrupt_metadata_for_ouroboros_tool(name: str) -> Mapping[str, Any]:
    metadata = dict(_spec_for_ouroboros_tool_name(name).interrupt)
    if metadata.get("execution_mode") == "blocking":
        background_companions = tuple(
            start_tool
            for start_tool, blocking_tool in _OUROBOROS_BACKGROUND_BLOCKING_COMPANIONS.items()
            if blocking_tool == name and start_tool in _available_ouroboros_tool_names()
        )
        return {
            **metadata,
            "background_companions": background_companions,
            "target_context_keys": tuple(metadata.get("target_context_keys", ())),
        }

    if metadata.get("mode") != "resumable_background_job":
        return metadata

    resume_companions = _filter_available_ouroboros_tools(metadata.get("resume_companions", ()))
    cancel_companions = _filter_available_ouroboros_tools(metadata.get("cancel_companions", ()))
    return {
        **metadata,
        "resumable": bool(resume_companions),
        "cancellable": bool(cancel_companions),
        "resume_companions": resume_companions,
        "cancel_companions": cancel_companions,
        "target_context_keys": tuple(metadata.get("target_context_keys", ())),
    }


def _cancel_metadata_for_ouroboros_tool(name: str) -> Mapping[str, Any]:
    metadata = _OUROBOROS_CANCEL_METADATA.get(name, _OUROBOROS_UNSUPPORTED_CANCEL_METADATA)
    raw_companions = metadata.get("companions", ())
    companions = _filter_available_ouroboros_tools(raw_companions)
    if metadata.get("supported") and not companions:
        metadata = _OUROBOROS_UNSUPPORTED_CANCEL_METADATA
        companions = ()
    return {
        key: companions
        if key == "companions"
        else tuple(value)
        if isinstance(value, tuple)
        else value
        for key, value in metadata.items()
    }


def _interview_code_investigation_request_schema() -> dict[str, Any]:
    """Return the runtime request model for interview code-fact investigation."""
    target_schema: dict[str, Any] = {
        "type": "object",
        "oneOf": [
            {
                "title": "WorkspaceTarget",
                "additionalProperties": False,
                "required": ["target_type", "scope"],
                "properties": {
                    "target_type": {"const": "workspace"},
                    "scope": {
                        "type": "string",
                        "enum": ["active", "selected_repositories", "all_available"],
                    },
                },
            },
            {
                "title": "RelativePathTarget",
                "additionalProperties": False,
                "required": ["target_type", "path"],
                "properties": {
                    "target_type": {"const": "relative_path"},
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Repository-relative file or directory path.",
                    },
                },
            },
            {
                "title": "GlobTarget",
                "additionalProperties": False,
                "required": ["target_type", "pattern"],
                "properties": {
                    "target_type": {"const": "glob"},
                    "pattern": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Repository-relative glob pattern.",
                    },
                },
            },
            {
                "title": "SymbolTarget",
                "additionalProperties": False,
                "required": ["target_type", "name"],
                "properties": {
                    "target_type": {"const": "symbol"},
                    "name": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Function, class, module, command, or config symbol to locate.",
                    },
                    "path_hint": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Optional repository-relative search hint.",
                    },
                },
            },
        ],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "session_id",
            "question_identity",
            "question",
            "investigation_goal",
            "investigation_targets",
            "fact_categories",
            "allowed_capabilities",
            "repo_inspection_tool_capabilities",
            "confidence_policy",
            "answer_prefixes",
            "answer_contract",
            "mcp_tool_capability",
        ],
        "properties": {
            "session_id": {
                "type": "string",
                "description": "Current Ouroboros interview session ID.",
            },
            "question_identity": {
                "type": "string",
                "pattern": r"^interview-question:[0-9a-f]{16}$",
                "description": (
                    "Stable identity derived from the originating interview "
                    "question using stable_code_investigation_question_identity()."
                ),
            },
            "question": {
                "type": "string",
                "description": "The MCP-generated interview question requiring code facts.",
            },
            "last_question": {
                "type": "string",
                "description": "Previously asked question text, when available.",
            },
            "investigation_goal": {
                "type": "string",
                "enum": ["describe_current_state_from_code"],
                "description": "Code investigation is descriptive only; decisions route to the user.",
            },
            "investigation_targets": {
                "type": "array",
                "minItems": 1,
                "items": target_schema,
                "description": "Repository-agnostic descriptors for the code facts to inspect.",
            },
            "fact_categories": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "string",
                    "enum": [
                        "tech_stack",
                        "frameworks",
                        "dependencies",
                        "current_patterns",
                        "architecture",
                        "file_structure",
                        "configuration",
                    ],
                },
            },
            "allowed_capabilities": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "enum": ["inspect_code"]},
                "description": "Runtime capability used for local code facts.",
            },
            "repo_inspection_tool_capabilities": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": True,
                    "required": [
                        "tool_name",
                        "stable_id",
                        "source_kind",
                        "source_name",
                        "input_schema",
                        "mutation_class",
                        "parallel_safety",
                        "interruptibility",
                        "approval_class",
                        "origin",
                        "scope",
                        "execution_mode",
                        "logical_capability",
                        "side_effects",
                        "fallback_used",
                    ],
                    "properties": {
                        "tool_name": {"type": "string", "enum": ["Read", "Glob", "Grep"]},
                        "source_kind": {"const": "builtin"},
                        "execution_mode": {"const": "repo_inspection"},
                        "logical_capability": {"const": "inspect_code"},
                        "fallback_used": {"const": False},
                    },
                },
                "description": (
                    "Concrete runtime repo-inspection tools a code-fact "
                    "subagent can use to satisfy allowed_capabilities=inspect_code."
                ),
            },
            "confidence_policy": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "auto_confirm_when",
                    "confirmation_required_when",
                    "human_judgment_when",
                ],
                "properties": {
                    "auto_confirm_when": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "confirmation_required_when": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "human_judgment_when": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
            "answer_prefixes": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "string",
                    "enum": ["[from-code]", "[from-code][auto-confirmed]"],
                },
            },
            "answer_contract": {
                "const": _interview_code_investigation_answer_contract(),
                "description": "Exact response contract attached to this investigation request.",
            },
            "mcp_tool_capability": {
                "type": "object",
                "additionalProperties": True,
                "required": [
                    "tool_name",
                    "stable_id",
                    "source_kind",
                    "source_name",
                    "input_schema",
                    "mutation_class",
                    "execution_mode",
                    "companions",
                    "required_context_keys",
                    "mutation_targets",
                    "state_mutations",
                    "side_effects",
                    "retry",
                    "interrupt",
                    "cancel",
                    "fallback_used",
                    "orchestration",
                ],
                "properties": {
                    "tool_name": {"const": "ouroboros_interview"},
                    "fallback_used": {"const": False},
                },
                "description": (
                    "Explicit Ouroboros-owned MCP capability metadata for the "
                    "tool that emitted this investigation request."
                ),
            },
        },
    }


def _interview_code_investigation_answer_contract() -> dict[str, Any]:
    """Return the answer contract for one code-fact investigation request."""
    answer_schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "session_id",
            "question_identity",
            "answer_prefix",
            "answer_text",
            "confidence",
            "evidence",
            "requires_user_confirmation",
        ],
        "properties": {
            "session_id": {
                "type": "string",
                "description": "Current Ouroboros interview session ID.",
            },
            "question_identity": {
                "type": "string",
                "pattern": r"^interview-question:[0-9a-f]{16}$",
                "description": "Matches the originating code investigation request.",
            },
            "answer_prefix": {
                "type": "string",
                "enum": ["[from-code]", "[from-code][auto-confirmed]"],
                "description": "Prefix to prepend when forwarding the answer to interview MCP.",
            },
            "answer_text": {
                "type": "string",
                "minLength": 1,
                "description": "Concise descriptive fact answer without prescription.",
            },
            "confidence": {
                "type": "string",
                "enum": ["high_exact_match", "medium_inferred", "low_uncertain"],
            },
            "evidence": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["source", "claim"],
                    "properties": {
                        "source": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Repository-relative file, symbol, or manifest source.",
                        },
                        "claim": {
                            "type": "string",
                            "minLength": 1,
                            "description": "The factual claim supported by this evidence.",
                        },
                        "locator": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Optional line, key, dependency, or symbol locator.",
                        },
                    },
                },
            },
            "requires_user_confirmation": {
                "type": "boolean",
                "description": "True when the answer must be confirmed before forwarding.",
            },
            "user_confirmation_prompt": {
                "type": "string",
                "minLength": 1,
                "description": "Prompt text to show when confirmation is required.",
            },
        },
        "allOf": [
            {
                "if": {
                    "properties": {"answer_prefix": {"const": "[from-code][auto-confirmed]"}},
                    "required": ["answer_prefix"],
                },
                "then": {
                    "properties": {
                        "confidence": {"const": "high_exact_match"},
                        "requires_user_confirmation": {"const": False},
                    }
                },
            },
            {
                "if": {
                    "properties": {"requires_user_confirmation": {"const": True}},
                    "required": ["requires_user_confirmation"],
                },
                "then": {"required": ["user_confirmation_prompt"]},
            },
            {
                "if": {
                    "properties": {"answer_prefix": {"const": "[from-code]"}},
                    "required": ["answer_prefix"],
                },
                "then": {
                    "properties": {"requires_user_confirmation": {"const": True}},
                    "required": ["user_confirmation_prompt"],
                },
            },
        ],
    }
    return {
        "contract_id": "code_fact_investigation_answer.v1",
        "scope": "single_code_fact_investigation_request",
        "response_model_schema": answer_schema,
        "prefix_semantics": {
            "[from-code][auto-confirmed]": {
                "confidence": "high_exact_match",
                "requires_user_confirmation": False,
                "forwarding": "send_to_mcp_immediately",
            },
            "[from-code]": {
                "confidence": "medium_or_low",
                "requires_user_confirmation": True,
                "forwarding": "confirm_with_user_before_mcp",
            },
        },
        "evidence_policy": {
            "minimum_items": 1,
            "source_format": "repository_relative_path_or_symbol",
            "server_local_paths_allowed": False,
        },
        "runtime_instruction": (
            "Produce exactly one structured answer payload for the originating "
            "question_identity. Use [from-code][auto-confirmed] only for an "
            "unambiguous manifest/config exact match; otherwise require user "
            "confirmation and use [from-code] after confirmation."
        ),
    }


def interview_code_investigation_answer_contract() -> dict[str, Any]:
    """Return the public code-fact answer contract for generated requests."""
    return _interview_code_investigation_answer_contract()


def _code_investigation_repo_inspection_tool_capabilities() -> tuple[dict[str, Any], ...]:
    """Return concrete repo-inspection tool capabilities for code-fact subagents."""
    tool_schemas: Mapping[str, Mapping[str, Any]] = {
        "Read": {
            "type": "object",
            "additionalProperties": True,
            "required": ["file_path"],
            "properties": {
                "file_path": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Repository-local file path to inspect.",
                },
                "offset": {"type": "integer", "minimum": 1},
                "limit": {"type": "integer", "minimum": 1},
            },
        },
        "Glob": {
            "type": "object",
            "additionalProperties": True,
            "required": ["pattern"],
            "properties": {
                "pattern": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Repository-local glob pattern to enumerate.",
                },
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Optional repository-local search root.",
                },
            },
        },
        "Grep": {
            "type": "object",
            "additionalProperties": True,
            "required": ["pattern"],
            "properties": {
                "pattern": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Search pattern for repository-local evidence.",
                },
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Optional repository-local file or directory scope.",
                },
                "glob": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Optional file glob narrowing the search.",
                },
            },
        },
    }
    capabilities: list[dict[str, Any]] = []
    for tool_name in ("Read", "Glob", "Grep"):
        semantics = _BUILTIN_SEMANTICS[tool_name]
        capabilities.append(
            {
                "tool_name": tool_name,
                "stable_id": f"builtin:{tool_name}",
                "source_kind": "builtin",
                "source_name": "built-in",
                "input_schema": dict(tool_schemas[tool_name]),
                "mutation_class": semantics.mutation_class.value,
                "parallel_safety": semantics.parallel_safety.value,
                "interruptibility": semantics.interruptibility.value,
                "approval_class": semantics.approval_class.value,
                "origin": semantics.origin.value,
                "scope": semantics.scope.value,
                "execution_mode": "repo_inspection",
                "logical_capability": "inspect_code",
                "side_effects": ["side_effect_free"],
                "fallback_used": False,
            }
        )
    return tuple(capabilities)


def _interview_question_advisory_request_schema() -> dict[str, Any]:
    """Return the runtime request model for per-question answer assistance."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "session_id",
            "question_identity",
            "question",
            "phase",
            "user_question_first",
            "advisory_goal",
            "parallel_preference",
            "sequential_fallback",
            "allowed_capabilities",
            "lanes",
            "synthesis_contract",
            "mcp_tool_capability",
        ],
        "properties": {
            "session_id": {
                "type": "string",
                "description": "Current Ouroboros interview session ID.",
            },
            "question_identity": {
                "type": "string",
                "pattern": r"^interview-question:[0-9a-f]{16}$",
                "description": (
                    "Stable identity derived from the originating interview "
                    "question using stable_code_investigation_question_identity()."
                ),
            },
            "question": {
                "type": "string",
                "minLength": 1,
                "description": "The already user-visible MCP interview question.",
            },
            "last_question": {
                "type": "string",
                "description": "Previously asked question text, when available.",
            },
            "phase": {
                "type": "string",
                "enum": ["start", "resume_pending", "answer"],
            },
            "ambiguity_score": {
                "type": ["number", "null"],
                "minimum": 0,
                "maximum": 1,
            },
            "milestone": {
                "type": ["string", "null"],
                "enum": ["initial", "progress", "refined", "ready", None],
            },
            "user_question_first": {
                "const": True,
                "description": (
                    "The parent runtime must surface the interview question before "
                    "or while advisory fanout runs; advisory must never hide the "
                    "question behind background research."
                ),
            },
            "advisory_goal": {
                "const": "help_human_answer_interview_question",
                "description": (
                    "Generate concise answer options, uncertainty notes, and a "
                    "recommended draft without mutating interview state."
                ),
            },
            "parallel_preference": {
                "const": "parallel_when_runtime_supports_subagents",
            },
            "sequential_fallback": {
                "type": "object",
                "additionalProperties": False,
                "required": ["supported", "mode", "trigger"],
                "properties": {
                    "supported": {"const": True},
                    "mode": {"const": "sequential_advisory_lane_dispatch"},
                    "trigger": {"const": "runtime_has_no_native_parallel_subagent_primitive"},
                },
            },
            "allowed_capabilities": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "string",
                    "enum": ["inspect_code", "web_research", "run_lateral_review"],
                },
            },
            "lanes": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["lane_id", "purpose", "capability", "required"],
                    "properties": {
                        "lane_id": {
                            "type": "string",
                            "enum": [
                                "code_context",
                                "web_context",
                                "ambiguity_contrarian",
                                "answer_simplifier",
                                "architecture_implications",
                            ],
                        },
                        "purpose": {"type": "string", "minLength": 1},
                        "capability": {
                            "type": "string",
                            "enum": ["inspect_code", "web_research", "run_lateral_review"],
                        },
                        "persona": {
                            "type": "string",
                            "enum": ["researcher", "contrarian", "simplifier", "architect"],
                        },
                        "required": {"type": "boolean"},
                    },
                },
            },
            "code_investigation_request": {
                "type": "object",
                "additionalProperties": True,
                "description": (
                    "Optional code-fact request emitted alongside this advisory; "
                    "reuse it for the code_context lane when present."
                ),
            },
            "synthesis_contract": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "output_shape",
                    "max_options",
                    "include_recommended_draft",
                    "preserve_user_agency",
                    "forward_to_mcp_only_after_user_or_auto_confirm",
                ],
                "properties": {
                    "output_shape": {
                        "const": "answer_advisory",
                    },
                    "max_options": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 5,
                    },
                    "include_recommended_draft": {"type": "boolean"},
                    "preserve_user_agency": {"const": True},
                    "forward_to_mcp_only_after_user_or_auto_confirm": {"const": True},
                },
            },
            "mcp_tool_capability": {
                "type": "object",
                "additionalProperties": True,
                "required": [
                    "tool_name",
                    "stable_id",
                    "source_kind",
                    "source_name",
                    "input_schema",
                    "mutation_class",
                    "execution_mode",
                    "companions",
                    "required_context_keys",
                    "mutation_targets",
                    "state_mutations",
                    "side_effects",
                    "retry",
                    "interrupt",
                    "cancel",
                    "fallback_used",
                    "orchestration",
                ],
                "properties": {
                    "tool_name": {"const": "ouroboros_interview"},
                    "fallback_used": {"const": False},
                },
            },
        },
    }


def _interview_question_advisory_fanout_metadata() -> dict[str, Any]:
    """Return structured metadata for parent-session interview answer help."""
    lanes = [
        {
            "lane_id": "code_context",
            "purpose": "Find repo-local facts that may answer or constrain the question.",
            "capability": "inspect_code",
            "required": False,
        },
        {
            "lane_id": "web_context",
            "purpose": (
                "Check current external facts only when the question depends on "
                "third-party APIs, pricing, standards, security, or recent changes."
            ),
            "capability": "web_research",
            "required": False,
        },
        {
            "lane_id": "ambiguity_contrarian",
            "purpose": "Name hidden assumptions, missing decisions, and risky vague words.",
            "capability": "run_lateral_review",
            "persona": "contrarian",
            "required": True,
        },
        {
            "lane_id": "answer_simplifier",
            "purpose": "Turn the question into easy choices or a concise answer draft.",
            "capability": "run_lateral_review",
            "persona": "simplifier",
            "required": True,
        },
        {
            "lane_id": "architecture_implications",
            "purpose": (
                "Check whether the answer would change system shape, ownership, "
                "interfaces, or rollout strategy."
            ),
            "capability": "run_lateral_review",
            "persona": "architect",
            "required": False,
        },
    ]
    return {
        "contract_id": "interview_question_advisory_fanout.v1",
        "mcp_tool": "ouroboros_interview",
        "companion_tool": "ouroboros_lateral_think",
        "dispatch_timing": "after_question_is_visible_to_user",
        "parallel_preference": "parallel_when_runtime_supports_subagents",
        "sequential_fallback": {
            "supported": True,
            "mode": "sequential_advisory_lane_dispatch",
            "trigger": "runtime_has_no_native_parallel_subagent_primitive",
        },
        "request_model_schema": _interview_question_advisory_request_schema(),
        "lanes": lanes,
        "synthesis_contract": {
            "output_shape": "answer_advisory",
            "max_options": 3,
            "include_recommended_draft": True,
            "preserve_user_agency": True,
            "forward_to_mcp_only_after_user_or_auto_confirm": True,
        },
        "response_payload_refs": {
            "plugin": "parent_runtime.ouroboros_dispatch.children",
            "result_correlation_key": "lane_id",
            "requires_prose_parsing": False,
            "synthesis_owner": "parent_session",
        },
        "runtime_instruction": (
            "Show the MCP interview question to the user first, then fan out "
            "advisory lanes for code context, current web facts when needed, "
            "ambiguity critique, simplification, and architecture implications. "
            "Read child task results as they complete and synthesize them into "
            "two or three answer options or one recommended draft. Do not forward advisory text to "
            "ouroboros_interview until the user approves, edits, or explicitly "
            "chooses auto-confirm."
        ),
    }


def _serialized_metadata_for_descriptor(
    descriptor: CapabilityDescriptor,
) -> dict[str, Any] | None:
    if descriptor.metadata is None:
        return None
    return {
        "input_schema": dict(descriptor.metadata.input_schema),
        "mutation_class": descriptor.metadata.mutation_class,
        "execution_mode": descriptor.metadata.execution_mode,
        "companions": list(descriptor.metadata.companions),
        "required_context_keys": list(descriptor.metadata.required_context_keys),
        "mutation_targets": list(descriptor.metadata.mutation_targets),
        "state_mutations": [
            {
                key: list(value) if isinstance(value, tuple) else value
                for key, value in mutation.items()
            }
            for mutation in descriptor.metadata.state_mutations
        ],
        "side_effects": list(descriptor.metadata.side_effects),
        "retry": dict(descriptor.metadata.retry),
        "interrupt": {
            key: list(value) if isinstance(value, tuple) else value
            for key, value in descriptor.metadata.interrupt.items()
        },
        "cancel": {
            key: list(value) if isinstance(value, tuple) else value
            for key, value in descriptor.metadata.cancel.items()
        },
        "fallback_used": descriptor.metadata.fallback_used,
        "orchestration": dict(descriptor.metadata.orchestration),
    }


_REQUIRED_TOOL_METADATA_FIELDS = frozenset(
    {
        "input_schema",
        "mutation_class",
        "execution_mode",
        "companions",
        "required_context_keys",
        "mutation_targets",
        "state_mutations",
        "side_effects",
        "retry",
        "interrupt",
        "cancel",
        "fallback_used",
        "orchestration",
    }
)


def _string_tuple_from_payload(value: Any) -> tuple[str, ...]:
    if isinstance(value, Sequence) and not isinstance(value, str):
        return tuple(str(item) for item in value if isinstance(item, str))
    return ()


def _normalize_tuple_fields(
    raw: Mapping[str, Any],
    tuple_fields: frozenset[str],
) -> dict[str, Any]:
    """Return a shallow metadata mapping with selected sequence fields as tuples."""
    return {
        key: _string_tuple_from_payload(value) if key in tuple_fields else value
        for key, value in raw.items()
    }


def validate_capability_tool_metadata(
    metadata: CapabilityToolMetadata,
    *,
    tool_name: str = "<unknown>",
    owned_tool: bool = False,
) -> None:
    """Validate the runtime-facing tool-specific metadata contract.

    Owned Ouroboros MCP tools must carry explicit metadata instead of inferred
    fallback fields. Generic attached MCP metadata may still validate as a
    fallback contract, but callers can set ``owned_tool=True`` to reject it.
    """
    if not isinstance(metadata.input_schema, Mapping):
        raise ValueError(f"{tool_name}: input_schema must be a mapping")
    if metadata.input_schema.get("type") != "object":
        raise ValueError(f"{tool_name}: input_schema.type must be object")
    if not isinstance(metadata.input_schema.get("properties"), Mapping):
        raise ValueError(f"{tool_name}: input_schema.properties must be a mapping")
    required = metadata.input_schema.get("required")
    if not isinstance(required, Sequence) or isinstance(required, str):
        raise ValueError(f"{tool_name}: input_schema.required must be a sequence")

    try:
        mutation_class = CapabilityMutationClass(str(metadata.mutation_class))
    except ValueError as exc:
        raise ValueError(f"{tool_name}: invalid mutation_class") from exc

    if not metadata.execution_mode:
        raise ValueError(f"{tool_name}: execution_mode is required")
    if not isinstance(metadata.companions, tuple) or not all(
        isinstance(companion, str) for companion in metadata.companions
    ):
        raise ValueError(f"{tool_name}: companions must be a tuple of strings")
    if not isinstance(metadata.required_context_keys, tuple) or not all(
        isinstance(key, str) for key in metadata.required_context_keys
    ):
        raise ValueError(f"{tool_name}: required_context_keys must be a tuple of strings")
    if not isinstance(metadata.mutation_targets, tuple) or not all(
        isinstance(target, str) for target in metadata.mutation_targets
    ):
        raise ValueError(f"{tool_name}: mutation_targets must be a tuple of strings")
    if not isinstance(metadata.state_mutations, tuple) or not all(
        isinstance(mutation, Mapping) for mutation in metadata.state_mutations
    ):
        raise ValueError(f"{tool_name}: state_mutations must be a tuple of mappings")
    if not isinstance(metadata.side_effects, tuple) or not all(
        isinstance(effect, str) for effect in metadata.side_effects
    ):
        raise ValueError(f"{tool_name}: side_effects must be a tuple of strings")
    if mutation_class is not CapabilityMutationClass.READ_ONLY and not metadata.side_effects:
        raise ValueError(f"{tool_name}: mutating capabilities require explicit side_effects")
    if mutation_class is not CapabilityMutationClass.READ_ONLY and not metadata.mutation_targets:
        raise ValueError(f"{tool_name}: mutating capabilities require explicit mutation_targets")
    for mutation in metadata.state_mutations:
        if set(mutation) != {"target", "operation", "side_effect", "context_keys"}:
            raise ValueError(
                f"{tool_name}: each state mutation must contain target, "
                "operation, side_effect, and context_keys"
            )
        if not all(
            isinstance(mutation[key], str) and mutation[key]
            for key in ("target", "operation", "side_effect")
        ):
            raise ValueError(
                f"{tool_name}: state mutation target, operation, and side_effect "
                "must be non-empty strings"
            )
        if not isinstance(mutation["context_keys"], tuple) or not all(
            isinstance(key, str) for key in mutation["context_keys"]
        ):
            raise ValueError(f"{tool_name}: state mutation context_keys must be a tuple of strings")
        if mutation["target"] not in metadata.mutation_targets:
            raise ValueError(
                f"{tool_name}: state mutation target must be listed in mutation_targets"
            )
        if mutation["side_effect"] not in metadata.side_effects:
            raise ValueError(
                f"{tool_name}: state mutation side_effect must be listed in side_effects"
            )

    if set(metadata.retry) != {"supported", "mode"}:
        raise ValueError(f"{tool_name}: retry must contain supported and mode")
    if (
        not isinstance(metadata.retry["supported"], bool)
        or not isinstance(metadata.retry["mode"], str)
        or not metadata.retry["mode"]
    ):
        raise ValueError(f"{tool_name}: retry has invalid supported or mode")

    interrupt_keys = set(metadata.interrupt)
    if metadata.interrupt.get("execution_mode") == "blocking":
        expected_interrupt_keys = {
            "supported",
            "mode",
            "execution_mode",
            "blocking_semantics",
            "resumable",
            "background_companions",
            "target_context_keys",
        }
        if interrupt_keys != expected_interrupt_keys:
            raise ValueError(
                f"{tool_name}: blocking interrupt must contain supported, mode, "
                "execution_mode, blocking_semantics, resumable, "
                "background_companions, and target_context_keys"
            )
        if metadata.interrupt["execution_mode"] != "blocking":
            raise ValueError(f"{tool_name}: blocking interrupt execution_mode mismatch")
        if (
            not isinstance(metadata.interrupt["blocking_semantics"], str)
            or not metadata.interrupt["blocking_semantics"]
        ):
            raise ValueError(f"{tool_name}: blocking interrupt requires blocking_semantics")
        if not isinstance(metadata.interrupt["resumable"], bool):
            raise ValueError(f"{tool_name}: blocking interrupt resumable must be a bool")
        if not isinstance(metadata.interrupt["background_companions"], tuple):
            raise ValueError(
                f"{tool_name}: blocking interrupt background_companions must be a tuple"
            )
        if not isinstance(metadata.interrupt["target_context_keys"], tuple):
            raise ValueError(f"{tool_name}: blocking interrupt target_context_keys must be a tuple")
    elif metadata.interrupt.get("mode") == "resumable_background_job":
        expected_interrupt_keys = {
            "supported",
            "mode",
            "resumable",
            "cancellable",
            "resume_companions",
            "cancel_companions",
            "target_context_keys",
        }
        if interrupt_keys != expected_interrupt_keys:
            raise ValueError(
                f"{tool_name}: background interrupt must contain "
                "supported, mode, resumable, cancellable, resume_companions, "
                "cancel_companions, and target_context_keys"
            )
        if not isinstance(metadata.interrupt["resumable"], bool) or not isinstance(
            metadata.interrupt["cancellable"], bool
        ):
            raise ValueError(
                f"{tool_name}: background interrupt resumable/cancellable must be booleans"
            )
        if not isinstance(metadata.interrupt["resume_companions"], tuple):
            raise ValueError(f"{tool_name}: background interrupt resume_companions must be a tuple")
        if not isinstance(metadata.interrupt["cancel_companions"], tuple):
            raise ValueError(f"{tool_name}: background interrupt cancel_companions must be a tuple")
        if not isinstance(metadata.interrupt["target_context_keys"], tuple):
            raise ValueError(
                f"{tool_name}: background interrupt target_context_keys must be a tuple"
            )
        if metadata.interrupt["resumable"] and not metadata.interrupt["resume_companions"]:
            raise ValueError(
                f"{tool_name}: resumable background interrupt requires resume companions"
            )
        if metadata.interrupt["cancellable"] and not metadata.interrupt["cancel_companions"]:
            raise ValueError(
                f"{tool_name}: cancellable background interrupt requires cancel companions"
            )
        if not metadata.interrupt["target_context_keys"]:
            raise ValueError(f"{tool_name}: background interrupt requires target context keys")
    elif metadata.interrupt.get("mode") == "read_only_non_mutating":
        expected_interrupt_keys = {
            "supported",
            "mode",
            "mutation_semantics",
            "resumable",
            "target_context_keys",
        }
        if interrupt_keys != expected_interrupt_keys:
            raise ValueError(
                f"{tool_name}: read-only interrupt must contain supported, mode, "
                "mutation_semantics, resumable, and target_context_keys"
            )
        if metadata.interrupt["supported"] is not True:
            raise ValueError(f"{tool_name}: read-only interrupt must be supported")
        if metadata.interrupt["mutation_semantics"] != "no_state_mutation":
            raise ValueError(f"{tool_name}: read-only interrupt must declare no_state_mutation")
        if metadata.interrupt["resumable"] is not False:
            raise ValueError(f"{tool_name}: read-only interrupt is not resumable")
        if not isinstance(metadata.interrupt["target_context_keys"], tuple):
            raise ValueError(
                f"{tool_name}: read-only interrupt target_context_keys must be a tuple"
            )
        if metadata.interrupt["target_context_keys"]:
            raise ValueError(f"{tool_name}: read-only interrupt must not require context keys")
    elif metadata.interrupt.get("mode") == "terminal_control":
        expected_interrupt_keys = {
            "supported",
            "mode",
            "terminal_action",
            "target_type",
            "target_context_keys",
            "directive_semantics",
            "terminal_statuses",
            "idempotent",
        }
        if interrupt_keys != expected_interrupt_keys:
            raise ValueError(
                f"{tool_name}: terminal-control interrupt must contain supported, "
                "mode, terminal_action, target_type, target_context_keys, "
                "directive_semantics, terminal_statuses, and idempotent"
            )
        if metadata.interrupt["supported"] is not True:
            raise ValueError(f"{tool_name}: terminal-control interrupt must be supported")
        if metadata.interrupt["terminal_action"] != "cancel":
            raise ValueError(f"{tool_name}: terminal-control interrupt action must be cancel")
        if (
            not isinstance(metadata.interrupt["target_type"], str)
            or not metadata.interrupt["target_type"]
        ):
            raise ValueError(f"{tool_name}: terminal-control interrupt requires target_type")
        if (
            not isinstance(metadata.interrupt["target_context_keys"], tuple)
            or not metadata.interrupt["target_context_keys"]
        ):
            raise ValueError(
                f"{tool_name}: terminal-control interrupt requires target context keys"
            )
        if (
            not isinstance(metadata.interrupt["directive_semantics"], str)
            or not metadata.interrupt["directive_semantics"]
        ):
            raise ValueError(
                f"{tool_name}: terminal-control interrupt requires directive semantics"
            )
        if (
            not isinstance(metadata.interrupt["terminal_statuses"], tuple)
            or not metadata.interrupt["terminal_statuses"]
        ):
            raise ValueError(f"{tool_name}: terminal-control interrupt requires terminal statuses")
        if not all(
            isinstance(status, str) and status for status in metadata.interrupt["terminal_statuses"]
        ):
            raise ValueError(
                f"{tool_name}: terminal-control interrupt terminal statuses must be strings"
            )
        if not isinstance(metadata.interrupt["idempotent"], bool):
            raise ValueError(f"{tool_name}: terminal-control interrupt idempotent must be a bool")
    elif interrupt_keys != {"supported", "mode"}:
        raise ValueError(f"{tool_name}: interrupt must contain supported and mode")
    if (
        not isinstance(metadata.interrupt["supported"], bool)
        or not isinstance(metadata.interrupt["mode"], str)
        or not metadata.interrupt["mode"]
    ):
        raise ValueError(f"{tool_name}: interrupt has invalid supported or mode")

    if set(metadata.cancel) != {
        "supported",
        "mode",
        "companions",
        "target_context_keys",
    }:
        raise ValueError(
            f"{tool_name}: cancel must contain supported, mode, companions, and target_context_keys"
        )
    if (
        not isinstance(metadata.cancel["supported"], bool)
        or not isinstance(metadata.cancel["mode"], str)
        or not metadata.cancel["mode"]
    ):
        raise ValueError(f"{tool_name}: cancel has invalid supported or mode")
    if not isinstance(metadata.cancel["companions"], tuple):
        raise ValueError(f"{tool_name}: cancel.companions must be a tuple")
    if not isinstance(metadata.cancel["target_context_keys"], tuple):
        raise ValueError(f"{tool_name}: cancel.target_context_keys must be a tuple")
    if metadata.cancel["supported"] and (
        not metadata.cancel["companions"] or not metadata.cancel["target_context_keys"]
    ):
        raise ValueError(f"{tool_name}: supported cancel requires companions and context keys")
    if not metadata.cancel["supported"] and (
        metadata.cancel["companions"] or metadata.cancel["target_context_keys"]
    ):
        raise ValueError(f"{tool_name}: unsupported cancel must carry explicit empty tuples")

    if not isinstance(metadata.fallback_used, bool):
        raise ValueError(f"{tool_name}: fallback_used must be a boolean")
    if owned_tool and metadata.fallback_used:
        raise ValueError(f"{tool_name}: owned tool metadata cannot use fallback")
    if not isinstance(metadata.orchestration, Mapping):
        raise ValueError(f"{tool_name}: orchestration must be a mapping")


def _normalize_embedded_ouroboros_tool_capability_metadata(
    raw_capability: Any,
) -> dict[str, Any] | None:
    """Normalize explicit embedded owned-tool metadata without rediscovery.

    Code-fact investigation requests cross runtime boundaries as standalone
    JSON payloads. Consumers may not have the Ouroboros MCP repository or live
    tool catalog available, so this helper treats the embedded capability
    object as the source of truth and refuses to synthesize missing required
    fields from local definitions.
    """
    if not isinstance(raw_capability, Mapping):
        return None
    if not _REQUIRED_TOOL_METADATA_FIELDS.issubset(raw_capability):
        return None
    if raw_capability.get("fallback_used") is not False:
        return None
    tool_name = raw_capability.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name.startswith("ouroboros_"):
        return None

    input_schema = raw_capability.get("input_schema")
    retry = raw_capability.get("retry")
    interrupt = raw_capability.get("interrupt")
    cancel = raw_capability.get("cancel")
    orchestration = raw_capability.get("orchestration")
    if (
        not isinstance(input_schema, Mapping)
        or not isinstance(retry, Mapping)
        or not isinstance(interrupt, Mapping)
        or not isinstance(cancel, Mapping)
        or not isinstance(orchestration, Mapping)
    ):
        return None

    return {
        "tool_name": tool_name,
        "stable_id": str(raw_capability.get("stable_id", "")),
        "source_kind": str(raw_capability.get("source_kind", "")),
        "source_name": str(raw_capability.get("source_name", "")),
        "input_schema": dict(input_schema),
        "mutation_class": str(raw_capability.get("mutation_class", "")),
        "execution_mode": str(raw_capability.get("execution_mode", "")),
        "companions": list(_string_tuple_from_payload(raw_capability.get("companions"))),
        "required_context_keys": list(
            _string_tuple_from_payload(raw_capability.get("required_context_keys"))
        ),
        "mutation_targets": list(
            _string_tuple_from_payload(raw_capability.get("mutation_targets"))
        ),
        "state_mutations": [
            {
                "target": str(mutation.get("target", "")),
                "operation": str(mutation.get("operation", "")),
                "side_effect": str(mutation.get("side_effect", "")),
                "context_keys": list(_string_tuple_from_payload(mutation.get("context_keys"))),
            }
            for mutation in raw_capability.get("state_mutations", ())
            if isinstance(mutation, Mapping)
        ],
        "side_effects": list(_string_tuple_from_payload(raw_capability.get("side_effects"))),
        "retry": dict(retry),
        "interrupt": dict(interrupt),
        "cancel": dict(cancel),
        "fallback_used": False,
        "orchestration": dict(orchestration),
    }


def deserialize_code_investigation_request_metadata(
    payload: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Rehydrate code-fact investigation metadata from its request payload.

    The deserialized request preserves explicit Ouroboros MCP tool-specific
    capability fields from ``mcp_tool_capability``. It intentionally does not
    call ``get_ouroboros_tools()`` or rebuild the capability graph; the request
    payload must already carry the explicit capability contract emitted by the
    interview MCP handler.
    """
    if not isinstance(payload, Mapping):
        return None

    capability = _normalize_embedded_ouroboros_tool_capability_metadata(
        payload.get("mcp_tool_capability")
    )
    if capability is None:
        return None

    normalized = dict(payload)
    normalized["mcp_tool_capability"] = capability
    return normalized


def ouroboros_tool_capability_metadata(tool_name: str) -> dict[str, Any]:
    """Return explicit JSON-safe capability metadata for one owned MCP tool."""
    canonical_name = _ouroboros_tool_name_from_identifier(tool_name)
    if canonical_name is None:
        raise KeyError(f"Unknown Ouroboros MCP tool: {tool_name}")

    metadata_model = lookup_ouroboros_tool_capability_metadata(canonical_name)
    if metadata_model is None:
        raise KeyError(f"Ouroboros MCP tool has no metadata: {tool_name}")
    if metadata_model.fallback_used:
        raise KeyError(f"Ouroboros MCP tool used fallback metadata: {tool_name}")

    definitions = _ouroboros_tool_definitions_by_name()
    descriptor = _descriptor_from_tool(definitions[canonical_name])
    metadata = _serialized_metadata_for_descriptor(descriptor)
    if metadata is None:
        raise KeyError(f"Ouroboros MCP tool has no metadata: {tool_name}")
    return {
        "tool_name": descriptor.name,
        "stable_id": descriptor.stable_id,
        "source_kind": descriptor.source_kind,
        "source_name": descriptor.source_name,
        **metadata,
    }


def _lateral_persona_panel_request_schema() -> dict[str, Any]:
    """Return the structured request model for lateral persona panels."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["problem_context", "current_approach"],
        "properties": {
            "problem_context": {
                "type": "string",
                "minLength": 1,
                "description": "The stuck state or problem being reframed.",
            },
            "current_approach": {
                "type": "string",
                "minLength": 1,
                "description": "What has already been tried.",
            },
            "persona": {
                "type": "string",
                "enum": [
                    "hacker",
                    "researcher",
                    "simplifier",
                    "architect",
                    "contrarian",
                    "all",
                ],
            },
            "personas": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": [
                        "hacker",
                        "researcher",
                        "simplifier",
                        "architect",
                        "contrarian",
                    ],
                },
            },
            "failed_attempts": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
    }


def _lateral_persona_panel_metadata() -> LateralPersonaPanelMetadata:
    """Return the metadata contract for lateral persona panel dispatch."""
    persona_roles = (
        ("hacker", "Finds unconventional workarounds"),
        ("researcher", "Seeks additional information"),
        ("simplifier", "Reduces complexity"),
        ("architect", "Restructures the approach"),
        ("contrarian", "Challenges assumptions"),
    )
    return LateralPersonaPanelMetadata(
        panel_id="lateral_persona_panel.v1",
        mcp_tool="ouroboros_lateral_think",
        dispatch_modes=("plugin", "inline_fallback"),
        parallel_preference="parallel_when_runtime_supports_subagents",
        sequential_fallback={
            "supported": True,
            "mode": "sequential_persona_payload_dispatch",
            "trigger": "runtime_has_no_native_parallel_subagent_primitive",
        },
        personas=tuple(
            LateralPersonaMetadata(
                persona_id=persona_id,
                role=role,
                prompt={
                    "source": "build_lateral_multi_subagent",
                    "payload_field": "payloads[].prompt",
                    "context_field": "payloads[].context",
                    "requires_prose_parsing": False,
                },
                response_payload_ref={
                    "plugin": "MCPToolResult.meta._subagents[persona_id]",
                    "inline_meta": "MCPToolResult.meta.payloads[persona_id]",
                    "inline_content": (
                        "content sentinel ouroboros-lateral-inline-dispatch-v1.payloads[persona_id]"
                    ),
                },
            )
            for persona_id, role in persona_roles
        ),
        request_model_schema=_lateral_persona_panel_request_schema(),
        response_payload_refs={
            "plugin": "MCPToolResult.meta._subagents",
            "inline_meta": "MCPToolResult.meta.payloads",
            "inline_content": ("content sentinel ouroboros-lateral-inline-dispatch-v1.payloads"),
            "result_correlation_key": "context.persona",
            "requires_prose_parsing": False,
        },
        runtime_instruction=(
            "Call ouroboros_lateral_think first. If the response delegates via "
            "_subagents, consume those payloads. If it returns inline_fallback "
            "and the runtime has a native subagent primitive, dispatch each "
            "structured payload by context.persona; otherwise process those "
            "payloads sequentially."
        ),
    )


def _pm_interview_subagent_metadata() -> dict[str, Any]:
    """Return the metadata contract for PM interview subagent dispatch."""
    return {
        "directive": "run_pm_interview_subagent",
        "mcp_tool": "ouroboros_pm_interview",
        "dispatch_modes": ["plugin"],
        "payload_builder": "build_pm_interview_subagent",
        "request_model_schema": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "session_id": {"type": "string"},
                "action": {
                    "type": "string",
                    "enum": ["start", "answer", "resume", "generate", "select_repos"],
                    "default": "start",
                },
                "initial_context": {"type": "string"},
                "answer": {"type": "string"},
                "cwd": {"type": "string"},
                "selected_repos": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
        },
        "response_payload_refs": {
            "plugin": "MCPToolResult.meta._subagent",
            "content_json": "MCPToolResult.content[0].text._subagent",
            "result_correlation_key": "context.session_id",
            "requires_prose_parsing": False,
        },
        "subagent_context_keys": [
            "session_id",
            "action",
            "initial_context",
            "answer",
            "cwd",
            "selected_repos",
        ],
        "runtime_instruction": (
            "Dispatch PM interview work through the `_subagent` payload produced "
            "by build_pm_interview_subagent. Preserve session_id/action context "
            "and consume the structured payload directly; do not infer PM "
            "subagent routing from prose."
        ),
    }


def _background_lifecycle_metadata_for_ouroboros_tool(name: str) -> Mapping[str, Any]:
    """Return explicit job lifecycle companion metadata for background tools."""
    if name not in _OUROBOROS_BACKGROUND_TOOLS:
        return {}

    definitions = _ouroboros_tool_definitions_by_name()
    role_tools = {
        role: tool_name
        for role, tool_name in _OUROBOROS_BACKGROUND_LIFECYCLE_ROLE_TOOLS.items()
        if tool_name in definitions
    }

    blocking_companion = _OUROBOROS_BACKGROUND_BLOCKING_COMPANIONS.get(name)
    companion_roles: dict[str, str] = {"start": name}
    if blocking_companion in definitions:
        companion_roles["blocking"] = str(blocking_companion)
    companion_roles.update(role_tools)
    available_lifecycle_roles = tuple(
        role for role in ("status", "wait", "result", "cancel") if role in role_tools
    )

    return {
        "family_id": "background_job_lifecycle.v1",
        "execution_mode": "background",
        "companion_roles": companion_roles,
        "required_result_context_keys": ("job_id",),
        "required_result_context_keys_by_dispatch": {
            "non_plugin": ("job_id",),
            "plugin": (),
        },
        "plugin_delegation": {
            "supported": True,
            "dispatch_mode": "plugin",
            "status": "delegated_to_plugin",
            "job_id": None,
            "pollable": False,
            "cancel_via_job": False,
        },
        "cancel": _cancel_metadata_for_ouroboros_tool(name),
        "runtime_instruction": (
            "After the background tool returns a non-plugin job_id, use available "
            f"lifecycle companions: {', '.join(available_lifecycle_roles) or 'none'}. "
            "In plugin-delegated mode the tool may return job_id=None with status "
            "delegated_to_plugin; that branch is not pollable or cancellable via "
            "job lifecycle companions."
        ),
    }


def _job_lifecycle_sibling_metadata_for_ouroboros_tool(
    name: str,
) -> Mapping[str, Any]:
    """Return generic job status/wait/result/cancel sibling metadata."""
    sibling_roles = _available_job_lifecycle_sibling_roles()
    if name not in sibling_roles.values():
        return {}

    current_role = next(role for role, tool_name in sibling_roles.items() if tool_name == name)
    sibling_companions = {
        role: tool_name for role, tool_name in sibling_roles.items() if tool_name != name
    }
    return {
        "family_id": "generic_job_lifecycle_siblings.v1",
        "role": current_role,
        "companion_roles": sibling_roles,
        "sibling_companions": sibling_companions,
        "required_result_context_keys": ("job_id",),
        "runtime_instruction": (
            "This generic job lifecycle tool is part of the status/wait/result/"
            "cancel sibling family. Use job_id to move between sibling tools."
        ),
    }


def _run_family_metadata_for_ouroboros_tool(name: str) -> Mapping[str, Any]:
    """Return explicit run/execute companion metadata for the primary run tool."""
    if name != "ouroboros_execute_seed":
        return {}

    definitions = _ouroboros_tool_definitions_by_name()
    role_candidates = {
        "primary": "ouroboros_execute_seed",
        "start": "ouroboros_start_execute_seed",
        "execution_cancel": "ouroboros_cancel_execution",
        **_OUROBOROS_BACKGROUND_LIFECYCLE_ROLE_TOOLS,
    }
    companion_roles = {
        role: tool_name for role, tool_name in role_candidates.items() if tool_name in definitions
    }
    lifecycle_roles = tuple(
        role
        for role in ("start", "execution_cancel", "status", "wait", "result", "cancel")
        if role in companion_roles
    )

    return {
        "family_id": "run_execute_lifecycle.v1",
        "execution_mode": "blocking",
        "companion_roles": companion_roles,
        "start_variant": companion_roles.get("start"),
        "required_result_context_keys": {
            "execution_cancel": ("execution_id",) if "execution_cancel" in companion_roles else (),
            "background_job": ("job_id",)
            if any(role in companion_roles for role in ("status", "wait", "result", "cancel"))
            else (),
        },
        "cancel": _cancel_metadata_for_ouroboros_tool(name),
        "runtime_instruction": (
            "Use ouroboros_execute_seed for blocking run execution. Use the "
            "start variant when background execution is required, then follow "
            f"available lifecycle companions: {', '.join(lifecycle_roles) or 'none'}."
        ),
    }


def _evaluate_family_metadata_for_ouroboros_tool(name: str) -> Mapping[str, Any]:
    """Return explicit evaluate companion metadata for the primary evaluate tool."""
    if name != "ouroboros_evaluate":
        return {}

    definitions = _ouroboros_tool_definitions_by_name()
    role_candidates = {
        "primary": "ouroboros_evaluate",
        "start": "ouroboros_start_evaluate",
        "checklist_verify": "ouroboros_checklist_verify",
        "measure_drift": "ouroboros_measure_drift",
        "qa": "ouroboros_qa",
        **_OUROBOROS_BACKGROUND_LIFECYCLE_ROLE_TOOLS,
    }
    companion_roles = {
        role: tool_name for role, tool_name in role_candidates.items() if tool_name in definitions
    }
    lifecycle_roles = tuple(
        role
        for role in (
            "start",
            "checklist_verify",
            "measure_drift",
            "qa",
            "status",
            "wait",
            "result",
            "cancel",
        )
        if role in companion_roles
    )

    return {
        "family_id": "evaluate_lifecycle.v1",
        "execution_mode": "blocking",
        "companion_roles": companion_roles,
        "start_variant": companion_roles.get("start"),
        "required_result_context_keys": {
            "background_job": ("job_id",)
            if any(role in companion_roles for role in ("status", "wait", "result", "cancel"))
            else (),
        },
        "cancel": _cancel_metadata_for_ouroboros_tool(name),
        "background_cancel": _cancel_metadata_for_ouroboros_tool("ouroboros_start_evaluate")
        if "start" in companion_roles
        else _OUROBOROS_UNSUPPORTED_CANCEL_METADATA,
        "runtime_instruction": (
            "Use ouroboros_evaluate for blocking evaluation. Use the start "
            "variant when background evaluation is required, then follow "
            f"available evaluate family companions: {', '.join(lifecycle_roles) or 'none'}."
        ),
    }


def _evolve_family_metadata_for_ouroboros_tool(name: str) -> Mapping[str, Any]:
    """Return explicit evolve companion metadata for the primary evolve tool."""
    if name != "ouroboros_evolve_step":
        return {}

    definitions = _ouroboros_tool_definitions_by_name()
    role_candidates = {
        "primary": "ouroboros_evolve_step",
        "start": "ouroboros_start_evolve_step",
        "lineage_status": "ouroboros_lineage_status",
        "rewind": "ouroboros_evolve_rewind",
        "ralph": "ouroboros_ralph",
        "start_ralph": "ouroboros_start_ralph",
        **_OUROBOROS_BACKGROUND_LIFECYCLE_ROLE_TOOLS,
    }
    companion_roles = {
        role: tool_name for role, tool_name in role_candidates.items() if tool_name in definitions
    }
    lifecycle_roles = tuple(
        role
        for role in (
            "start",
            "lineage_status",
            "rewind",
            "ralph",
            "start_ralph",
            "status",
            "wait",
            "result",
            "cancel",
        )
        if role in companion_roles
    )

    return {
        "family_id": "evolve_lifecycle.v1",
        "execution_mode": "blocking",
        "companion_roles": companion_roles,
        "start_variant": companion_roles.get("start"),
        "required_result_context_keys": {
            "lineage": ("lineage_id",)
            if any(
                role in companion_roles
                for role in ("lineage_status", "rewind", "ralph", "start_ralph")
            )
            else (),
            "background_job": ("job_id",)
            if any(role in companion_roles for role in ("status", "wait", "result", "cancel"))
            else (),
        },
        "cancel": _cancel_metadata_for_ouroboros_tool(name),
        "background_cancel": _cancel_metadata_for_ouroboros_tool("ouroboros_start_evolve_step")
        if "start" in companion_roles
        else _OUROBOROS_UNSUPPORTED_CANCEL_METADATA,
        "runtime_instruction": (
            "Use ouroboros_evolve_step for blocking evolution. Use the start "
            "variant when background evolution is required, then follow "
            f"available evolve family companions: {', '.join(lifecycle_roles) or 'none'}."
        ),
    }


def _ralph_family_metadata_for_ouroboros_tool(name: str) -> Mapping[str, Any]:
    """Return explicit Ralph companion metadata for the primary Ralph tool."""
    if name != "ouroboros_ralph":
        return {}

    definitions = _ouroboros_tool_definitions_by_name()
    role_candidates = {
        "primary": "ouroboros_ralph",
        "start": "ouroboros_start_ralph",
        "evolve_step": "ouroboros_evolve_step",
        "start_evolve_step": "ouroboros_start_evolve_step",
        "lineage_status": "ouroboros_lineage_status",
        "rewind": "ouroboros_evolve_rewind",
        **_OUROBOROS_BACKGROUND_LIFECYCLE_ROLE_TOOLS,
    }
    companion_roles = {
        role: tool_name for role, tool_name in role_candidates.items() if tool_name in definitions
    }
    lifecycle_roles = tuple(
        role
        for role in (
            "start",
            "evolve_step",
            "start_evolve_step",
            "lineage_status",
            "rewind",
            "status",
            "wait",
            "result",
            "cancel",
        )
        if role in companion_roles
    )

    return {
        "family_id": "ralph_lifecycle.v1",
        "execution_mode": "background",
        "companion_roles": companion_roles,
        "start_variant": companion_roles.get("start"),
        "required_result_context_keys": {
            "lineage": ("lineage_id",)
            if any(
                role in companion_roles
                for role in (
                    "evolve_step",
                    "start_evolve_step",
                    "lineage_status",
                    "rewind",
                )
            )
            else (),
            "background_job": ("job_id",)
            if any(role in companion_roles for role in ("status", "wait", "result", "cancel"))
            else (),
        },
        "cancel": _cancel_metadata_for_ouroboros_tool(name),
        "background_cancel": _cancel_metadata_for_ouroboros_tool(name),
        "runtime_instruction": (
            "Use ouroboros_ralph for background Ralph loop orchestration. "
            "ouroboros_start_ralph is a fire-and-forget alias for the same "
            "background lifecycle. Follow available Ralph family companions: "
            f"{', '.join(lifecycle_roles) or 'none'}."
        ),
    }


def _orchestration_metadata_for_ouroboros_tool(name: str) -> Mapping[str, Any]:
    metadata: dict[str, Any] = {}
    lifecycle_metadata = _background_lifecycle_metadata_for_ouroboros_tool(name)
    if lifecycle_metadata:
        metadata["background_lifecycle"] = lifecycle_metadata
    job_lifecycle_siblings = _job_lifecycle_sibling_metadata_for_ouroboros_tool(name)
    if job_lifecycle_siblings:
        metadata["job_lifecycle_siblings"] = job_lifecycle_siblings
    run_family_metadata = _run_family_metadata_for_ouroboros_tool(name)
    if run_family_metadata:
        metadata["run_family"] = run_family_metadata
    evaluate_family_metadata = _evaluate_family_metadata_for_ouroboros_tool(name)
    if evaluate_family_metadata:
        metadata["evaluate_family"] = evaluate_family_metadata
    evolve_family_metadata = _evolve_family_metadata_for_ouroboros_tool(name)
    if evolve_family_metadata:
        metadata["evolve_family"] = evolve_family_metadata
    ralph_family_metadata = _ralph_family_metadata_for_ouroboros_tool(name)
    if ralph_family_metadata:
        metadata["ralph_family"] = ralph_family_metadata
    if name == "ouroboros_pm_interview":
        return {**metadata, "pm_interview_subagent": _pm_interview_subagent_metadata()}
    if name not in {"ouroboros_interview", "ouroboros_lateral_think"}:
        return metadata
    lateral_panel = _lateral_persona_panel_metadata().to_dict()
    if name == "ouroboros_lateral_think":
        return {**metadata, "lateral_panel": lateral_panel}
    return {
        **metadata,
        "code_investigation": {
            "request_model_schema": _interview_code_investigation_request_schema(),
            "answer_contract": _interview_code_investigation_answer_contract(),
            "repo_inspection_tool_capabilities": _code_investigation_repo_inspection_tool_capabilities(),
            "question_identity": {
                "source_field": "question",
                "helper": "stable_code_investigation_question_identity",
                "algorithm": "sha256",
                "digest_chars": 16,
                "normalization": "NFKC + trim + whitespace collapse",
                "format": "interview-question:{digest}",
                "deterministic": True,
            },
            "derivation_sources": (
                "get_ouroboros_tools().ouroboros_interview.definition",
                "skills/interview/SKILL.md inspect_code PATH 1",
            ),
            "runtime_instruction": (
                "Use the active runtime inspect_code capability for descriptive "
                "repo-local facts before continuing the interview. Route any "
                "decision or low-confidence confirmation to the user."
            ),
        },
        "question_advisory_fanout": _interview_question_advisory_fanout_metadata(),
        "lateral_panel": lateral_panel,
    }


def _explicit_ouroboros_semantics(tool: MCPToolDefinition) -> CapabilitySemantics:
    name = tool.name
    side_effects = _resolved_side_effects_for_ouroboros_tool(name)
    spec_mutation_class = _spec_for_ouroboros_tool_name(name).mutation_class
    if spec_mutation_class is CapabilityMutationClass.READ_ONLY or (
        spec_mutation_class is None
        and (
            name in _OUROBOROS_STATUS_TOOLS
            or side_effects
            in {
                ("event_store_read",),
                ("runtime_state_read",),
                _OUROBOROS_SIDE_EFFECT_FREE_METADATA,
            }
        )
    ):
        mutation_class = CapabilityMutationClass.READ_ONLY
        parallel_safety = CapabilityParallelSafety.SAFE
        interruptibility = CapabilityInterruptibility.NONE
        approval_class = CapabilityApprovalClass.DEFAULT
    elif name in _OUROBOROS_CANCEL_TOOLS:
        mutation_class = CapabilityMutationClass.EXTERNAL_SIDE_EFFECT
        parallel_safety = CapabilityParallelSafety.SERIALIZED
        interruptibility = CapabilityInterruptibility.HARD
        approval_class = CapabilityApprovalClass.ELEVATED
    else:
        mutation_class = CapabilityMutationClass.WORKSPACE_WRITE
        parallel_safety = CapabilityParallelSafety.SERIALIZED
        interruptibility = CapabilityInterruptibility.SOFT
        approval_class = CapabilityApprovalClass.DEFAULT

    return CapabilitySemantics(
        mutation_class=mutation_class,
        parallel_safety=parallel_safety,
        interruptibility=interruptibility,
        approval_class=approval_class,
        origin=CapabilityOrigin.ATTACHED_MCP,
        scope=CapabilityScope.ATTACHMENT,
    )


def _metadata_from_ouroboros_spec(
    tool: MCPToolDefinition,
    spec: _OuroborosToolCapabilitySpec,
) -> CapabilityToolMetadata:
    name = tool.name
    state_mutations = _state_mutations_for_ouroboros_tool(name)
    return CapabilityToolMetadata(
        input_schema=_input_schema_for_ouroboros_tool(tool),
        mutation_class=_explicit_ouroboros_semantics(tool).mutation_class.value,
        execution_mode=spec.execution_mode,
        companions=_derived_ouroboros_companions_for_tool(name),
        required_context_keys=_required_context_keys_for_ouroboros_tool(tool),
        mutation_targets=_mutation_targets_for_state_mutations(
            _mutation_targets_for_side_effects(spec.side_effects),
            state_mutations,
        ),
        state_mutations=state_mutations,
        side_effects=spec.side_effects,
        retry=_retry_metadata_for_ouroboros_tool(name),
        interrupt=_interrupt_metadata_for_ouroboros_tool(name),
        cancel=_cancel_metadata_for_ouroboros_tool(name),
        fallback_used=False,
        orchestration=_orchestration_metadata_for_ouroboros_tool(name),
    )


def ouroboros_tool_capability_registry() -> Mapping[str, CapabilityToolMetadata]:
    """Return explicit capability metadata for every owned MCP tool definition."""
    definitions = _ouroboros_tool_definitions_by_name()
    missing_specs = sorted(set(definitions) - set(_OUROBOROS_TOOL_CAPABILITY_SPECS))
    missing_cancel_metadata = sorted(set(definitions) - set(_OUROBOROS_CANCEL_METADATA))
    if missing_specs or missing_cancel_metadata:
        raise RuntimeError(
            "Ouroboros MCP capability registry is out of sync: "
            f"missing_specs={missing_specs}, "
            f"missing_cancel_metadata={missing_cancel_metadata}"
        )
    registry = {
        name: _metadata_from_ouroboros_spec(
            definitions[name],
            _OUROBOROS_TOOL_CAPABILITY_SPECS[name],
        )
        for name in sorted(definitions)
    }
    for name, metadata in registry.items():
        validate_capability_tool_metadata(metadata, tool_name=name, owned_tool=True)
    return registry


def _metadata_for_ouroboros_tool(tool: MCPToolDefinition) -> CapabilityToolMetadata:
    name = tool.name
    if name in _OUROBOROS_TOOL_CAPABILITY_SPECS:
        return ouroboros_tool_capability_registry()[name]
    raise RuntimeError(
        "Ouroboros MCP capability registry has no explicit spec for "
        f"{name}; retry metadata must be defined per owned tool"
    )


def _ouroboros_tool_name_from_identifier(tool_identifier: str) -> str | None:
    """Return an owned MCP tool name from a known Ouroboros tool identifier."""
    normalized_identifier = tool_identifier.strip()
    if not normalized_identifier:
        return None

    definitions = _ouroboros_tool_definitions_by_name()
    if normalized_identifier in definitions:
        return normalized_identifier

    stable_id_prefix = "mcp:ouroboros:"
    if normalized_identifier.startswith(stable_id_prefix):
        candidate = normalized_identifier.removeprefix(stable_id_prefix)
        if candidate in definitions:
            return candidate

    return None


def lookup_ouroboros_tool_capability_metadata(
    tool_identifier: str,
) -> CapabilityToolMetadata | None:
    """Return explicit metadata for a known Ouroboros-owned MCP tool.

    Unknown or external attached MCP tools return ``None`` so their generic
    fallback inference path stays separate from the owned-tool contract.
    """
    tool_name = _ouroboros_tool_name_from_identifier(tool_identifier)
    if tool_name is None:
        return None
    return ouroboros_tool_capability_registry()[tool_name]


def _generic_attached_tool_metadata(tool: MCPToolDefinition) -> CapabilityToolMetadata:
    semantics = _infer_attached_semantics(tool)
    return CapabilityToolMetadata(
        input_schema=extract_capability_input_schema(tool),
        mutation_class=semantics.mutation_class.value,
        execution_mode="generic_attached",
        companions=(),
        required_context_keys=_required_parameter_names(tool),
        mutation_targets=("external",),
        state_mutations=(),
        side_effects=("unknown_external_side_effect",),
        retry={"supported": False, "mode": "unsupported"},
        interrupt={"supported": True, "mode": "soft"},
        cancel={
            "supported": False,
            "mode": "unsupported",
            "companions": (),
            "target_context_keys": (),
        },
        fallback_used=True,
        orchestration={},
    )


def _metadata_with_semantics(
    metadata: CapabilityToolMetadata,
    semantics: CapabilitySemantics,
) -> CapabilityToolMetadata:
    """Return fallback metadata reconciled with externally overridden semantics."""
    if not metadata.fallback_used:
        return metadata

    if semantics.mutation_class is CapabilityMutationClass.READ_ONLY:
        mutation_targets: tuple[str, ...] = ()
        side_effects: tuple[str, ...] = ()
    else:
        mutation_targets = metadata.mutation_targets or ("external",)
        side_effects = metadata.side_effects or ("unknown_external_side_effect",)

    return CapabilityToolMetadata(
        input_schema=metadata.input_schema,
        mutation_class=semantics.mutation_class.value,
        execution_mode=metadata.execution_mode,
        companions=metadata.companions,
        required_context_keys=metadata.required_context_keys,
        mutation_targets=mutation_targets,
        state_mutations=metadata.state_mutations,
        side_effects=side_effects,
        retry=metadata.retry,
        interrupt=metadata.interrupt,
        cancel=metadata.cancel,
        fallback_used=metadata.fallback_used,
        orchestration=metadata.orchestration,
    )


def _fallback_source_metadata(tool: MCPToolDefinition) -> ToolCatalogSourceMetadata:
    if _is_ouroboros_owned_tool(tool):
        return ToolCatalogSourceMetadata(
            kind="attached_mcp",
            name="ouroboros",
            original_name=tool.name,
            server_name=tool.server_name,
        )
    source_kind = "attached_mcp" if tool.server_name else "builtin"
    source_name = tool.server_name or "built-in"
    return ToolCatalogSourceMetadata(
        kind=source_kind,
        name=source_name,
        original_name=tool.name,
        server_name=tool.server_name,
    )


def _infer_attached_semantics(tool: MCPToolDefinition) -> CapabilitySemantics:
    fingerprint = f"{tool.name} {tool.description}".lower()
    if any(token in fingerprint for token in ("delete", "destroy", "drop", "remove", "kill")):
        mutation_class = CapabilityMutationClass.DESTRUCTIVE
        parallel_safety = CapabilityParallelSafety.ISOLATED_SESSION_REQUIRED
        interruptibility = CapabilityInterruptibility.HARD
        approval_class = CapabilityApprovalClass.BYPASS_FORBIDDEN
    elif any(token in fingerprint for token in ("read", "list", "search", "fetch", "query")):
        mutation_class = CapabilityMutationClass.READ_ONLY
        parallel_safety = CapabilityParallelSafety.SAFE
        interruptibility = CapabilityInterruptibility.NONE
        approval_class = CapabilityApprovalClass.DEFAULT
    elif any(token in fingerprint for token in ("exec", "run", "shell", "command")):
        mutation_class = CapabilityMutationClass.EXTERNAL_SIDE_EFFECT
        parallel_safety = CapabilityParallelSafety.ISOLATED_SESSION_REQUIRED
        interruptibility = CapabilityInterruptibility.HARD
        approval_class = CapabilityApprovalClass.ELEVATED
    else:
        mutation_class = CapabilityMutationClass.EXTERNAL_SIDE_EFFECT
        parallel_safety = CapabilityParallelSafety.SERIALIZED
        interruptibility = CapabilityInterruptibility.SOFT
        approval_class = CapabilityApprovalClass.ELEVATED

    return CapabilitySemantics(
        mutation_class=mutation_class,
        parallel_safety=parallel_safety,
        interruptibility=interruptibility,
        approval_class=approval_class,
        origin=CapabilityOrigin.ATTACHED_MCP,
        scope=CapabilityScope.ATTACHMENT,
    )


def _coerce_capability_semantics(
    raw: Mapping[str, Any],
    *,
    fallback: CapabilitySemantics,
    context: str,
) -> CapabilitySemantics | None:
    """Merge a raw override mapping onto ``fallback``.

    Returns ``None`` — and logs a structured warning — when the raw
    mapping contains an unrecognized enum value.  The caller decides
    what to do on failure (use fallback, skip the tool, etc.); this
    function does not raise, so callers do not need to re-wrap it in
    try/except just to preserve their own control flow.
    """
    try:
        return CapabilitySemantics(
            mutation_class=CapabilityMutationClass(
                str(raw.get("mutation_class", fallback.mutation_class.value))
            ),
            parallel_safety=CapabilityParallelSafety(
                str(raw.get("parallel_safety", fallback.parallel_safety.value))
            ),
            interruptibility=CapabilityInterruptibility(
                str(raw.get("interruptibility", fallback.interruptibility.value))
            ),
            approval_class=CapabilityApprovalClass(
                str(raw.get("approval_class", fallback.approval_class.value))
            ),
            origin=CapabilityOrigin(str(raw.get("origin", fallback.origin.value))),
            scope=CapabilityScope(str(raw.get("scope", fallback.scope.value))),
        )
    except ValueError as exc:
        log.warning(
            "capability_override.invalid_enum",
            context=context,
            error=str(exc),
        )
        return None


def _default_tool_capability_override_path() -> Path:
    configured = os.environ.get("OUROBOROS_TOOL_CAPABILITIES")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".ouroboros" / "tool_capabilities.yaml"


# Mapping from resolved override path to (mtime, raw overrides).  Invalidated by
# mtime so edits to ~/.ouroboros/tool_capabilities.yaml take effect without a
# process restart, while repeated graph builds in the same process avoid
# re-reading and re-parsing the YAML file on every call.
_RAW_OVERRIDES_CACHE: dict[Path, tuple[float, dict[str, Mapping[str, Any]]]] = {}


def _read_raw_tool_capability_overrides(path: Path) -> dict[str, Mapping[str, Any]]:
    """Read and parse the override YAML, returning raw per-tool mappings.

    Every failure mode — missing file, non-regular file (FIFO, socket,
    device, directory), unreadable file, malformed YAML, unexpected
    top-level shape — is handled locally.  A broken user config must
    never propagate out of this function, because the override loader
    sits on the default capability-graph construction path and is
    therefore reached from interview, evaluation, and execution sessions
    alike.  A single malformed YAML line — or a ``OUROBOROS_TOOL_CAPABILITIES``
    variable pointing at a FIFO — would otherwise take down unrelated
    orchestration paths or hang startup indefinitely on ``read_text()``.
    """
    try:
        stat_result = path.stat()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        log.warning(
            "capability_override.stat_failed",
            path=str(path),
            error=str(exc),
        )
        return {}

    # Refuse to open non-regular files.  ``read_text()`` on a FIFO or
    # character device will block indefinitely because those paths have no
    # EOF, and on a directory will raise ``IsADirectoryError`` too late
    # (after the caller already paid the syscall).  Stop here so the
    # override layer cannot wedge the orchestrator hot path.
    if not stat.S_ISREG(stat_result.st_mode):
        log.warning(
            "capability_override.not_regular_file",
            path=str(path),
            mode=oct(stat_result.st_mode),
        )
        return {}

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning(
            "capability_override.read_failed",
            path=str(path),
            error=str(exc),
        )
        return {}

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        log.warning(
            "capability_override.yaml_parse_failed",
            path=str(path),
            error=str(exc),
        )
        return {}

    if not isinstance(raw, Mapping):
        return {}

    raw_tools = raw.get("tools", raw)
    if not isinstance(raw_tools, Mapping):
        return {}

    parsed: dict[str, Mapping[str, Any]] = {}
    for key, value in raw_tools.items():
        if not isinstance(key, str) or not isinstance(value, Mapping):
            continue
        parsed[key] = dict(value)
    return parsed


def _load_raw_tool_capability_overrides(
    path: str | Path | None = None,
) -> dict[str, Mapping[str, Any]]:
    """Load raw override mappings with mtime-based caching.

    Fault-tolerant by design: any failure (missing file, permission error,
    non-regular path, filesystem glitch) returns an empty mapping so that
    downstream graph construction always succeeds.  The override layer is
    an optional enhancement, not a prerequisite for orchestration.
    """
    try:
        config_path = (
            Path(path).expanduser()
            if path is not None
            else _default_tool_capability_override_path()
        )
    except (OSError, ValueError) as exc:
        log.warning(
            "capability_override.path_resolution_failed",
            path=str(path),
            error=str(exc),
        )
        return {}

    try:
        stat_result = config_path.stat()
    except FileNotFoundError:
        _RAW_OVERRIDES_CACHE.pop(config_path, None)
        return {}
    except OSError as exc:
        log.warning(
            "capability_override.read_failed",
            path=str(config_path),
            error=str(exc),
        )
        return {}

    # Defense in depth: ``_read_raw_tool_capability_overrides`` also checks
    # this, but rejecting non-regular files before we even touch the cache
    # means a FIFO path cannot poison the cache with a bogus mtime entry.
    if not stat.S_ISREG(stat_result.st_mode):
        _RAW_OVERRIDES_CACHE.pop(config_path, None)
        log.warning(
            "capability_override.not_regular_file",
            path=str(config_path),
            mode=oct(stat_result.st_mode),
        )
        return {}

    mtime = stat_result.st_mtime
    cached = _RAW_OVERRIDES_CACHE.get(config_path)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    raw = _read_raw_tool_capability_overrides(config_path)
    _RAW_OVERRIDES_CACHE[config_path] = (mtime, raw)
    return raw


def _apply_raw_override_to_semantics(
    inferred: CapabilitySemantics,
    raw: Mapping[str, Any],
    *,
    context: str,
) -> CapabilitySemantics:
    """Merge raw override fields onto already-inferred semantics.

    Preserves inferred values for any dimension the user did not
    explicitly set in their override YAML.  Returns inferred unchanged
    when the override declares an invalid enum value (the warning is
    logged by ``_coerce_capability_semantics``).
    """
    merged = _coerce_capability_semantics(raw, fallback=inferred, context=context)
    return merged if merged is not None else inferred


def _semantics_for_entry(
    tool: MCPToolDefinition,
    source: ToolCatalogSourceMetadata,
) -> CapabilitySemantics:
    if _is_ouroboros_owned_tool(tool):
        return _explicit_ouroboros_semantics(tool)
    if source.kind == "builtin":
        return _BUILTIN_SEMANTICS.get(
            tool.name,
            CapabilitySemantics(
                mutation_class=CapabilityMutationClass.READ_ONLY,
                parallel_safety=CapabilityParallelSafety.SAFE,
                interruptibility=CapabilityInterruptibility.NONE,
                approval_class=CapabilityApprovalClass.DEFAULT,
                origin=CapabilityOrigin.BUILTIN,
                scope=CapabilityScope.KERNEL,
            ),
        )
    return _infer_attached_semantics(tool)


def _stable_id(tool: MCPToolDefinition, source: ToolCatalogSourceMetadata) -> str:
    if source.kind == "builtin":
        return f"builtin:{tool.name}"
    source_name = source.server_name or source.name
    return f"mcp:{source_name}:{tool.name}"


def _descriptor_from_tool(
    tool: MCPToolDefinition,
    source: ToolCatalogSourceMetadata | None = None,
    *,
    stable_id: str | None = None,
    raw_capability_overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> CapabilityDescriptor:
    resolved_source = source or _fallback_source_metadata(tool)
    if _is_ouroboros_owned_tool(tool) and resolved_source.kind == "attached_mcp":
        resolved_source = ToolCatalogSourceMetadata(
            kind=resolved_source.kind,
            name="ouroboros",
            original_name=resolved_source.original_name,
            server_name=resolved_source.server_name,
        )
    resolved_stable_id = stable_id or _stable_id(tool, resolved_source)
    if _is_ouroboros_owned_tool(tool):
        resolved_stable_id = _stable_id(tool, resolved_source)
    semantics = _semantics_for_entry(tool, resolved_source)
    metadata = (
        _metadata_for_ouroboros_tool(tool)
        if _is_ouroboros_owned_tool(tool)
        else (_generic_attached_tool_metadata(tool) if resolved_source.kind != "builtin" else None)
    )
    # Built-in tools deliberately bypass user overrides: their semantics are
    # part of the engine contract (e.g., Bash must remain EXTERNAL_SIDE_EFFECT
    # regardless of user YAML) so that role envelopes cannot be silently
    # widened.  Attached and provider-native tools are reclassifiable.
    if resolved_source.kind != "builtin" and raw_capability_overrides:
        raw = _match_raw_capability_override(
            tool,
            resolved_source,
            resolved_stable_id,
            raw_capability_overrides,
        )
        if raw is not None:
            semantics = _apply_raw_override_to_semantics(
                semantics,
                raw,
                context=f"tool:{resolved_stable_id}",
            )
            if metadata is not None:
                metadata = _metadata_with_semantics(metadata, semantics)
    return CapabilityDescriptor(
        stable_id=resolved_stable_id,
        name=tool.name,
        original_name=resolved_source.original_name,
        description=tool.description,
        server_name=tool.server_name,
        source_kind=resolved_source.kind,
        source_name=resolved_source.name,
        semantics=semantics,
        metadata=metadata,
    )


def _match_raw_capability_override(
    tool: MCPToolDefinition,
    source: ToolCatalogSourceMetadata,
    stable_id: str,
    raw_capability_overrides: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    source_name = source.server_name or source.name
    candidates = (
        stable_id,
        f"{source.kind}:{source_name}:{tool.name}",
        f"{source_name}:{tool.name}",
        source.original_name,
        tool.name,
    )
    for candidate in candidates:
        if candidate in raw_capability_overrides:
            return raw_capability_overrides[candidate]
    return None


def _descriptor_from_inherited_capability(name: str) -> CapabilityDescriptor:
    """Represent a delegated MCP grant without making it executable."""
    return CapabilityDescriptor(
        stable_id=f"inherited:{name}",
        name=name,
        original_name=name,
        description="Inherited delegated capability pending live MCP discovery",
        server_name=None,
        source_kind="inherited_capability",
        source_name="delegated_parent",
        semantics=_DEFAULT_ATTACHED_SEMANTICS,
        metadata=None,
    )


def build_capability_graph(
    tool_catalog: SessionToolCatalog
    | Sequence[MCPToolDefinition]
    | Sequence[SessionToolCatalogEntry],
) -> CapabilityGraph:
    """Build a deterministic capability graph from the current tool surface.

    User-defined capability overrides from
    ``~/.ouroboros/tool_capabilities.yaml`` (or the path in
    ``OUROBOROS_TOOL_CAPABILITIES``) are loaded lazily, cached by mtime,
    and merged *onto* the inferred semantics so callers can override
    only the specific dimensions they care about.
    """
    descriptors: list[CapabilityDescriptor] = []
    raw_overrides = _load_raw_tool_capability_overrides()

    inherited_capabilities: frozenset[str] = frozenset()
    if isinstance(tool_catalog, SessionToolCatalog):
        entries = tool_catalog.entries
        inherited_capabilities = tool_catalog.inherited_capabilities
    else:
        entries = tool_catalog

    for entry in entries:
        if isinstance(entry, SessionToolCatalogEntry):
            descriptors.append(
                _descriptor_from_tool(
                    entry.tool,
                    entry.source,
                    stable_id=entry.stable_id,
                    raw_capability_overrides=raw_overrides,
                )
            )
        else:
            descriptors.append(
                _descriptor_from_tool(
                    entry,
                    raw_capability_overrides=raw_overrides,
                )
            )

    for capability_name in sorted(inherited_capabilities):
        descriptors.append(_descriptor_from_inherited_capability(capability_name))

    return CapabilityGraph(capabilities=tuple(descriptors))


def resolve_skill_capability_descriptor(
    skill_name: str,
    *,
    graph: CapabilityGraph | Sequence[CapabilityDescriptor] | None = None,
) -> CapabilityDescriptor | None:
    """Resolve a packaged skill name to its Ouroboros-owned MCP capability.

    Skill frontmatter owns the skill-to-tool mapping. The capability graph owns
    the executable tool metadata. This helper intentionally joins those two
    contracts without falling back to keyword inference for known Ouroboros MCP
    tools.
    """

    from ouroboros.orchestrator.skill_tool_mapping import get_skill_tool_mapping

    mapping = get_skill_tool_mapping(skill_name)
    if mapping is None:
        return None

    if graph is None:
        graph = build_capability_graph(tuple(_ouroboros_tool_definitions_by_name().values()))

    descriptors = graph.capabilities if isinstance(graph, CapabilityGraph) else tuple(graph)
    descriptor_by_name = {descriptor.name: descriptor for descriptor in descriptors}
    descriptor = descriptor_by_name.get(mapping.mcp_tool)
    if descriptor is None:
        return None
    if not _is_ouroboros_owned_tool_name(descriptor.name):
        return None
    if descriptor.metadata is None or descriptor.metadata.fallback_used:
        return None
    return descriptor


def resolve_mcp_tool_capability_descriptor(
    tool_identifier: str,
    *,
    graph: CapabilityGraph | Sequence[CapabilityDescriptor] | None = None,
) -> CapabilityDescriptor | None:
    """Resolve a known Ouroboros-owned MCP tool identifier to explicit metadata.

    ``tool_identifier`` may be the normalized tool name, catalog stable id, or
    original MCP name from a resolved runtime catalog entry. Unknown or external
    attached tools intentionally return ``None`` so callers keep the generic
    fallback path separate from the owned-tool contract.
    """

    normalized_identifier = tool_identifier.strip()
    if not normalized_identifier:
        return None

    if graph is None:
        graph = build_capability_graph(tuple(_ouroboros_tool_definitions_by_name().values()))

    descriptors = graph.capabilities if isinstance(graph, CapabilityGraph) else tuple(graph)
    for descriptor in descriptors:
        if normalized_identifier not in {
            descriptor.name,
            descriptor.original_name,
            descriptor.stable_id,
        }:
            continue
        if not _is_ouroboros_owned_tool_name(descriptor.name):
            return None
        if descriptor.metadata is None or descriptor.metadata.fallback_used:
            return None
        return descriptor
    return None


def serialize_capability_graph(
    graph: CapabilityGraph | Sequence[CapabilityDescriptor],
) -> list[dict[str, Any]]:
    """Serialize a capability graph into JSON-safe metadata."""
    capabilities = graph.capabilities if isinstance(graph, CapabilityGraph) else tuple(graph)
    return [
        {
            "stable_id": descriptor.stable_id,
            "name": descriptor.name,
            "original_name": descriptor.original_name,
            "description": descriptor.description,
            "server_name": descriptor.server_name,
            "source_kind": descriptor.source_kind,
            "source_name": descriptor.source_name,
            "semantics": {
                "mutation_class": descriptor.semantics.mutation_class.value,
                "parallel_safety": descriptor.semantics.parallel_safety.value,
                "interruptibility": descriptor.semantics.interruptibility.value,
                "approval_class": descriptor.semantics.approval_class.value,
                "origin": descriptor.semantics.origin.value,
                "scope": descriptor.semantics.scope.value,
            },
            "metadata": _serialized_metadata_for_descriptor(descriptor),
        }
        for descriptor in capabilities
    ]


def normalize_serialized_capability_graph(
    payload: Sequence[Mapping[str, Any]] | None,
) -> CapabilityGraph | None:
    """Rehydrate a serialized capability graph payload."""
    if not payload:
        return None

    descriptors: list[CapabilityDescriptor] = []
    for entry in payload:
        semantics = entry.get("semantics")
        if not isinstance(semantics, Mapping):
            continue
        raw_metadata = entry.get("metadata")
        metadata = None
        if isinstance(raw_metadata, Mapping):
            metadata = CapabilityToolMetadata(
                input_schema=raw_metadata.get("input_schema", {})
                if isinstance(raw_metadata.get("input_schema"), Mapping)
                else {},
                mutation_class=str(raw_metadata.get("mutation_class", "")),
                execution_mode=str(raw_metadata.get("execution_mode", "")),
                companions=tuple(
                    str(value)
                    for value in raw_metadata.get("companions", ())
                    if isinstance(value, str)
                )
                if isinstance(raw_metadata.get("companions"), Sequence)
                and not isinstance(raw_metadata.get("companions"), str)
                else (),
                required_context_keys=tuple(
                    str(value)
                    for value in raw_metadata.get("required_context_keys", ())
                    if isinstance(value, str)
                )
                if isinstance(raw_metadata.get("required_context_keys"), Sequence)
                and not isinstance(raw_metadata.get("required_context_keys"), str)
                else (),
                mutation_targets=tuple(
                    str(value)
                    for value in raw_metadata.get("mutation_targets", ())
                    if isinstance(value, str)
                )
                if isinstance(raw_metadata.get("mutation_targets"), Sequence)
                and not isinstance(raw_metadata.get("mutation_targets"), str)
                else (),
                state_mutations=tuple(
                    {
                        "target": str(value.get("target", "")),
                        "operation": str(value.get("operation", "")),
                        "side_effect": str(value.get("side_effect", "")),
                        "context_keys": _string_tuple_from_payload(value.get("context_keys")),
                    }
                    for value in raw_metadata.get("state_mutations", ())
                    if isinstance(value, Mapping)
                )
                if isinstance(raw_metadata.get("state_mutations"), Sequence)
                and not isinstance(raw_metadata.get("state_mutations"), str)
                else (),
                side_effects=tuple(
                    str(value)
                    for value in raw_metadata.get("side_effects", ())
                    if isinstance(value, str)
                )
                if isinstance(raw_metadata.get("side_effects"), Sequence)
                and not isinstance(raw_metadata.get("side_effects"), str)
                else (),
                retry=raw_metadata.get("retry", {})
                if isinstance(raw_metadata.get("retry"), Mapping)
                else {},
                interrupt=_normalize_tuple_fields(
                    raw_metadata.get("interrupt", {}),
                    frozenset(
                        {
                            "background_companions",
                            "resume_companions",
                            "cancel_companions",
                            "target_context_keys",
                        }
                    ),
                )
                if isinstance(raw_metadata.get("interrupt"), Mapping)
                else {},
                cancel=_normalize_tuple_fields(
                    raw_metadata.get("cancel", {}),
                    frozenset({"companions", "target_context_keys"}),
                )
                if isinstance(raw_metadata.get("cancel"), Mapping)
                else {},
                fallback_used=bool(raw_metadata.get("fallback_used", False)),
                orchestration=raw_metadata.get("orchestration", {})
                if isinstance(raw_metadata.get("orchestration"), Mapping)
                else {},
            )
        try:
            descriptors.append(
                CapabilityDescriptor(
                    stable_id=str(entry.get("stable_id", "")),
                    name=str(entry.get("name", "")),
                    original_name=str(entry.get("original_name", "")),
                    description=str(entry.get("description", "")),
                    server_name=entry.get("server_name")
                    if isinstance(entry.get("server_name"), str)
                    else None,
                    source_kind=str(entry.get("source_kind", "")),
                    source_name=str(entry.get("source_name", "")),
                    semantics=CapabilitySemantics(
                        mutation_class=CapabilityMutationClass(
                            str(semantics.get("mutation_class"))
                        ),
                        parallel_safety=CapabilityParallelSafety(
                            str(semantics.get("parallel_safety"))
                        ),
                        interruptibility=CapabilityInterruptibility(
                            str(semantics.get("interruptibility"))
                        ),
                        approval_class=CapabilityApprovalClass(
                            str(semantics.get("approval_class"))
                        ),
                        origin=CapabilityOrigin(str(semantics.get("origin"))),
                        scope=CapabilityScope(str(semantics.get("scope"))),
                    ),
                    metadata=metadata,
                )
            )
        except ValueError:
            continue

    return CapabilityGraph(capabilities=tuple(descriptors))


__all__ = [
    "CapabilityApprovalClass",
    "CapabilityDescriptor",
    "CapabilityGraph",
    "CapabilityInterruptibility",
    "CapabilityMutationClass",
    "CapabilityOrigin",
    "CapabilityParallelSafety",
    "CapabilityScope",
    "CapabilitySemantics",
    "CapabilityToolMetadata",
    "LateralPersonaMetadata",
    "LateralPersonaPanelMetadata",
    "build_capability_graph",
    "deserialize_code_investigation_request_metadata",
    "extract_capability_input_schema",
    "lookup_ouroboros_tool_capability_metadata",
    "mcp_tool_required_parameter_keys",
    "normalize_serialized_capability_graph",
    "ouroboros_tool_capability_registry",
    "ouroboros_tool_capability_metadata",
    "resolve_mcp_tool_capability_descriptor",
    "resolve_skill_capability_descriptor",
    "serialize_capability_graph",
    "stable_code_investigation_question_identity",
    "validate_capability_tool_metadata",
]
