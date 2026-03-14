use std::path::Path;

use rusqlite::Connection;
use serde_json::Value;

use crate::state::*;

pub struct SessionRow {
    pub aggregate_id: String,
    pub goal: String,
    pub status: String,
    pub event_count: usize,
    pub timestamp: String,
}

pub struct EventRow {
    pub id: String,
    pub aggregate_type: String,
    pub aggregate_id: String,
    pub event_type: String,
    pub payload: Value,
    pub timestamp: String,
}

pub struct OuroborosDb {
    conn: Connection,
    last_seen_id: Option<String>,
}

impl OuroborosDb {
    pub fn open(path: &Path) -> Result<Self, String> {
        let conn = Connection::open(path).map_err(|e| format!("Failed to open DB: {e}"))?;
        Ok(Self {
            conn,
            last_seen_id: None,
        })
    }

    pub fn read_all_events(&mut self) -> Vec<EventRow> {
        let mut stmt = match self.conn.prepare(
            "SELECT id, aggregate_type, aggregate_id, event_type, payload, timestamp \
             FROM events ORDER BY timestamp ASC",
        ) {
            Ok(s) => s,
            Err(_) => return Vec::new(),
        };

        let rows: Vec<EventRow> = stmt
            .query_map([], |row| {
                let payload_str: String = row.get(4)?;
                let payload: Value = serde_json::from_str(&payload_str).unwrap_or(Value::Null);
                Ok(EventRow {
                    id: row.get(0)?,
                    aggregate_type: row.get(1)?,
                    aggregate_id: row.get(2)?,
                    event_type: row.get(3)?,
                    payload,
                    timestamp: row.get(5)?,
                })
            })
            .ok()
            .map(|iter| iter.filter_map(|r| r.ok()).collect())
            .unwrap_or_default();

        if let Some(last) = rows.last() {
            self.last_seen_id = Some(last.id.clone());
        }
        rows
    }

    pub fn read_new_events(&mut self) -> Vec<EventRow> {
        let last_id = match &self.last_seen_id {
            Some(id) => id.clone(),
            None => {
                return self.read_all_events();
            }
        };

        let mut stmt = match self.conn.prepare(
            "SELECT id, aggregate_type, aggregate_id, event_type, payload, timestamp \
             FROM events WHERE timestamp > (SELECT timestamp FROM events WHERE id = ?1) \
             ORDER BY timestamp ASC",
        ) {
            Ok(s) => s,
            Err(_) => return Vec::new(),
        };

        let rows: Vec<EventRow> = stmt
            .query_map([&last_id], |row| {
                let payload_str: String = row.get(4)?;
                let payload: Value = serde_json::from_str(&payload_str).unwrap_or(Value::Null);
                Ok(EventRow {
                    id: row.get(0)?,
                    aggregate_type: row.get(1)?,
                    aggregate_id: row.get(2)?,
                    event_type: row.get(3)?,
                    payload,
                    timestamp: row.get(5)?,
                })
            })
            .ok()
            .map(|iter| iter.filter_map(|r| r.ok()).collect())
            .unwrap_or_default();

        if let Some(last) = rows.last() {
            self.last_seen_id = Some(last.id.clone());
        }
        rows
    }

    pub fn event_count(&self) -> usize {
        self.conn
            .query_row("SELECT count(*) FROM events", [], |row| row.get(0))
            .unwrap_or(0)
    }

    pub fn read_events_for_session(&mut self, aggregate_id: &str) -> Vec<EventRow> {
        let mut stmt = match self.conn.prepare(
            "SELECT id, aggregate_type, aggregate_id, event_type, payload, timestamp \
             FROM events WHERE aggregate_id = ?1 ORDER BY timestamp ASC",
        ) {
            Ok(s) => s,
            Err(_) => return Vec::new(),
        };

        let rows: Vec<EventRow> = stmt
            .query_map([aggregate_id], |row| {
                let payload_str: String = row.get(4)?;
                let payload: Value = serde_json::from_str(&payload_str).unwrap_or(Value::Null);
                Ok(EventRow {
                    id: row.get(0)?,
                    aggregate_type: row.get(1)?,
                    aggregate_id: row.get(2)?,
                    event_type: row.get(3)?,
                    payload,
                    timestamp: row.get(5)?,
                })
            })
            .ok()
            .map(|iter| iter.filter_map(|r| r.ok()).collect())
            .unwrap_or_default();

        if let Some(last) = rows.last() {
            self.last_seen_id = Some(last.id.clone());
        }
        rows
    }

