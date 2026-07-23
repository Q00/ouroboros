"""Ouroboros-owned MCP tool capability specs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ouroboros.orchestrator.capabilities import CapabilityMutationClass


@dataclass(frozen=True, slots=True)
class _OuroborosToolCapabilitySpec:
    """Explicit capability metadata spec for one Ouroboros-owned MCP tool."""

    execution_mode: str
    companions: tuple[str, ...]
    side_effects: tuple[str, ...]
    retry: Mapping[str, Any]
    interrupt: Mapping[str, Any]
    mutation_class: CapabilityMutationClass | None = None


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
    "ouroboros_submit_fanout_results": _OuroborosToolCapabilitySpec(
        # Re-entry accumulates partial submissions on the persisted fan-out
        # record and marks completed fan-outs terminal (Q00/ouroboros#1671),
        # so it writes session state — it is no longer a read-only routing
        # step even though the synthesis it returns is deterministic.
        execution_mode="status",
        companions=("ouroboros_interview", "ouroboros_lateral_think"),
        side_effects=("session_state_write",),
        retry=_OUROBOROS_DEFAULT_RETRY_METADATA,
        # Soft interrupt like the other state-writing synchronous handlers
        # (interview, lateral_think): re-entry writes session state, so it
        # must not claim the read-only interrupt contract (PR #1703 round-8);
        # every write is idempotent and an interrupted submission is safely
        # resubmitted against the same fanout_id.
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
    "ouroboros_submit_fanout_results": _OUROBOROS_UNSUPPORTED_CANCEL_METADATA,
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


__all__ = [
    "_OuroborosToolCapabilitySpec",
    "_OUROBOROS_BACKGROUND_BLOCKING_COMPANIONS",
    "_OUROBOROS_BACKGROUND_LIFECYCLE_ROLE_TOOLS",
    "_OUROBOROS_BACKGROUND_TOOLS",
    "_OUROBOROS_CANCEL_METADATA",
    "_OUROBOROS_CANCEL_TOOLS",
    "_OUROBOROS_COMPANION_FAMILIES",
    "_OUROBOROS_DEFAULT_EXECUTION_MODE",
    "_OUROBOROS_JOB_LIFECYCLE_SIBLING_ORDER",
    "_OUROBOROS_MUTATION_TARGETS_BY_SIDE_EFFECT",
    "_OUROBOROS_STATE_MUTATIONS_BY_TOOL",
    "_OUROBOROS_STATUS_TOOLS",
    "_OUROBOROS_SUBAGENT_TOOLS",
    "_OUROBOROS_TOOL_CAPABILITY_SPECS",
    "_OUROBOROS_WORKSPACE_WRITE_TOOLS",
]
