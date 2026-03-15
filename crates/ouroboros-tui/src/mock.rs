use crate::state::*;

const TOOL_NAMES: &[(&str, &str)] = &[
    ("Read", "src/validators.rs"),
    ("Grep", "validate_schema"),
    ("Edit", "src/main.rs:42"),
    ("Bash", "cargo test"),
    ("Read", "config.toml"),
    ("Grep", "error_handler"),
    ("Write", "src/output.rs"),
    ("Bash", "cargo clippy"),
    ("Read", "schema.json"),
    ("Edit", "src/lib.rs:18"),
];

const LOG_MESSAGES: &[(&str, &str)] = &[
    ("orchestrator", "Starting execution phase: Discover"),
    ("decomposer", "Decomposed AC into 3 sub-tasks"),
    ("executor", "SubAgent started for ac_0"),
    ("executor", "Tool call: Read src/validators.rs"),
    ("executor", "Tool call completed in 0.2s"),
    ("drift", "Drift measurement: combined=0.08"),
    ("executor", "AC ac_0 completed successfully"),
    ("orchestrator", "Phase transition: Discover -> Define"),
    ("cost", "Token usage: 4,200 tokens ($0.12)"),
    ("executor", "SubAgent started for ac_1"),
    ("decomposer", "AC ac_1 marked as atomic"),
    ("executor", "Tool call: Grep validate_schema"),
    ("executor", "SubAgent completed for ac_1"),
    ("orchestrator", "Phase transition: Define -> Design"),
    ("drift", "Drift measurement: combined=0.15"),
    ("executor", "Starting parallel batch: [ac_2, ac_3]"),
    ("cost", "Token usage: 8,100 tokens ($0.24)"),
    ("executor", "AC ac_2 completed successfully"),
    ("executor", "Tool call: Edit src/output.rs"),
    ("orchestrator", "Phase transition: Design -> Deliver"),
];