    /// Returns sessions matching Python TUI behavior:
    /// only aggregate_ids that have an `orchestrator.session.started` event,
    /// with goal and status extracted from events.
    pub fn distinct_sessions(&self) -> Vec<SessionRow> {
        // Find all session-started events
        let mut stmt = match self.conn.prepare(
            "SELECT e.aggregate_id, e.payload, e.timestamp \
             FROM events e \
             WHERE e.event_type = 'orchestrator.session.started' \
             ORDER BY e.timestamp DESC",
        ) {
            Ok(s) => s,
            Err(_) => return Vec::new(),
        };

        let started: Vec<(String, Value, String)> = stmt
            .query_map([], |row| {
                let payload_str: String = row.get(1)?;
                let payload: Value =
                    serde_json::from_str(&payload_str).unwrap_or(Value::Null);
                Ok((row.get(0)?, payload, row.get(2)?))
            })
            .ok()
            .map(|iter| iter.filter_map(|r| r.ok()).collect())
            .unwrap_or_default();

        let mut sessions = Vec::new();
        for (agg_id, payload, timestamp) in started {
            let goal = payload
                .get("seed_goal")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();

            // Count total events for this session
            let event_count: usize = self
                .conn
                .query_row(
                    "SELECT COUNT(*) FROM events WHERE aggregate_id = ?1",
                    [&agg_id],
                    |row| row.get(0),
                )
                .unwrap_or(0);

            // Determine status from latest event
            let status = self
                .conn
                .query_row(
                    "SELECT event_type FROM events \
                     WHERE aggregate_id = ?1 \
                     ORDER BY timestamp DESC LIMIT 1",
                    [&agg_id],
                    |row| row.get::<_, String>(0),
                )
                .map(|et| {
                    if et.contains("completed") {
                        "done"
                    } else if et.contains("failed") {
                        "failed"
                    } else if et.contains("cancelled") {
                        "cancelled"
                    } else if et.contains("paused") {
                        "paused"
                    } else {
                        "running"
                    }
                    .to_string()
                })
                .unwrap_or_else(|_| "unknown".to_string());

            sessions.push(SessionRow {
                aggregate_id: agg_id,
                goal,
                status,
                event_count,
                timestamp,
            });
        }
        sessions
    }
}

