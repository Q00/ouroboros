"""Immutable builtin tool names for the production server composition.

Importing this module never constructs handlers, runtime adapters or stores.
The composition-root contract test must stay in sync when tools are added.
"""

BUILTIN_TOOL_NAMES: frozenset[str] = frozenset(
    (
        "ouroboros_ac_dashboard",
        "ouroboros_ac_tree_hud",
        "ouroboros_auto",
        "ouroboros_brownfield",
        "ouroboros_cancel_execution",
        "ouroboros_cancel_job",
        "ouroboros_checklist_verify",
        "ouroboros_evaluate",
        "ouroboros_evolve_rewind",
        "ouroboros_evolve_step",
        "ouroboros_execute_seed",
        "ouroboros_fetch_artifact",
        "ouroboros_generate_seed",
        "ouroboros_interview",
        "ouroboros_job_result",
        "ouroboros_job_status",
        "ouroboros_job_wait",
        "ouroboros_lateral_think",
        "ouroboros_lineage_status",
        "ouroboros_measure_drift",
        "ouroboros_pm_interview",
        "ouroboros_project_status",
        "ouroboros_qa",
        "ouroboros_query_events",
        "ouroboros_query_projection",
        "ouroboros_ralph",
        "ouroboros_record_conductor_decision",
        "ouroboros_session_signal",
        "ouroboros_session_signal_targets",
        "ouroboros_session_status",
        "ouroboros_start_auto",
        "ouroboros_start_evaluate",
        "ouroboros_start_evolve_step",
        "ouroboros_start_execute_seed",
        "ouroboros_start_ralph",
        "ouroboros_submit_fanout_results",
    )
)