pub fn init_mock_state(state: &mut AppState) {
    state.execution_id = "exec_a1b2c3d4".to_string();
    state.session_id = "ses_x9y8z7".to_string();
    state.seed_goal = "Build a robust data processing pipeline with validation and tests".to_string();
    state.status = ExecutionStatus::Running;
    state.current_phase = Phase::Define;
    state.iteration = 2;
    state.elapsed = "02:34".to_string();

    state.drift = DriftMetrics {
        goal: 0.08,
        constraint: 0.12,
        ontology: 0.05,
        combined: 0.09,
        history: vec![0.0, 0.02, 0.05, 0.08, 0.12, 0.09, 0.07, 0.08, 0.10, 0.09].into(),
    };
    state.cost = CostMetrics {
        total_tokens: 12_400,
        total_cost_usd: 0.42,
        history: vec![0.02, 0.05, 0.08, 0.14, 0.22, 0.28, 0.35, 0.42].into(),
    };

    state.ac_root = vec![
        ACNode {
            id: "ac_0".into(),
            content: "Validate all input fields against the JSON schema definition".into(),
            status: ACStatus::Completed,
            depth: 1,
            is_atomic: false,
            children: vec![
                ACNode {
                    id: "sub_ac_0_0".into(),
                    content: "Parse JSON schema from config".into(),
                    status: ACStatus::Completed,
                    depth: 2,
                    is_atomic: true,
                    children: vec![],
                },
                ACNode {
                    id: "sub_ac_0_1".into(),
                    content: "Validate required fields presence".into(),
                    status: ACStatus::Completed,
                    depth: 2,
                    is_atomic: true,
                    children: vec![],
                },
                ACNode {
                    id: "sub_ac_0_2".into(),
                    content: "Type-check all field values".into(),
                    status: ACStatus::Completed,
                    depth: 2,
                    is_atomic: true,
                    children: vec![],
                },
            ],
        },
        ACNode {
            id: "ac_1".into(),
            content: "Process validated data through transformation pipeline".into(),
            status: ACStatus::Executing,
            depth: 1,
            is_atomic: false,
            children: vec![
                ACNode {
                    id: "sub_ac_1_0".into(),
                    content: "Normalize string encodings to UTF-8".into(),
                    status: ACStatus::Completed,
                    depth: 2,
                    is_atomic: true,
                    children: vec![],
                },
                ACNode {
                    id: "sub_ac_1_1".into(),
                    content: "Apply business rule transformations".into(),
                    status: ACStatus::Executing,
                    depth: 2,
                    is_atomic: true,
                    children: vec![],
                },
            ],
        },
        ACNode {
            id: "ac_2".into(),
            content: "Generate structured output in target format".into(),
            status: ACStatus::Pending,
            depth: 1,
            is_atomic: true,
            children: vec![],
        },
        ACNode {
            id: "ac_3".into(),
            content: "Write comprehensive test suite for edge cases".into(),
            status: ACStatus::Pending,
            depth: 1,
            is_atomic: false,
            children: vec![
                ACNode {
                    id: "sub_ac_3_0".into(),
                    content: "Unit tests for schema validation".into(),
                    status: ACStatus::Pending,
                    depth: 2,
                    is_atomic: true,
                    children: vec![],
                },
                ACNode {
                    id: "sub_ac_3_1".into(),
                    content: "Integration tests for pipeline".into(),
                    status: ACStatus::Pending,
                    depth: 2,
                    is_atomic: true,
                    children: vec![],
                },
            ],
        },
        ACNode {
            id: "ac_4".into(),
            content: "Performance benchmarks and optimization pass".into(),
            status: ACStatus::Pending,
            depth: 1,
            is_atomic: true,
            children: vec![],
        },
    ];

    state.active_tools.insert(
        "sub_ac_1_1".into(),
        ToolInfo {
            tool_name: "Edit".into(),
            tool_detail: "src/transform.rs:87".into(),
            call_index: 3,
        },
    );

    state.tool_history.insert(
        "ac_0".into(),
        vec![
            ToolHistoryEntry {
                tool_name: "Read".into(),
                tool_detail: "schema.json".into(),
                duration_secs: 0.2,
                success: true,
            },
            ToolHistoryEntry {
                tool_name: "Grep".into(),
                tool_detail: "required_fields".into(),
                duration_secs: 0.1,
                success: true,
            },
            ToolHistoryEntry {
                tool_name: "Edit".into(),
                tool_detail: "src/validators.rs:15".into(),
                duration_secs: 0.3,
                success: true,
            },
        ],
    );
    state.tool_history.insert(
        "sub_ac_1_1".into(),
        vec![
            ToolHistoryEntry {
                tool_name: "Read".into(),
                tool_detail: "src/pipeline.rs".into(),
                duration_secs: 0.15,
                success: true,
            },
            ToolHistoryEntry {
                tool_name: "Grep".into(),
                tool_detail: "transform_rule".into(),
                duration_secs: 0.08,
                success: true,
            },
        ],
    );

    state.thinking.insert(
        "sub_ac_1_1".into(),
        "Analyzing the transformation pipeline to apply business rules. \
         Need to handle nullable fields and default values correctly."
            .into(),
    );

    state.selected_node_id = Some("sub_ac_1_1".into());

    for (source, msg) in LOG_MESSAGES.iter().take(10) {
        let level = if msg.contains("error") || msg.contains("Error") {
            LogLevel::Error
        } else if msg.contains("Drift") || msg.contains("drift") {
            LogLevel::Warning
        } else if msg.contains("Token") || msg.contains("cost") {
            LogLevel::Debug
        } else {
            LogLevel::Info
        };
        state.add_log(level, source, msg);
    }

    // Populate raw_events with a realistic event stream
    let mock_events = [
        ("00:00:01", "orchestrator.session.started", "ses_x9y8z7",
         r#"{"execution_id": "exec_a1b2c3d4", "seed_goal": "Build data pipeline"}"#),
        ("00:00:03", "execution.started", "exec_a1b2c3d4",
         r#"{"seed_id": "seed_pipeline_v2", "phase": "discover"}"#),
        ("00:00:15", "execution.tool.called", "exec_a1b2c3d4",
         r#"{"tool": "Read", "detail": "src/pipeline/config.rs", "ac_id": "ac_0"}"#),
        ("00:00:22", "execution.tool.completed", "exec_a1b2c3d4",
         r#"{"tool": "Read", "success": true, "duration_secs": 0.8}"#),
        ("00:00:30", "observability.drift.measured", "exec_a1b2c3d4",
         r#"{"goal_drift": 0.02, "constraint_drift": 0.01, "ontology_drift": 0.0, "combined_drift": 0.01}"#),
        ("00:00:45", "observability.cost.updated", "exec_a1b2c3d4",
         r#"{"total_tokens": 2400, "total_cost_usd": 0.08}"#),
        ("00:01:12", "execution.phase.completed", "exec_a1b2c3d4",
         r#"{"phase": "discover", "iteration": 1}"#),
        ("00:01:15", "workflow.progress.updated", "exec_a1b2c3d4",
         r#"{"completed_count": 1, "total_count": 5, "current_phase": "Define"}"#),
        ("00:01:30", "execution.tool.called", "exec_a1b2c3d4",
         r#"{"tool": "Write", "detail": "src/pipeline/validator.rs", "ac_id": "sub_ac_0_0"}"#),
        ("00:01:48", "execution.tool.completed", "exec_a1b2c3d4",
         r#"{"tool": "Write", "success": true, "duration_secs": 2.1}"#),
        ("00:02:00", "observability.drift.measured", "exec_a1b2c3d4",
         r#"{"goal_drift": 0.05, "constraint_drift": 0.08, "ontology_drift": 0.03, "combined_drift": 0.05}"#),
        ("00:02:05", "observability.cost.updated", "exec_a1b2c3d4",
         r#"{"total_tokens": 6800, "total_cost_usd": 0.22}"#),
        ("00:02:15", "execution.tool.called", "exec_a1b2c3d4",
         r#"{"tool": "Bash", "detail": "cargo test --lib validator", "ac_id": "sub_ac_0_1"}"#),
        ("00:02:34", "execution.tool.completed", "exec_a1b2c3d4",
         r#"{"tool": "Bash", "success": true, "duration_secs": 4.2}"#),
        ("00:02:34", "observability.cost.updated", "exec_a1b2c3d4",
         r#"{"total_tokens": 12400, "total_cost_usd": 0.42}"#),
        ("00:02:34", "observability.drift.measured", "exec_a1b2c3d4",
         r#"{"goal_drift": 0.08, "constraint_drift": 0.12, "ontology_drift": 0.05, "combined_drift": 0.09}"#),
    ];
    for (ts, etype, agg, data) in &mock_events {
        state.raw_events.push(RawEvent {
            event_type: etype.to_string(),
            aggregate_id: agg.to_string(),
            timestamp: ts.to_string(),
            data_preview: data.to_string(),
        });
    }

    state.lineages = vec![
        Lineage {
            id: "lin_abc123".into(),
            seed_goal: "Build a robust data processing pipeline with validation".into(),
            generations: vec![
                LineageGeneration {
                    number: 1,
                    status: ACStatus::Completed,
                    phase: "completed".into(),
                    score: 0.65,
                    ac_pass_count: 3,
                    ac_total: 5,
                    summary: "Initial implementation with basic validation".into(),
                    seed_id: "seed_001".into(),
                    created_at: "2026-03-15T10:00:00Z".into(),
                    highest_stage: 2,
                    drift_score: 0.08,
                    final_approved: false,
                    failure_reason: "AC 3/5 failed: missing edge case handling".into(),
                    wonder_questions: Vec::new(),
                    ontology_name: "DataPipeline".into(),
                    ontology_fields: vec![
                        OntologyFieldEntry { name: "input_schema".into(), field_type: "JSONSchema".into(), description: "Schema for input validation".into(), required: true },
                        OntologyFieldEntry { name: "transform_rules".into(), field_type: "Vec<Rule>".into(), description: "Ordered transformation rules".into(), required: true },
                        OntologyFieldEntry { name: "output_format".into(), field_type: "String".into(), description: "Target output format".into(), required: true },
                        OntologyFieldEntry { name: "error_strategy".into(), field_type: "ErrorStrategy".into(), description: "How to handle validation errors".into(), required: false },
                    ],
                    ontology_delta: Vec::new(),
                    execution_output: "Compiled successfully. 3/5 tests passed. Failed: test_empty_input, test_malformed_json".into(),
                },
                LineageGeneration {
                    number: 2,
                    status: ACStatus::Completed,
                    phase: "completed".into(),
                    score: 0.82,
                    ac_pass_count: 4,
                    ac_total: 5,
                    summary: "Added edge case handling and error recovery".into(),
                    seed_id: "seed_002".into(),
                    created_at: "2026-03-15T10:15:00Z".into(),
                    highest_stage: 2,
                    drift_score: 0.12,
                    final_approved: false,
                    failure_reason: "AC 4/5: performance benchmark not met".into(),
                    wonder_questions: vec![
                        "Could we use streaming instead of batch processing?".into(),
                        "What if the schema evolves between pipeline runs?".into(),
                    ],
                    ontology_name: "DataPipeline".into(),
                    ontology_fields: vec![
                        OntologyFieldEntry { name: "input_schema".into(), field_type: "JSONSchema".into(), description: "Schema for input validation".into(), required: true },
                        OntologyFieldEntry { name: "transform_rules".into(), field_type: "Vec<Rule>".into(), description: "Ordered transformation rules".into(), required: true },
                        OntologyFieldEntry { name: "output_format".into(), field_type: "String".into(), description: "Target output format".into(), required: true },
                        OntologyFieldEntry { name: "error_strategy".into(), field_type: "ErrorStrategy".into(), description: "How to handle validation errors".into(), required: false },
                    ],
                    ontology_delta: vec![
                        "+Added: retry_policy (RetryConfig)".into(),
                        "~Modified: error_strategy (String->ErrorStrategy)".into(),
                    ],
                    execution_output: "Compiled successfully. 4/5 tests passed. Failed: test_large_payload_perf (timeout 3.2s > 2s limit)".into(),
                },
                LineageGeneration {
                    number: 3,
                    status: ACStatus::Executing,
                    phase: "executing".into(),
                    score: 0.0,
                    ac_pass_count: 0,
                    ac_total: 5,
                    summary: "Optimizing performance and adding tests".into(),
                    seed_id: "seed_003".into(),
                    created_at: "2026-03-15T10:30:00Z".into(),
                    highest_stage: 0,
                    drift_score: 0.0,
                    final_approved: false,
                    failure_reason: String::new(),
                    wonder_questions: Vec::new(),
                    ontology_name: "DataPipeline".into(),
                    ontology_fields: Vec::new(),
                    ontology_delta: Vec::new(),
                    execution_output: String::new(),
                },
            ],
            current_gen: 3,
            status: "active".into(),
            convergence_similarity: 0.0,
            convergence_reason: String::new(),
            created_at: "2026-03-15T10:00:00Z".into(),
        },
        Lineage {
            id: "lin_def456".into(),
            seed_goal: "Create REST API with authentication middleware".into(),
            generations: vec![LineageGeneration {
                number: 1,
                status: ACStatus::Completed,
                phase: "completed".into(),
                score: 0.90,
                ac_pass_count: 4,
                ac_total: 4,
                summary: "Full implementation with JWT auth".into(),
                seed_id: "seed_010".into(),
                created_at: "2026-03-15T08:00:00Z".into(),
                highest_stage: 3,
                drift_score: 0.03,
                final_approved: true,
                failure_reason: String::new(),
                wonder_questions: Vec::new(),
                ontology_name: "RestAPI".into(),
                ontology_fields: vec![
                    OntologyFieldEntry { name: "endpoints".into(), field_type: "Vec<Endpoint>".into(), description: "API endpoint definitions".into(), required: true },
                    OntologyFieldEntry { name: "auth_middleware".into(), field_type: "JWTConfig".into(), description: "JWT authentication config".into(), required: true },
                    OntologyFieldEntry { name: "rate_limiter".into(), field_type: "RateLimitConfig".into(), description: "Rate limiting configuration".into(), required: false },
                ],
                ontology_delta: Vec::new(),
                execution_output: "All 4 acceptance criteria passed. JWT auth middleware verified with RS256.".into(),
            }],
            current_gen: 1,
            status: "converged".into(),
            convergence_similarity: 0.95,
            convergence_reason: "All acceptance criteria passed with score >= 0.9".into(),
            created_at: "2026-03-15T08:00:00Z".into(),
        },
    ];
    state.lineage_list = slt::ListState::new(
        state
            .lineages
            .iter()
            .map(|l| {
                let status_icon = match l.status.as_str() {
                    "converged" => "●",
                    "exhausted" => "✖",
                    "aborted" => "⊘",
                    _ => "◐",
                };
                format!(
                    "{} {} — {} (gen {})",
                    status_icon,
                    l.id,
                    &l.seed_goal,
                    l.current_gen,
                )
            })
            .collect(),
    );

    state.rebuild_tree_state();
}

/// Advance mock simulation by one tick (called each frame when auto_simulate is on).
pub fn tick_mock(state: &mut AppState) {
    state.mock_tick += 1;
    let tick = state.mock_tick;

    if tick % 60 == 0 {
        let drift_val = 0.08 + 0.04 * ((tick as f64 / 30.0).sin());
        state.drift.combined = drift_val;
        state.drift.history.push_back(drift_val);
        if state.drift.history.len() > 30 {
            state.drift.history.pop_front();
        }
    }

    if tick % 90 == 0 {
        state.cost.total_tokens += 200;
        state.cost.total_cost_usd += 0.01;
        state.cost.history.push_back(state.cost.total_cost_usd);
        if state.cost.history.len() > 20 {
            state.cost.history.pop_front();
        }
    }

    if tick % 120 == 0 {
        let idx = ((tick / 120) as usize) % LOG_MESSAGES.len();
        let (source, msg) = LOG_MESSAGES[idx];
        let level = if msg.contains("error") {
            LogLevel::Error
        } else if msg.contains("Drift") || msg.contains("drift") {
            LogLevel::Warning
        } else if msg.contains("Token") || msg.contains("cost") {
            LogLevel::Debug
        } else {
            LogLevel::Info
        };
        state.add_log(level, source, msg);
    }

    if tick % 180 == 0 {
        let tool_idx = ((tick / 180) as usize) % TOOL_NAMES.len();
        let (name, detail) = TOOL_NAMES[tool_idx];
        state.active_tools.insert(
            "sub_ac_1_1".into(),
            ToolInfo {
                tool_name: name.to_string(),
                tool_detail: detail.to_string(),
                call_index: (tick / 180) as u32,
            },
        );
        state.add_raw_event(
            "execution.tool.started",
            "sub_ac_1_1",
            &format!(r#"{{"tool_name": "{name}", "detail": "{detail}"}}"#),
        );
    }

    if tick % 600 == 0 {
        let phase_idx = ((tick / 600) as usize) % Phase::ALL.len();
        state.current_phase = Phase::ALL[phase_idx];
        state.iteration = (tick / 600) as u32 + 1;
    }

    let minutes = tick / 3600;
    let secs = (tick / 60) % 60;
    state.elapsed = format!("{minutes:02}:{secs:02}");
}