pub fn populate_state_from_events(state: &mut AppState, events: &[EventRow]) {
    for ev in events {
        state.add_raw_event(
            &ev.event_type,
            &ev.aggregate_id,
            &short_payload(&ev.payload),
        );

        state.execution_events.push(crate::state::ExecutionEvent {
            timestamp: ev.timestamp.clone(),
            event_type: ev.event_type.clone(),
            detail: short_payload(&ev.payload),
            phase: ev
                .payload
                .get("phase")
                .and_then(|v| v.as_str())
                .map(String::from),
        });

        let log_level = if ev.event_type.contains("fail") || ev.event_type.contains("error") {
            LogLevel::Error
        } else if ev.event_type.contains("drift") || ev.event_type.contains("warn") {
            LogLevel::Warning
        } else if ev.event_type.contains("debug") || ev.event_type.contains("cost") {
            LogLevel::Debug
        } else {
            LogLevel::Info
        };
        state.add_log(
            log_level,
            &ev.aggregate_type,
            &format!("{}: {}", ev.event_type, short_payload(&ev.payload)),
        );

        match ev.event_type.as_str() {
            "orchestrator.session.started" => {
                state.execution_id = ev
                    .payload
                    .get("execution_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or(&ev.aggregate_id)
                    .to_string();
                state.session_id = ev.aggregate_id.clone();
                state.status = ExecutionStatus::Running;
            }
            "orchestrator.session.completed" => {
                state.status = ExecutionStatus::Completed;
            }
            "orchestrator.session.failed" => {
                state.status = ExecutionStatus::Failed;
            }
            "orchestrator.session.paused" => {
                state.status = ExecutionStatus::Paused;
                state.is_paused = true;
            }
            "orchestrator.session.cancelled" => {
                state.status = ExecutionStatus::Cancelled;
            }
            "execution.phase.completed" => {
                if let Some(phase_str) = ev.payload.get("phase").and_then(|v| v.as_str()) {
                    state.current_phase = match phase_str.to_lowercase().as_str() {
                        "discover" => Phase::Discover,
                        "define" => Phase::Define,
                        "design" => Phase::Design,
                        "deliver" => Phase::Deliver,
                        _ => state.current_phase,
                    };
                }
                if let Some(iter) = ev.payload.get("iteration").and_then(|v| v.as_u64()) {
                    state.iteration = iter as u32;
                }
            }
            "observability.drift.measured" => {
                state.drift.goal = ev
                    .payload
                    .get("goal_drift")
                    .and_then(|v| v.as_f64())
                    .unwrap_or(state.drift.goal);
                state.drift.constraint = ev
                    .payload
                    .get("constraint_drift")
                    .and_then(|v| v.as_f64())
                    .unwrap_or(state.drift.constraint);
                state.drift.ontology = ev
                    .payload
                    .get("ontology_drift")
                    .and_then(|v| v.as_f64())
                    .unwrap_or(state.drift.ontology);
                state.drift.combined = ev
                    .payload
                    .get("combined_drift")
                    .and_then(|v| v.as_f64())
                    .unwrap_or(state.drift.combined);
                state.drift.history.push(state.drift.combined);
                if state.drift.history.len() > 30 {
                    state.drift.history.remove(0);
                }
            }
            "observability.cost.updated" => {
                state.cost.total_tokens = ev
                    .payload
                    .get("total_tokens")
                    .and_then(|v| v.as_u64())
                    .unwrap_or(state.cost.total_tokens);
                state.cost.total_cost_usd = ev
                    .payload
                    .get("total_cost_usd")
                    .and_then(|v| v.as_f64())
                    .unwrap_or(state.cost.total_cost_usd);
                state.cost.history.push(state.cost.total_cost_usd);
                if state.cost.history.len() > 20 {
                    state.cost.history.remove(0);
                }
            }
            "workflow.progress.updated" => {
                if let Some(phase_str) = ev.payload.get("current_phase").and_then(|v| v.as_str()) {
                    state.current_phase = match phase_str.to_lowercase().as_str() {
                        "discover" => Phase::Discover,
                        "define" => Phase::Define,
                        "design" => Phase::Design,
                        "deliver" => Phase::Deliver,
                        _ => state.current_phase,
                    };
                }
                if let Some(detail) = ev.payload.get("activity_detail").and_then(|v| v.as_str()) {
                    let phase_key = ev
                        .payload
                        .get("current_phase")
                        .and_then(|v| v.as_str())
                        .unwrap_or("discover")
                        .to_lowercase();
                    state
                        .phase_outputs
                        .entry(phase_key)
                        .or_default()
                        .push(detail.to_string());
                }
            }
            "execution.tool.started" => {
                let ac_id = ev
                    .payload
                    .get("ac_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let tool_name = ev
                    .payload
                    .get("tool_name")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let tool_detail = ev
                    .payload
                    .get("tool_detail")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                if !ac_id.is_empty() {
                    state.active_tools.insert(
                        ac_id,
                        ToolInfo {
                            tool_name,
                            tool_detail,
                            call_index: 0,
                        },
                    );
                }
            }
            "execution.tool.completed" => {
                let ac_id = ev
                    .payload
                    .get("ac_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let tool_name = ev
                    .payload
                    .get("tool_name")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let tool_detail = ev
                    .payload
                    .get("tool_detail")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let dur = ev
                    .payload
                    .get("duration_seconds")
                    .and_then(|v| v.as_f64())
                    .unwrap_or(0.0);
                let success = ev
                    .payload
                    .get("success")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(true);
                state.active_tools.remove(ac_id);
                state
                    .tool_history
                    .entry(ac_id.to_string())
                    .or_default()
                    .push(ToolHistoryEntry {
                        tool_name,
                        tool_detail,
                        duration_secs: dur,
                        success,
                    });
            }
            "interview.started" => {
                state.session_id = ev.aggregate_id.clone();
                state.status = ExecutionStatus::Running;
            }
            "interview.response.recorded" => {
                if let Some(round) = ev.payload.get("round_number").and_then(|v| v.as_u64()) {
                    state.iteration = round as u32;
                }
            }
            "lineage.created" => {
                let goal = ev
                    .payload
                    .get("goal")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .trim()
                    .to_string();
                state.lineages.push(Lineage {
                    id: ev.aggregate_id.clone(),
                    seed_goal: goal,
                    generations: Vec::new(),
                    current_gen: 0,
                    converged: false,
                });
            }
            "lineage.generation.started" => {
                if let Some(lin) = state.lineages.iter_mut().find(|l| l.id == ev.aggregate_id) {
                    let gen_num = ev
                        .payload
                        .get("generation_number")
                        .and_then(|v| v.as_u64())
                        .unwrap_or(1) as u32;
                    lin.current_gen = gen_num;
                    lin.generations.push(LineageGeneration {
                        number: gen_num,
                        status: ACStatus::Executing,
                        score: 0.0,
                        ac_pass_count: 0,
                        ac_total: 0,
                        summary: ev
                            .payload
                            .get("phase")
                            .and_then(|v| v.as_str())
                            .unwrap_or("executing")
                            .to_string(),
                    });
                }
            }
            "lineage.generation.completed" => {
                if let Some(lin) = state.lineages.iter_mut().find(|l| l.id == ev.aggregate_id) {
                    let gen_num = ev
                        .payload
                        .get("generation_number")
                        .and_then(|v| v.as_u64())
                        .unwrap_or(1) as u32;
                    if let Some(gen) = lin.generations.iter_mut().find(|g| g.number == gen_num) {
                        gen.status = ACStatus::Completed;
                        if let Some(eval) = ev.payload.get("evaluation_summary") {
                            gen.score = eval
                                .get("score")
                                .and_then(|v| v.as_f64())
                                .unwrap_or(0.0) as f32;
                            gen.summary = eval
                                .get("failure_reason")
                                .and_then(|v| v.as_str())
                                .or_else(|| {
                                    if eval
                                        .get("final_approved")
                                        .and_then(|v| v.as_bool())
                                        .unwrap_or(false)
                                    {
                                        Some("Approved")
                                    } else {
                                        None
                                    }
                                })
                                .unwrap_or("Completed")
                                .to_string();
                        }
                    }
                }
            }
            "lineage.generation.failed" => {
                if let Some(lin) = state.lineages.iter_mut().find(|l| l.id == ev.aggregate_id) {
                    let gen_num = ev
                        .payload
                        .get("generation_number")
                        .and_then(|v| v.as_u64())
                        .unwrap_or(1) as u32;
                    if let Some(gen) = lin.generations.iter_mut().find(|g| g.number == gen_num) {
                        gen.status = ACStatus::Failed;
                        gen.summary = ev
                            .payload
                            .get("error")
                            .and_then(|v| v.as_str())
                            .unwrap_or("Failed")
                            .to_string();
                    }
                }
            }
            "lineage.converged" => {
                if let Some(lin) = state.lineages.iter_mut().find(|l| l.id == ev.aggregate_id) {
                    lin.converged = true;
                }
            }
            "lineage.stagnated" | "lineage.rewound" | "lineage.ontology.evolved" => {
                // tracked via logs, no state update needed
            }
            _ => {}
        }
    }

    // Cap execution_events after processing all events (batch trim, not per-event)
    if state.execution_events.len() > 500 {
        state.execution_events.drain(..state.execution_events.len() - 500);
    }

    // Rebuild lineage list state
    if !state.lineages.is_empty() {
        state.lineage_list = slt::ListState::new(
            state
                .lineages
                .iter()
                .map(|l| {
                    format!(
                        "{} — {} (gen {}{})",
                        l.id,
                        crate::state::truncate(&l.seed_goal, 40),
                        l.current_gen,
                        if l.converged { " ✓" } else { "" }
                    )
                })
                .collect::<Vec<_>>(),
        );
    }

    state.rebuild_tree_state();
}

fn short_payload(payload: &Value) -> String {
    let s = payload.to_string();
    if s.chars().count() > 120 {
        let t: String = s.chars().take(117).collect();
        format!("{t}...")
    } else {
        s
    }
}
