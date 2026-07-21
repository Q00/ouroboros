"""Tests for the engine capability graph."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, replace
from types import SimpleNamespace
from unittest.mock import patch

from jsonschema import Draft202012Validator
import pytest

from ouroboros.mcp.tools.definitions import get_ouroboros_tools
from ouroboros.mcp.tools.evaluation_handlers import LateralThinkHandler

try:
    from ouroboros.mcp.tools.subagent import (
        lateral_review_response_to_interview_orchestration_entries,
    )
except ImportError:  # pragma: no cover - earlier stack layers do not carry PR6.
    lateral_review_response_to_interview_orchestration_entries = None
from ouroboros.mcp.types import MCPToolDefinition, MCPToolParameter, ToolInputType
import ouroboros.orchestrator.capabilities as capabilities_module
from ouroboros.orchestrator.capabilities import (
    CapabilityApprovalClass,
    CapabilityInterruptibility,
    CapabilityMutationClass,
    CapabilityOrigin,
    CapabilityParallelSafety,
    CapabilityScope,
    CapabilityToolMetadata,
    build_capability_graph,
    deserialize_code_investigation_request_metadata,
    extract_capability_input_schema,
    interview_code_investigation_answer_contract,
    lookup_ouroboros_tool_capability_metadata,
    mcp_tool_required_parameter_keys,
    normalize_serialized_capability_graph,
    ouroboros_tool_capability_metadata,
    ouroboros_tool_capability_registry,
    resolve_mcp_tool_capability_descriptor,
    resolve_skill_capability_descriptor,
    serialize_capability_graph,
    stable_code_investigation_question_identity,
    validate_capability_tool_metadata,
)
import ouroboros.orchestrator.capabilities.tool_specs as tool_specs_module
from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog
from ouroboros.orchestrator.policy import (
    PolicyContext,
    PolicyExecutionPhase,
    PolicySessionRole,
    allowed_capability_names,
)
from ouroboros.orchestrator.skill_tool_mapping import (
    get_packaged_skill_tool_mappings,
    get_skill_tool_mapping,
    skill_tool_mapping_by_skill,
)


def _frontmatter_mcp_tools() -> set[str]:
    return {mapping.mcp_tool for mapping in get_packaged_skill_tool_mappings()}


_EXPECTED_OUROBOROS_TOOL_EXECUTION_MODES = {
    "ouroboros_ac_tree_hud": "status",
    "ouroboros_auto": "blocking",
    "ouroboros_brownfield": "blocking",
    "ouroboros_cancel_execution": "cancel",
    "ouroboros_cancel_job": "cancel",
    "ouroboros_checklist_verify": "blocking",
    "ouroboros_evaluate": "blocking",
    "ouroboros_evolve_rewind": "blocking",
    "ouroboros_evolve_step": "blocking",
    "ouroboros_execute_seed": "blocking",
    "ouroboros_generate_seed": "blocking",
    "ouroboros_interview": "subagent_orchestration",
    "ouroboros_job_result": "status",
    "ouroboros_job_status": "status",
    "ouroboros_job_wait": "status",
    "ouroboros_lateral_think": "subagent_orchestration",
    "ouroboros_lineage_status": "status",
    "ouroboros_measure_drift": "blocking",
    "ouroboros_pm_interview": "subagent_orchestration",
    "ouroboros_qa": "blocking",
    "ouroboros_query_events": "status",
    "ouroboros_query_projection": "status",
    "ouroboros_ralph": "background",
    "ouroboros_session_status": "status",
    "ouroboros_start_auto": "background",
    "ouroboros_start_evaluate": "background",
    "ouroboros_start_evolve_step": "background",
    "ouroboros_start_execute_seed": "background",
    "ouroboros_start_ralph": "background",
    "ouroboros_submit_fanout_results": "status",
}

_VALID_OUROBOROS_TOOL_EXECUTION_MODES = frozenset(
    {"blocking", "background", "status", "cancel", "subagent_orchestration"}
)

_PRIVATE_TOOL_SPEC_COMPATIBILITY_ATTRIBUTES = (
    "_OuroborosToolCapabilitySpec",
    "_OUROBOROS_COMPANION_FAMILIES",
    "_OUROBOROS_BACKGROUND_TOOLS",
    "_OUROBOROS_STATUS_TOOLS",
    "_OUROBOROS_CANCEL_TOOLS",
    "_OUROBOROS_WORKSPACE_WRITE_TOOLS",
    "_OUROBOROS_SUBAGENT_TOOLS",
    "_OUROBOROS_DEFAULT_EXECUTION_MODE",
    "_OUROBOROS_DEFAULT_RETRY_METADATA",
    "_OUROBOROS_JOB_POLL_RETRY_METADATA",
    "_OUROBOROS_UNSUPPORTED_RETRY_METADATA",
    "_OUROBOROS_DEFAULT_INTERRUPT_METADATA",
    "_OUROBOROS_BLOCKING_INTERRUPT_METADATA",
    "_OUROBOROS_BACKGROUND_INTERRUPT_METADATA",
    "_OUROBOROS_TERMINAL_CONTROL_INTERRUPT_METADATA_BY_TOOL",
    "_OUROBOROS_UNSUPPORTED_INTERRUPT_METADATA",
    "_OUROBOROS_READ_ONLY_INTERRUPT_METADATA",
    "_OUROBOROS_UNSUPPORTED_CANCEL_METADATA",
    "_OUROBOROS_SIDE_EFFECT_FREE_METADATA",
    "_OUROBOROS_MUTATION_TARGETS_BY_SIDE_EFFECT",
    "_OUROBOROS_STATE_MUTATIONS_BY_TOOL",
    "_OUROBOROS_BACKGROUND_JOB_CANCEL_METADATA",
    "_OUROBOROS_EXECUTION_SESSION_CANCEL_METADATA",
    "_OUROBOROS_BACKGROUND_JOB_CANCEL_CONTROL_METADATA",
    "_OUROBOROS_EXECUTION_SESSION_CANCEL_CONTROL_METADATA",
    "_OUROBOROS_TOOL_CAPABILITY_SPECS",
    "_OUROBOROS_CANCEL_METADATA",
    "_OUROBOROS_BACKGROUND_BLOCKING_COMPANIONS",
    "_OUROBOROS_BACKGROUND_LIFECYCLE_ROLE_TOOLS",
    "_OUROBOROS_JOB_LIFECYCLE_SIBLING_ORDER",
)

_REQUIRED_OUROBOROS_TOOL_METADATA_FIELDS = frozenset(
    {
        "input_schema",
        "execution_mode",
        "companions",
        "required_context_keys",
        "side_effects",
        "retry",
        "interrupt",
        "cancel",
    }
)


def test_private_tool_spec_attributes_remain_importable_from_capabilities_root() -> None:
    for attribute_name in _PRIVATE_TOOL_SPEC_COMPATIBILITY_ATTRIBUTES:
        assert getattr(capabilities_module, attribute_name) is getattr(
            tool_specs_module, attribute_name
        )


_EXPECTED_OUROBOROS_REQUIRED_CONTEXT_KEYS = {
    "ouroboros_ac_tree_hud": ("session_id", "cursor"),
    "ouroboros_auto": (),
    "ouroboros_brownfield": ("indices",),
    "ouroboros_cancel_execution": ("execution_id",),
    "ouroboros_cancel_job": ("job_id",),
    "ouroboros_checklist_verify": ("session_id", "seed_content", "artifact"),
    "ouroboros_evaluate": (
        "session_id",
        "artifact",
        "seed_content",
        "acceptance_criterion",
        "working_dir",
    ),
    "ouroboros_evolve_rewind": ("lineage_id", "to_generation"),
    "ouroboros_evolve_step": ("lineage_id",),
    "ouroboros_execute_seed": ("seed_path", "cwd"),
    "ouroboros_generate_seed": ("session_id",),
    "ouroboros_interview": (
        "initial_context",
        "cwd",
        "confused_terms",
        "references",
        "session_id",
        "answer",
        "last_question",
    ),
    "ouroboros_job_result": ("job_id",),
    "ouroboros_job_status": ("job_id",),
    "ouroboros_job_wait": ("job_id", "cursor"),
    "ouroboros_lateral_think": (
        "problem_context",
        "current_approach",
        "failed_attempts",
    ),
    "ouroboros_lineage_status": ("lineage_id",),
    "ouroboros_measure_drift": ("session_id", "current_output", "seed_content"),
    "ouroboros_pm_interview": ("initial_context", "cwd", "session_id"),
    "ouroboros_qa": (
        "artifact",
        "quality_bar",
        "reference",
        "seed_content",
        "qa_session_id",
        "iteration_history",
    ),
    "ouroboros_query_events": (),
    "ouroboros_query_projection": (),
    "ouroboros_ralph": ("lineage_id",),
    "ouroboros_session_status": ("session_id",),
    "ouroboros_start_auto": (
        "goal",
        "resume",
        "cwd",
        "max_interview_rounds",
        "max_repair_rounds",
        "skip_run",
        "complete_product",
        "pipeline_timeout_seconds",
        "efficiency_mode",
        "frugality_assurance",
    ),
    "ouroboros_start_evaluate": ("session_id", "artifact"),
    "ouroboros_start_evolve_step": ("lineage_id",),
    "ouroboros_start_execute_seed": (
        "seed_content",
        "efficiency_mode",
        "frugality_assurance",
        "session_id",
    ),
    "ouroboros_start_ralph": ("lineage_id",),
    "ouroboros_submit_fanout_results": ("fanout_id", "results"),
}

_EXPECTED_OUROBOROS_TOOL_COMPANIONS = {
    "ouroboros_ac_tree_hud": (
        "ouroboros_session_status",
        "ouroboros_query_events",
        "ouroboros_query_projection",
    ),
    "ouroboros_auto": ("ouroboros_start_auto",),
    "ouroboros_brownfield": (
        "ouroboros_interview",
        "ouroboros_generate_seed",
        "ouroboros_pm_interview",
    ),
    "ouroboros_cancel_execution": (
        "ouroboros_execute_seed",
        "ouroboros_start_execute_seed",
    ),
    "ouroboros_cancel_job": (
        "ouroboros_start_auto",
        "ouroboros_start_execute_seed",
        "ouroboros_start_evaluate",
        "ouroboros_start_evolve_step",
        "ouroboros_start_ralph",
        "ouroboros_job_status",
        "ouroboros_job_wait",
        "ouroboros_job_result",
    ),
    "ouroboros_checklist_verify": (
        "ouroboros_evaluate",
        "ouroboros_start_evaluate",
        "ouroboros_measure_drift",
        "ouroboros_qa",
    ),
    "ouroboros_evaluate": (
        "ouroboros_start_evaluate",
        "ouroboros_job_status",
        "ouroboros_job_wait",
        "ouroboros_job_result",
        "ouroboros_cancel_job",
        "ouroboros_checklist_verify",
        "ouroboros_measure_drift",
        "ouroboros_qa",
    ),
    "ouroboros_evolve_rewind": (
        "ouroboros_evolve_step",
        "ouroboros_start_evolve_step",
        "ouroboros_lineage_status",
        "ouroboros_ralph",
        "ouroboros_start_ralph",
    ),
    "ouroboros_evolve_step": (
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
    "ouroboros_execute_seed": (
        "ouroboros_start_execute_seed",
        "ouroboros_cancel_execution",
        "ouroboros_job_status",
        "ouroboros_job_wait",
        "ouroboros_job_result",
        "ouroboros_cancel_job",
    ),
    "ouroboros_generate_seed": (
        "ouroboros_interview",
        "ouroboros_pm_interview",
        "ouroboros_brownfield",
    ),
    "ouroboros_interview": (
        "ouroboros_generate_seed",
        "ouroboros_pm_interview",
        "ouroboros_brownfield",
        "ouroboros_lateral_think",
    ),
    "ouroboros_job_result": (
        "ouroboros_start_auto",
        "ouroboros_start_execute_seed",
        "ouroboros_start_evaluate",
        "ouroboros_start_evolve_step",
        "ouroboros_start_ralph",
        "ouroboros_job_status",
        "ouroboros_job_wait",
        "ouroboros_cancel_job",
    ),
    "ouroboros_job_status": (
        "ouroboros_start_auto",
        "ouroboros_start_execute_seed",
        "ouroboros_start_evaluate",
        "ouroboros_start_evolve_step",
        "ouroboros_start_ralph",
        "ouroboros_job_wait",
        "ouroboros_job_result",
        "ouroboros_cancel_job",
    ),
    "ouroboros_job_wait": (
        "ouroboros_start_auto",
        "ouroboros_start_execute_seed",
        "ouroboros_start_evaluate",
        "ouroboros_start_evolve_step",
        "ouroboros_start_ralph",
        "ouroboros_job_status",
        "ouroboros_job_result",
        "ouroboros_cancel_job",
    ),
    "ouroboros_lateral_think": ("ouroboros_interview",),
    "ouroboros_lineage_status": (
        "ouroboros_evolve_step",
        "ouroboros_start_evolve_step",
        "ouroboros_evolve_rewind",
        "ouroboros_ralph",
        "ouroboros_start_ralph",
    ),
    "ouroboros_measure_drift": (
        "ouroboros_evaluate",
        "ouroboros_start_evaluate",
        "ouroboros_checklist_verify",
        "ouroboros_qa",
    ),
    "ouroboros_pm_interview": (
        "ouroboros_interview",
        "ouroboros_generate_seed",
        "ouroboros_brownfield",
    ),
    "ouroboros_qa": (
        "ouroboros_evaluate",
        "ouroboros_start_evaluate",
        "ouroboros_checklist_verify",
        "ouroboros_measure_drift",
    ),
    "ouroboros_query_events": (
        "ouroboros_session_status",
        "ouroboros_query_projection",
        "ouroboros_ac_tree_hud",
    ),
    "ouroboros_query_projection": (
        "ouroboros_session_status",
        "ouroboros_query_events",
        "ouroboros_ac_tree_hud",
    ),
    "ouroboros_ralph": (
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
    "ouroboros_session_status": (
        "ouroboros_query_events",
        "ouroboros_query_projection",
        "ouroboros_ac_tree_hud",
    ),
    "ouroboros_start_auto": (
        "ouroboros_auto",
        "ouroboros_job_status",
        "ouroboros_job_wait",
        "ouroboros_job_result",
        "ouroboros_cancel_job",
    ),
    "ouroboros_start_evaluate": (
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
    "ouroboros_start_evolve_step": (
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
    "ouroboros_start_execute_seed": (
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
    "ouroboros_start_ralph": (
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
    "ouroboros_submit_fanout_results": (
        "ouroboros_interview",
        "ouroboros_lateral_think",
    ),
}

_EXPECTED_OUROBOROS_TOOL_SIDE_EFFECTS = {
    "ouroboros_ac_tree_hud": (),
    "ouroboros_auto": ("workspace_write", "event_store_write"),
    "ouroboros_brownfield": ("workspace_write", "event_store_write"),
    "ouroboros_cancel_execution": (
        "runtime_control",
        "event_store_write",
        "session_state_write",
    ),
    "ouroboros_cancel_job": (
        "runtime_control",
        "event_store_write",
        "checkpoint_store_write",
        "session_state_write",
    ),
    "ouroboros_checklist_verify": ("workspace_write", "event_store_write"),
    "ouroboros_evaluate": ("session_state_write",),
    "ouroboros_evolve_rewind": ("session_state_write",),
    "ouroboros_evolve_step": ("workspace_write", "event_store_write"),
    "ouroboros_execute_seed": ("workspace_write", "event_store_write"),
    "ouroboros_generate_seed": ("workspace_write", "event_store_write"),
    "ouroboros_interview": ("subagent_dispatch", "session_state_write"),
    "ouroboros_job_result": (),
    "ouroboros_job_status": (),
    "ouroboros_job_wait": (),
    "ouroboros_lateral_think": ("subagent_dispatch", "session_state_write"),
    "ouroboros_lineage_status": (),
    "ouroboros_measure_drift": ("session_state_write",),
    "ouroboros_pm_interview": ("subagent_dispatch", "session_state_write"),
    "ouroboros_qa": ("session_state_write",),
    "ouroboros_query_events": (),
    "ouroboros_query_projection": (),
    "ouroboros_ralph": ("workspace_write", "event_store_write"),
    "ouroboros_session_status": (),
    "ouroboros_start_auto": ("workspace_write", "event_store_write"),
    "ouroboros_start_evaluate": ("background_job_start", "event_store_write"),
    "ouroboros_start_evolve_step": ("workspace_write", "event_store_write"),
    "ouroboros_start_execute_seed": ("workspace_write", "event_store_write"),
    "ouroboros_start_ralph": ("workspace_write", "event_store_write"),
    "ouroboros_submit_fanout_results": (),
}

_EXPECTED_MUTATION_TARGETS_BY_SIDE_EFFECT = {
    "background_job_start": ("background_job",),
    "checkpoint_store_write": ("checkpoint_store",),
    "event_store_write": ("event_store",),
    "runtime_control": ("runtime",),
    "session_state_write": ("session_state",),
    "side_effect_free": (),
    "subagent_dispatch": ("subagent",),
    "workspace_write": ("workspace",),
}


def _expected_mutation_targets_for_side_effects(
    side_effects: tuple[str, ...],
) -> tuple[str, ...]:
    targets: list[str] = []
    for side_effect in side_effects:
        for target in _EXPECTED_MUTATION_TARGETS_BY_SIDE_EFFECT[side_effect]:
            if target not in targets:
                targets.append(target)
    return tuple(targets)


_EXPECTED_OUROBOROS_TOOL_STATE_MUTATIONS = dict.fromkeys(
    _EXPECTED_OUROBOROS_TOOL_SIDE_EFFECTS,
    (),
) | {
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

_EXPECTED_OUROBOROS_TOOL_MUTATION_TARGETS = {
    name: tuple(
        dict.fromkeys(
            (
                *_expected_mutation_targets_for_side_effects(side_effects),
                *(
                    mutation["target"]
                    for mutation in _EXPECTED_OUROBOROS_TOOL_STATE_MUTATIONS[name]
                ),
            )
        )
    )
    for name, side_effects in _EXPECTED_OUROBOROS_TOOL_SIDE_EFFECTS.items()
}

_EXPECTED_READ_ONLY_OUROBOROS_TOOLS = {
    "ouroboros_ac_tree_hud",
    "ouroboros_job_result",
    "ouroboros_job_status",
    "ouroboros_job_wait",
    "ouroboros_lineage_status",
    "ouroboros_query_events",
    "ouroboros_query_projection",
    "ouroboros_session_status",
    "ouroboros_submit_fanout_results",
}

_EXPECTED_OUROBOROS_TOOL_RETRY = {
    "ouroboros_ac_tree_hud": {"supported": True, "mode": "handler_owned"},
    "ouroboros_auto": {"supported": True, "mode": "handler_owned"},
    "ouroboros_brownfield": {"supported": True, "mode": "handler_owned"},
    "ouroboros_cancel_execution": {"supported": False, "mode": "unsupported"},
    "ouroboros_cancel_job": {"supported": False, "mode": "unsupported"},
    "ouroboros_checklist_verify": {"supported": True, "mode": "handler_owned"},
    "ouroboros_evaluate": {"supported": True, "mode": "handler_owned"},
    "ouroboros_evolve_rewind": {"supported": True, "mode": "handler_owned"},
    "ouroboros_evolve_step": {"supported": True, "mode": "handler_owned"},
    "ouroboros_execute_seed": {"supported": True, "mode": "handler_owned"},
    "ouroboros_generate_seed": {"supported": True, "mode": "handler_owned"},
    "ouroboros_interview": {"supported": True, "mode": "handler_owned"},
    "ouroboros_job_result": {"supported": True, "mode": "handler_owned"},
    "ouroboros_job_status": {"supported": True, "mode": "handler_owned"},
    "ouroboros_job_wait": {"supported": True, "mode": "handler_owned"},
    "ouroboros_lateral_think": {"supported": True, "mode": "handler_owned"},
    "ouroboros_lineage_status": {"supported": True, "mode": "handler_owned"},
    "ouroboros_measure_drift": {"supported": True, "mode": "handler_owned"},
    "ouroboros_pm_interview": {"supported": True, "mode": "handler_owned"},
    "ouroboros_qa": {"supported": True, "mode": "handler_owned"},
    "ouroboros_query_events": {"supported": True, "mode": "handler_owned"},
    "ouroboros_query_projection": {"supported": True, "mode": "handler_owned"},
    "ouroboros_ralph": {"supported": True, "mode": "job_poll"},
    "ouroboros_session_status": {"supported": True, "mode": "handler_owned"},
    "ouroboros_start_auto": {"supported": True, "mode": "job_poll"},
    "ouroboros_start_evaluate": {"supported": True, "mode": "job_poll"},
    "ouroboros_start_evolve_step": {"supported": True, "mode": "job_poll"},
    "ouroboros_start_execute_seed": {"supported": True, "mode": "job_poll"},
    "ouroboros_start_ralph": {"supported": True, "mode": "job_poll"},
    "ouroboros_submit_fanout_results": {"supported": True, "mode": "handler_owned"},
}

_BACKGROUND_INTERRUPT = {
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

_READ_ONLY_INTERRUPT = {
    "supported": True,
    "mode": "read_only_non_mutating",
    "mutation_semantics": "no_state_mutation",
    "resumable": False,
    "target_context_keys": (),
}

_TERMINAL_CONTROL_INTERRUPTS = {
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
}


def _blocking_interrupt(*background_companions: str) -> dict[str, object]:
    return {
        "supported": True,
        "mode": "soft",
        "execution_mode": "blocking",
        "blocking_semantics": "synchronous_handler",
        "resumable": False,
        "background_companions": tuple(background_companions),
        "target_context_keys": (),
    }


_EXPECTED_OUROBOROS_TOOL_INTERRUPT = {
    "ouroboros_ac_tree_hud": _READ_ONLY_INTERRUPT,
    "ouroboros_auto": _blocking_interrupt("ouroboros_start_auto"),
    "ouroboros_brownfield": _blocking_interrupt(),
    "ouroboros_cancel_execution": _TERMINAL_CONTROL_INTERRUPTS["ouroboros_cancel_execution"],
    "ouroboros_cancel_job": _TERMINAL_CONTROL_INTERRUPTS["ouroboros_cancel_job"],
    "ouroboros_checklist_verify": _blocking_interrupt(),
    "ouroboros_evaluate": _blocking_interrupt("ouroboros_start_evaluate"),
    "ouroboros_evolve_rewind": _blocking_interrupt(),
    "ouroboros_evolve_step": _blocking_interrupt("ouroboros_start_evolve_step"),
    "ouroboros_execute_seed": _blocking_interrupt("ouroboros_start_execute_seed"),
    "ouroboros_generate_seed": _blocking_interrupt(),
    "ouroboros_interview": {"supported": True, "mode": "soft"},
    "ouroboros_job_result": _READ_ONLY_INTERRUPT,
    "ouroboros_job_status": _READ_ONLY_INTERRUPT,
    "ouroboros_job_wait": _READ_ONLY_INTERRUPT,
    "ouroboros_lateral_think": {"supported": True, "mode": "soft"},
    "ouroboros_lineage_status": _READ_ONLY_INTERRUPT,
    "ouroboros_measure_drift": _blocking_interrupt(),
    "ouroboros_pm_interview": {"supported": True, "mode": "soft"},
    "ouroboros_qa": _blocking_interrupt(),
    "ouroboros_query_events": _READ_ONLY_INTERRUPT,
    "ouroboros_query_projection": _READ_ONLY_INTERRUPT,
    "ouroboros_ralph": _BACKGROUND_INTERRUPT,
    "ouroboros_session_status": _READ_ONLY_INTERRUPT,
    "ouroboros_start_auto": _BACKGROUND_INTERRUPT,
    "ouroboros_start_evaluate": _BACKGROUND_INTERRUPT,
    "ouroboros_start_evolve_step": _BACKGROUND_INTERRUPT,
    "ouroboros_start_execute_seed": _BACKGROUND_INTERRUPT,
    "ouroboros_start_ralph": _BACKGROUND_INTERRUPT,
    "ouroboros_submit_fanout_results": _READ_ONLY_INTERRUPT,
}

_UNSUPPORTED_CANCEL = {
    "supported": False,
    "mode": "unsupported",
    "companions": (),
    "target_context_keys": (),
}
_BACKGROUND_JOB_CANCEL = {
    "supported": True,
    "mode": "background_job",
    "companions": ("ouroboros_cancel_job",),
    "target_context_keys": ("job_id",),
}
_EXECUTION_SESSION_CANCEL = {
    "supported": True,
    "mode": "execution_session",
    "companions": ("ouroboros_cancel_execution",),
    "target_context_keys": ("execution_id",),
}
_BACKGROUND_JOB_CANCEL_CONTROL = {
    "supported": True,
    "mode": "background_job_control",
    "companions": (
        "ouroboros_job_status",
        "ouroboros_job_wait",
        "ouroboros_job_result",
    ),
    "target_context_keys": ("job_id",),
}
_EXECUTION_SESSION_CANCEL_CONTROL = {
    "supported": True,
    "mode": "execution_session_control",
    "companions": (
        "ouroboros_execute_seed",
        "ouroboros_start_execute_seed",
    ),
    "target_context_keys": ("execution_id",),
}

_EXPECTED_OUROBOROS_TOOL_CANCEL = {
    "ouroboros_ac_tree_hud": _UNSUPPORTED_CANCEL,
    "ouroboros_auto": _UNSUPPORTED_CANCEL,
    "ouroboros_brownfield": _UNSUPPORTED_CANCEL,
    "ouroboros_cancel_execution": _EXECUTION_SESSION_CANCEL_CONTROL,
    "ouroboros_cancel_job": _BACKGROUND_JOB_CANCEL_CONTROL,
    "ouroboros_checklist_verify": _UNSUPPORTED_CANCEL,
    "ouroboros_evaluate": _UNSUPPORTED_CANCEL,
    "ouroboros_evolve_rewind": _UNSUPPORTED_CANCEL,
    "ouroboros_evolve_step": _UNSUPPORTED_CANCEL,
    "ouroboros_execute_seed": _EXECUTION_SESSION_CANCEL,
    "ouroboros_generate_seed": _UNSUPPORTED_CANCEL,
    "ouroboros_interview": _UNSUPPORTED_CANCEL,
    "ouroboros_job_result": _UNSUPPORTED_CANCEL,
    "ouroboros_job_status": _UNSUPPORTED_CANCEL,
    "ouroboros_job_wait": _UNSUPPORTED_CANCEL,
    "ouroboros_lateral_think": _UNSUPPORTED_CANCEL,
    "ouroboros_lineage_status": _UNSUPPORTED_CANCEL,
    "ouroboros_measure_drift": _UNSUPPORTED_CANCEL,
    "ouroboros_pm_interview": _UNSUPPORTED_CANCEL,
    "ouroboros_qa": _UNSUPPORTED_CANCEL,
    "ouroboros_query_events": _UNSUPPORTED_CANCEL,
    "ouroboros_query_projection": _UNSUPPORTED_CANCEL,
    "ouroboros_ralph": _BACKGROUND_JOB_CANCEL,
    "ouroboros_session_status": _UNSUPPORTED_CANCEL,
    "ouroboros_start_auto": _BACKGROUND_JOB_CANCEL,
    "ouroboros_start_evaluate": _BACKGROUND_JOB_CANCEL,
    "ouroboros_start_evolve_step": _BACKGROUND_JOB_CANCEL,
    "ouroboros_start_execute_seed": _BACKGROUND_JOB_CANCEL,
    "ouroboros_start_ralph": _BACKGROUND_JOB_CANCEL,
    "ouroboros_submit_fanout_results": _UNSUPPORTED_CANCEL,
}

_OUROBOROS_TOOL_CONTRACT_CASE_SETS = {
    "execution_modes": _EXPECTED_OUROBOROS_TOOL_EXECUTION_MODES,
    "required_context_keys": _EXPECTED_OUROBOROS_REQUIRED_CONTEXT_KEYS,
    "companions": _EXPECTED_OUROBOROS_TOOL_COMPANIONS,
    "side_effects": _EXPECTED_OUROBOROS_TOOL_SIDE_EFFECTS,
    "mutation_targets": _EXPECTED_OUROBOROS_TOOL_MUTATION_TARGETS,
    "state_mutations": _EXPECTED_OUROBOROS_TOOL_STATE_MUTATIONS,
    "retry": _EXPECTED_OUROBOROS_TOOL_RETRY,
    "interrupt": _EXPECTED_OUROBOROS_TOOL_INTERRUPT,
    "cancel": _EXPECTED_OUROBOROS_TOOL_CANCEL,
}


def _assert_lifecycle_metadata_shape(
    metadata: CapabilityToolMetadata,
    tool_name: str,
) -> None:
    retry = metadata.retry
    interrupt = metadata.interrupt
    cancel = metadata.cancel

    assert set(retry) == {"supported", "mode"}, tool_name
    assert isinstance(retry["supported"], bool), tool_name
    assert isinstance(retry["mode"], str), tool_name
    assert retry["mode"], tool_name

    if interrupt.get("execution_mode") == "blocking":
        assert set(interrupt) == {
            "supported",
            "mode",
            "execution_mode",
            "blocking_semantics",
            "resumable",
            "background_companions",
            "target_context_keys",
        }, tool_name
        assert interrupt["execution_mode"] == "blocking", tool_name
        assert interrupt["blocking_semantics"] == "synchronous_handler", tool_name
        assert interrupt["resumable"] is False, tool_name
        assert isinstance(interrupt["background_companions"], tuple), tool_name
        assert isinstance(interrupt["target_context_keys"], tuple), tool_name
    elif interrupt.get("mode") == "resumable_background_job":
        assert set(interrupt) == {
            "supported",
            "mode",
            "resumable",
            "cancellable",
            "resume_companions",
            "cancel_companions",
            "target_context_keys",
        }, tool_name
        assert isinstance(interrupt["resumable"], bool), tool_name
        assert isinstance(interrupt["cancellable"], bool), tool_name
        assert isinstance(interrupt["resume_companions"], tuple), tool_name
        assert isinstance(interrupt["cancel_companions"], tuple), tool_name
        assert isinstance(interrupt["target_context_keys"], tuple), tool_name
    elif interrupt.get("mode") == "read_only_non_mutating":
        assert set(interrupt) == {
            "supported",
            "mode",
            "mutation_semantics",
            "resumable",
            "target_context_keys",
        }, tool_name
        assert interrupt["supported"] is True, tool_name
        assert interrupt["mutation_semantics"] == "no_state_mutation", tool_name
        assert interrupt["resumable"] is False, tool_name
        assert interrupt["target_context_keys"] == (), tool_name
    elif interrupt.get("mode") == "terminal_control":
        assert set(interrupt) == {
            "supported",
            "mode",
            "terminal_action",
            "target_type",
            "target_context_keys",
            "directive_semantics",
            "terminal_statuses",
            "idempotent",
        }, tool_name
        assert interrupt["supported"] is True, tool_name
        assert interrupt["terminal_action"] == "cancel", tool_name
        assert isinstance(interrupt["target_type"], str), tool_name
        assert interrupt["target_type"], tool_name
        assert isinstance(interrupt["target_context_keys"], tuple), tool_name
        assert interrupt["target_context_keys"], tool_name
        assert isinstance(interrupt["directive_semantics"], str), tool_name
        assert interrupt["directive_semantics"], tool_name
        assert isinstance(interrupt["terminal_statuses"], tuple), tool_name
        assert interrupt["terminal_statuses"], tool_name
        assert isinstance(interrupt["idempotent"], bool), tool_name
    else:
        assert set(interrupt) == {"supported", "mode"}, tool_name
    assert isinstance(interrupt["supported"], bool), tool_name
    assert isinstance(interrupt["mode"], str), tool_name
    assert interrupt["mode"], tool_name

    assert set(cancel) == {
        "supported",
        "mode",
        "companions",
        "target_context_keys",
    }, tool_name
    assert isinstance(cancel["supported"], bool), tool_name
    assert isinstance(cancel["mode"], str), tool_name
    assert cancel["mode"], tool_name
    assert isinstance(cancel["companions"], tuple), tool_name
    assert isinstance(cancel["target_context_keys"], tuple), tool_name


def _sample_value_for_parameter(parameter: MCPToolParameter) -> object:
    if parameter.enum:
        return parameter.enum[0]
    if parameter.type is ToolInputType.STRING:
        return "sample"
    if parameter.type is ToolInputType.NUMBER:
        return 1.5
    if parameter.type is ToolInputType.INTEGER:
        return 1
    if parameter.type is ToolInputType.BOOLEAN:
        return True
    if parameter.type is ToolInputType.ARRAY:
        return []
    if parameter.type is ToolInputType.OBJECT:
        return {}
    raise AssertionError(f"Unhandled parameter type: {parameter.type}")


def _code_investigation_base_request(question: str) -> dict[str, object]:
    return {
        "session_id": "sess-123",
        "question_identity": stable_code_investigation_question_identity(question),
        "question": question,
        "investigation_goal": "describe_current_state_from_code",
        "fact_categories": ["frameworks"],
        "allowed_capabilities": ["inspect_code"],
        "repo_inspection_tool_capabilities": list(
            ouroboros_tool_capability_metadata("ouroboros_interview")["orchestration"][
                "code_investigation"
            ]["repo_inspection_tool_capabilities"]
        ),
        "confidence_policy": {
            "auto_confirm_when": ["exact manifest match"],
            "confirmation_required_when": ["multiple candidates"],
            "human_judgment_when": ["desired behavior"],
        },
        "answer_prefixes": ["[from-code]", "[from-code][auto-confirmed]"],
        "answer_contract": interview_code_investigation_answer_contract(),
        "mcp_tool_capability": ouroboros_tool_capability_metadata("ouroboros_interview"),
    }


def test_extract_capability_input_schema_converts_mcp_tool_parameters() -> None:
    definition = MCPToolDefinition(
        name="ouroboros_schema_probe",
        description="Synthetic schema extraction probe.",
        parameters=(
            MCPToolParameter(
                name="session_id",
                type=ToolInputType.STRING,
                description="Session identifier.",
                required=True,
            ),
            MCPToolParameter(
                name="mode",
                type=ToolInputType.STRING,
                description="Execution mode.",
                required=False,
                default="summary",
                enum=("summary", "full"),
            ),
            MCPToolParameter(
                name="attempt_ids",
                type=ToolInputType.ARRAY,
                description="Attempt identifiers.",
                required=False,
                items={"type": "string"},
            ),
        ),
        server_name="ouroboros",
    )

    schema = extract_capability_input_schema(definition)

    Draft202012Validator.check_schema(schema)
    assert schema == {
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "Session identifier.",
            },
            "mode": {
                "type": "string",
                "description": "Execution mode.",
                "default": "summary",
                "enum": ["summary", "full"],
            },
            "attempt_ids": {
                "type": "array",
                "description": "Attempt identifiers.",
                "items": {"type": "string"},
            },
        },
        "required": ["session_id"],
    }
    assert schema == definition.to_input_schema()
    assert schema is not definition.to_input_schema()
    Draft202012Validator(schema).validate({"session_id": "sess-123"})


def test_extract_capability_input_schema_ignores_free_text_description_fields() -> None:
    definition = MCPToolDefinition(
        name="external_free_text_probe",
        description=(
            "Parameters: session_id: string required; mode: enum(summary, full) "
            "optional default=summary; artifact: object with path: string and "
            "line: integer; retry: boolean = false. This is ordinary operator "
            "guidance in prose, not a machine-readable input schema."
        ),
        parameters=(),
        server_name="external",
    )

    schema = extract_capability_input_schema(definition)

    Draft202012Validator.check_schema(schema)
    assert schema == definition.to_input_schema()
    assert schema == {"type": "object", "properties": {}, "required": []}
    assert set(schema["properties"]) == set()
    assert schema["required"] == []
    assert "session_id" not in schema["properties"]
    assert "mode" not in schema["properties"]
    assert "artifact" not in schema["properties"]
    assert "path" not in schema["properties"]
    assert "line" not in schema["properties"]
    assert "retry" not in schema["properties"]


def test_owned_tool_registry_input_schemas_are_extracted_from_definitions() -> None:
    definitions = {handler.definition.name: handler.definition for handler in get_ouroboros_tools()}
    registry = ouroboros_tool_capability_registry()

    assert set(registry) == set(definitions)
    for name, definition in definitions.items():
        schema = extract_capability_input_schema(definition)

        assert registry[name].input_schema == schema
        assert registry[name].input_schema == definition.to_input_schema()
        Draft202012Validator.check_schema(registry[name].input_schema)


@pytest.mark.parametrize(
    "definition",
    tuple(handler.definition for handler in get_ouroboros_tools()),
    ids=lambda definition: definition.name,
)
def test_each_ouroboros_owned_tool_has_required_capability_metadata_shape(
    definition: MCPToolDefinition,
) -> None:
    graph = build_capability_graph(assemble_session_tool_catalog(attached_tools=(definition,)))
    descriptor = graph.by_name()[definition.name]

    assert descriptor.metadata is not None
    metadata = descriptor.metadata
    metadata_contract_entry = {
        field_name: getattr(metadata, field_name)
        for field_name in _REQUIRED_OUROBOROS_TOOL_METADATA_FIELDS
    }
    serialized_entry = serialize_capability_graph(graph)[0]
    serialized_metadata = serialized_entry["metadata"]

    assert set(metadata_contract_entry) == _REQUIRED_OUROBOROS_TOOL_METADATA_FIELDS
    assert serialized_metadata is not None
    assert set(serialized_metadata) >= _REQUIRED_OUROBOROS_TOOL_METADATA_FIELDS
    assert set(metadata_contract_entry) <= {field.name for field in fields(metadata)}
    assert serialized_entry["name"] == definition.name
    assert metadata == ouroboros_tool_capability_registry()[definition.name]
    assert metadata.fallback_used is False
    assert metadata.input_schema == definition.to_input_schema()
    Draft202012Validator.check_schema(metadata.input_schema)
    assert metadata.input_schema["type"] == "object"
    assert isinstance(metadata.input_schema["properties"], Mapping)
    assert isinstance(metadata.input_schema["required"], list)
    assert metadata.execution_mode in _VALID_OUROBOROS_TOOL_EXECUTION_MODES
    assert isinstance(metadata.companions, tuple)
    assert isinstance(metadata.required_context_keys, tuple)
    assert isinstance(metadata.side_effects, tuple)
    _assert_lifecycle_metadata_shape(metadata, definition.name)
    validate_capability_tool_metadata(
        metadata,
        tool_name=definition.name,
        owned_tool=True,
    )


def test_each_known_ouroboros_owned_mcp_tool_has_explicit_metadata_entry() -> None:
    definitions = {handler.definition.name: handler.definition for handler in get_ouroboros_tools()}
    explicit_specs = capabilities_module._OUROBOROS_TOOL_CAPABILITY_SPECS

    missing_specs = sorted(set(definitions) - set(explicit_specs))
    assert missing_specs == []

    registry = ouroboros_tool_capability_registry()
    assert set(registry) == set(definitions)
    for name in definitions:
        assert name in explicit_specs
        assert name in registry
        assert registry[name].fallback_used is False


@pytest.mark.parametrize(
    "definition",
    tuple(handler.definition for handler in get_ouroboros_tools()),
    ids=lambda definition: definition.name,
)
def test_each_ouroboros_owned_tool_metadata_is_tool_specific_not_generic_inference(
    definition: MCPToolDefinition,
    monkeypatch,
) -> None:
    def fail_on_generic_attached_metadata(tool: MCPToolDefinition):
        raise AssertionError(f"Ouroboros-owned tool used generic attached metadata: {tool.name}")

    def fail_on_generic_attached_semantics(tool: MCPToolDefinition):
        raise AssertionError(f"Ouroboros-owned tool used generic attached semantics: {tool.name}")

    monkeypatch.setattr(
        "ouroboros.orchestrator.capabilities._generic_attached_tool_metadata",
        fail_on_generic_attached_metadata,
    )
    monkeypatch.setattr(
        "ouroboros.orchestrator.capabilities._infer_attached_semantics",
        fail_on_generic_attached_semantics,
    )

    graph = build_capability_graph(assemble_session_tool_catalog(attached_tools=(definition,)))
    descriptor = graph.by_name()[definition.name]

    assert descriptor.metadata is not None
    metadata = descriptor.metadata
    required_metadata = {
        field_name: getattr(metadata, field_name)
        for field_name in _REQUIRED_OUROBOROS_TOOL_METADATA_FIELDS
    }

    assert set(required_metadata) == _REQUIRED_OUROBOROS_TOOL_METADATA_FIELDS
    assert metadata == ouroboros_tool_capability_registry()[definition.name]
    assert metadata.fallback_used is False
    assert metadata.execution_mode != "generic_attached"
    assert metadata.side_effects != ("unknown_external_side_effect",)
    assert metadata.mutation_targets != ("external",)
    assert metadata.input_schema == definition.to_input_schema()
    assert metadata.execution_mode == (_EXPECTED_OUROBOROS_TOOL_EXECUTION_MODES[definition.name])
    assert metadata.companions == _EXPECTED_OUROBOROS_TOOL_COMPANIONS[definition.name]
    assert (
        metadata.required_context_keys
        == (_EXPECTED_OUROBOROS_REQUIRED_CONTEXT_KEYS[definition.name])
    )
    assert metadata.mutation_targets == _EXPECTED_OUROBOROS_TOOL_MUTATION_TARGETS[definition.name]
    assert metadata.state_mutations == _EXPECTED_OUROBOROS_TOOL_STATE_MUTATIONS[definition.name]
    assert metadata.side_effects == _EXPECTED_OUROBOROS_TOOL_SIDE_EFFECTS[definition.name]
    assert metadata.retry == _EXPECTED_OUROBOROS_TOOL_RETRY[definition.name]
    assert metadata.interrupt == _EXPECTED_OUROBOROS_TOOL_INTERRUPT[definition.name]
    assert metadata.cancel == _EXPECTED_OUROBOROS_TOOL_CANCEL[definition.name]
    validate_capability_tool_metadata(
        metadata,
        tool_name=definition.name,
        owned_tool=True,
    )


@pytest.mark.parametrize(
    "definition",
    tuple(handler.definition for handler in get_ouroboros_tools()),
    ids=lambda definition: definition.name,
)
def test_owned_tool_contract_rejects_inferred_attached_snapshot_coverage(
    definition: MCPToolDefinition,
) -> None:
    inferred_snapshot = capabilities_module._generic_attached_tool_metadata(
        replace(definition, server_name="external")
    )

    validate_capability_tool_metadata(
        inferred_snapshot,
        tool_name=definition.name,
        owned_tool=False,
    )
    assert inferred_snapshot.fallback_used is True
    assert inferred_snapshot.execution_mode == "generic_attached"
    assert inferred_snapshot.side_effects == ("unknown_external_side_effect",)

    with pytest.raises(ValueError, match="owned tool metadata cannot use fallback"):
        validate_capability_tool_metadata(
            inferred_snapshot,
            tool_name=definition.name,
            owned_tool=True,
        )


def test_ouroboros_owned_tool_contract_cases_exactly_match_live_catalog() -> None:
    live_tool_names = {handler.definition.name for handler in get_ouroboros_tools()}

    for case_set_name, case_set in _OUROBOROS_TOOL_CONTRACT_CASE_SETS.items():
        assert set(case_set) == live_tool_names, case_set_name


def test_owned_tool_descriptor_uses_canonical_schema_not_attached_description_text() -> None:
    definitions = {handler.definition.name: handler.definition for handler in get_ouroboros_tools()}
    canonical = definitions["ouroboros_session_status"]
    attached_stale_definition = MCPToolDefinition(
        name=canonical.name,
        description=(
            "Poisoned stale attached metadata mentions session_id in prose but "
            "does not carry the canonical parameter schema."
        ),
        parameters=(),
        server_name="third-party-alias",
    )

    graph = build_capability_graph(
        assemble_session_tool_catalog(attached_tools=(attached_stale_definition,))
    )

    descriptor = graph.capabilities[0]
    assert descriptor.metadata is not None
    assert descriptor.name == canonical.name
    assert descriptor.description == attached_stale_definition.description
    assert descriptor.metadata.fallback_used is False
    assert descriptor.metadata.input_schema == extract_capability_input_schema(canonical)
    assert descriptor.metadata.input_schema == canonical.to_input_schema()
    assert descriptor.metadata.input_schema != attached_stale_definition.to_input_schema()
    assert descriptor.metadata.input_schema["required"] == ["session_id"]
    assert descriptor.metadata.input_schema["properties"]["session_id"]["type"] == "string"


def _interview_code_investigation_metadata() -> dict[str, object]:
    owned_tools = tuple(handler.definition for handler in get_ouroboros_tools(include_auto=False))
    catalog = assemble_session_tool_catalog(attached_tools=owned_tools)
    graph = build_capability_graph(catalog)
    descriptors = {descriptor.name: descriptor for descriptor in graph.capabilities}
    interview = descriptors["ouroboros_interview"]
    assert interview.metadata is not None
    return dict(interview.metadata.orchestration["code_investigation"])


def test_mcp_tool_required_parameter_keys_extracts_definition_required_keys() -> None:
    definition = MCPToolDefinition(
        name="external_contract_probe",
        description="Synthetic tool with required and optional parameters",
        parameters=(
            MCPToolParameter(
                name="session_id",
                type=ToolInputType.STRING,
                required=True,
            ),
            MCPToolParameter(
                name="optional_note",
                type=ToolInputType.STRING,
                required=False,
            ),
            MCPToolParameter(
                name="artifact",
                type=ToolInputType.OBJECT,
                required=True,
            ),
        ),
    )
    optional_only_definition = MCPToolDefinition(
        name="external_optional_probe",
        description="Synthetic tool without required parameters",
        parameters=(
            MCPToolParameter(
                name="cursor",
                type=ToolInputType.STRING,
                required=False,
            ),
        ),
    )

    assert mcp_tool_required_parameter_keys(definition) == ("session_id", "artifact")
    assert mcp_tool_required_parameter_keys(optional_only_definition) == ()


def test_owned_required_context_keys_union_definition_and_skill_usage(
    monkeypatch,
) -> None:
    definition = MCPToolDefinition(
        name="ouroboros_union_probe",
        description="Synthetic owned tool for context-key union.",
        parameters=(
            MCPToolParameter(
                name="session_id",
                type=ToolInputType.STRING,
                required=True,
            ),
            MCPToolParameter(
                name="cwd",
                type=ToolInputType.STRING,
                required=False,
            ),
            MCPToolParameter(
                name="artifact",
                type=ToolInputType.STRING,
                required=True,
            ),
        ),
        server_name="ouroboros",
    )

    monkeypatch.setattr(
        "ouroboros.orchestrator.capabilities._ouroboros_tool_definitions_by_name",
        lambda: {definition.name: definition},
    )
    monkeypatch.setattr(
        "ouroboros.orchestrator.capabilities.get_packaged_skill_context_keys",
        lambda: {
            definition.name: (
                "cwd",
                "session_id",
                "artifact",
                "last_question",
            )
        },
    )
    monkeypatch.setattr(
        "ouroboros.orchestrator.capabilities._OUROBOROS_TOOL_CAPABILITY_SPECS",
        {
            definition.name: capabilities_module._OuroborosToolCapabilitySpec(
                execution_mode="blocking",
                companions=(),
                side_effects=("session_state_write",),
                retry={"supported": True, "mode": "handler_owned"},
                interrupt={"supported": True, "mode": "soft"},
            )
        },
    )
    monkeypatch.setattr(
        "ouroboros.orchestrator.capabilities._OUROBOROS_CANCEL_METADATA",
        {definition.name: _UNSUPPORTED_CANCEL},
    )

    graph = build_capability_graph((definition,))
    descriptor = graph.by_name()[definition.name]

    assert descriptor.metadata is not None
    assert descriptor.metadata.fallback_used is False
    assert descriptor.metadata.input_schema["required"] == ["session_id", "artifact"]
    assert descriptor.metadata.required_context_keys == (
        "session_id",
        "artifact",
        "cwd",
        "last_question",
    )


def test_packaged_skill_tool_mappings_are_stored_apart_from_descriptors() -> None:
    expected = {
        "auto": "ouroboros_start_auto",
        "run": "ouroboros_execute_seed",
        "ralph": "ouroboros_ralph",
        "status": "ouroboros_session_status",
        "seed": "ouroboros_generate_seed",
        "interview": "ouroboros_interview",
    }

    get_packaged_skill_tool_mappings.cache_clear()
    try:
        with patch(
            "ouroboros.mcp.tools.definitions.get_ouroboros_tools",
            side_effect=AssertionError("descriptor catalog must not be used"),
        ):
            queried_mappings = {skill: get_skill_tool_mapping(skill) for skill in expected}
            assert get_skill_tool_mapping("   ") is None
            mappings = get_packaged_skill_tool_mappings()
    finally:
        get_packaged_skill_tool_mappings.cache_clear()

    by_skill = skill_tool_mapping_by_skill(mappings)
    assert {skill: by_skill[skill].mcp_tool for skill in expected} == expected
    assert {
        skill: mapping.mcp_tool for skill, mapping in queried_mappings.items() if mapping
    } == expected
    for skill_name in expected:
        mapping = by_skill[skill_name]
        assert mapping.skill_path == f"skills/{skill_name}/SKILL.md"
        assert mapping.mcp_args is not None


def test_skill_capability_resolution_maps_core_skills_to_owned_mcp_tools() -> None:
    expected = {
        "auto": "ouroboros_start_auto",
        "run": "ouroboros_execute_seed",
        "ralph": "ouroboros_ralph",
        "status": "ouroboros_session_status",
        "seed": "ouroboros_generate_seed",
        "interview": "ouroboros_interview",
    }
    tool_definitions = tuple(handler.definition for handler in get_ouroboros_tools())
    graph = build_capability_graph(assemble_session_tool_catalog(attached_tools=tool_definitions))

    resolved = {
        skill_name: resolve_skill_capability_descriptor(skill_name, graph=graph)
        for skill_name in expected
    }

    assert {
        skill: descriptor.name for skill, descriptor in resolved.items() if descriptor
    } == expected
    for skill_name, descriptor in resolved.items():
        assert descriptor is not None, skill_name
        assert descriptor.source_kind == "attached_mcp"
        assert descriptor.semantics.origin is CapabilityOrigin.ATTACHED_MCP
        assert descriptor.metadata is not None
        assert descriptor.metadata.fallback_used is False
        assert descriptor.metadata.input_schema
    assert resolve_skill_capability_descriptor("unknown-skill", graph=graph) is None


def test_core_skills_resolve_end_to_end_to_explicit_mcp_capabilities() -> None:
    definitions = {handler.definition.name: handler.definition for handler in get_ouroboros_tools()}
    graph = build_capability_graph(
        assemble_session_tool_catalog(attached_tools=definitions.values())
    )

    expected = {
        "auto": {
            "tool_name": "ouroboros_start_auto",
            "execution_mode": "background",
            "side_effects": ("workspace_write", "event_store_write"),
        },
        "run": {
            "tool_name": "ouroboros_execute_seed",
            "execution_mode": "blocking",
            "side_effects": ("workspace_write", "event_store_write"),
        },
        "ralph": {
            "tool_name": "ouroboros_ralph",
            "execution_mode": "background",
            "side_effects": ("workspace_write", "event_store_write"),
        },
        "status": {
            "tool_name": "ouroboros_session_status",
            "execution_mode": "status",
            "side_effects": (),
        },
        "seed": {
            "tool_name": "ouroboros_generate_seed",
            "execution_mode": "blocking",
            "side_effects": ("workspace_write", "event_store_write"),
        },
        "interview": {
            "tool_name": "ouroboros_interview",
            "execution_mode": "subagent_orchestration",
            "side_effects": ("subagent_dispatch", "session_state_write"),
        },
    }

    for skill_name, expected_metadata in expected.items():
        descriptor = resolve_skill_capability_descriptor(skill_name, graph=graph)

        assert descriptor is not None, skill_name
        assert descriptor.name == expected_metadata["tool_name"]
        assert descriptor.original_name == expected_metadata["tool_name"]
        assert descriptor.source_kind == "attached_mcp"
        assert descriptor.source_name == "ouroboros"
        assert descriptor.stable_id == (
            f"mcp:{descriptor.source_name}:{expected_metadata['tool_name']}"
        )
        assert descriptor.semantics.origin is CapabilityOrigin.ATTACHED_MCP
        assert descriptor.semantics.scope is CapabilityScope.ATTACHMENT
        assert descriptor.metadata is not None
        assert descriptor.metadata.fallback_used is False
        assert (
            descriptor.metadata.input_schema
            == definitions[expected_metadata["tool_name"]].to_input_schema()
        )
        assert descriptor.metadata.mutation_class == descriptor.semantics.mutation_class.value
        assert descriptor.metadata.execution_mode == expected_metadata["execution_mode"]
        assert (
            descriptor.metadata.mutation_targets
            == (_EXPECTED_OUROBOROS_TOOL_MUTATION_TARGETS[expected_metadata["tool_name"]])
        )
        assert (
            descriptor.metadata.state_mutations
            == (_EXPECTED_OUROBOROS_TOOL_STATE_MUTATIONS[expected_metadata["tool_name"]])
        )
        assert descriptor.metadata.side_effects == expected_metadata["side_effects"]
        assert (
            descriptor.metadata.required_context_keys
            == (_EXPECTED_OUROBOROS_REQUIRED_CONTEXT_KEYS[expected_metadata["tool_name"]])
        )
        assert (
            descriptor.metadata
            == ouroboros_tool_capability_registry()[expected_metadata["tool_name"]]
        )
        assert (
            resolve_mcp_tool_capability_descriptor(
                descriptor.stable_id,
                graph=graph,
            )
            == descriptor
        )


def test_ouroboros_owned_tools_have_explicit_capability_metadata(monkeypatch) -> None:
    tool_definitions = tuple(handler.definition for handler in get_ouroboros_tools())
    catalog = assemble_session_tool_catalog(attached_tools=tool_definitions)

    fallback_calls: list[str] = []

    def fail_on_generic_fallback(tool: MCPToolDefinition):
        fallback_calls.append(tool.name)
        raise AssertionError(f"Ouroboros-owned tool used generic attached inference: {tool.name}")

    def fail_on_required_parameter_inference(tool: MCPToolDefinition) -> tuple[str, ...]:
        raise AssertionError(f"Ouroboros-owned tool used required-parameter inference: {tool.name}")

    monkeypatch.setattr(
        "ouroboros.orchestrator.capabilities._infer_attached_semantics",
        fail_on_generic_fallback,
    )
    monkeypatch.setattr(
        "ouroboros.orchestrator.capabilities._required_parameter_names",
        fail_on_required_parameter_inference,
    )

    graph = build_capability_graph(catalog)
    direct_graph = build_capability_graph(tool_definitions)

    descriptors = {descriptor.name: descriptor for descriptor in graph.capabilities}
    direct_descriptors = {descriptor.name: descriptor for descriptor in direct_graph.capabilities}
    assert set(descriptors) == {definition.name for definition in tool_definitions}
    assert set(direct_descriptors) == set(descriptors)
    for definition in tool_definitions:
        descriptor = descriptors[definition.name]
        direct_descriptor = direct_descriptors[definition.name]
        metadata = descriptor.metadata
        assert metadata is not None, definition.name
        assert direct_descriptor.metadata is not None, definition.name
        assert direct_descriptor.source_kind == "attached_mcp"
        assert direct_descriptor.source_name == "ouroboros"
        assert metadata.fallback_used is False, definition.name
        assert direct_descriptor.metadata.fallback_used is False, definition.name
        assert metadata.input_schema == definition.to_input_schema()
        assert direct_descriptor.metadata.input_schema == definition.to_input_schema()
        assert metadata.mutation_class == descriptor.semantics.mutation_class.value
        assert (
            direct_descriptor.metadata.mutation_class
            == direct_descriptor.semantics.mutation_class.value
        )
        assert metadata.execution_mode
        assert isinstance(metadata.companions, tuple)
        assert all(companion in descriptors for companion in metadata.companions), definition.name
        assert (
            metadata.required_context_keys
            == (_EXPECTED_OUROBOROS_REQUIRED_CONTEXT_KEYS[definition.name])
        )
        assert isinstance(metadata.required_context_keys, tuple)
        assert (
            metadata.mutation_targets
            == (_EXPECTED_OUROBOROS_TOOL_MUTATION_TARGETS[definition.name])
        )
        assert (
            metadata.state_mutations == (_EXPECTED_OUROBOROS_TOOL_STATE_MUTATIONS[definition.name])
        )
        assert metadata.side_effects is not None
        _assert_lifecycle_metadata_shape(metadata, definition.name)
        _assert_lifecycle_metadata_shape(direct_descriptor.metadata, definition.name)
        assert descriptor.semantics.origin is CapabilityOrigin.ATTACHED_MCP
        assert descriptor.semantics.scope is CapabilityScope.ATTACHMENT
    assert fallback_calls == []


def test_ouroboros_capability_registry_covers_every_owned_tool_identifier() -> None:
    definitions = {handler.definition.name: handler.definition for handler in get_ouroboros_tools()}

    registry = ouroboros_tool_capability_registry()

    assert set(registry) == set(definitions)
    assert set(registry) == set(_EXPECTED_OUROBOROS_REQUIRED_CONTEXT_KEYS)
    for name, metadata in registry.items():
        definition = definitions[name]
        required_parameters = tuple(
            parameter for parameter in definition.parameters if parameter.required
        )
        required_parameter_keys = mcp_tool_required_parameter_keys(definition)
        assert metadata.fallback_used is False, name
        assert metadata.input_schema == definition.to_input_schema()
        Draft202012Validator.check_schema(metadata.input_schema)
        assert metadata.input_schema["type"] == "object"
        assert set(metadata.input_schema["properties"]) == {
            parameter.name for parameter in definition.parameters
        }
        assert metadata.input_schema["required"] == list(required_parameter_keys)
        assert metadata.mutation_class in {
            mutation_class.value for mutation_class in CapabilityMutationClass
        }
        for parameter in definition.parameters:
            property_schema = metadata.input_schema["properties"][parameter.name]
            assert property_schema["type"] == parameter.type.value
            if parameter.enum:
                assert property_schema["enum"] == list(parameter.enum)
            if parameter.items:
                assert property_schema["items"] == parameter.items
        Draft202012Validator(metadata.input_schema).validate(
            {
                parameter.name: _sample_value_for_parameter(parameter)
                for parameter in required_parameters
            }
        )
        assert metadata.execution_mode, name
        assert isinstance(metadata.companions, tuple)
        expected_context_keys = _EXPECTED_OUROBOROS_REQUIRED_CONTEXT_KEYS[name]
        assert metadata.required_context_keys == expected_context_keys
        assert all(key in metadata.input_schema["properties"] for key in expected_context_keys)
        if not expected_context_keys:
            assert metadata.input_schema["required"] == []
        assert metadata.mutation_targets == _EXPECTED_OUROBOROS_TOOL_MUTATION_TARGETS[name]
        assert isinstance(metadata.mutation_targets, tuple)
        assert metadata.state_mutations == _EXPECTED_OUROBOROS_TOOL_STATE_MUTATIONS[name]
        assert isinstance(metadata.state_mutations, tuple)
        assert isinstance(metadata.side_effects, tuple)
        _assert_lifecycle_metadata_shape(metadata, name)
        assert isinstance(metadata.orchestration, Mapping)


@pytest.mark.parametrize(
    "tool_name",
    (
        "ouroboros_interview",
        "ouroboros_start_execute_seed",
        "ouroboros_job_status",
        "ouroboros_cancel_job",
    ),
)
def test_explicit_ouroboros_tool_capability_lookup_returns_tool_specific_metadata(
    tool_name: str,
) -> None:
    definitions = {handler.definition.name: handler.definition for handler in get_ouroboros_tools()}
    registry = ouroboros_tool_capability_registry()

    by_name = lookup_ouroboros_tool_capability_metadata(tool_name)
    by_stable_id = lookup_ouroboros_tool_capability_metadata(f"mcp:ouroboros:{tool_name}")

    assert by_name == registry[tool_name]
    assert by_stable_id == registry[tool_name]
    assert by_name is not None
    assert by_name.fallback_used is False
    assert by_name.execution_mode == _EXPECTED_OUROBOROS_TOOL_EXECUTION_MODES[tool_name]
    assert by_name.input_schema == definitions[tool_name].to_input_schema()
    assert by_name.side_effects == _EXPECTED_OUROBOROS_TOOL_SIDE_EFFECTS[tool_name]
    assert by_name.retry == _EXPECTED_OUROBOROS_TOOL_RETRY[tool_name]
    assert by_name.interrupt == _EXPECTED_OUROBOROS_TOOL_INTERRUPT[tool_name]
    assert by_name.cancel == _EXPECTED_OUROBOROS_TOOL_CANCEL[tool_name]


def test_explicit_ouroboros_tool_capability_lookup_rejects_unknown_ids() -> None:
    assert lookup_ouroboros_tool_capability_metadata("external_delete_widget") is None
    assert lookup_ouroboros_tool_capability_metadata("mcp:external:ouroboros_interview") is None


def test_ouroboros_owned_tools_have_explicit_valid_execution_mode_assignments() -> None:
    definitions = {handler.definition.name: handler.definition for handler in get_ouroboros_tools()}

    registry = ouroboros_tool_capability_registry()
    graph = build_capability_graph(
        assemble_session_tool_catalog(attached_tools=definitions.values())
    )
    descriptors = {descriptor.name: descriptor for descriptor in graph.capabilities}

    assert set(registry) == set(_EXPECTED_OUROBOROS_TOOL_EXECUTION_MODES)
    assert set(definitions) == set(_EXPECTED_OUROBOROS_TOOL_EXECUTION_MODES)
    for name, expected_mode in _EXPECTED_OUROBOROS_TOOL_EXECUTION_MODES.items():
        metadata = registry[name]
        descriptor = descriptors[name]
        assert expected_mode in _VALID_OUROBOROS_TOOL_EXECUTION_MODES, name
        assert metadata.execution_mode == expected_mode, name
        assert descriptor.metadata is not None, name
        assert descriptor.metadata.mutation_class == descriptor.semantics.mutation_class.value
        assert descriptor.metadata.execution_mode == expected_mode, name
        assert descriptor.metadata.fallback_used is False, name


def test_ouroboros_owned_tools_have_explicit_tool_specific_companions() -> None:
    definitions = {handler.definition.name: handler.definition for handler in get_ouroboros_tools()}
    registry = ouroboros_tool_capability_registry()
    graph = build_capability_graph(
        assemble_session_tool_catalog(attached_tools=definitions.values())
    )
    descriptors = {descriptor.name: descriptor for descriptor in graph.capabilities}

    assert set(registry) == set(_EXPECTED_OUROBOROS_TOOL_COMPANIONS)
    assert set(definitions) == set(_EXPECTED_OUROBOROS_TOOL_COMPANIONS)
    for name, expected_companions in _EXPECTED_OUROBOROS_TOOL_COMPANIONS.items():
        metadata = registry[name]
        descriptor = descriptors[name]

        assert metadata.companions == expected_companions, name
        assert name not in metadata.companions
        assert len(metadata.companions) == len(set(metadata.companions)), name
        assert all(companion in definitions for companion in metadata.companions), name
        assert descriptor.metadata is not None, name
        assert descriptor.metadata.companions == expected_companions, name
        assert descriptor.metadata.fallback_used is False, name


def test_background_tools_expose_explicit_lifecycle_companion_roles() -> None:
    definitions = {handler.definition.name: handler.definition for handler in get_ouroboros_tools()}
    graph = build_capability_graph(
        assemble_session_tool_catalog(attached_tools=definitions.values())
    )
    descriptors = {descriptor.name: descriptor for descriptor in graph.capabilities}
    background_tools = {
        "ouroboros_start_auto": "ouroboros_auto",
        "ouroboros_start_evaluate": "ouroboros_evaluate",
        "ouroboros_start_evolve_step": "ouroboros_evolve_step",
        "ouroboros_start_execute_seed": "ouroboros_execute_seed",
        "ouroboros_ralph": None,
        "ouroboros_start_ralph": None,
    }
    lifecycle_roles = {
        "status": "ouroboros_job_status",
        "wait": "ouroboros_job_wait",
        "result": "ouroboros_job_result",
        "cancel": "ouroboros_cancel_job",
    }

    assert set(background_tools).issubset(definitions)
    assert set(lifecycle_roles.values()).issubset(definitions)
    for start_tool, blocking_tool in background_tools.items():
        descriptor = descriptors[start_tool]
        expected_companion_roles = {"start": start_tool, **lifecycle_roles}
        if blocking_tool:
            expected_companion_roles["blocking"] = blocking_tool

        assert descriptor.metadata is not None, start_tool
        assert descriptor.metadata.fallback_used is False, start_tool
        assert descriptor.metadata.execution_mode == "background", start_tool
        assert set(lifecycle_roles.values()).issubset(descriptor.metadata.companions), start_tool
        lifecycle = descriptor.metadata.orchestration["background_lifecycle"]

        assert lifecycle["family_id"] == "background_job_lifecycle.v1"
        assert lifecycle["execution_mode"] == "background"
        assert lifecycle["companion_roles"] == expected_companion_roles
        assert lifecycle["required_result_context_keys"] == ("job_id",)
        assert lifecycle["required_result_context_keys_by_dispatch"] == {
            "non_plugin": ("job_id",),
            "plugin": (),
        }
        assert lifecycle["plugin_delegation"] == {
            "supported": True,
            "dispatch_mode": "plugin",
            "status": "delegated_to_plugin",
            "job_id": None,
            "pollable": False,
            "cancel_via_job": False,
        }
        assert lifecycle["cancel"] == {
            "supported": True,
            "mode": "background_job",
            "companions": ("ouroboros_cancel_job",),
            "target_context_keys": ("job_id",),
        }
        assert "status" in lifecycle["runtime_instruction"]
        assert "wait" in lifecycle["runtime_instruction"]
        assert "result" in lifecycle["runtime_instruction"]
        assert "cancel" in lifecycle["runtime_instruction"]
        assert "job_id=None" in lifecycle["runtime_instruction"]
        assert "not pollable" in lifecycle["runtime_instruction"]


def test_generic_job_lifecycle_tools_derive_sibling_companions_from_catalog(
    monkeypatch,
) -> None:
    definitions = {handler.definition.name: handler.definition for handler in get_ouroboros_tools()}
    lifecycle_roles = {
        "status": "ouroboros_job_status",
        "wait": "ouroboros_job_wait",
        "result": "ouroboros_job_result",
        "cancel": "ouroboros_cancel_job",
    }
    lifecycle_tools = set(lifecycle_roles.values())
    patched_specs = {
        name: replace(spec, companions=()) if name in lifecycle_tools else spec
        for name, spec in capabilities_module._OUROBOROS_TOOL_CAPABILITY_SPECS.items()
    }
    monkeypatch.setattr(
        capabilities_module,
        "_OUROBOROS_TOOL_CAPABILITY_SPECS",
        patched_specs,
    )

    registry = ouroboros_tool_capability_registry()
    graph = build_capability_graph(
        assemble_session_tool_catalog(attached_tools=definitions.values())
    )

    assert lifecycle_tools.issubset(definitions)
    for role, tool_name in lifecycle_roles.items():
        expected_siblings = tuple(
            sibling_name for sibling_name in lifecycle_roles.values() if sibling_name != tool_name
        )
        metadata = registry[tool_name]
        descriptor = graph.by_name()[tool_name]

        assert metadata.companions == expected_siblings
        assert descriptor.metadata is not None
        assert descriptor.metadata.companions == expected_siblings
        assert descriptor.metadata.fallback_used is False
        sibling_metadata = metadata.orchestration["job_lifecycle_siblings"]
        assert sibling_metadata["family_id"] == "generic_job_lifecycle_siblings.v1"
        assert sibling_metadata["role"] == role
        assert sibling_metadata["companion_roles"] == lifecycle_roles
        assert sibling_metadata["sibling_companions"] == {
            sibling_role: sibling_name
            for sibling_role, sibling_name in lifecycle_roles.items()
            if sibling_name != tool_name
        }
        assert sibling_metadata["required_result_context_keys"] == ("job_id",)
        assert "status/wait/result/cancel" in sibling_metadata["runtime_instruction"]


@pytest.mark.parametrize(
    ("omitted_role", "omitted_tool"),
    (
        ("status", "ouroboros_job_status"),
        ("wait", "ouroboros_job_wait"),
        ("result", "ouroboros_job_result"),
        ("cancel", "ouroboros_cancel_job"),
    ),
)
def test_generic_job_lifecycle_sibling_metadata_filters_each_absent_sibling(
    monkeypatch,
    omitted_role: str,
    omitted_tool: str,
) -> None:
    full_definitions = {
        handler.definition.name: handler.definition for handler in get_ouroboros_tools()
    }
    partial_definitions = tuple(
        definition for name, definition in full_definitions.items() if name != omitted_tool
    )
    partial_handlers = tuple(
        SimpleNamespace(definition=definition) for definition in partial_definitions
    )
    lifecycle_roles = {
        "status": "ouroboros_job_status",
        "wait": "ouroboros_job_wait",
        "result": "ouroboros_job_result",
        "cancel": "ouroboros_cancel_job",
    }
    lifecycle_tools = set(lifecycle_roles.values())
    patched_specs = {
        name: replace(spec, companions=()) if name in lifecycle_tools else spec
        for name, spec in capabilities_module._OUROBOROS_TOOL_CAPABILITY_SPECS.items()
    }

    capabilities_module._ouroboros_tool_definitions_by_name.cache_clear()
    monkeypatch.setattr(
        "ouroboros.mcp.tools.definitions.get_ouroboros_tools",
        lambda: partial_handlers,
    )
    monkeypatch.setattr(
        capabilities_module,
        "_OUROBOROS_TOOL_CAPABILITY_SPECS",
        patched_specs,
    )

    try:
        registry = ouroboros_tool_capability_registry()
    finally:
        capabilities_module._ouroboros_tool_definitions_by_name.cache_clear()

    assert omitted_tool not in registry
    available_roles = {
        role: tool_name for role, tool_name in lifecycle_roles.items() if tool_name != omitted_tool
    }
    assert omitted_role not in available_roles
    for role, tool_name in available_roles.items():
        metadata = registry[tool_name]
        expected_siblings = tuple(
            sibling_name for sibling_name in available_roles.values() if sibling_name != tool_name
        )

        assert metadata.fallback_used is False
        assert metadata.companions == expected_siblings
        assert omitted_tool not in metadata.companions
        sibling_metadata = metadata.orchestration["job_lifecycle_siblings"]
        assert sibling_metadata["role"] == role
        assert sibling_metadata["companion_roles"] == available_roles
        assert omitted_role not in sibling_metadata["companion_roles"]
        assert sibling_metadata["sibling_companions"] == {
            sibling_role: sibling_name
            for sibling_role, sibling_name in available_roles.items()
            if sibling_name != tool_name
        }
        assert omitted_role not in sibling_metadata["sibling_companions"]
        assert omitted_tool not in sibling_metadata["sibling_companions"].values()


def test_background_lifecycle_omits_absent_companion_tools(monkeypatch) -> None:
    full_definitions = {
        handler.definition.name: handler.definition for handler in get_ouroboros_tools()
    }
    omitted_companions = {"ouroboros_job_result", "ouroboros_cancel_job"}
    partial_definitions = tuple(
        definition
        for name, definition in full_definitions.items()
        if name not in omitted_companions
    )
    partial_handlers = tuple(
        SimpleNamespace(definition=definition) for definition in partial_definitions
    )

    capabilities_module._ouroboros_tool_definitions_by_name.cache_clear()
    monkeypatch.setattr(
        "ouroboros.mcp.tools.definitions.get_ouroboros_tools",
        lambda: partial_handlers,
    )

    try:
        registry = ouroboros_tool_capability_registry()
        graph = build_capability_graph(
            assemble_session_tool_catalog(attached_tools=partial_definitions)
        )
    finally:
        capabilities_module._ouroboros_tool_definitions_by_name.cache_clear()

    descriptors = {descriptor.name: descriptor for descriptor in graph.capabilities}
    assert set(registry) == {definition.name for definition in partial_definitions}
    assert omitted_companions.isdisjoint(registry)

    start_auto = registry["ouroboros_start_auto"]
    descriptor = descriptors["ouroboros_start_auto"]
    assert descriptor.metadata is not None
    assert descriptor.metadata.fallback_used is False
    assert descriptor.metadata.companions == start_auto.companions
    assert "ouroboros_job_status" in start_auto.companions
    assert "ouroboros_job_wait" in start_auto.companions
    assert omitted_companions.isdisjoint(start_auto.companions)

    lifecycle = start_auto.orchestration["background_lifecycle"]
    assert lifecycle["companion_roles"] == {
        "start": "ouroboros_start_auto",
        "blocking": "ouroboros_auto",
        "status": "ouroboros_job_status",
        "wait": "ouroboros_job_wait",
    }
    assert "result" not in lifecycle["runtime_instruction"]
    assert "cancel" not in lifecycle["companion_roles"]
    assert lifecycle["cancel"] == {
        "supported": False,
        "mode": "unsupported",
        "companions": (),
        "target_context_keys": (),
    }


def test_run_family_exposes_start_and_lifecycle_companion_metadata() -> None:
    definitions = {handler.definition.name: handler.definition for handler in get_ouroboros_tools()}
    graph = build_capability_graph(
        assemble_session_tool_catalog(attached_tools=definitions.values())
    )
    execute = graph.by_name()["ouroboros_execute_seed"]
    expected_roles = {
        "primary": "ouroboros_execute_seed",
        "start": "ouroboros_start_execute_seed",
        "execution_cancel": "ouroboros_cancel_execution",
        "status": "ouroboros_job_status",
        "wait": "ouroboros_job_wait",
        "result": "ouroboros_job_result",
        "cancel": "ouroboros_cancel_job",
    }

    assert set(expected_roles.values()).issubset(definitions)
    assert execute.metadata is not None
    assert execute.metadata.fallback_used is False
    assert execute.metadata.execution_mode == "blocking"
    assert set(expected_roles.values()) - {"ouroboros_execute_seed"} <= set(
        execute.metadata.companions
    )

    run_family = execute.metadata.orchestration["run_family"]
    assert run_family["family_id"] == "run_execute_lifecycle.v1"
    assert run_family["execution_mode"] == "blocking"
    assert run_family["companion_roles"] == expected_roles
    assert run_family["start_variant"] == "ouroboros_start_execute_seed"
    assert run_family["required_result_context_keys"] == {
        "execution_cancel": ("execution_id",),
        "background_job": ("job_id",),
    }
    assert run_family["cancel"] == {
        "supported": True,
        "mode": "execution_session",
        "companions": ("ouroboros_cancel_execution",),
        "target_context_keys": ("execution_id",),
    }
    assert "start variant" in run_family["runtime_instruction"]
    assert "status" in run_family["runtime_instruction"]
    assert "wait" in run_family["runtime_instruction"]
    assert "result" in run_family["runtime_instruction"]
    assert "cancel" in run_family["runtime_instruction"]


def test_run_family_omits_absent_lifecycle_companion_tools(monkeypatch) -> None:
    full_definitions = {
        handler.definition.name: handler.definition for handler in get_ouroboros_tools()
    }
    omitted_companions = {"ouroboros_job_result", "ouroboros_cancel_job"}
    partial_definitions = tuple(
        definition
        for name, definition in full_definitions.items()
        if name not in omitted_companions
    )
    partial_handlers = tuple(
        SimpleNamespace(definition=definition) for definition in partial_definitions
    )

    capabilities_module._ouroboros_tool_definitions_by_name.cache_clear()
    monkeypatch.setattr(
        "ouroboros.mcp.tools.definitions.get_ouroboros_tools",
        lambda: partial_handlers,
    )

    try:
        registry = ouroboros_tool_capability_registry()
        graph = build_capability_graph(
            assemble_session_tool_catalog(attached_tools=partial_definitions)
        )
    finally:
        capabilities_module._ouroboros_tool_definitions_by_name.cache_clear()

    execute = graph.by_name()["ouroboros_execute_seed"]
    assert omitted_companions.isdisjoint(registry)
    assert execute.metadata is not None
    assert execute.metadata.fallback_used is False
    assert "ouroboros_start_execute_seed" in execute.metadata.companions
    assert "ouroboros_cancel_execution" in execute.metadata.companions
    assert "ouroboros_job_status" in execute.metadata.companions
    assert "ouroboros_job_wait" in execute.metadata.companions
    assert omitted_companions.isdisjoint(execute.metadata.companions)

    run_family = execute.metadata.orchestration["run_family"]
    assert run_family["companion_roles"] == {
        "primary": "ouroboros_execute_seed",
        "start": "ouroboros_start_execute_seed",
        "execution_cancel": "ouroboros_cancel_execution",
        "status": "ouroboros_job_status",
        "wait": "ouroboros_job_wait",
    }
    assert run_family["start_variant"] == "ouroboros_start_execute_seed"
    assert run_family["required_result_context_keys"] == {
        "execution_cancel": ("execution_id",),
        "background_job": ("job_id",),
    }
    assert "result" not in run_family["runtime_instruction"]
    assert "execution_cancel" in run_family["runtime_instruction"]


def test_evaluate_family_exposes_start_and_lifecycle_companion_metadata() -> None:
    definitions = {handler.definition.name: handler.definition for handler in get_ouroboros_tools()}
    graph = build_capability_graph(
        assemble_session_tool_catalog(attached_tools=definitions.values())
    )
    evaluate = graph.by_name()["ouroboros_evaluate"]
    expected_roles = {
        "primary": "ouroboros_evaluate",
        "start": "ouroboros_start_evaluate",
        "checklist_verify": "ouroboros_checklist_verify",
        "measure_drift": "ouroboros_measure_drift",
        "qa": "ouroboros_qa",
        "status": "ouroboros_job_status",
        "wait": "ouroboros_job_wait",
        "result": "ouroboros_job_result",
        "cancel": "ouroboros_cancel_job",
    }

    assert set(expected_roles.values()).issubset(definitions)
    assert evaluate.metadata is not None
    assert evaluate.metadata.fallback_used is False
    assert evaluate.metadata.execution_mode == "blocking"
    assert set(expected_roles.values()) - {"ouroboros_evaluate"} <= set(
        evaluate.metadata.companions
    )

    evaluate_family = evaluate.metadata.orchestration["evaluate_family"]
    assert evaluate_family["family_id"] == "evaluate_lifecycle.v1"
    assert evaluate_family["execution_mode"] == "blocking"
    assert evaluate_family["companion_roles"] == expected_roles
    assert evaluate_family["start_variant"] == "ouroboros_start_evaluate"
    assert evaluate_family["required_result_context_keys"] == {
        "background_job": ("job_id",),
    }
    assert evaluate_family["cancel"] == _UNSUPPORTED_CANCEL
    assert evaluate_family["background_cancel"] == _BACKGROUND_JOB_CANCEL
    assert "start variant" in evaluate_family["runtime_instruction"]
    assert "checklist_verify" in evaluate_family["runtime_instruction"]
    assert "measure_drift" in evaluate_family["runtime_instruction"]
    assert "qa" in evaluate_family["runtime_instruction"]
    assert "status" in evaluate_family["runtime_instruction"]
    assert "wait" in evaluate_family["runtime_instruction"]
    assert "result" in evaluate_family["runtime_instruction"]
    assert "cancel" in evaluate_family["runtime_instruction"]


def test_evaluate_family_omits_absent_lifecycle_companion_tools(monkeypatch) -> None:
    full_definitions = {
        handler.definition.name: handler.definition for handler in get_ouroboros_tools()
    }
    omitted_companions = {
        "ouroboros_job_result",
        "ouroboros_cancel_job",
        "ouroboros_checklist_verify",
    }
    partial_definitions = tuple(
        definition
        for name, definition in full_definitions.items()
        if name not in omitted_companions
    )
    partial_handlers = tuple(
        SimpleNamespace(definition=definition) for definition in partial_definitions
    )

    capabilities_module._ouroboros_tool_definitions_by_name.cache_clear()
    monkeypatch.setattr(
        "ouroboros.mcp.tools.definitions.get_ouroboros_tools",
        lambda: partial_handlers,
    )

    try:
        registry = ouroboros_tool_capability_registry()
        graph = build_capability_graph(
            assemble_session_tool_catalog(attached_tools=partial_definitions)
        )
    finally:
        capabilities_module._ouroboros_tool_definitions_by_name.cache_clear()

    evaluate = graph.by_name()["ouroboros_evaluate"]
    assert omitted_companions.isdisjoint(registry)
    assert evaluate.metadata is not None
    assert evaluate.metadata.fallback_used is False
    assert "ouroboros_start_evaluate" in evaluate.metadata.companions
    assert "ouroboros_measure_drift" in evaluate.metadata.companions
    assert "ouroboros_qa" in evaluate.metadata.companions
    assert "ouroboros_job_status" in evaluate.metadata.companions
    assert "ouroboros_job_wait" in evaluate.metadata.companions
    assert omitted_companions.isdisjoint(evaluate.metadata.companions)

    evaluate_family = evaluate.metadata.orchestration["evaluate_family"]
    assert evaluate_family["companion_roles"] == {
        "primary": "ouroboros_evaluate",
        "start": "ouroboros_start_evaluate",
        "measure_drift": "ouroboros_measure_drift",
        "qa": "ouroboros_qa",
        "status": "ouroboros_job_status",
        "wait": "ouroboros_job_wait",
    }
    assert evaluate_family["start_variant"] == "ouroboros_start_evaluate"
    assert evaluate_family["required_result_context_keys"] == {
        "background_job": ("job_id",),
    }
    assert evaluate_family["background_cancel"] == {
        "supported": False,
        "mode": "unsupported",
        "companions": (),
        "target_context_keys": (),
    }
    assert "checklist_verify" not in evaluate_family["runtime_instruction"]
    assert "result" not in evaluate_family["runtime_instruction"]
    assert "cancel" not in evaluate_family["runtime_instruction"]
    assert "status" in evaluate_family["runtime_instruction"]


def test_evolve_family_exposes_start_and_lifecycle_companion_metadata() -> None:
    definitions = {handler.definition.name: handler.definition for handler in get_ouroboros_tools()}
    graph = build_capability_graph(
        assemble_session_tool_catalog(attached_tools=definitions.values())
    )
    evolve = graph.by_name()["ouroboros_evolve_step"]
    expected_roles = {
        "primary": "ouroboros_evolve_step",
        "start": "ouroboros_start_evolve_step",
        "lineage_status": "ouroboros_lineage_status",
        "rewind": "ouroboros_evolve_rewind",
        "ralph": "ouroboros_ralph",
        "start_ralph": "ouroboros_start_ralph",
        "status": "ouroboros_job_status",
        "wait": "ouroboros_job_wait",
        "result": "ouroboros_job_result",
        "cancel": "ouroboros_cancel_job",
    }

    assert set(expected_roles.values()).issubset(definitions)
    assert evolve.metadata is not None
    assert evolve.metadata.fallback_used is False
    assert evolve.metadata.execution_mode == "blocking"
    assert set(expected_roles.values()) - {"ouroboros_evolve_step"} <= set(
        evolve.metadata.companions
    )

    evolve_family = evolve.metadata.orchestration["evolve_family"]
    assert evolve_family["family_id"] == "evolve_lifecycle.v1"
    assert evolve_family["execution_mode"] == "blocking"
    assert evolve_family["companion_roles"] == expected_roles
    assert evolve_family["start_variant"] == "ouroboros_start_evolve_step"
    assert evolve_family["required_result_context_keys"] == {
        "lineage": ("lineage_id",),
        "background_job": ("job_id",),
    }
    assert evolve_family["cancel"] == _UNSUPPORTED_CANCEL
    assert evolve_family["background_cancel"] == _BACKGROUND_JOB_CANCEL
    assert "start variant" in evolve_family["runtime_instruction"]
    assert "lineage_status" in evolve_family["runtime_instruction"]
    assert "rewind" in evolve_family["runtime_instruction"]
    assert "ralph" in evolve_family["runtime_instruction"]
    assert "start_ralph" in evolve_family["runtime_instruction"]
    assert "status" in evolve_family["runtime_instruction"]
    assert "wait" in evolve_family["runtime_instruction"]
    assert "result" in evolve_family["runtime_instruction"]
    assert "cancel" in evolve_family["runtime_instruction"]


def test_evolve_family_omits_absent_lifecycle_companion_tools(monkeypatch) -> None:
    full_definitions = {
        handler.definition.name: handler.definition for handler in get_ouroboros_tools()
    }
    omitted_companions = {
        "ouroboros_job_result",
        "ouroboros_cancel_job",
        "ouroboros_start_ralph",
    }
    partial_definitions = tuple(
        definition
        for name, definition in full_definitions.items()
        if name not in omitted_companions
    )
    partial_handlers = tuple(
        SimpleNamespace(definition=definition) for definition in partial_definitions
    )

    capabilities_module._ouroboros_tool_definitions_by_name.cache_clear()
    monkeypatch.setattr(
        "ouroboros.mcp.tools.definitions.get_ouroboros_tools",
        lambda: partial_handlers,
    )

    try:
        registry = ouroboros_tool_capability_registry()
        graph = build_capability_graph(
            assemble_session_tool_catalog(attached_tools=partial_definitions)
        )
    finally:
        capabilities_module._ouroboros_tool_definitions_by_name.cache_clear()

    evolve = graph.by_name()["ouroboros_evolve_step"]
    assert omitted_companions.isdisjoint(registry)
    assert evolve.metadata is not None
    assert evolve.metadata.fallback_used is False
    assert "ouroboros_start_evolve_step" in evolve.metadata.companions
    assert "ouroboros_lineage_status" in evolve.metadata.companions
    assert "ouroboros_evolve_rewind" in evolve.metadata.companions
    assert "ouroboros_ralph" in evolve.metadata.companions
    assert "ouroboros_job_status" in evolve.metadata.companions
    assert "ouroboros_job_wait" in evolve.metadata.companions
    assert omitted_companions.isdisjoint(evolve.metadata.companions)

    evolve_family = evolve.metadata.orchestration["evolve_family"]
    assert evolve_family["companion_roles"] == {
        "primary": "ouroboros_evolve_step",
        "start": "ouroboros_start_evolve_step",
        "lineage_status": "ouroboros_lineage_status",
        "rewind": "ouroboros_evolve_rewind",
        "ralph": "ouroboros_ralph",
        "status": "ouroboros_job_status",
        "wait": "ouroboros_job_wait",
    }
    assert evolve_family["start_variant"] == "ouroboros_start_evolve_step"
    assert evolve_family["required_result_context_keys"] == {
        "lineage": ("lineage_id",),
        "background_job": ("job_id",),
    }
    assert evolve_family["background_cancel"] == {
        "supported": False,
        "mode": "unsupported",
        "companions": (),
        "target_context_keys": (),
    }
    assert "start_ralph" not in evolve_family["runtime_instruction"]
    assert "result" not in evolve_family["runtime_instruction"]
    assert "cancel" not in evolve_family["runtime_instruction"]
    assert "status" in evolve_family["runtime_instruction"]


def test_ralph_family_exposes_start_and_lifecycle_companion_metadata() -> None:
    definitions = {handler.definition.name: handler.definition for handler in get_ouroboros_tools()}
    graph = build_capability_graph(
        assemble_session_tool_catalog(attached_tools=definitions.values())
    )
    ralph = graph.by_name()["ouroboros_ralph"]
    start_ralph = graph.by_name()["ouroboros_start_ralph"]
    expected_roles = {
        "primary": "ouroboros_ralph",
        "start": "ouroboros_start_ralph",
        "evolve_step": "ouroboros_evolve_step",
        "start_evolve_step": "ouroboros_start_evolve_step",
        "lineage_status": "ouroboros_lineage_status",
        "rewind": "ouroboros_evolve_rewind",
        "status": "ouroboros_job_status",
        "wait": "ouroboros_job_wait",
        "result": "ouroboros_job_result",
        "cancel": "ouroboros_cancel_job",
    }

    assert set(expected_roles.values()).issubset(definitions)
    assert ralph.metadata is not None
    assert ralph.metadata.fallback_used is False
    assert ralph.metadata.execution_mode == "background"
    assert set(expected_roles.values()) - {"ouroboros_ralph"} <= set(ralph.metadata.companions)
    assert start_ralph.metadata is not None
    assert start_ralph.metadata.fallback_used is False
    assert start_ralph.metadata.execution_mode == "background"
    assert set(expected_roles.values()) - {
        "ouroboros_ralph",
        "ouroboros_start_ralph",
    } <= set(start_ralph.metadata.companions)

    ralph_family = ralph.metadata.orchestration["ralph_family"]
    assert ralph_family["family_id"] == "ralph_lifecycle.v1"
    assert ralph_family["execution_mode"] == "background"
    assert ralph_family["companion_roles"] == expected_roles
    assert ralph_family["start_variant"] == "ouroboros_start_ralph"
    assert ralph_family["required_result_context_keys"] == {
        "lineage": ("lineage_id",),
        "background_job": ("job_id",),
    }
    assert ralph_family["cancel"] == _BACKGROUND_JOB_CANCEL
    assert ralph_family["background_cancel"] == _BACKGROUND_JOB_CANCEL
    assert "fire-and-forget alias" in ralph_family["runtime_instruction"]
    assert "evolve_step" in ralph_family["runtime_instruction"]
    assert "start_evolve_step" in ralph_family["runtime_instruction"]
    assert "lineage_status" in ralph_family["runtime_instruction"]
    assert "rewind" in ralph_family["runtime_instruction"]
    assert "status" in ralph_family["runtime_instruction"]
    assert "wait" in ralph_family["runtime_instruction"]
    assert "result" in ralph_family["runtime_instruction"]
    assert "cancel" in ralph_family["runtime_instruction"]


def test_ralph_family_omits_absent_lifecycle_companion_tools(monkeypatch) -> None:
    full_definitions = {
        handler.definition.name: handler.definition for handler in get_ouroboros_tools()
    }
    omitted_companions = {
        "ouroboros_job_result",
        "ouroboros_cancel_job",
        "ouroboros_start_evolve_step",
    }
    partial_definitions = tuple(
        definition
        for name, definition in full_definitions.items()
        if name not in omitted_companions
    )
    partial_handlers = tuple(
        SimpleNamespace(definition=definition) for definition in partial_definitions
    )

    capabilities_module._ouroboros_tool_definitions_by_name.cache_clear()
    monkeypatch.setattr(
        "ouroboros.mcp.tools.definitions.get_ouroboros_tools",
        lambda: partial_handlers,
    )

    try:
        registry = ouroboros_tool_capability_registry()
        graph = build_capability_graph(
            assemble_session_tool_catalog(attached_tools=partial_definitions)
        )
    finally:
        capabilities_module._ouroboros_tool_definitions_by_name.cache_clear()

    ralph = graph.by_name()["ouroboros_ralph"]
    assert omitted_companions.isdisjoint(registry)
    assert ralph.metadata is not None
    assert ralph.metadata.fallback_used is False
    assert "ouroboros_start_ralph" in ralph.metadata.companions
    assert "ouroboros_evolve_step" in ralph.metadata.companions
    assert "ouroboros_lineage_status" in ralph.metadata.companions
    assert "ouroboros_evolve_rewind" in ralph.metadata.companions
    assert "ouroboros_job_status" in ralph.metadata.companions
    assert "ouroboros_job_wait" in ralph.metadata.companions
    assert omitted_companions.isdisjoint(ralph.metadata.companions)

    ralph_family = ralph.metadata.orchestration["ralph_family"]
    assert ralph_family["companion_roles"] == {
        "primary": "ouroboros_ralph",
        "start": "ouroboros_start_ralph",
        "evolve_step": "ouroboros_evolve_step",
        "lineage_status": "ouroboros_lineage_status",
        "rewind": "ouroboros_evolve_rewind",
        "status": "ouroboros_job_status",
        "wait": "ouroboros_job_wait",
    }
    assert ralph_family["start_variant"] == "ouroboros_start_ralph"
    assert ralph_family["required_result_context_keys"] == {
        "lineage": ("lineage_id",),
        "background_job": ("job_id",),
    }
    assert ralph_family["background_cancel"] == {
        "supported": False,
        "mode": "unsupported",
        "companions": (),
        "target_context_keys": (),
    }
    assert "start_evolve_step" not in ralph_family["runtime_instruction"]
    assert "result" not in ralph_family["runtime_instruction"]
    assert "cancel" not in ralph_family["companion_roles"]
    assert "status" in ralph_family["runtime_instruction"]


def test_ouroboros_owned_tools_have_explicit_mutation_targets_and_side_effects() -> None:
    definitions = {handler.definition.name: handler.definition for handler in get_ouroboros_tools()}
    registry = ouroboros_tool_capability_registry()
    graph = build_capability_graph(
        assemble_session_tool_catalog(attached_tools=definitions.values())
    )
    descriptors = {descriptor.name: descriptor for descriptor in graph.capabilities}

    assert set(registry) == set(_EXPECTED_OUROBOROS_TOOL_SIDE_EFFECTS)
    assert set(definitions) == set(_EXPECTED_OUROBOROS_TOOL_SIDE_EFFECTS)
    assert set(registry) == set(_EXPECTED_OUROBOROS_TOOL_MUTATION_TARGETS)
    assert set(registry) == set(_EXPECTED_OUROBOROS_TOOL_STATE_MUTATIONS)
    for name, expected_side_effects in _EXPECTED_OUROBOROS_TOOL_SIDE_EFFECTS.items():
        metadata = registry[name]
        descriptor = descriptors[name]
        expected_mutation_targets = _EXPECTED_OUROBOROS_TOOL_MUTATION_TARGETS[name]
        expected_state_mutations = _EXPECTED_OUROBOROS_TOOL_STATE_MUTATIONS[name]

        assert metadata.mutation_class == descriptor.semantics.mutation_class.value, name
        assert metadata.mutation_targets == expected_mutation_targets, name
        assert metadata.state_mutations == expected_state_mutations, name
        assert metadata.side_effects == expected_side_effects, name
        if descriptor.semantics.mutation_class is not CapabilityMutationClass.READ_ONLY:
            assert metadata.side_effects, name
        assert len(metadata.side_effects) == len(set(metadata.side_effects)), name
        assert len(metadata.mutation_targets) == len(set(metadata.mutation_targets)), name
        for mutation in metadata.state_mutations:
            assert mutation["target"] in metadata.mutation_targets, name
            assert mutation["side_effect"] in metadata.side_effects, name
            assert isinstance(mutation["context_keys"], tuple), name
        assert descriptor.metadata is not None, name
        assert descriptor.metadata.mutation_targets == expected_mutation_targets, name
        assert descriptor.metadata.state_mutations == expected_state_mutations, name
        assert descriptor.metadata.side_effects == expected_side_effects, name
        assert descriptor.metadata.fallback_used is False, name


def test_execution_evolution_tools_represent_concrete_execution_side_effects() -> None:
    definitions = {handler.definition.name: handler.definition for handler in get_ouroboros_tools()}
    graph = build_capability_graph(
        assemble_session_tool_catalog(attached_tools=definitions.values())
    )
    execution_evolution_tools = {
        "ouroboros_auto": "auto_session_state",
        "ouroboros_start_auto": "auto_session_state",
        "ouroboros_execute_seed": "execution_session",
        "ouroboros_start_execute_seed": "execution_session",
        "ouroboros_evolve_step": "lineage_state",
        "ouroboros_start_evolve_step": "lineage_state",
        "ouroboros_ralph": "ralph_loop_state",
        "ouroboros_start_ralph": "ralph_loop_state",
    }

    assert set(execution_evolution_tools).issubset(definitions)
    for tool_name, expected_state_target in execution_evolution_tools.items():
        descriptor = graph.by_name()[tool_name]
        registry_metadata = ouroboros_tool_capability_registry()[tool_name]

        assert descriptor.metadata is not None, tool_name
        assert descriptor.metadata == registry_metadata, tool_name
        assert registry_metadata.fallback_used is False, tool_name
        assert {"workspace_write", "event_store_write"} <= set(registry_metadata.side_effects), (
            tool_name
        )
        assert "workspace" in registry_metadata.mutation_targets, tool_name
        assert "event_store" in registry_metadata.mutation_targets, tool_name
        assert expected_state_target in registry_metadata.mutation_targets, tool_name
        assert registry_metadata.state_mutations, tool_name
        represented_targets = {mutation["target"] for mutation in registry_metadata.state_mutations}
        represented_side_effects = {
            mutation["side_effect"] for mutation in registry_metadata.state_mutations
        }
        represented_operations = {
            mutation["operation"] for mutation in registry_metadata.state_mutations
        }

        assert expected_state_target in represented_targets, tool_name
        assert "workspace" in represented_targets, tool_name
        assert {"workspace_write", "event_store_write"} <= represented_side_effects, tool_name
        assert all(operation for operation in represented_operations), tool_name
        validate_capability_tool_metadata(
            registry_metadata,
            tool_name=tool_name,
            owned_tool=True,
        )


def test_cancel_tools_represent_runtime_and_persistent_side_effects() -> None:
    definitions = {handler.definition.name: handler.definition for handler in get_ouroboros_tools()}
    graph = build_capability_graph(
        assemble_session_tool_catalog(attached_tools=definitions.values())
    )
    state_changing_command_names = {
        name
        for name in definitions
        if any(command in name for command in ("cancel", "publish", "update"))
    }
    expected_cancel_tools = {
        "ouroboros_cancel_execution": {
            "side_effects": {
                "runtime_control",
                "event_store_write",
                "session_state_write",
            },
            "mutation_targets": {"runtime", "event_store", "session_state", "execution_session"},
            "operations": {
                "mark_execution_session_cancelled",
                "append_session_cancelled_event",
                "signal_execution_runner_to_stop_via_cancellation_event",
            },
        },
        "ouroboros_cancel_job": {
            "side_effects": {
                "runtime_control",
                "event_store_write",
                "checkpoint_store_write",
                "session_state_write",
            },
            "mutation_targets": {
                "runtime",
                "event_store",
                "checkpoint_store",
                "session_state",
                "background_job",
            },
            "operations": {
                "mark_background_job_cancel_requested",
                "persist_durable_agent_process_cancel_signal",
                "cancel_live_background_job_tasks",
                "mark_linked_execution_session_cancelled_when_needed",
            },
        },
    }

    assert state_changing_command_names == set(expected_cancel_tools)
    assert "ouroboros_publish" not in definitions
    assert "ouroboros_update" not in definitions

    for tool_name, expected in expected_cancel_tools.items():
        descriptor = graph.by_name()[tool_name]
        metadata = ouroboros_tool_capability_registry()[tool_name]

        assert descriptor.metadata is not None, tool_name
        assert descriptor.metadata == metadata, tool_name
        assert metadata.fallback_used is False, tool_name
        assert metadata.execution_mode == "cancel", tool_name
        assert descriptor.semantics.mutation_class is CapabilityMutationClass.EXTERNAL_SIDE_EFFECT
        assert set(metadata.side_effects) == expected["side_effects"], tool_name
        assert set(metadata.mutation_targets) == expected["mutation_targets"], tool_name
        assert {mutation["operation"] for mutation in metadata.state_mutations} == expected[
            "operations"
        ], tool_name
        assert {mutation["side_effect"] for mutation in metadata.state_mutations} == expected[
            "side_effects"
        ], tool_name
        assert all(mutation["context_keys"] for mutation in metadata.state_mutations), tool_name
        validate_capability_tool_metadata(
            metadata,
            tool_name=tool_name,
            owned_tool=True,
        )


def test_read_only_ouroboros_owned_tools_are_explicitly_non_mutating() -> None:
    definitions = {handler.definition.name: handler.definition for handler in get_ouroboros_tools()}
    registry = ouroboros_tool_capability_registry()
    graph = build_capability_graph(
        assemble_session_tool_catalog(attached_tools=definitions.values())
    )
    descriptors = {descriptor.name: descriptor for descriptor in graph.capabilities}
    read_only_side_effects = {()}

    assert set(definitions) >= _EXPECTED_READ_ONLY_OUROBOROS_TOOLS
    assert {
        name
        for name, side_effects in _EXPECTED_OUROBOROS_TOOL_SIDE_EFFECTS.items()
        if side_effects in read_only_side_effects
    } == _EXPECTED_READ_ONLY_OUROBOROS_TOOLS
    for name in sorted(_EXPECTED_READ_ONLY_OUROBOROS_TOOLS):
        spec = capabilities_module._OUROBOROS_TOOL_CAPABILITY_SPECS[name]
        metadata = registry[name]
        descriptor = descriptors[name]

        assert spec.mutation_class is CapabilityMutationClass.READ_ONLY, name
        assert metadata.mutation_class == CapabilityMutationClass.READ_ONLY.value, name
        assert metadata.mutation_targets == (), name
        assert metadata.state_mutations == (), name
        assert metadata.side_effects == (), name
        assert metadata.fallback_used is False, name
        assert descriptor.metadata is not None, name
        assert descriptor.metadata.mutation_targets == (), name
        assert descriptor.metadata.state_mutations == (), name
        assert descriptor.metadata.side_effects == (), name
        assert descriptor.metadata.mutation_class == CapabilityMutationClass.READ_ONLY.value
        assert descriptor.semantics.mutation_class is CapabilityMutationClass.READ_ONLY
        assert descriptor.semantics.parallel_safety is CapabilityParallelSafety.SAFE
        assert descriptor.semantics.interruptibility is CapabilityInterruptibility.NONE
        assert descriptor.semantics.approval_class is CapabilityApprovalClass.DEFAULT


def test_read_only_query_status_projection_tools_have_non_mutating_interrupt_metadata() -> None:
    definitions = {handler.definition.name: handler.definition for handler in get_ouroboros_tools()}
    registry = ouroboros_tool_capability_registry()
    graph = build_capability_graph(
        assemble_session_tool_catalog(attached_tools=definitions.values())
    )
    descriptors = {descriptor.name: descriptor for descriptor in graph.capabilities}
    expected_query_status_projection_tools = {
        "ouroboros_ac_tree_hud",
        "ouroboros_job_result",
        "ouroboros_job_status",
        "ouroboros_job_wait",
        "ouroboros_lineage_status",
        "ouroboros_query_events",
        "ouroboros_query_projection",
        "ouroboros_session_status",
        "ouroboros_submit_fanout_results",
    }

    assert expected_query_status_projection_tools == _EXPECTED_READ_ONLY_OUROBOROS_TOOLS
    assert expected_query_status_projection_tools.issubset(definitions)
    assert {
        name
        for name, metadata in registry.items()
        if metadata.execution_mode == "status"
        and metadata.mutation_class == CapabilityMutationClass.READ_ONLY.value
    } == expected_query_status_projection_tools

    for name in sorted(expected_query_status_projection_tools):
        metadata = registry[name]
        descriptor = descriptors[name]

        assert descriptor.metadata is not None, name
        assert descriptor.metadata.fallback_used is False, name
        assert descriptor.metadata.interrupt == _READ_ONLY_INTERRUPT, name
        assert metadata.interrupt == _READ_ONLY_INTERRUPT, name
        assert metadata.interrupt["mode"] != "unsupported", name
        assert metadata.interrupt["supported"] is True, name
        assert metadata.interrupt["mutation_semantics"] == "no_state_mutation", name
        assert metadata.interrupt["resumable"] is False, name
        assert metadata.interrupt["target_context_keys"] == (), name
        assert metadata.side_effects == (), name
        assert metadata.mutation_targets == (), name
        assert metadata.state_mutations == (), name
        assert descriptor.semantics.mutation_class is CapabilityMutationClass.READ_ONLY
        assert descriptor.semantics.interruptibility is CapabilityInterruptibility.NONE
        validate_capability_tool_metadata(metadata, tool_name=name, owned_tool=True)


def test_ouroboros_owned_tools_have_explicit_tool_specific_retry_behavior() -> None:
    definitions = {handler.definition.name: handler.definition for handler in get_ouroboros_tools()}
    registry = ouroboros_tool_capability_registry()
    graph = build_capability_graph(
        assemble_session_tool_catalog(attached_tools=definitions.values())
    )
    descriptors = {descriptor.name: descriptor for descriptor in graph.capabilities}

    assert set(registry) == set(_EXPECTED_OUROBOROS_TOOL_RETRY)
    assert set(definitions) == set(_EXPECTED_OUROBOROS_TOOL_RETRY)
    for name, expected_retry in _EXPECTED_OUROBOROS_TOOL_RETRY.items():
        metadata = registry[name]
        descriptor = descriptors[name]

        assert metadata.retry == expected_retry, name
        assert set(metadata.retry) == {"supported", "mode"}, name
        assert isinstance(metadata.retry["supported"], bool), name
        assert metadata.retry["mode"] in {"handler_owned", "job_poll", "unsupported"}
        assert descriptor.metadata is not None, name
        assert descriptor.metadata.retry == expected_retry, name
        assert descriptor.metadata.fallback_used is False, name


def test_ouroboros_owned_retry_taxonomy_has_distinct_explicit_semantics() -> None:
    definitions = {handler.definition.name: handler.definition for handler in get_ouroboros_tools()}
    graph = build_capability_graph(
        assemble_session_tool_catalog(attached_tools=definitions.values())
    )
    descriptors = {descriptor.name: descriptor for descriptor in graph.capabilities}

    retry_capable_tools = {
        "ouroboros_execute_seed",
        "ouroboros_interview",
        "ouroboros_evaluate",
        "ouroboros_lateral_think",
        "ouroboros_qa",
    }
    conditionally_retryable_tools = {
        "ouroboros_start_execute_seed",
        "ouroboros_start_evaluate",
        "ouroboros_start_evolve_step",
        "ouroboros_start_ralph",
    }
    non_retryable_tools = {
        "ouroboros_cancel_job",
        "ouroboros_cancel_execution",
    }

    expected_by_group = (
        (
            retry_capable_tools,
            {"supported": True, "mode": "handler_owned"},
            "blocking-or-orchestrated handlers own safe retry decisions",
        ),
        (
            conditionally_retryable_tools,
            {"supported": True, "mode": "job_poll"},
            "background starters retry through job lifecycle polling companions",
        ),
        (
            non_retryable_tools,
            {"supported": False, "mode": "unsupported"},
            "cancel/runtime-control tools must not be replayed implicitly",
        ),
    )

    assert not retry_capable_tools & conditionally_retryable_tools
    assert not retry_capable_tools & non_retryable_tools
    assert not conditionally_retryable_tools & non_retryable_tools

    for tool_names, expected_retry, _reason in expected_by_group:
        for name in tool_names:
            descriptor = descriptors[name]

            assert descriptor.metadata is not None, name
            assert descriptor.metadata.retry == expected_retry, name
            assert descriptor.metadata.fallback_used is False, name
            assert descriptor.metadata.retry != {"supported": False, "mode": "generic"}, name

    assert {
        tuple(sorted(descriptors[name].metadata.retry.items()))
        for names, _expected, _reason in expected_by_group
        for name in names
    } == {
        (("mode", "handler_owned"), ("supported", True)),
        (("mode", "job_poll"), ("supported", True)),
        (("mode", "unsupported"), ("supported", False)),
    }


def test_ouroboros_owned_tools_have_explicit_tool_specific_interrupt_behavior() -> None:
    definitions = {handler.definition.name: handler.definition for handler in get_ouroboros_tools()}
    registry = ouroboros_tool_capability_registry()
    graph = build_capability_graph(
        assemble_session_tool_catalog(attached_tools=definitions.values())
    )
    descriptors = {descriptor.name: descriptor for descriptor in graph.capabilities}

    assert set(registry) == set(_EXPECTED_OUROBOROS_TOOL_INTERRUPT)
    assert set(definitions) == set(_EXPECTED_OUROBOROS_TOOL_INTERRUPT)
    for name, expected_interrupt in _EXPECTED_OUROBOROS_TOOL_INTERRUPT.items():
        metadata = registry[name]
        descriptor = descriptors[name]

        assert metadata.interrupt == expected_interrupt, name
        _assert_lifecycle_metadata_shape(metadata, name)
        assert isinstance(metadata.interrupt["supported"], bool), name
        assert metadata.interrupt["mode"] in {
            "soft",
            "hard",
            "terminal_control",
            "unsupported",
            "resumable_background_job",
            "read_only_non_mutating",
        }
        assert descriptor.metadata is not None, name
        assert descriptor.metadata.interrupt == expected_interrupt, name
        assert descriptor.metadata.fallback_used is False, name


def test_blocking_ouroboros_tools_have_synchronous_interrupt_metadata() -> None:
    definitions = {handler.definition.name: handler.definition for handler in get_ouroboros_tools()}
    registry = ouroboros_tool_capability_registry()
    graph = build_capability_graph(
        assemble_session_tool_catalog(attached_tools=definitions.values())
    )
    descriptors = {descriptor.name: descriptor for descriptor in graph.capabilities}
    blocking_tools = {
        name for name, metadata in registry.items() if metadata.execution_mode == "blocking"
    }

    assert blocking_tools == {
        "ouroboros_auto",
        "ouroboros_brownfield",
        "ouroboros_checklist_verify",
        "ouroboros_evaluate",
        "ouroboros_evolve_rewind",
        "ouroboros_evolve_step",
        "ouroboros_execute_seed",
        "ouroboros_generate_seed",
        "ouroboros_measure_drift",
        "ouroboros_qa",
    }

    for name in sorted(blocking_tools):
        descriptor = descriptors[name]
        metadata = registry[name]
        interrupt = metadata.interrupt

        assert descriptor.metadata is not None, name
        assert descriptor.metadata.fallback_used is False, name
        assert descriptor.metadata.interrupt == interrupt, name
        assert descriptor.semantics.interruptibility is CapabilityInterruptibility.SOFT
        assert interrupt["supported"] is True, name
        assert interrupt["mode"] == "soft", name
        assert interrupt["execution_mode"] == "blocking", name
        assert interrupt["blocking_semantics"] == "synchronous_handler", name
        assert interrupt["resumable"] is False, name
        assert isinstance(interrupt["background_companions"], tuple), name
        assert isinstance(interrupt["target_context_keys"], tuple), name
        assert interrupt["target_context_keys"] == (), name
        assert interrupt == _EXPECTED_OUROBOROS_TOOL_INTERRUPT[name], name
        assert all(
            companion in metadata.companions for companion in interrupt["background_companions"]
        ), name


def test_background_starting_tools_have_resumable_cancellable_interrupt_metadata() -> None:
    definitions = {handler.definition.name: handler.definition for handler in get_ouroboros_tools()}
    graph = build_capability_graph(
        assemble_session_tool_catalog(attached_tools=definitions.values())
    )
    descriptors = {descriptor.name: descriptor for descriptor in graph.capabilities}
    background_tools = {
        name
        for name, expected_mode in _EXPECTED_OUROBOROS_TOOL_EXECUTION_MODES.items()
        if expected_mode == "background"
    }

    assert background_tools == {
        "ouroboros_start_auto",
        "ouroboros_start_evaluate",
        "ouroboros_start_evolve_step",
        "ouroboros_start_execute_seed",
        "ouroboros_ralph",
        "ouroboros_start_ralph",
    }
    assert background_tools.issubset(definitions)

    for name in sorted(background_tools):
        descriptor = descriptors[name]

        assert descriptor.metadata is not None, name
        assert descriptor.metadata.fallback_used is False, name
        assert descriptor.metadata.execution_mode == "background", name
        assert descriptor.semantics.interruptibility is CapabilityInterruptibility.SOFT
        assert descriptor.metadata.interrupt == _BACKGROUND_INTERRUPT, name
        assert set(descriptor.metadata.interrupt["resume_companions"]) == {
            "ouroboros_job_status",
            "ouroboros_job_wait",
            "ouroboros_job_result",
        }, name
        assert descriptor.metadata.interrupt["cancel_companions"] == ("ouroboros_cancel_job",), name
        assert descriptor.metadata.interrupt["target_context_keys"] == ("job_id",)
        assert all(
            companion in descriptor.metadata.companions
            for companion in (
                *descriptor.metadata.interrupt["resume_companions"],
                *descriptor.metadata.interrupt["cancel_companions"],
            )
        ), name
        assert descriptor.metadata.cancel == _BACKGROUND_JOB_CANCEL, name
        lifecycle = descriptor.metadata.orchestration["background_lifecycle"]
        assert lifecycle["required_result_context_keys"] == ("job_id",)
        assert lifecycle["cancel"] == _BACKGROUND_JOB_CANCEL


def test_cancellation_control_tools_have_terminal_control_interrupt_metadata() -> None:
    definitions = {handler.definition.name: handler.definition for handler in get_ouroboros_tools()}
    registry = ouroboros_tool_capability_registry()
    graph = build_capability_graph(
        assemble_session_tool_catalog(attached_tools=definitions.values())
    )
    descriptors = {descriptor.name: descriptor for descriptor in graph.capabilities}
    control_tools = {
        name for name, metadata in registry.items() if metadata.execution_mode == "cancel"
    }

    assert control_tools == set(_TERMINAL_CONTROL_INTERRUPTS)
    assert control_tools == set(capabilities_module._OUROBOROS_CANCEL_TOOLS)
    assert control_tools.issubset(definitions)

    for name in sorted(control_tools):
        descriptor = descriptors[name]
        metadata = registry[name]
        expected_interrupt = _TERMINAL_CONTROL_INTERRUPTS[name]
        target_keys = expected_interrupt["target_context_keys"]

        assert descriptor.metadata is not None, name
        assert descriptor.metadata.fallback_used is False, name
        assert descriptor.metadata == metadata, name
        assert descriptor.semantics.mutation_class is (CapabilityMutationClass.EXTERNAL_SIDE_EFFECT)
        assert descriptor.semantics.parallel_safety is CapabilityParallelSafety.SERIALIZED
        assert descriptor.semantics.interruptibility is CapabilityInterruptibility.HARD
        assert descriptor.semantics.approval_class is CapabilityApprovalClass.ELEVATED
        assert metadata.execution_mode == "cancel", name
        assert metadata.side_effects == _EXPECTED_OUROBOROS_TOOL_SIDE_EFFECTS[name], name
        assert "runtime_control" in metadata.side_effects, name
        assert metadata.mutation_targets == _EXPECTED_OUROBOROS_TOOL_MUTATION_TARGETS[name], name
        assert "runtime" in metadata.mutation_targets, name
        assert metadata.state_mutations == _EXPECTED_OUROBOROS_TOOL_STATE_MUTATIONS[name], name
        assert metadata.retry == {"supported": False, "mode": "unsupported"}, name
        assert metadata.interrupt == expected_interrupt, name
        assert metadata.interrupt["mode"] == "terminal_control", name
        assert metadata.interrupt["terminal_action"] == "cancel", name
        assert metadata.interrupt["idempotent"] is True, name
        assert metadata.interrupt["terminal_statuses"] == ("cancelled",), name
        assert metadata.interrupt["directive_semantics"].startswith("request_terminal_"), name
        assert target_keys == metadata.required_context_keys, name
        assert metadata.cancel == _EXPECTED_OUROBOROS_TOOL_CANCEL[name], name
        assert metadata.cancel["supported"] is True, name
        assert metadata.cancel["mode"].endswith("_control"), name
        assert metadata.cancel["target_context_keys"] == target_keys, name
        assert metadata.cancel["companions"], name
        assert all(
            key in definitions[name].to_input_schema()["properties"] for key in target_keys
        ), name
        validate_capability_tool_metadata(metadata, tool_name=name, owned_tool=True)


def test_ouroboros_owned_tools_have_explicit_tool_specific_cancel_behavior() -> None:
    definitions = {handler.definition.name: handler.definition for handler in get_ouroboros_tools()}
    registry = ouroboros_tool_capability_registry()
    graph = build_capability_graph(
        assemble_session_tool_catalog(attached_tools=definitions.values())
    )
    descriptors = {descriptor.name: descriptor for descriptor in graph.capabilities}

    assert set(registry) == set(_EXPECTED_OUROBOROS_TOOL_CANCEL)
    assert set(definitions) == set(_EXPECTED_OUROBOROS_TOOL_CANCEL)
    for name, expected_cancel in _EXPECTED_OUROBOROS_TOOL_CANCEL.items():
        metadata = registry[name]
        descriptor = descriptors[name]

        assert metadata.cancel == expected_cancel, name
        assert set(metadata.cancel) == {
            "supported",
            "mode",
            "companions",
            "target_context_keys",
        }, name
        assert isinstance(metadata.cancel["supported"], bool), name
        assert metadata.cancel["mode"] in {
            "background_job",
            "background_job_control",
            "execution_session",
            "execution_session_control",
            "unsupported",
        }, name
        assert isinstance(metadata.cancel["companions"], tuple), name
        assert isinstance(metadata.cancel["target_context_keys"], tuple), name
        assert all(companion in definitions for companion in metadata.cancel["companions"]), name
        if metadata.cancel["supported"]:
            assert metadata.cancel["companions"], name
            assert metadata.cancel["target_context_keys"], name
            if metadata.cancel["mode"].endswith("_control"):
                assert all(
                    key in definitions[name].to_input_schema()["properties"]
                    for key in metadata.cancel["target_context_keys"]
                ), name
            else:
                assert all(
                    key in definitions[companion].to_input_schema()["properties"]
                    for companion in metadata.cancel["companions"]
                    for key in metadata.cancel["target_context_keys"]
                ), name
        else:
            assert metadata.cancel["companions"] == (), name
            assert metadata.cancel["target_context_keys"] == (), name
        assert descriptor.metadata is not None, name
        assert descriptor.metadata.cancel == expected_cancel, name
        assert descriptor.metadata.fallback_used is False, name


def test_ouroboros_owned_cancel_lookup_never_uses_generic_attached_inference(
    monkeypatch,
) -> None:
    definitions = {handler.definition.name: handler.definition for handler in get_ouroboros_tools()}

    def fail_on_generic_fallback(tool: MCPToolDefinition):
        raise AssertionError(
            f"Ouroboros-owned cancel lookup used generic attached inference: {tool.name}"
        )

    monkeypatch.setattr(
        capabilities_module,
        "_infer_attached_semantics",
        fail_on_generic_fallback,
    )

    graph = build_capability_graph(
        assemble_session_tool_catalog(attached_tools=definitions.values())
    )

    assert set(graph.by_name()) == set(definitions)
    for name, expected_cancel in _EXPECTED_OUROBOROS_TOOL_CANCEL.items():
        descriptor = graph.by_name()[name]
        by_name = lookup_ouroboros_tool_capability_metadata(name)
        by_stable_id = lookup_ouroboros_tool_capability_metadata(descriptor.stable_id)
        resolved = resolve_mcp_tool_capability_descriptor(descriptor.stable_id, graph=graph)

        assert descriptor.metadata is not None, name
        assert by_name is not None, name
        assert by_stable_id is not None, name
        assert resolved is descriptor, name
        assert descriptor.metadata.fallback_used is False, name
        assert by_name.fallback_used is False, name
        assert by_stable_id.fallback_used is False, name
        assert descriptor.metadata.cancel == expected_cancel, name
        assert by_name.cancel == expected_cancel, name
        assert by_stable_id.cancel == expected_cancel, name


def test_non_cancel_ouroboros_owned_tools_explicitly_do_not_expose_cancel_semantics() -> None:
    definitions = {handler.definition.name: handler.definition for handler in get_ouroboros_tools()}
    registry = ouroboros_tool_capability_registry()
    graph = build_capability_graph(
        assemble_session_tool_catalog(attached_tools=definitions.values())
    )
    descriptors = {descriptor.name: descriptor for descriptor in graph.capabilities}
    serialized = {entry["name"]: entry for entry in serialize_capability_graph(graph)}
    non_cancel_tools = {
        name
        for name, expected_cancel in _EXPECTED_OUROBOROS_TOOL_CANCEL.items()
        if expected_cancel == _UNSUPPORTED_CANCEL
    }
    cancel_capable_tools = set(definitions) - non_cancel_tools

    assert non_cancel_tools == {
        "ouroboros_ac_tree_hud",
        "ouroboros_auto",
        "ouroboros_brownfield",
        "ouroboros_checklist_verify",
        "ouroboros_evaluate",
        "ouroboros_evolve_rewind",
        "ouroboros_evolve_step",
        "ouroboros_generate_seed",
        "ouroboros_interview",
        "ouroboros_job_result",
        "ouroboros_job_status",
        "ouroboros_job_wait",
        "ouroboros_lateral_think",
        "ouroboros_lineage_status",
        "ouroboros_measure_drift",
        "ouroboros_pm_interview",
        "ouroboros_qa",
        "ouroboros_query_events",
        "ouroboros_query_projection",
        "ouroboros_session_status",
        "ouroboros_submit_fanout_results",
    }
    assert cancel_capable_tools == {
        "ouroboros_cancel_execution",
        "ouroboros_cancel_job",
        "ouroboros_execute_seed",
        "ouroboros_ralph",
        "ouroboros_start_auto",
        "ouroboros_start_evaluate",
        "ouroboros_start_evolve_step",
        "ouroboros_start_execute_seed",
        "ouroboros_start_ralph",
    }
    assert non_cancel_tools.isdisjoint(capabilities_module._OUROBOROS_CANCEL_TOOLS)

    for name in sorted(non_cancel_tools):
        descriptor = descriptors[name]
        metadata = registry[name]
        serialized_metadata = serialized[name]["metadata"]

        assert name in capabilities_module._OUROBOROS_CANCEL_METADATA
        assert capabilities_module._OUROBOROS_CANCEL_METADATA[name] == _UNSUPPORTED_CANCEL
        assert metadata.cancel == _UNSUPPORTED_CANCEL, name
        assert metadata.cancel["supported"] is False, name
        assert metadata.cancel["mode"] == "unsupported", name
        assert metadata.cancel["companions"] == (), name
        assert metadata.cancel["target_context_keys"] == (), name
        assert metadata.execution_mode != "cancel", name
        assert metadata.fallback_used is False, name
        assert descriptor.metadata is not None, name
        assert descriptor.metadata.cancel == _UNSUPPORTED_CANCEL, name
        assert descriptor.metadata.fallback_used is False, name
        assert serialized_metadata is not None, name
        assert serialized_metadata["cancel"] == {
            "supported": False,
            "mode": "unsupported",
            "companions": [],
            "target_context_keys": [],
        }, name
        validate_capability_tool_metadata(metadata, tool_name=name, owned_tool=True)


def test_tool_specific_capability_metadata_schema_validates_owned_tools() -> None:
    definitions = {handler.definition.name: handler.definition for handler in get_ouroboros_tools()}
    graph = build_capability_graph(
        assemble_session_tool_catalog(attached_tools=definitions.values())
    )
    descriptors = {descriptor.name: descriptor for descriptor in graph.capabilities}

    for name, metadata in ouroboros_tool_capability_registry().items():
        descriptor = descriptors[name]

        validate_capability_tool_metadata(metadata, tool_name=name, owned_tool=True)
        assert metadata.fallback_used is False, name
        assert metadata.mutation_class == descriptor.semantics.mutation_class.value
        assert isinstance(metadata.mutation_targets, tuple), name
        assert isinstance(metadata.state_mutations, tuple), name
        if descriptor.semantics.mutation_class is CapabilityMutationClass.READ_ONLY:
            assert metadata.mutation_targets == (), name
            assert metadata.state_mutations == (), name
            assert metadata.side_effects == (), name
        else:
            assert metadata.mutation_targets, name
            assert metadata.side_effects, name


def test_tool_specific_capability_metadata_schema_rejects_invalid_metadata() -> None:
    metadata = ouroboros_tool_capability_registry()["ouroboros_execute_seed"]

    invalid_cases = (
        (
            replace(metadata, mutation_class="not-a-real-mutation"),
            "invalid mutation_class",
        ),
        (
            replace(metadata, side_effects=()),
            "mutating capabilities require explicit side_effects",
        ),
        (
            replace(metadata, mutation_targets=()),
            "mutating capabilities require explicit mutation_targets",
        ),
        (
            replace(
                metadata,
                state_mutations=(
                    {
                        "target": "missing_target",
                        "operation": "append_bad_state",
                        "side_effect": "workspace_write",
                        "context_keys": (),
                    },
                ),
            ),
            "state mutation target must be listed in mutation_targets",
        ),
        (
            replace(metadata, fallback_used=True),
            "owned tool metadata cannot use fallback",
        ),
        (
            replace(metadata, cancel={"supported": False, "companions": ()}),
            "cancel must contain supported, mode, companions, and target_context_keys",
        ),
    )

    for invalid_metadata, expected_message in invalid_cases:
        with pytest.raises(ValueError, match=expected_message):
            validate_capability_tool_metadata(
                invalid_metadata,
                tool_name="ouroboros_execute_seed",
                owned_tool=True,
            )


def test_resolved_ouroboros_mcp_tool_identifier_returns_registered_descriptor() -> None:
    definitions = {handler.definition.name: handler.definition for handler in get_ouroboros_tools()}
    catalog = assemble_session_tool_catalog(attached_tools=(definitions["ouroboros_interview"],))
    graph = build_capability_graph(catalog)
    descriptor = graph.capabilities[0]
    assert descriptor.stable_id.endswith(":ouroboros_interview")

    resolved_by_stable_id = resolve_mcp_tool_capability_descriptor(
        descriptor.stable_id,
        graph=graph,
    )
    resolved_by_name = resolve_mcp_tool_capability_descriptor(
        "ouroboros_interview",
        graph=graph,
    )

    assert resolved_by_stable_id == descriptor
    assert resolved_by_name == descriptor
    assert descriptor.metadata == ouroboros_tool_capability_registry()["ouroboros_interview"]
    assert descriptor.metadata is not None
    assert descriptor.metadata.fallback_used is False
    assert descriptor.metadata.execution_mode == "subagent_orchestration"
    assert resolve_mcp_tool_capability_descriptor("mcp:external:unknown", graph=graph) is None


def test_resolving_ouroboros_mcp_identifiers_never_uses_generic_attached_inference(
    monkeypatch,
) -> None:
    definitions = {handler.definition.name: handler.definition for handler in get_ouroboros_tools()}
    owned_definition = definitions["ouroboros_interview"]

    def fail_on_generic_fallback(tool: MCPToolDefinition):
        raise AssertionError(f"Ouroboros-owned lookup used generic attached inference: {tool.name}")

    monkeypatch.setattr(
        "ouroboros.orchestrator.capabilities._infer_attached_semantics",
        fail_on_generic_fallback,
    )

    graph = build_capability_graph(
        assemble_session_tool_catalog(attached_tools=(owned_definition,))
    )
    descriptor = graph.capabilities[0]

    for identifier in (
        "ouroboros_interview",
        owned_definition.name,
        descriptor.stable_id,
    ):
        resolved = resolve_mcp_tool_capability_descriptor(identifier, graph=graph)
        assert resolved == descriptor
        assert resolved.metadata is not None
        assert resolved.metadata.fallback_used is False
        assert resolved.metadata.execution_mode == "subagent_orchestration"

    implicit_graph_resolved = resolve_mcp_tool_capability_descriptor("ouroboros_interview")
    assert implicit_graph_resolved is not None
    assert implicit_graph_resolved.name == "ouroboros_interview"
    assert implicit_graph_resolved.metadata is not None
    assert implicit_graph_resolved.metadata.fallback_used is False


def test_unknown_attached_mcp_tool_keeps_generic_fallback_metadata() -> None:
    catalog = assemble_session_tool_catalog(
        attached_tools=(
            MCPToolDefinition(
                name="external_delete_widget",
                description="Delete a widget from an external system",
                server_name="external",
            ),
        ),
    )

    graph = build_capability_graph(catalog)

    descriptor = graph.capabilities[0]
    assert descriptor.metadata is not None
    assert descriptor.metadata.fallback_used is True
    assert descriptor.metadata.execution_mode == "generic_attached"
    assert descriptor.metadata.mutation_targets == ("external",)
    assert descriptor.metadata.side_effects == ("unknown_external_side_effect",)
    assert descriptor.semantics.mutation_class is CapabilityMutationClass.DESTRUCTIVE


def test_unmapped_ouroboros_mcp_tool_descriptor_uses_explicit_metadata() -> None:
    frontmatter_tools = _frontmatter_mcp_tools()
    definitions = {handler.definition.name: handler.definition for handler in get_ouroboros_tools()}
    unmapped_definition = definitions["ouroboros_query_events"]
    assert unmapped_definition.name not in frontmatter_tools
    catalog = assemble_session_tool_catalog(
        attached_tools=(replace(unmapped_definition, server_name="ouroboros"),)
    )

    graph = build_capability_graph(catalog)

    descriptor = graph.capabilities[0]
    assert descriptor.name == "ouroboros_query_events"
    assert descriptor.source_kind == "attached_mcp"
    assert descriptor.source_name == "ouroboros"
    assert descriptor.semantics.origin is CapabilityOrigin.ATTACHED_MCP
    assert descriptor.semantics.mutation_class is CapabilityMutationClass.READ_ONLY
    assert descriptor.metadata is not None
    assert descriptor.metadata.fallback_used is False
    assert descriptor.metadata.input_schema == unmapped_definition.to_input_schema()
    assert descriptor.metadata.execution_mode == "status"
    assert set(descriptor.metadata.companions) >= {
        "ouroboros_session_status",
        "ouroboros_query_projection",
    }
    assert descriptor.metadata.required_context_keys == mcp_tool_required_parameter_keys(
        unmapped_definition
    )
    assert descriptor.metadata.mutation_targets == ()
    assert descriptor.metadata.side_effects == ()
    _assert_lifecycle_metadata_shape(descriptor.metadata, descriptor.name)


def test_ouroboros_owned_descriptor_requires_explicit_tool_specific_retry_metadata(
    monkeypatch,
) -> None:
    synthetic_definition = MCPToolDefinition(
        name="ouroboros_contract_probe",
        description="Synthetic owned tool without explicit retry capability metadata",
        parameters=(
            MCPToolParameter(
                name="session_id",
                type=ToolInputType.STRING,
                description="Session identifier.",
                required=True,
            ),
        ),
        server_name="ouroboros",
    )

    monkeypatch.setattr(
        "ouroboros.orchestrator.capabilities._ouroboros_tool_definitions_by_name",
        lambda: {synthetic_definition.name: synthetic_definition},
    )

    with pytest.raises(RuntimeError, match="retry metadata must be defined"):
        build_capability_graph((synthetic_definition,))


def test_ouroboros_owned_read_only_tool_serializes_explicit_empty_side_effects(
    monkeypatch,
) -> None:
    synthetic_definition = MCPToolDefinition(
        name="ouroboros_empty_side_effect_probe",
        description="Synthetic read-only owned tool with no declared side effects",
        server_name="ouroboros",
    )

    monkeypatch.setattr(
        "ouroboros.orchestrator.capabilities._ouroboros_tool_definitions_by_name",
        lambda: {synthetic_definition.name: synthetic_definition},
    )
    monkeypatch.setattr(
        "ouroboros.orchestrator.capabilities._OUROBOROS_TOOL_CAPABILITY_SPECS",
        {
            synthetic_definition.name: capabilities_module._OuroborosToolCapabilitySpec(
                execution_mode="blocking",
                companions=(),
                side_effects=(),
                retry={"supported": True, "mode": "handler_owned"},
                interrupt=capabilities_module._OUROBOROS_READ_ONLY_INTERRUPT_METADATA,
                mutation_class=CapabilityMutationClass.READ_ONLY,
            )
        },
    )
    monkeypatch.setattr(
        "ouroboros.orchestrator.capabilities._OUROBOROS_CANCEL_METADATA",
        {synthetic_definition.name: _UNSUPPORTED_CANCEL},
    )

    graph = build_capability_graph((synthetic_definition,))
    descriptor = graph.by_name()[synthetic_definition.name]

    assert descriptor.metadata is not None
    assert descriptor.metadata.fallback_used is False
    assert descriptor.metadata.mutation_class == CapabilityMutationClass.READ_ONLY.value
    assert descriptor.metadata.side_effects == ()
    validate_capability_tool_metadata(
        descriptor.metadata,
        tool_name=synthetic_definition.name,
        owned_tool=True,
    )

    serialized = serialize_capability_graph(graph)

    assert serialized[0]["metadata"] is not None
    assert "side_effects" in serialized[0]["metadata"]
    assert serialized[0]["metadata"]["side_effects"] == []

    restored = normalize_serialized_capability_graph(serialized)

    assert restored is not None
    restored_descriptor = restored.by_name()[synthetic_definition.name]
    assert restored_descriptor.metadata is not None
    assert restored_descriptor.metadata.fallback_used is False
    assert restored_descriptor.metadata.side_effects == ()


def test_owned_mcp_tool_without_context_keys_serializes_explicit_empty_value(
    monkeypatch,
) -> None:
    synthetic_definition = MCPToolDefinition(
        name="ouroboros_context_free_probe",
        description="Synthetic owned tool with no required context keys",
        server_name="ouroboros",
    )

    monkeypatch.setattr(
        "ouroboros.orchestrator.capabilities._ouroboros_tool_definitions_by_name",
        lambda: {synthetic_definition.name: synthetic_definition},
    )
    monkeypatch.setattr(
        "ouroboros.orchestrator.capabilities._OUROBOROS_TOOL_CAPABILITY_SPECS",
        {
            synthetic_definition.name: capabilities_module._OuroborosToolCapabilitySpec(
                execution_mode="status",
                companions=(),
                side_effects=(),
                retry={"supported": True, "mode": "handler_owned"},
                interrupt=capabilities_module._OUROBOROS_READ_ONLY_INTERRUPT_METADATA,
                mutation_class=CapabilityMutationClass.READ_ONLY,
            )
        },
    )
    monkeypatch.setattr(
        "ouroboros.orchestrator.capabilities._OUROBOROS_CANCEL_METADATA",
        {synthetic_definition.name: _UNSUPPORTED_CANCEL},
    )
    monkeypatch.setattr(
        "ouroboros.orchestrator.capabilities.get_packaged_skill_context_keys",
        lambda: {},
    )

    graph = build_capability_graph((synthetic_definition,))
    descriptor = graph.by_name()[synthetic_definition.name]

    assert descriptor.metadata is not None
    assert descriptor.metadata.fallback_used is False
    assert descriptor.metadata.input_schema["required"] == []
    assert descriptor.metadata.required_context_keys == ()

    serialized = serialize_capability_graph(graph)

    assert serialized[0]["metadata"] is not None
    assert "required_context_keys" in serialized[0]["metadata"]
    assert serialized[0]["metadata"]["required_context_keys"] == []

    restored = normalize_serialized_capability_graph(serialized)

    assert restored is not None
    restored_descriptor = restored.by_name()[synthetic_definition.name]
    assert restored_descriptor.metadata is not None
    assert restored_descriptor.metadata.fallback_used is False
    assert restored_descriptor.metadata.required_context_keys == ()
    validate_capability_tool_metadata(
        restored_descriptor.metadata,
        tool_name=synthetic_definition.name,
        owned_tool=True,
    )


def test_owned_mcp_tool_without_companions_serializes_explicit_empty_value(
    monkeypatch,
) -> None:
    synthetic_definition = MCPToolDefinition(
        name="ouroboros_standalone_probe",
        description="Synthetic owned tool with no companion relationship",
        parameters=(
            MCPToolParameter(
                name="session_id",
                type=ToolInputType.STRING,
                description="Session identifier.",
                required=True,
            ),
        ),
        server_name="ouroboros",
    )

    monkeypatch.setattr(
        "ouroboros.orchestrator.capabilities._ouroboros_tool_definitions_by_name",
        lambda: {synthetic_definition.name: synthetic_definition},
    )
    monkeypatch.setattr(
        "ouroboros.orchestrator.capabilities._OUROBOROS_TOOL_CAPABILITY_SPECS",
        {
            synthetic_definition.name: capabilities_module._OuroborosToolCapabilitySpec(
                execution_mode="status",
                companions=(),
                side_effects=(),
                retry={"supported": True, "mode": "handler_owned"},
                interrupt=capabilities_module._OUROBOROS_READ_ONLY_INTERRUPT_METADATA,
                mutation_class=CapabilityMutationClass.READ_ONLY,
            )
        },
    )
    monkeypatch.setattr(
        "ouroboros.orchestrator.capabilities._OUROBOROS_CANCEL_METADATA",
        {synthetic_definition.name: _UNSUPPORTED_CANCEL},
    )

    graph = build_capability_graph((synthetic_definition,))
    descriptor = graph.by_name()[synthetic_definition.name]

    assert descriptor.metadata is not None
    assert descriptor.metadata.fallback_used is False
    assert descriptor.metadata.companions == ()

    serialized = serialize_capability_graph(graph)

    assert serialized[0]["metadata"] is not None
    assert "companions" in serialized[0]["metadata"]
    assert serialized[0]["metadata"]["companions"] == []
    assert "side_effects" in serialized[0]["metadata"]
    assert serialized[0]["metadata"]["side_effects"] == []

    restored = normalize_serialized_capability_graph(serialized)

    assert restored is not None
    restored_descriptor = restored.by_name()[synthetic_definition.name]
    assert restored_descriptor.metadata is not None
    assert restored_descriptor.metadata.fallback_used is False
    assert restored_descriptor.metadata.companions == ()
    validate_capability_tool_metadata(
        restored_descriptor.metadata,
        tool_name=synthetic_definition.name,
        owned_tool=True,
    )


def test_owned_mcp_tool_without_lifecycle_behaviors_serializes_explicit_empty_values(
    monkeypatch,
) -> None:
    synthetic_definition = MCPToolDefinition(
        name="ouroboros_lifecycle_empty_probe",
        description="Synthetic owned tool with no retry, interrupt, or cancel behavior",
        server_name="ouroboros",
    )
    unsupported_retry = {"supported": False, "mode": "unsupported"}
    unsupported_interrupt = {"supported": False, "mode": "unsupported"}

    monkeypatch.setattr(
        "ouroboros.orchestrator.capabilities._ouroboros_tool_definitions_by_name",
        lambda: {synthetic_definition.name: synthetic_definition},
    )
    monkeypatch.setattr(
        "ouroboros.orchestrator.capabilities._OUROBOROS_TOOL_CAPABILITY_SPECS",
        {
            synthetic_definition.name: capabilities_module._OuroborosToolCapabilitySpec(
                execution_mode="status",
                companions=(),
                side_effects=(),
                retry=unsupported_retry,
                interrupt=unsupported_interrupt,
                mutation_class=CapabilityMutationClass.READ_ONLY,
            )
        },
    )
    monkeypatch.setattr(
        "ouroboros.orchestrator.capabilities._OUROBOROS_CANCEL_METADATA",
        {synthetic_definition.name: _UNSUPPORTED_CANCEL},
    )

    graph = build_capability_graph((synthetic_definition,))
    descriptor = graph.by_name()[synthetic_definition.name]

    assert descriptor.metadata is not None
    assert descriptor.metadata.fallback_used is False
    assert descriptor.metadata.retry == unsupported_retry
    assert descriptor.metadata.interrupt == unsupported_interrupt
    assert descriptor.metadata.cancel == _UNSUPPORTED_CANCEL
    assert descriptor.metadata.cancel["companions"] == ()
    assert descriptor.metadata.cancel["target_context_keys"] == ()
    validate_capability_tool_metadata(
        descriptor.metadata,
        tool_name=synthetic_definition.name,
        owned_tool=True,
    )

    serialized = serialize_capability_graph(graph)
    serialized_metadata = serialized[0]["metadata"]

    assert serialized_metadata is not None
    assert serialized_metadata["retry"] == unsupported_retry
    assert serialized_metadata["interrupt"] == unsupported_interrupt
    assert serialized_metadata["cancel"] == {
        "supported": False,
        "mode": "unsupported",
        "companions": [],
        "target_context_keys": [],
    }

    restored = normalize_serialized_capability_graph(serialized)

    assert restored is not None
    restored_descriptor = restored.by_name()[synthetic_definition.name]
    assert restored_descriptor.metadata is not None
    assert restored_descriptor.metadata.fallback_used is False
    assert restored_descriptor.metadata.retry == unsupported_retry
    assert restored_descriptor.metadata.interrupt == unsupported_interrupt
    assert restored_descriptor.metadata.cancel == _UNSUPPORTED_CANCEL
    validate_capability_tool_metadata(
        restored_descriptor.metadata,
        tool_name=synthetic_definition.name,
        owned_tool=True,
    )


def test_build_capability_graph_preserves_builtin_and_attached_semantics() -> None:
    catalog = assemble_session_tool_catalog(
        builtin_tools=["Read", "Edit", "Bash"],
        attached_tools=(
            MCPToolDefinition(
                name="search_docs",
                description="Search project docs",
                server_name="docs",
            ),
        ),
    )

    graph = build_capability_graph(catalog)

    names = {descriptor.name: descriptor for descriptor in graph.capabilities}
    assert names["Read"].semantics.mutation_class is CapabilityMutationClass.READ_ONLY
    assert names["Read"].semantics.origin is CapabilityOrigin.BUILTIN
    assert names["Edit"].semantics.mutation_class is CapabilityMutationClass.WORKSPACE_WRITE
    assert names["Bash"].semantics.scope is CapabilityScope.SHELL_ONLY
    assert names["search_docs"].semantics.origin is CapabilityOrigin.ATTACHED_MCP
    assert names["search_docs"].semantics.scope is CapabilityScope.ATTACHMENT


def test_unknown_attached_mcp_tool_uses_generic_inference_fallback() -> None:
    known_ouroboros_tool_names = {
        handler.definition.name for handler in get_ouroboros_tools(include_auto=False)
    }
    unknown_tool = MCPToolDefinition(
        name="delete_remote_cache",
        description="Delete a remote cache entry on a third-party service",
        server_name="cache_vendor",
    )
    assert unknown_tool.name not in known_ouroboros_tool_names
    catalog = assemble_session_tool_catalog(attached_tools=(unknown_tool,))

    graph = build_capability_graph(catalog)

    descriptor = graph.capabilities[0]
    assert descriptor.name == "delete_remote_cache"
    assert descriptor.source_kind == "attached_mcp"
    assert descriptor.source_name == "cache_vendor"
    assert descriptor.stable_id == "mcp:cache_vendor:delete_remote_cache"
    assert descriptor.semantics.origin is CapabilityOrigin.ATTACHED_MCP
    assert descriptor.semantics.scope is CapabilityScope.ATTACHMENT
    assert descriptor.semantics.mutation_class is CapabilityMutationClass.DESTRUCTIVE
    assert (
        descriptor.semantics.parallel_safety is CapabilityParallelSafety.ISOLATED_SESSION_REQUIRED
    )
    assert descriptor.semantics.interruptibility is CapabilityInterruptibility.HARD
    assert descriptor.semantics.approval_class is CapabilityApprovalClass.BYPASS_FORBIDDEN
    assert descriptor.metadata is not None
    assert descriptor.metadata.fallback_used is True
    assert descriptor.metadata.state_mutations == ()


def test_interview_capability_exposes_code_investigation_request_model_schema() -> None:
    code_investigation = _interview_code_investigation_metadata()
    schema = code_investigation["request_model_schema"]

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert "working_directory" not in schema["properties"]
    assert "cwd" not in schema["properties"]
    assert "repository_path" not in schema["properties"]
    assert "server_repository_path" not in schema["properties"]
    assert set(schema["required"]) == {
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
    }
    assert schema["properties"]["answer_contract"] == {
        "const": code_investigation["answer_contract"],
        "description": "Exact response contract attached to this investigation request.",
    }
    capability_schema = schema["properties"]["mcp_tool_capability"]
    assert set(capability_schema["required"]) == {
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
    }
    assert capability_schema["properties"]["tool_name"] == {"const": "ouroboros_interview"}
    assert capability_schema["properties"]["fallback_used"] == {"const": False}
    assert schema["properties"]["investigation_goal"]["enum"] == [
        "describe_current_state_from_code"
    ]
    targets = schema["properties"]["investigation_targets"]
    assert targets["type"] == "array"
    assert targets["minItems"] == 1
    assert {
        option["properties"]["target_type"]["const"] for option in targets["items"]["oneOf"]
    } == {"workspace", "relative_path", "glob", "symbol"}
    assert schema["properties"]["allowed_capabilities"]["items"]["enum"] == ["inspect_code"]
    repo_tool_schema = schema["properties"]["repo_inspection_tool_capabilities"]
    assert repo_tool_schema["minItems"] == 1
    assert repo_tool_schema["items"]["properties"]["tool_name"]["enum"] == [
        "Read",
        "Glob",
        "Grep",
    ]
    assert repo_tool_schema["items"]["properties"]["execution_mode"] == {"const": "repo_inspection"}
    assert repo_tool_schema["items"]["properties"]["logical_capability"] == {
        "const": "inspect_code"
    }
    assert repo_tool_schema["items"]["properties"]["fallback_used"] == {"const": False}
    assert "[from-code]" in schema["properties"]["answer_prefixes"]["items"]["enum"]
    assert "[from-code][auto-confirmed]" in schema["properties"]["answer_prefixes"]["items"]["enum"]
    assert schema["properties"]["question_identity"]["pattern"] == (
        r"^interview-question:[0-9a-f]{16}$"
    )
    assert set(schema["properties"]["confidence_policy"]["required"]) == {
        "auto_confirm_when",
        "confirmation_required_when",
        "human_judgment_when",
    }
    assert code_investigation["question_identity"] == {
        "source_field": "question",
        "helper": "stable_code_investigation_question_identity",
        "algorithm": "sha256",
        "digest_chars": 16,
        "normalization": "NFKC + trim + whitespace collapse",
        "format": "interview-question:{digest}",
        "deterministic": True,
    }
    assert (
        "skills/interview/SKILL.md inspect_code PATH 1" in code_investigation["derivation_sources"]
    )


def test_interview_code_investigation_exposes_repo_inspection_tool_capabilities() -> None:
    code_investigation = _interview_code_investigation_metadata()

    tool_capabilities = code_investigation["repo_inspection_tool_capabilities"]

    tool_by_name = {tool["tool_name"]: tool for tool in tool_capabilities}
    assert set(tool_by_name) == {"Read", "Glob", "Grep"}

    for tool_name, capability in tool_by_name.items():
        assert capability["stable_id"] == f"builtin:{tool_name}"
        assert capability["source_kind"] == "builtin"
        assert capability["source_name"] == "built-in"
        assert capability["mutation_class"] == "read_only"
        assert capability["parallel_safety"] == "safe"
        assert capability["interruptibility"] == "none"
        assert capability["approval_class"] == "default"
        assert capability["origin"] == "builtin"
        assert capability["scope"] == "kernel"
        assert capability["execution_mode"] == "repo_inspection"
        assert capability["logical_capability"] == "inspect_code"
        assert capability["side_effects"] == ["side_effect_free"]
        assert capability["fallback_used"] is False
        Draft202012Validator.check_schema(capability["input_schema"])

    assert tool_by_name["Read"]["input_schema"]["required"] == ["file_path"]
    assert tool_by_name["Glob"]["input_schema"]["required"] == ["pattern"]
    assert tool_by_name["Grep"]["input_schema"]["required"] == ["pattern"]


def test_code_investigation_subagent_envelope_permits_modeled_repo_inspection_tools() -> None:
    code_investigation = _interview_code_investigation_metadata()
    tool_capabilities = code_investigation["repo_inspection_tool_capabilities"]
    modeled_tool_names = tuple(str(capability["tool_name"]) for capability in tool_capabilities)
    catalog = assemble_session_tool_catalog(builtin_tools=(*modeled_tool_names, "Bash", "Edit"))

    allowed = set(
        allowed_capability_names(
            build_capability_graph(catalog),
            PolicyContext(
                runtime_backend="codex",
                session_role=PolicySessionRole.INTERVIEW,
                execution_phase=PolicyExecutionPhase.INTERVIEW,
            ),
        )
    )

    assert set(
        code_investigation["request_model_schema"]["properties"]["allowed_capabilities"]["items"][
            "enum"
        ]
    ) == {"inspect_code"}
    for capability in tool_capabilities:
        assert capability["logical_capability"] == "inspect_code"
        assert capability["execution_mode"] == "repo_inspection"
        assert capability["mutation_class"] == "read_only"
        assert capability["source_kind"] == "builtin"
        assert capability["tool_name"] in allowed

    assert set(modeled_tool_names) <= allowed
    assert "Bash" not in allowed
    assert "Edit" not in allowed


def test_code_investigation_owned_mcp_tools_have_explicit_tool_specific_metadata(
    monkeypatch,
) -> None:
    definitions = {handler.definition.name: handler.definition for handler in get_ouroboros_tools()}
    code_investigation_mcp_tools = ("ouroboros_interview",)

    def fail_on_generic_attached_metadata(tool: MCPToolDefinition):
        raise AssertionError(
            f"code investigation owned MCP tool used generic metadata: {tool.name}"
        )

    def fail_on_generic_attached_semantics(tool: MCPToolDefinition):
        raise AssertionError(
            f"code investigation owned MCP tool used generic semantics: {tool.name}"
        )

    monkeypatch.setattr(
        "ouroboros.orchestrator.capabilities._generic_attached_tool_metadata",
        fail_on_generic_attached_metadata,
    )
    monkeypatch.setattr(
        "ouroboros.orchestrator.capabilities._infer_attached_semantics",
        fail_on_generic_attached_semantics,
    )

    graph = build_capability_graph(
        assemble_session_tool_catalog(
            attached_tools=(definitions[tool_name] for tool_name in code_investigation_mcp_tools)
        )
    )
    descriptors = graph.by_name()

    for tool_name in code_investigation_mcp_tools:
        definition = definitions[tool_name]
        descriptor = descriptors[tool_name]

        assert descriptor.source_kind == "attached_mcp"
        assert descriptor.source_name == "ouroboros"
        assert descriptor.semantics.origin is CapabilityOrigin.ATTACHED_MCP
        assert descriptor.metadata is not None
        metadata = descriptor.metadata
        assert metadata == ouroboros_tool_capability_registry()[tool_name]
        assert metadata.fallback_used is False
        assert metadata.input_schema == definition.to_input_schema()
        assert metadata.execution_mode == "subagent_orchestration"
        assert metadata.companions == _EXPECTED_OUROBOROS_TOOL_COMPANIONS[tool_name]
        assert (
            metadata.required_context_keys == (_EXPECTED_OUROBOROS_REQUIRED_CONTEXT_KEYS[tool_name])
        )
        assert metadata.side_effects == ("subagent_dispatch", "session_state_write")
        assert metadata.retry == _EXPECTED_OUROBOROS_TOOL_RETRY[tool_name]
        assert metadata.interrupt == _EXPECTED_OUROBOROS_TOOL_INTERRUPT[tool_name]
        assert metadata.cancel == _UNSUPPORTED_CANCEL
        validate_capability_tool_metadata(
            metadata,
            tool_name=tool_name,
            owned_tool=True,
        )

        code_investigation = metadata.orchestration["code_investigation"]
        assert code_investigation["derivation_sources"] == (
            f"get_ouroboros_tools().{tool_name}.definition",
            "skills/interview/SKILL.md inspect_code PATH 1",
        )
        assert code_investigation["request_model_schema"]["properties"]["mcp_tool_capability"][
            "properties"
        ]["tool_name"] == {"const": tool_name}
        assert code_investigation["request_model_schema"]["properties"]["mcp_tool_capability"][
            "properties"
        ]["fallback_used"] == {"const": False}
        assert (
            set(
                code_investigation["request_model_schema"]["properties"]["mcp_tool_capability"][
                    "required"
                ]
            )
            >= _REQUIRED_OUROBOROS_TOOL_METADATA_FIELDS
        )
        assert all(
            repo_tool["fallback_used"] is False
            for repo_tool in code_investigation["repo_inspection_tool_capabilities"]
        )


def test_subagent_job_status_polling_tool_is_explicit_and_callable_in_orchestration_envelope(
    monkeypatch,
) -> None:
    definitions = {handler.definition.name: handler.definition for handler in get_ouroboros_tools()}

    def fail_on_generic_attached_metadata(tool: MCPToolDefinition):
        raise AssertionError(f"subagent status poller used generic attached metadata: {tool.name}")

    def fail_on_generic_attached_semantics(tool: MCPToolDefinition):
        raise AssertionError(f"subagent status poller used generic attached semantics: {tool.name}")

    monkeypatch.setattr(
        "ouroboros.orchestrator.capabilities._generic_attached_tool_metadata",
        fail_on_generic_attached_metadata,
    )
    monkeypatch.setattr(
        "ouroboros.orchestrator.capabilities._infer_attached_semantics",
        fail_on_generic_attached_semantics,
    )

    graph = build_capability_graph(
        assemble_session_tool_catalog(
            attached_tools=(
                definitions["ouroboros_interview"],
                definitions["ouroboros_lateral_think"],
                definitions["ouroboros_job_status"],
            )
        )
    )
    descriptors = graph.by_name()
    status_descriptor = resolve_mcp_tool_capability_descriptor(
        "ouroboros_job_status",
        graph=graph,
    )

    assert status_descriptor == descriptors["ouroboros_job_status"]
    assert status_descriptor is not None
    assert status_descriptor.source_kind == "attached_mcp"
    assert status_descriptor.source_name == "ouroboros"
    assert status_descriptor.semantics.mutation_class is CapabilityMutationClass.READ_ONLY
    assert status_descriptor.semantics.origin is CapabilityOrigin.ATTACHED_MCP
    assert status_descriptor.metadata is not None
    metadata = status_descriptor.metadata
    assert metadata == ouroboros_tool_capability_registry()["ouroboros_job_status"]
    assert metadata.fallback_used is False
    assert metadata.execution_mode == "status"
    assert metadata.input_schema == definitions["ouroboros_job_status"].to_input_schema()
    assert metadata.input_schema["required"] == ["job_id"]
    assert metadata.required_context_keys == ("job_id",)
    assert metadata.side_effects == ()
    assert metadata.retry == {"supported": True, "mode": "handler_owned"}
    assert metadata.interrupt == {
        "supported": True,
        "mode": "read_only_non_mutating",
        "mutation_semantics": "no_state_mutation",
        "resumable": False,
        "target_context_keys": (),
    }
    assert metadata.cancel == _UNSUPPORTED_CANCEL
    validate_capability_tool_metadata(
        metadata,
        tool_name="ouroboros_job_status",
        owned_tool=True,
    )
    Draft202012Validator(metadata.input_schema).validate(
        {"job_id": "job-subagent-123", "view": "summary"}
    )

    job_lifecycle = metadata.orchestration["job_lifecycle_siblings"]
    assert job_lifecycle["role"] == "status"
    assert job_lifecycle["required_result_context_keys"] == ("job_id",)
    assert job_lifecycle["sibling_companions"] == {
        "wait": "ouroboros_job_wait",
        "result": "ouroboros_job_result",
        "cancel": "ouroboros_cancel_job",
    }

    for orchestrator_name in ("ouroboros_interview", "ouroboros_lateral_think"):
        orchestrator = descriptors[orchestrator_name]
        assert orchestrator.metadata is not None
        assert orchestrator.metadata.fallback_used is False
        assert orchestrator.metadata.execution_mode == "subagent_orchestration"


def test_subagent_job_wait_progress_polling_tool_is_explicit_and_callable_in_orchestration_envelope(
    monkeypatch,
) -> None:
    definitions = {handler.definition.name: handler.definition for handler in get_ouroboros_tools()}

    def fail_on_generic_attached_metadata(tool: MCPToolDefinition):
        raise AssertionError(
            f"subagent progress wait poller used generic attached metadata: {tool.name}"
        )

    def fail_on_generic_attached_semantics(tool: MCPToolDefinition):
        raise AssertionError(
            f"subagent progress wait poller used generic attached semantics: {tool.name}"
        )

    monkeypatch.setattr(
        "ouroboros.orchestrator.capabilities._generic_attached_tool_metadata",
        fail_on_generic_attached_metadata,
    )
    monkeypatch.setattr(
        "ouroboros.orchestrator.capabilities._infer_attached_semantics",
        fail_on_generic_attached_semantics,
    )

    graph = build_capability_graph(
        assemble_session_tool_catalog(
            attached_tools=(
                definitions["ouroboros_interview"],
                definitions["ouroboros_lateral_think"],
                definitions["ouroboros_job_wait"],
            )
        )
    )
    descriptors = graph.by_name()
    wait_descriptor = resolve_mcp_tool_capability_descriptor(
        "ouroboros_job_wait",
        graph=graph,
    )

    assert wait_descriptor == descriptors["ouroboros_job_wait"]
    assert wait_descriptor is not None
    assert wait_descriptor.source_kind == "attached_mcp"
    assert wait_descriptor.source_name == "ouroboros"
    assert wait_descriptor.semantics.mutation_class is CapabilityMutationClass.READ_ONLY
    assert wait_descriptor.semantics.origin is CapabilityOrigin.ATTACHED_MCP
    assert wait_descriptor.metadata is not None
    metadata = wait_descriptor.metadata
    assert metadata == ouroboros_tool_capability_registry()["ouroboros_job_wait"]
    assert metadata.fallback_used is False
    assert metadata.execution_mode == "status"
    assert metadata.input_schema == definitions["ouroboros_job_wait"].to_input_schema()
    assert metadata.input_schema["required"] == ["job_id"]
    assert metadata.required_context_keys == ("job_id", "cursor")
    assert metadata.side_effects == ()
    assert metadata.retry == {"supported": True, "mode": "handler_owned"}
    assert metadata.interrupt == {
        "supported": True,
        "mode": "read_only_non_mutating",
        "mutation_semantics": "no_state_mutation",
        "resumable": False,
        "target_context_keys": (),
    }
    assert metadata.cancel == _UNSUPPORTED_CANCEL
    validate_capability_tool_metadata(
        metadata,
        tool_name="ouroboros_job_wait",
        owned_tool=True,
    )
    Draft202012Validator(metadata.input_schema).validate(
        {
            "job_id": "job-subagent-123",
            "cursor": 7,
            "stream": "linked",
            "timeout_seconds": 0,
            "view": "summary",
        }
    )

    job_lifecycle = metadata.orchestration["job_lifecycle_siblings"]
    assert job_lifecycle["role"] == "wait"
    assert job_lifecycle["required_result_context_keys"] == ("job_id",)
    assert job_lifecycle["sibling_companions"] == {
        "status": "ouroboros_job_status",
        "result": "ouroboros_job_result",
        "cancel": "ouroboros_cancel_job",
    }
    assert (
        "linked session, lineage, and subagent events"
        in (metadata.input_schema["properties"]["stream"]["description"])
    )

    for orchestrator_name in ("ouroboros_interview", "ouroboros_lateral_think"):
        orchestrator = descriptors[orchestrator_name]
        assert orchestrator.metadata is not None
        assert orchestrator.metadata.fallback_used is False
        assert orchestrator.metadata.execution_mode == "subagent_orchestration"


def test_subagent_job_cancellation_tool_is_explicit_and_callable_in_orchestration_envelope(
    monkeypatch,
) -> None:
    definitions = {handler.definition.name: handler.definition for handler in get_ouroboros_tools()}

    def fail_on_generic_attached_metadata(tool: MCPToolDefinition):
        raise AssertionError(
            f"subagent cancellation tool used generic attached metadata: {tool.name}"
        )

    def fail_on_generic_attached_semantics(tool: MCPToolDefinition):
        raise AssertionError(
            f"subagent cancellation tool used generic attached semantics: {tool.name}"
        )

    monkeypatch.setattr(
        "ouroboros.orchestrator.capabilities._generic_attached_tool_metadata",
        fail_on_generic_attached_metadata,
    )
    monkeypatch.setattr(
        "ouroboros.orchestrator.capabilities._infer_attached_semantics",
        fail_on_generic_attached_semantics,
    )

    graph = build_capability_graph(
        assemble_session_tool_catalog(
            attached_tools=(
                definitions["ouroboros_interview"],
                definitions["ouroboros_lateral_think"],
                definitions["ouroboros_cancel_job"],
            )
        )
    )
    descriptors = graph.by_name()
    cancel_descriptor = resolve_mcp_tool_capability_descriptor(
        "ouroboros_cancel_job",
        graph=graph,
    )

    assert cancel_descriptor == descriptors["ouroboros_cancel_job"]
    assert cancel_descriptor is not None
    assert cancel_descriptor.source_kind == "attached_mcp"
    assert cancel_descriptor.source_name == "ouroboros"
    assert cancel_descriptor.semantics.mutation_class is (
        CapabilityMutationClass.EXTERNAL_SIDE_EFFECT
    )
    assert cancel_descriptor.semantics.parallel_safety is (CapabilityParallelSafety.SERIALIZED)
    assert cancel_descriptor.semantics.interruptibility is (CapabilityInterruptibility.HARD)
    assert cancel_descriptor.semantics.approval_class is (CapabilityApprovalClass.ELEVATED)
    assert cancel_descriptor.metadata is not None
    metadata = cancel_descriptor.metadata
    assert metadata == ouroboros_tool_capability_registry()["ouroboros_cancel_job"]
    assert metadata.fallback_used is False
    assert metadata.execution_mode == "cancel"
    assert metadata.input_schema == definitions["ouroboros_cancel_job"].to_input_schema()
    assert metadata.input_schema["required"] == ["job_id"]
    assert metadata.required_context_keys == ("job_id",)
    assert metadata.side_effects == (
        "runtime_control",
        "event_store_write",
        "checkpoint_store_write",
        "session_state_write",
    )
    assert metadata.retry == {"supported": False, "mode": "unsupported"}
    assert metadata.interrupt == {
        "supported": True,
        "mode": "terminal_control",
        "terminal_action": "cancel",
        "target_type": "background_job",
        "target_context_keys": ("job_id",),
        "directive_semantics": "request_terminal_job_cancellation",
        "terminal_statuses": ("cancelled",),
        "idempotent": True,
    }
    assert metadata.cancel == {
        "supported": True,
        "mode": "background_job_control",
        "companions": (
            "ouroboros_job_status",
            "ouroboros_job_wait",
            "ouroboros_job_result",
        ),
        "target_context_keys": ("job_id",),
    }
    validate_capability_tool_metadata(
        metadata,
        tool_name="ouroboros_cancel_job",
        owned_tool=True,
    )
    Draft202012Validator(metadata.input_schema).validate(
        {"job_id": "job-subagent-123", "reason": "cancel delegated subagent job"}
    )

    job_lifecycle = metadata.orchestration["job_lifecycle_siblings"]
    assert job_lifecycle["role"] == "cancel"
    assert job_lifecycle["required_result_context_keys"] == ("job_id",)
    assert job_lifecycle["sibling_companions"] == {
        "status": "ouroboros_job_status",
        "wait": "ouroboros_job_wait",
        "result": "ouroboros_job_result",
    }
    assert job_lifecycle["companion_roles"]["cancel"] == "ouroboros_cancel_job"
    assert "status/wait/result/cancel sibling family" in (job_lifecycle["runtime_instruction"])

    for orchestrator_name in ("ouroboros_interview", "ouroboros_lateral_think"):
        orchestrator = descriptors[orchestrator_name]
        assert orchestrator.metadata is not None
        assert orchestrator.metadata.fallback_used is False
        assert orchestrator.metadata.execution_mode == "subagent_orchestration"


def test_interview_capability_exposes_single_code_investigation_answer_contract() -> None:
    code_investigation = _interview_code_investigation_metadata()

    answer_contract = code_investigation["answer_contract"]
    assert answer_contract["contract_id"] == "code_fact_investigation_answer.v1"
    assert answer_contract["scope"] == "single_code_fact_investigation_request"
    assert answer_contract["evidence_policy"] == {
        "minimum_items": 1,
        "source_format": "repository_relative_path_or_symbol",
        "server_local_paths_allowed": False,
    }
    assert "exactly one structured answer payload" in answer_contract["runtime_instruction"]

    prefix_semantics = answer_contract["prefix_semantics"]
    assert prefix_semantics["[from-code][auto-confirmed]"] == {
        "confidence": "high_exact_match",
        "requires_user_confirmation": False,
        "forwarding": "send_to_mcp_immediately",
    }
    assert prefix_semantics["[from-code]"] == {
        "confidence": "medium_or_low",
        "requires_user_confirmation": True,
        "forwarding": "confirm_with_user_before_mcp",
    }

    response_schema = answer_contract["response_model_schema"]
    Draft202012Validator.check_schema(response_schema)
    assert response_schema["additionalProperties"] is False
    assert set(response_schema["required"]) == {
        "session_id",
        "question_identity",
        "answer_prefix",
        "answer_text",
        "confidence",
        "evidence",
        "requires_user_confirmation",
    }
    assert response_schema["properties"]["answer_prefix"]["enum"] == [
        "[from-code]",
        "[from-code][auto-confirmed]",
    ]
    assert response_schema["properties"]["confidence"]["enum"] == [
        "high_exact_match",
        "medium_inferred",
        "low_uncertain",
    ]
    assert response_schema["properties"]["question_identity"]["pattern"] == (
        r"^interview-question:[0-9a-f]{16}$"
    )


def test_code_investigation_answer_contract_validates_single_auto_confirmed_answer() -> None:
    code_investigation = _interview_code_investigation_metadata()
    response_schema = code_investigation["answer_contract"]["response_model_schema"]
    validator = Draft202012Validator(response_schema)
    question = "What framework does this project use?"

    validator.validate(
        {
            "session_id": "sess-123",
            "question_identity": stable_code_investigation_question_identity(question),
            "answer_prefix": "[from-code][auto-confirmed]",
            "answer_text": "Python 3.12, FastAPI",
            "confidence": "high_exact_match",
            "evidence": [
                {
                    "source": "pyproject.toml",
                    "locator": "project.dependencies",
                    "claim": "FastAPI is declared as a dependency.",
                }
            ],
            "requires_user_confirmation": False,
        }
    )


def test_code_investigation_answer_contract_rejects_implicit_or_mismatched_answer() -> None:
    code_investigation = _interview_code_investigation_metadata()
    response_schema = code_investigation["answer_contract"]["response_model_schema"]
    validator = Draft202012Validator(response_schema)
    question = "What framework does this project use?"

    errors = list(
        validator.iter_errors(
            {
                "session_id": "sess-123",
                "question_identity": stable_code_investigation_question_identity(question),
                "answer_prefix": "[from-code][auto-confirmed]",
                "answer_text": "Probably FastAPI",
                "confidence": "medium_inferred",
                "evidence": [
                    {
                        "source": "src/app.py",
                        "claim": "Imports look like FastAPI.",
                    }
                ],
                "requires_user_confirmation": True,
                "server_repository_path": "/server/local/repo",
            }
        )
    )

    messages = [error.message for error in errors]
    assert any("'high_exact_match' was expected" in message for message in messages)
    assert any("False was expected" in message for message in messages)
    assert any(
        "Additional properties are not allowed" in message and "server_repository_path" in message
        for message in messages
    )


def test_code_investigation_question_identity_is_stable_from_question_text() -> None:
    question = "  What framework\n does this project use?  "

    first = stable_code_investigation_question_identity(question)
    second = stable_code_investigation_question_identity("What framework does this project use?")
    changed = stable_code_investigation_question_identity("What auth method does this project use?")

    assert first == second
    assert first.startswith("interview-question:")
    assert first != changed


def test_code_investigation_request_model_rejects_malformed_question_identity() -> None:
    owned_tools = tuple(handler.definition for handler in get_ouroboros_tools(include_auto=False))
    catalog = assemble_session_tool_catalog(attached_tools=owned_tools)
    graph = build_capability_graph(catalog)
    descriptors = {descriptor.name: descriptor for descriptor in graph.capabilities}
    interview = descriptors["ouroboros_interview"]
    assert interview.metadata is not None
    schema = interview.metadata.orchestration["code_investigation"]["request_model_schema"]
    validator = Draft202012Validator(schema)

    request = {
        **_code_investigation_base_request("What framework does this project use?"),
        "question_identity": "question-1",
        "investigation_targets": [{"target_type": "workspace", "scope": "active"}],
    }

    errors = list(validator.iter_errors(request))
    assert any(
        "'question-1' does not match" in error.message and "interview-question" in error.message
        for error in errors
    )


def test_code_investigation_request_deserializes_embedded_tool_metadata_without_repo_access(
    monkeypatch,
) -> None:
    question = "What framework does this project use?"
    request = {
        **_code_investigation_base_request(question),
        "investigation_targets": [{"target_type": "workspace", "scope": "active"}],
    }
    expected_capability = request["mcp_tool_capability"]
    assert isinstance(expected_capability, dict)

    monkeypatch.setattr(
        "ouroboros.orchestrator.capabilities._ouroboros_tool_definitions_by_name",
        lambda: (_ for _ in ()).throw(
            AssertionError("deserialization must use embedded request metadata")
        ),
    )

    deserialized = deserialize_code_investigation_request_metadata(request)

    assert deserialized is not None
    capability = deserialized["mcp_tool_capability"]
    assert capability["tool_name"] == "ouroboros_interview"
    assert capability["stable_id"] == expected_capability["stable_id"]
    assert capability["source_kind"] == "attached_mcp"
    assert capability["source_name"] == "ouroboros"
    assert capability["fallback_used"] is False
    assert capability["execution_mode"] == "subagent_orchestration"
    for field_name in (
        "input_schema",
        "mutation_class",
        "execution_mode",
        "companions",
        "required_context_keys",
        "mutation_targets",
        "side_effects",
        "retry",
        "interrupt",
        "cancel",
        "fallback_used",
        "orchestration",
    ):
        assert field_name in capability
    assert capability["input_schema"] == expected_capability["input_schema"]
    assert capability["mutation_class"] == expected_capability["mutation_class"]
    assert capability["companions"] == expected_capability["companions"]
    assert capability["required_context_keys"] == expected_capability["required_context_keys"]
    assert capability["mutation_targets"] == expected_capability["mutation_targets"]
    assert capability["state_mutations"] == [
        {
            **mutation,
            "context_keys": list(mutation["context_keys"]),
        }
        for mutation in expected_capability["state_mutations"]
    ]
    assert capability["side_effects"] == expected_capability["side_effects"]
    assert capability["retry"] == expected_capability["retry"]
    assert capability["interrupt"] == expected_capability["interrupt"]
    assert capability["cancel"] == expected_capability["cancel"]
    assert set(capability["orchestration"]) >= {
        "code_investigation",
        "lateral_panel",
    }


def test_lateral_persona_panel_metadata_is_structured_without_prose_parsing() -> None:
    owned_tools = tuple(handler.definition for handler in get_ouroboros_tools(include_auto=False))
    catalog = assemble_session_tool_catalog(attached_tools=owned_tools)

    graph = build_capability_graph(catalog)

    descriptors = {descriptor.name: descriptor for descriptor in graph.capabilities}
    lateral = descriptors["ouroboros_lateral_think"]
    assert lateral.metadata is not None
    assert lateral.metadata.fallback_used is False
    assert lateral.metadata.execution_mode == "subagent_orchestration"

    panel = lateral.metadata.orchestration["lateral_panel"]
    assert panel["panel_id"] == "lateral_persona_panel.v1"
    assert panel["mcp_tool"] == "ouroboros_lateral_think"
    assert panel["dispatch_modes"] == ["plugin", "sequential"]
    assert panel["legacy_dispatch_modes"] == ["inline_fallback"]
    assert panel["parallel_preference"] == "parallel_when_runtime_supports_subagents"
    assert panel["sequential_fallback"] == {
        "supported": True,
        "mode": "sequential_persona_payload_dispatch",
        "trigger": "runtime_has_no_native_parallel_subagent_primitive",
    }

    schema = panel["request_model_schema"]
    assert schema["required"] == ["problem_context", "current_approach"]
    assert schema["properties"]["personas"]["items"]["enum"] == [
        "hacker",
        "researcher",
        "simplifier",
        "architect",
        "contrarian",
    ]

    persona_by_id = {persona["persona_id"]: persona for persona in panel["personas"]}
    assert set(persona_by_id) == {
        "hacker",
        "researcher",
        "simplifier",
        "architect",
        "contrarian",
    }
    assert persona_by_id["hacker"]["role"] == "Finds unconventional workarounds"
    assert persona_by_id["researcher"]["role"] == "Seeks additional information"

    for persona in persona_by_id.values():
        assert persona["prompt"] == {
            "source": "build_lateral_multi_subagent",
            "payload_field": "payloads[].prompt",
            "context_field": "payloads[].context",
            "requires_prose_parsing": False,
        }
        refs = persona["response_payload_ref"]
        assert refs["plugin"] == "MCPToolResult.meta._subagents[persona_id]"
        assert refs["inline_meta"] == "MCPToolResult.meta.payloads[persona_id]"
        assert "ouroboros-lateral-inline-dispatch-v1.payloads[persona_id]" in refs["inline_content"]

    panel_refs = panel["response_payload_refs"]
    assert panel_refs["plugin"] == "MCPToolResult.meta._subagents"
    assert panel_refs["inline_meta"] == "MCPToolResult.meta.payloads"
    assert panel_refs["result_correlation_key"] == "context.persona"
    assert panel_refs["requires_prose_parsing"] is False
    assert "structured payload" in panel["runtime_instruction"]


def test_pm_interview_subagent_metadata_is_structured_without_prose_parsing() -> None:
    owned_tools = tuple(handler.definition for handler in get_ouroboros_tools(include_auto=False))
    catalog = assemble_session_tool_catalog(attached_tools=owned_tools)

    graph = build_capability_graph(catalog)

    pm_interview = graph.by_name()["ouroboros_pm_interview"]
    assert pm_interview.metadata is not None
    assert pm_interview.metadata.fallback_used is False
    assert pm_interview.metadata.execution_mode == "subagent_orchestration"

    contract = pm_interview.metadata.orchestration["pm_interview_subagent"]
    assert contract["directive"] == "run_pm_interview_subagent"
    assert contract["mcp_tool"] == "ouroboros_pm_interview"
    assert contract["dispatch_modes"] == ["plugin"]
    assert contract["payload_builder"] == "build_pm_interview_subagent"

    schema = contract["request_model_schema"]
    assert schema["properties"]["action"]["enum"] == [
        "start",
        "answer",
        "resume",
        "generate",
        "select_repos",
    ]
    assert schema["properties"]["selected_repos"]["items"] == {"type": "string"}

    refs = contract["response_payload_refs"]
    assert refs["plugin"] == "MCPToolResult.meta._subagent"
    assert refs["content_json"] == "MCPToolResult.content[0].text._subagent"
    assert refs["result_correlation_key"] == "context.session_id"
    assert refs["requires_prose_parsing"] is False
    assert contract["subagent_context_keys"] == [
        "session_id",
        "action",
        "initial_context",
        "answer",
        "cwd",
        "selected_repos",
    ]
    assert "structured payload" in contract["runtime_instruction"]


@pytest.mark.asyncio
async def test_owned_lateral_thinking_capability_invokes_subagent_orchestration() -> None:
    if lateral_review_response_to_interview_orchestration_entries is None:
        pytest.skip("lateral orchestration reader is introduced later in the stack")

    owned_tools = tuple(handler.definition for handler in get_ouroboros_tools(include_auto=False))
    catalog = assemble_session_tool_catalog(attached_tools=owned_tools)
    graph = build_capability_graph(catalog)

    descriptor = graph.by_name()["ouroboros_lateral_think"]
    metadata = descriptor.metadata
    assert metadata is not None
    assert metadata.fallback_used is False
    assert metadata.execution_mode == "subagent_orchestration"
    assert metadata.companions == ("ouroboros_interview",)
    assert metadata.required_context_keys == (
        "problem_context",
        "current_approach",
        "failed_attempts",
    )
    assert metadata.side_effects == ("subagent_dispatch", "session_state_write")
    assert metadata.retry == {"supported": True, "mode": "handler_owned"}
    assert metadata.interrupt == {"supported": True, "mode": "soft"}
    assert metadata.cancel == {
        "supported": False,
        "mode": "unsupported",
        "companions": (),
        "target_context_keys": (),
    }
    assert set(metadata.orchestration) >= {"lateral_panel"}

    panel = metadata.orchestration["lateral_panel"]
    assert panel["mcp_tool"] == "ouroboros_lateral_think"
    assert panel["parallel_preference"] == "parallel_when_runtime_supports_subagents"
    assert panel["sequential_fallback"]["supported"] is True
    assert panel["response_payload_refs"]["requires_prose_parsing"] is False

    tool_args = {
        "problem_context": "Interview milestone changed from initial to progress.",
        "current_approach": "Continue with the next Socratic interview question.",
        "personas": ["researcher", "contrarian"],
        "failed_attempts": [],
    }
    Draft202012Validator(metadata.input_schema).validate(tool_args)

    handler = LateralThinkHandler(
        agent_runtime_backend="opencode",
        opencode_mode="plugin",
    )
    result = await handler.handle(tool_args)

    assert result.is_ok, result
    payload = result.unwrap()
    assert payload.meta is not None
    assert "_subagents" in payload.meta
    assert {subagent["tool_name"] for subagent in payload.meta["_subagents"]} == {
        "ouroboros_lateral_think"
    }

    entries = lateral_review_response_to_interview_orchestration_entries(
        payload,
        session_id="sess-lateral-capability",
        runtime_supports_parallel_subagents=True,
        lateral_panel_metadata=panel,
    )

    assert [entry["persona_id"] for entry in entries] == [
        "researcher",
        "contrarian",
    ]
    assert {entry["mcp_tool"] for entry in entries} == {"ouroboros_lateral_think"}
    assert {entry["execution_mode"] for entry in entries} == {"parallel_subagent_panel"}
    assert all(entry["requires_prose_parsing"] is False for entry in entries)
    assert all(entry["sequential_fallback_used"] is False for entry in entries)
    assert all(entry["context"]["persona"] == entry["persona_id"] for entry in entries)


@pytest.mark.asyncio
async def test_owned_lateral_review_tool_path_uses_explicit_metadata_and_is_callable(
    monkeypatch,
) -> None:
    if lateral_review_response_to_interview_orchestration_entries is None:
        pytest.skip("lateral orchestration reader is introduced later in the stack")

    handlers = get_ouroboros_tools(
        runtime_backend="opencode",
        opencode_mode="plugin",
        include_auto=False,
    )
    definitions = {handler.definition.name: handler.definition for handler in handlers}
    review_handler = next(
        handler for handler in handlers if handler.definition.name == "ouroboros_lateral_think"
    )

    def fail_on_generic_attached_metadata(tool: MCPToolDefinition):
        raise AssertionError(f"review tool used generic metadata: {tool.name}")

    def fail_on_generic_attached_semantics(tool: MCPToolDefinition):
        raise AssertionError(f"review tool used generic semantics: {tool.name}")

    monkeypatch.setattr(
        "ouroboros.orchestrator.capabilities._generic_attached_tool_metadata",
        fail_on_generic_attached_metadata,
    )
    monkeypatch.setattr(
        "ouroboros.orchestrator.capabilities._infer_attached_semantics",
        fail_on_generic_attached_semantics,
    )

    graph = build_capability_graph(
        assemble_session_tool_catalog(attached_tools=(definitions["ouroboros_lateral_think"],))
    )
    descriptor = graph.by_name()["ouroboros_lateral_think"]
    metadata = descriptor.metadata

    assert metadata is not None
    assert metadata == ouroboros_tool_capability_registry()["ouroboros_lateral_think"]
    assert metadata.fallback_used is False
    assert metadata.input_schema == definitions["ouroboros_lateral_think"].to_input_schema()
    assert metadata.execution_mode == "subagent_orchestration"
    assert metadata.companions == ("ouroboros_interview",)
    assert metadata.required_context_keys == (
        "problem_context",
        "current_approach",
        "failed_attempts",
    )
    assert metadata.side_effects == ("subagent_dispatch", "session_state_write")
    assert metadata.retry == {"supported": True, "mode": "handler_owned"}
    assert metadata.interrupt == {"supported": True, "mode": "soft"}
    assert metadata.cancel == _UNSUPPORTED_CANCEL
    validate_capability_tool_metadata(
        metadata,
        tool_name="ouroboros_lateral_think",
        owned_tool=True,
    )

    panel = metadata.orchestration["lateral_panel"]
    assert panel["mcp_tool"] == "ouroboros_lateral_think"
    assert panel["parallel_preference"] == "parallel_when_runtime_supports_subagents"
    assert panel["sequential_fallback"] == {
        "supported": True,
        "mode": "sequential_persona_payload_dispatch",
        "trigger": "runtime_has_no_native_parallel_subagent_primitive",
    }
    assert panel["response_payload_refs"]["requires_prose_parsing"] is False

    tool_args = {
        "problem_context": (
            "Interview lateral review for milestone transition initial -> progress."
        ),
        "current_approach": "Continue only after synthesized review findings.",
        "personas": ["researcher", "contrarian"],
        "failed_attempts": [],
    }
    Draft202012Validator(metadata.input_schema).validate(tool_args)

    result = await review_handler.handle(tool_args)

    assert result.is_ok, result
    payload = result.unwrap()
    assert payload.meta is not None
    assert payload.meta["dispatch_mode"] == "plugin"
    assert "_subagents" in payload.meta
    assert {subagent["tool_name"] for subagent in payload.meta["_subagents"]} == {
        "ouroboros_lateral_think"
    }

    parallel_entries = lateral_review_response_to_interview_orchestration_entries(
        payload,
        session_id="sess-review-tool-capability",
        runtime_supports_parallel_subagents=True,
        lateral_panel_metadata=panel,
    )
    sequential_entries = lateral_review_response_to_interview_orchestration_entries(
        payload,
        session_id="sess-review-tool-capability",
        runtime_supports_parallel_subagents=False,
        lateral_panel_metadata=panel,
    )

    assert [entry["persona_id"] for entry in parallel_entries] == [
        "researcher",
        "contrarian",
    ]
    assert {entry["mcp_tool"] for entry in parallel_entries} == {"ouroboros_lateral_think"}
    assert {entry["execution_mode"] for entry in parallel_entries} == {"parallel_subagent_panel"}
    assert all(entry["sequential_fallback_used"] is False for entry in parallel_entries)
    assert all(entry["requires_prose_parsing"] is False for entry in parallel_entries)

    assert [entry["persona_id"] for entry in sequential_entries] == [
        "researcher",
        "contrarian",
    ]
    assert {entry["execution_mode"] for entry in sequential_entries} == {
        "sequential_persona_payload_dispatch"
    }
    assert all(entry["sequential_fallback_used"] is True for entry in sequential_entries)
    assert all(entry["requires_prose_parsing"] is False for entry in sequential_entries)


def test_interview_metadata_includes_lateral_persona_panel_contract() -> None:
    owned_tools = tuple(handler.definition for handler in get_ouroboros_tools(include_auto=False))
    catalog = assemble_session_tool_catalog(attached_tools=owned_tools)

    graph = build_capability_graph(catalog)

    descriptors = {descriptor.name: descriptor for descriptor in graph.capabilities}
    interview = descriptors["ouroboros_interview"]
    assert interview.metadata is not None
    panel = interview.metadata.orchestration["lateral_panel"]
    assert panel["panel_id"] == "lateral_persona_panel.v1"
    assert panel["response_payload_refs"]["requires_prose_parsing"] is False
    assert panel["sequential_fallback"]["supported"] is True


def test_interview_metadata_includes_question_advisory_fanout_contract() -> None:
    owned_tools = tuple(handler.definition for handler in get_ouroboros_tools(include_auto=False))
    catalog = assemble_session_tool_catalog(attached_tools=owned_tools)

    graph = build_capability_graph(catalog)

    interview = graph.by_name()["ouroboros_interview"]
    assert interview.metadata is not None
    fanout = interview.metadata.orchestration["question_advisory_fanout"]
    assert fanout["contract_id"] == "interview_question_advisory_fanout.v1"
    assert fanout["dispatch_timing"] == "after_question_is_visible_to_user"
    assert fanout["parallel_preference"] == "parallel_when_runtime_supports_subagents"
    assert fanout["sequential_fallback"] == {
        "supported": True,
        "mode": "sequential_advisory_lane_dispatch",
        "trigger": "runtime_has_no_native_parallel_subagent_primitive",
    }
    assert fanout["synthesis_contract"] == {
        "output_shape": "answer_advisory",
        "max_options": 3,
        "include_recommended_draft": True,
        "preserve_user_agency": True,
        "forward_to_mcp_only_after_user_or_auto_confirm": True,
    }
    assert fanout["response_payload_refs"]["plugin"] == "parent_runtime.ouroboros_dispatch.children"
    assert fanout["response_payload_refs"]["requires_prose_parsing"] is False
    assert fanout["response_payload_refs"]["synthesis_owner"] == "parent_session"
    assert "Show the MCP interview question to the user first" in fanout["runtime_instruction"]

    lane_by_id = {lane["lane_id"]: lane for lane in fanout["lanes"]}
    assert set(lane_by_id) == {
        "code_context",
        "web_context",
        "data_context",
        "ambiguity_contrarian",
        "answer_simplifier",
        "architecture_implications",
    }
    assert lane_by_id["code_context"]["capability"] == "inspect_code"
    assert lane_by_id["web_context"]["capability"] == "web_research"
    assert lane_by_id["data_context"]["capability"] == "call_mcp"
    assert lane_by_id["ambiguity_contrarian"]["persona"] == "contrarian"
    assert lane_by_id["answer_simplifier"]["persona"] == "simplifier"
    assert lane_by_id["architecture_implications"]["persona"] == "architect"


def test_question_advisory_data_context_lane_ships_read_only_proposer_policy() -> None:
    """The data_context lane is a read-only proposer (Q00/ouroboros#1671).

    The machine-readable ``data_policy`` block is what hosts with permission
    systems enforce; the lane prompt is the fallback. The evidence policy caps
    what can flow into persisted interview state (aggregates only, no raw
    rows, PII scrub) — a ship blocker per the discussion, so pin it here.
    """
    metadata = ouroboros_tool_capability_metadata("ouroboros_interview")
    fanout = metadata["orchestration"]["question_advisory_fanout"]
    lane_by_id = {lane["lane_id"]: lane for lane in fanout["lanes"]}
    data_lane = lane_by_id["data_context"]

    assert data_lane["required"] is False
    policy = data_lane["data_policy"]
    assert policy["read_only"] is True
    assert policy["aggregate_only"] is True
    assert policy["relevance_gate"] == "decide_from_question_text_before_any_tool_call"
    assert policy["direct_execution_scope"] == "local_free_read_only_lookups_only"
    assert policy["metered_or_uncertain_sources"] == "return_proposed_queries_without_executing"
    assert policy["error_shaped_tool_output"] == "return_no_evidence_finding"
    assert "drop" in policy["forbidden_operation_patterns"]
    assert "save" in policy["forbidden_operation_patterns"]

    evidence = policy["evidence_policy"]
    assert evidence["aggregates_only"] is True
    assert evidence["raw_rows_allowed"] is False
    assert evidence["pii_scrub_required"] is True
    assert evidence["max_evidence_items"] == 5
    assert evidence["max_evidence_chars"] == 2000


def test_question_advisory_v1_contract_carries_lane_compatibility_rules() -> None:
    """v1-in-place lane additions require explicit host compatibility rules."""
    metadata = ouroboros_tool_capability_metadata("ouroboros_interview")
    fanout = metadata["orchestration"]["question_advisory_fanout"]

    assert fanout["contract_id"] == "interview_question_advisory_fanout.v1"
    assert fanout["lane_compatibility_rules"] == {
        "unknown_lane_id": "dispatch_with_generic_prompt_or_skip",
        "unsupported_capability": "dispatch_and_return_noop_finding",
        "noop_finding_is_completion_signal": True,
    }


def test_question_advisory_request_model_validates_parent_runtime_payload() -> None:
    metadata = ouroboros_tool_capability_metadata("ouroboros_interview")
    fanout = metadata["orchestration"]["question_advisory_fanout"]
    schema = fanout["request_model_schema"]
    Draft202012Validator.check_schema(schema)

    question = "Which users need this first?"
    request = {
        "contract_id": fanout["contract_id"],
        "session_id": "sess-123",
        "question_identity": stable_code_investigation_question_identity(question),
        "question": question,
        "phase": "answer",
        "ambiguity_score": 0.35,
        "milestone": "progress",
        "user_question_first": True,
        "advisory_goal": "help_human_answer_interview_question",
        "parallel_preference": fanout["parallel_preference"],
        "sequential_fallback": dict(fanout["sequential_fallback"]),
        "allowed_capabilities": ["inspect_code", "web_research", "run_lateral_review"],
        "lanes": list(fanout["lanes"]),
        "synthesis_contract": dict(fanout["synthesis_contract"]),
        "code_investigation_request": {
            **_code_investigation_base_request(question),
            "investigation_targets": [{"target_type": "workspace", "scope": "active"}],
        },
        "mcp_tool_capability": metadata,
    }

    Draft202012Validator(schema).validate(request)


def test_code_investigation_request_model_validates_supported_target_forms() -> None:
    owned_tools = tuple(handler.definition for handler in get_ouroboros_tools(include_auto=False))
    catalog = assemble_session_tool_catalog(attached_tools=owned_tools)
    graph = build_capability_graph(catalog)
    descriptors = {descriptor.name: descriptor for descriptor in graph.capabilities}
    interview = descriptors["ouroboros_interview"]
    assert interview.metadata is not None
    schema = interview.metadata.orchestration["code_investigation"]["request_model_schema"]
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    base_request = _code_investigation_base_request("What framework does this project use?")
    supported_targets = (
        {"target_type": "workspace", "scope": "active"},
        {"target_type": "relative_path", "path": "pyproject.toml"},
        {"target_type": "glob", "pattern": "**/package.json"},
        {"target_type": "symbol", "name": "AuthService", "path_hint": "src"},
    )

    for target in supported_targets:
        validator.validate({**base_request, "investigation_targets": [target]})


def test_code_investigation_request_model_requires_explicit_targets() -> None:
    owned_tools = tuple(handler.definition for handler in get_ouroboros_tools(include_auto=False))
    catalog = assemble_session_tool_catalog(attached_tools=owned_tools)
    graph = build_capability_graph(catalog)
    descriptors = {descriptor.name: descriptor for descriptor in graph.capabilities}
    interview = descriptors["ouroboros_interview"]
    assert interview.metadata is not None
    schema = interview.metadata.orchestration["code_investigation"]["request_model_schema"]
    validator = Draft202012Validator(schema)

    errors = sorted(
        validator.iter_errors(
            {
                **_code_investigation_base_request("What framework does this project use?"),
                "working_directory": "/repo",
                "answer_prefixes": ["[from-code]"],
            }
        ),
        key=lambda error: error.message,
    )

    assert any(
        "'investigation_targets' is a required property" in error.message for error in errors
    )


def test_code_investigation_request_model_rejects_server_local_repository_fields() -> None:
    owned_tools = tuple(handler.definition for handler in get_ouroboros_tools(include_auto=False))
    catalog = assemble_session_tool_catalog(attached_tools=owned_tools)
    graph = build_capability_graph(catalog)
    descriptors = {descriptor.name: descriptor for descriptor in graph.capabilities}
    interview = descriptors["ouroboros_interview"]
    assert interview.metadata is not None
    schema = interview.metadata.orchestration["code_investigation"]["request_model_schema"]
    validator = Draft202012Validator(schema)

    base_request = {
        **_code_investigation_base_request("What framework does this project use?"),
        "investigation_targets": [{"target_type": "workspace", "scope": "active"}],
        "answer_prefixes": ["[from-code]"],
    }

    for field_name in (
        "working_directory",
        "cwd",
        "repository_path",
        "server_repository_path",
    ):
        errors = list(validator.iter_errors({**base_request, field_name: "/server/local/repo"}))
        assert any(
            "Additional properties are not allowed" in error.message and field_name in error.message
            for error in errors
        ), field_name


def test_capability_graph_serialization_round_trips() -> None:
    graph = build_capability_graph(assemble_session_tool_catalog(["Read", "Edit"]))

    restored = normalize_serialized_capability_graph(serialize_capability_graph(graph))

    assert restored is not None
    assert [descriptor.name for descriptor in restored.capabilities] == ["Read", "Edit"]
    assert restored.capabilities[0].semantics.mutation_class is CapabilityMutationClass.READ_ONLY


def test_owned_mcp_tool_metadata_serialization_round_trips_mutation_fields() -> None:
    definitions = {handler.definition.name: handler.definition for handler in get_ouroboros_tools()}
    graph = build_capability_graph(
        assemble_session_tool_catalog(attached_tools=(definitions["ouroboros_interview"],))
    )
    serialized = serialize_capability_graph(graph)

    assert serialized[0]["metadata"]["mutation_targets"] == list(
        _EXPECTED_OUROBOROS_TOOL_MUTATION_TARGETS["ouroboros_interview"]
    )
    assert serialized[0]["metadata"]["state_mutations"] == [
        {
            **mutation,
            "context_keys": list(mutation["context_keys"]),
        }
        for mutation in _EXPECTED_OUROBOROS_TOOL_STATE_MUTATIONS["ouroboros_interview"]
    ]
    assert serialized[0]["metadata"]["side_effects"] == [
        "subagent_dispatch",
        "session_state_write",
    ]

    restored = normalize_serialized_capability_graph(serialized)

    assert restored is not None
    descriptor = restored.by_name()["ouroboros_interview"]
    assert descriptor.metadata is not None
    assert descriptor.metadata.fallback_used is False
    assert (
        descriptor.metadata.mutation_targets
        == (_EXPECTED_OUROBOROS_TOOL_MUTATION_TARGETS["ouroboros_interview"])
    )
    assert (
        descriptor.metadata.state_mutations
        == (_EXPECTED_OUROBOROS_TOOL_STATE_MUTATIONS["ouroboros_interview"])
    )
    assert descriptor.metadata.side_effects == (
        "subagent_dispatch",
        "session_state_write",
    )
    validate_capability_tool_metadata(
        descriptor.metadata,
        tool_name="ouroboros_interview",
        owned_tool=True,
    )


def test_build_capability_graph_records_inherited_capabilities_without_entries() -> None:
    catalog = replace(
        assemble_session_tool_catalog(["Read"]),
        inherited_capabilities=frozenset({"mcp__chrome-devtools__click"}),
    )

    graph = build_capability_graph(catalog)

    descriptors = {descriptor.name: descriptor for descriptor in graph.capabilities}
    inherited = descriptors["mcp__chrome-devtools__click"]
    assert [descriptor.name for descriptor in graph.capabilities] == [
        "Read",
        "mcp__chrome-devtools__click",
    ]
    assert inherited.stable_id == "inherited:mcp__chrome-devtools__click"
    assert inherited.source_kind == "inherited_capability"
    assert inherited.semantics.origin is CapabilityOrigin.ATTACHED_MCP
    assert inherited.semantics.scope is CapabilityScope.ATTACHMENT


def test_full_override_replaces_every_classified_dimension(tmp_path, monkeypatch) -> None:
    """A fully-specified override sets every dimension explicitly."""
    override_path = tmp_path / "tool_capabilities.yaml"
    override_path.write_text(
        """
tools:
  browser:chrome_navigate:
    mutation_class: read_only
    parallel_safety: safe
    interruptibility: none
    approval_class: default
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("OUROBOROS_TOOL_CAPABILITIES", str(override_path))
    catalog = assemble_session_tool_catalog(
        attached_tools=(
            MCPToolDefinition(
                name="chrome_navigate",
                description="Navigate the browser",
                server_name="browser",
            ),
        ),
    )

    graph = build_capability_graph(catalog)

    descriptor = graph.capabilities[0]
    assert descriptor.semantics.mutation_class is CapabilityMutationClass.READ_ONLY
    assert descriptor.semantics.parallel_safety is CapabilityParallelSafety.SAFE
    assert descriptor.semantics.approval_class is CapabilityApprovalClass.DEFAULT
    assert descriptor.metadata is not None
    assert descriptor.metadata.fallback_used is True
    assert descriptor.metadata.mutation_class == CapabilityMutationClass.READ_ONLY.value
    assert descriptor.metadata.mutation_targets == ()
    assert descriptor.metadata.side_effects == ()

    serialized = serialize_capability_graph(graph)[0]
    assert serialized["semantics"]["mutation_class"] == "read_only"
    assert serialized["metadata"]["mutation_class"] == "read_only"
    assert serialized["metadata"]["mutation_targets"] == []
    assert serialized["metadata"]["side_effects"] == []


def test_partial_override_merges_onto_inferred_semantics(tmp_path, monkeypatch) -> None:
    """Partial overrides should retain inferred fields the user did not set.

    The user's YAML only declares ``approval_class``.  Every other
    dimension must keep the value inferred from the tool's
    name/description fingerprint — the override must not silently
    reset unspecified fields back to conservative defaults.
    """
    from ouroboros.orchestrator.capabilities import CapabilityInterruptibility

    override_path = tmp_path / "tool_capabilities.yaml"
    override_path.write_text(
        """
tools:
  docs:search_docs:
    approval_class: elevated
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("OUROBOROS_TOOL_CAPABILITIES", str(override_path))
    catalog = assemble_session_tool_catalog(
        attached_tools=(
            MCPToolDefinition(
                name="search_docs",
                description="Search indexed project docs",
                server_name="docs",
            ),
        ),
    )

    graph = build_capability_graph(catalog)

    descriptor = graph.capabilities[0]
    # The only dimension the YAML declared:
    assert descriptor.semantics.approval_class is CapabilityApprovalClass.ELEVATED
    # Everything else is inherited from the read-leaning fingerprint
    # ("search" keyword → READ_ONLY / SAFE / NONE).  These assertions
    # would fail if the override layer were wholesale-replacing
    # semantics instead of merging per-field.
    assert descriptor.semantics.mutation_class is CapabilityMutationClass.READ_ONLY
    assert descriptor.semantics.parallel_safety is CapabilityParallelSafety.SAFE
    assert descriptor.semantics.interruptibility is CapabilityInterruptibility.NONE
    assert descriptor.semantics.origin is CapabilityOrigin.ATTACHED_MCP
    assert descriptor.semantics.scope is CapabilityScope.ATTACHMENT
    assert descriptor.metadata is not None
    assert descriptor.metadata.mutation_class == CapabilityMutationClass.READ_ONLY.value
    assert descriptor.metadata.mutation_targets == ()
    assert descriptor.metadata.side_effects == ()


def test_attached_override_to_mutating_semantics_updates_fallback_metadata(
    tmp_path,
    monkeypatch,
) -> None:
    override_path = tmp_path / "tool_capabilities.yaml"
    override_path.write_text(
        """
tools:
  docs:search_docs:
    mutation_class: destructive
    parallel_safety: isolated_session_required
    interruptibility: hard
    approval_class: bypass_forbidden
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("OUROBOROS_TOOL_CAPABILITIES", str(override_path))
    catalog = assemble_session_tool_catalog(
        attached_tools=(
            MCPToolDefinition(
                name="search_docs",
                description="Search indexed project docs",
                server_name="docs",
            ),
        ),
    )

    graph = build_capability_graph(catalog)

    descriptor = graph.capabilities[0]
    assert descriptor.semantics.mutation_class is CapabilityMutationClass.DESTRUCTIVE
    assert descriptor.metadata is not None
    assert descriptor.metadata.fallback_used is True
    assert descriptor.metadata.mutation_class == CapabilityMutationClass.DESTRUCTIVE.value
    assert descriptor.metadata.mutation_targets == ("external",)
    assert descriptor.metadata.side_effects == ("unknown_external_side_effect",)


def test_invalid_override_enum_value_is_logged_and_skipped(tmp_path, monkeypatch) -> None:
    """Malformed overrides should log a warning instead of being silenced."""
    import structlog

    override_path = tmp_path / "tool_capabilities.yaml"
    override_path.write_text(
        """
tools:
  browser:chrome_navigate:
    mutation_class: totally-not-a-real-enum
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("OUROBOROS_TOOL_CAPABILITIES", str(override_path))
    catalog = assemble_session_tool_catalog(
        attached_tools=(
            MCPToolDefinition(
                name="chrome_navigate",
                description="Navigate the browser",
                server_name="browser",
            ),
        ),
    )

    with structlog.testing.capture_logs() as captured_events:
        graph = build_capability_graph(catalog)

    # Graph still produced with inferred semantics (fail-open classification
    # rather than silent discard of the tool itself).
    assert len(graph.capabilities) == 1
    # A structlog warning was emitted so user typos do not go unnoticed.
    assert any(
        event.get("event") == "capability_override.invalid_enum" for event in captured_events
    )


def test_malformed_yaml_does_not_break_capability_graph(tmp_path, monkeypatch) -> None:
    """YAML parse failures in the user override file must not propagate.

    Regression guard for the design note that a single bad user config
    line would otherwise take down unrelated orchestration paths
    (interview, evaluation, execution) because they all build a
    capability graph on the default path.
    """
    import structlog

    override_path = tmp_path / "tool_capabilities.yaml"
    # Invalid YAML: unmatched indentation + stray tabs.
    override_path.write_text(
        "tools:\n  browser:\n\tchrome_navigate:\n  mutation_class: [unclosed\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OUROBOROS_TOOL_CAPABILITIES", str(override_path))
    catalog = assemble_session_tool_catalog(
        attached_tools=(
            MCPToolDefinition(
                name="chrome_navigate",
                description="Navigate the browser",
                server_name="browser",
            ),
        ),
    )

    with structlog.testing.capture_logs() as captured_events:
        # Must not raise — override layer is optional enhancement.
        graph = build_capability_graph(catalog)

    assert len(graph.capabilities) == 1
    # The failure must still be visible to operators.
    assert any(
        event.get("event") == "capability_override.yaml_parse_failed" for event in captured_events
    )


def test_unreadable_override_path_does_not_break_capability_graph(tmp_path, monkeypatch) -> None:
    """A directory (or other non-regular path) at the override location
    must be handled gracefully rather than raising ``IsADirectoryError``.
    """
    # Point the override env var at a directory instead of a file.
    monkeypatch.setenv("OUROBOROS_TOOL_CAPABILITIES", str(tmp_path))
    catalog = assemble_session_tool_catalog(["Read"])

    # Must not raise.
    graph = build_capability_graph(catalog)

    assert [descriptor.name for descriptor in graph.capabilities] == ["Read"]


def test_fifo_override_path_does_not_hang_capability_graph(tmp_path, monkeypatch) -> None:
    """A FIFO at the override location must not block ``read_text()``.

    Regression guard for the reviewer's blocking finding on PR #353:
    ``read_text()`` on a FIFO (or other non-regular file that has no
    EOF — socket, character device) blocks indefinitely.  Because the
    override loader sits on the default capability-graph construction
    path, such a path would wedge interview/evaluation/execution
    startup.  The loader must stat-check and refuse non-regular files
    before attempting to read them.
    """
    import os
    import sys

    if sys.platform == "win32":
        import pytest

        pytest.skip("os.mkfifo is not available on Windows")

    fifo_path = tmp_path / "tool_capabilities.yaml"
    os.mkfifo(fifo_path)
    monkeypatch.setenv("OUROBOROS_TOOL_CAPABILITIES", str(fifo_path))
    catalog = assemble_session_tool_catalog(["Read"])

    # If the guard is missing, this call hangs forever waiting on the FIFO
    # write end.  With the guard in place it returns immediately with the
    # inferred builtin semantics and no user overrides applied.
    graph = build_capability_graph(catalog)

    assert [descriptor.name for descriptor in graph.capabilities] == ["Read"]
