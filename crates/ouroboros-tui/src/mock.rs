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
    state.status = ExecutionStatus::Running;
    state.current_phase = Phase::Define;
    state.iteration = 2;
    state.elapsed = "02:34".to_string();

    state.drift = DriftMetrics {
        goal: 0.08,
        constraint: 0.12,
        ontology: 0.05,
        combined: 0.09,
        history: vec![0.0, 0.02, 0.05, 0.08, 0.12, 0.09, 0.07, 0.08, 0.10, 0.09],
    };
    state.cost = CostMetrics {
        total_tokens: 12_400,
        total_cost_usd: 0.42,
        history: vec![0.02, 0.05, 0.08, 0.14, 0.22, 0.28, 0.35, 0.42],
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

    state.raw_events.push(RawEvent {
        event_type: "orchestrator.session.started".into(),
        aggregate_id: "ses_x9y8z7".into(),
        timestamp: "00:00:01".into(),
        data_preview: r#"{"execution_id": "exec_a1b2c3d4", "seed_goal": "Build data pipeline"}"#
            .into(),
    });
    state.raw_events.push(RawEvent {
        event_type: "execution.phase.completed".into(),
        aggregate_id: "exec_a1b2c3d4".into(),
        timestamp: "00:01:12".into(),
        data_preview: r#"{"phase": "discover", "iteration": 1}"#.into(),
    });
    state.raw_events.push(RawEvent {
        event_type: "workflow.progress.updated".into(),
        aggregate_id: "exec_a1b2c3d4".into(),
        timestamp: "00:01:45".into(),
        data_preview: r#"{"completed_count": 1, "total_count": 5, "current_phase": "Define"}"#
            .into(),
    });

    state.lineages = vec![
        Lineage {
            id: "lin_abc123".into(),
            seed_goal: "Build a robust data processing pipeline with validation".into(),
            generations: vec![
                LineageGeneration {
                    number: 1,
                    status: ACStatus::Completed,
                    score: 0.65,
                    ac_pass_count: 3,
                    ac_total: 5,
                    summary: "Initial implementation with basic validation".into(),
                },
                LineageGeneration {
                    number: 2,
                    status: ACStatus::Completed,
                    score: 0.82,
                    ac_pass_count: 4,
                    ac_total: 5,
                    summary: "Added edge case handling and error recovery".into(),
                },
                LineageGeneration {
                    number: 3,
                    status: ACStatus::Executing,
                    score: 0.0,
                    ac_pass_count: 0,
                    ac_total: 5,
                    summary: "Optimizing performance and adding tests".into(),
                },
            ],
            current_gen: 3,
            converged: false,
        },
        Lineage {
            id: "lin_def456".into(),
            seed_goal: "Create REST API with authentication middleware".into(),
            generations: vec![LineageGeneration {
                number: 1,
                status: ACStatus::Completed,
                score: 0.90,
                ac_pass_count: 4,
                ac_total: 4,
                summary: "Full implementation with JWT auth".into(),
            }],
            current_gen: 1,
            converged: true,
        },
    ];
    state.lineage_list = slt::ListState::new(
        state
            .lineages
            .iter()
            .map(|l| format!("{} — {}", l.id, truncate(&l.seed_goal, 40)))
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
        state.drift.history.push(drift_val);
        if state.drift.history.len() > 30 {
            state.drift.history.remove(0);
        }
    }

    if tick % 90 == 0 {
        state.cost.total_tokens += 200;
        state.cost.total_cost_usd += 0.01;
        state.cost.history.push(state.cost.total_cost_usd);
        if state.cost.history.len() > 20 {
            state.cost.history.remove(0);
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
