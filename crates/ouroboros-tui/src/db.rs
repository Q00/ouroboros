use std::path::Path;

use rusqlite::Connection;
use serde_json::Value;

use crate::state::*;

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
        let query = match &self.last_seen_id {
            Some(last_id) => {
                let ts_query = format!(
                    "SELECT id, aggregate_type, aggregate_id, event_type, payload, timestamp \
                     FROM events WHERE timestamp > (SELECT timestamp FROM events WHERE id = '{}') \
                     ORDER BY timestamp ASC",
                    last_id.replace('\'', "''")
                );
                ts_query
            }
            None => {
                return self.read_all_events();
            }
        };

        let mut stmt = match self.conn.prepare(&query) {
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

    pub fn event_count(&self) -> usize {
        self.conn
            .query_row("SELECT count(*) FROM events", [], |row| row.get(0))
            .unwrap_or(0)
    }

    pub fn distinct_sessions(&self) -> Vec<(String, String, String, usize)> {
        let mut stmt = match self.conn.prepare(
            "SELECT aggregate_type, aggregate_id, \
                    MIN(timestamp) as first_ts, COUNT(*) as cnt \
             FROM events \
             GROUP BY aggregate_type, aggregate_id \
             ORDER BY first_ts DESC",
        ) {
            Ok(s) => s,
            Err(_) => return Vec::new(),
        };

        stmt.query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, usize>(3)?,
            ))
        })
        .ok()
        .map(|iter| iter.filter_map(|r| r.ok()).collect())
        .unwrap_or_default()
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
            _ => {}
        }
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
