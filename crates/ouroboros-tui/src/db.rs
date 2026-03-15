use std::collections::HashMap;
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

    /// Load all events for a session: orchestrator events + execution events + AC sub-events.
    /// First finds the execution_id from the session's orchestrator.session.started event,
    /// then loads all events whose aggregate_id matches the session OR the execution prefix.
    pub fn read_events_for_session(&mut self, session_aggregate_id: &str) -> Vec<EventRow> {
        // 1. Find execution_id from session.started event
        let execution_id: Option<String> = self
            .conn
            .query_row(
                "SELECT payload FROM events \
                 WHERE aggregate_id = ?1 AND event_type = 'orchestrator.session.started' \
                 LIMIT 1",
                [session_aggregate_id],
                |row| row.get::<_, String>(0),
            )
            .ok()
            .and_then(|payload_str| {
                serde_json::from_str::<Value>(&payload_str)
                    .ok()
                    .and_then(|v| v.get("execution_id").and_then(|e| e.as_str()).map(String::from))
            });

        // 2. Build query: session events + execution events + AC sub-events
        let query = match &execution_id {
            Some(exec_id) => format!(
                "SELECT id, aggregate_type, aggregate_id, event_type, payload, timestamp \
                 FROM events \
                 WHERE aggregate_id = '{sid}' OR aggregate_id LIKE '{eid}%' \
                 ORDER BY timestamp ASC",
                sid = session_aggregate_id.replace('\'', "''"),
                eid = exec_id.replace('\'', "''"),
            ),
            None => format!(
                "SELECT id, aggregate_type, aggregate_id, event_type, payload, timestamp \
                 FROM events WHERE aggregate_id = '{}' ORDER BY timestamp ASC",
                session_aggregate_id.replace('\'', "''"),
            ),
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

        // Don't update last_seen_id here — session loads are scoped reads
        // that shouldn't advance the global polling cursor.
        rows
    }

    /// Load ALL lineage events from the database (lineage.* event types).
    /// Lineage events have their own aggregate_ids (not tied to session/execution),
    /// so they must be loaded separately from session events.
    pub fn read_all_lineage_events(&self) -> Vec<EventRow> {
        let mut stmt = match self.conn.prepare(
            "SELECT id, aggregate_type, aggregate_id, event_type, payload, timestamp \
             FROM events WHERE event_type LIKE 'lineage.%' \
             OR (event_type = 'observability.drift.measured' \
                 AND aggregate_id IN (SELECT DISTINCT aggregate_id FROM events WHERE event_type LIKE 'lineage.%')) \
             ORDER BY timestamp ASC",
        ) {
            Ok(s) => s,
            Err(_) => return Vec::new(),
        };

        stmt.query_map([], |row| {
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
        .unwrap_or_default()
    }

    /// Returns sessions matching Python TUI behavior:
    /// only aggregate_ids that have an `orchestrator.session.started` event,
    /// with goal and status extracted from events.
    pub fn distinct_sessions(&self) -> Vec<SessionRow> {
        // Single query with correlated subqueries to avoid N+1
        let mut stmt = match self.conn.prepare(
            "SELECT e.aggregate_id, e.payload, e.timestamp, \
               (SELECT COUNT(*) FROM events e2 WHERE e2.aggregate_id = e.aggregate_id) as event_count, \
               (SELECT event_type FROM events e3 WHERE e3.aggregate_id = e.aggregate_id ORDER BY timestamp DESC LIMIT 1) as last_event \
             FROM events e \
             WHERE e.event_type = 'orchestrator.session.started' \
             ORDER BY e.timestamp DESC",
        ) {
            Ok(s) => s,
            Err(_) => return Vec::new(),
        };

        let sessions: Vec<SessionRow> = stmt
            .query_map([], |row| {
                let payload_str: String = row.get(1)?;
                let payload: Value =
                    serde_json::from_str(&payload_str).unwrap_or(Value::Null);
                let goal = payload
                    .get("seed_goal")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let event_count: usize = row.get(3)?;
                let last_event: String = row.get::<_, String>(4).unwrap_or_default();
                let status = if last_event.contains("completed") {
                    "done"
                } else if last_event.contains("failed") {
                    "failed"
                } else if last_event.contains("cancelled") {
                    "cancelled"
                } else if last_event.contains("paused") {
                    "paused"
                } else {
                    "running"
                }
                .to_string();

                Ok(SessionRow {
                    aggregate_id: row.get(0)?,
                    goal,
                    status,
                    event_count,
                    timestamp: row.get(2)?,
                })
            })
            .ok()
            .map(|iter| iter.filter_map(|r| r.ok()).collect())
            .unwrap_or_default();

        sessions
    }
}

pub fn populate_state_from_events(state: &mut AppState, events: &[EventRow]) {
    let mut lineage_idx: HashMap<String, usize> = state
        .lineages
        .iter()
        .enumerate()
        .map(|(i, l)| (l.id.clone(), i))
        .collect();
    let mut lineage_changed = false;

    for ev in events {
        let sp = short_payload(&ev.payload);

        state.add_raw_event(
            &ev.event_type,
            &ev.aggregate_id,
            &sp,
        );

        state.execution_events.push(crate::state::ExecutionEvent {
            timestamp: ev.timestamp.clone(),
            event_type: ev.event_type.clone(),
            detail: sp.clone(),
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
            &format!("{}: {}", ev.event_type, &sp),
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
                state.seed_goal = ev
                    .payload
                    .get("seed_goal")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                state.status = ExecutionStatus::Running;
                // Store start timestamp for elapsed calculation
                if let Some(ts) = ev.payload.get("start_time").and_then(|v| v.as_str()) {
                    state._start_ts = ts.to_string();
                }
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
                        "design" | "develop" => Phase::Design,
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
                state.drift.history.push_back(state.drift.combined);
                if state.drift.history.len() > 30 {
                    state.drift.history.pop_front();
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
                state.cost.history.push_back(state.cost.total_cost_usd);
                if state.cost.history.len() > 20 {
                    state.cost.history.pop_front();
                }
            }
            "workflow.progress.updated" => {
                if let Some(phase_str) = ev.payload.get("current_phase").and_then(|v| v.as_str()) {
                    state.current_phase = match phase_str.to_lowercase().as_str() {
                        "discover" => Phase::Discover,
                        "define" => Phase::Define,
                        "design" | "develop" => Phase::Design,
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
                // Extract cost/token data from workflow progress
                if let Some(tokens) = ev.payload.get("estimated_tokens").and_then(|v| v.as_u64()) {
                    if tokens > 0 {
                        state.cost.total_tokens = tokens;
                    }
                }
                if let Some(cost) = ev.payload.get("estimated_cost_usd").and_then(|v| v.as_f64()) {
                    if cost > 0.0 {
                        state.cost.total_cost_usd = cost;
                        state.cost.history.push_back(cost);
                        if state.cost.history.len() > 20 {
                            state.cost.history.pop_front();
                        }
                    }
                }
                // Extract messages/tool call counts
                if let Some(msgs) = ev.payload.get("messages_count").and_then(|v| v.as_u64()) {
                    state.cost.total_tokens = state.cost.total_tokens.max(msgs * 800);
                }
                if let Some(tools) = ev.payload.get("tool_calls_count").and_then(|v| v.as_u64()) {
                    if tools > 0 && state.cost.total_tokens == 0 {
                        state.cost.total_tokens = tools * 2000;
                    }
                }
                // Extract elapsed display
                if let Some(elapsed) = ev.payload.get("elapsed_display").and_then(|v| v.as_str()) {
                    if !elapsed.is_empty() {
                        state.elapsed = elapsed.to_string();
                    }
                }
                // Parse AC tree from acceptance_criteria array
                // Preserve existing children (sub-ACs) when rebuilding
                if let Some(acs) = ev.payload.get("acceptance_criteria").and_then(|v| v.as_array())
                {
                    // Index previous children by ac_id for preservation
                    let mut prev_children: HashMap<String, Vec<ACNode>> = state
                        .ac_root
                        .drain(..)
                        .map(|node| (node.id.clone(), node.children))
                        .collect();

                    for ac in acs {
                        let ac_id = ac
                            .get("ac_id")
                            .and_then(|v| v.as_str())
                            .unwrap_or("")
                            .to_string();
                        let content = ac
                            .get("content")
                            .and_then(|v| v.as_str())
                            .unwrap_or("")
                            .to_string();
                        let status_str = ac
                            .get("status")
                            .and_then(|v| v.as_str())
                            .unwrap_or("pending");
                        let status = match status_str {
                            "completed" | "passed" => ACStatus::Completed,
                            "executing" | "running" => ACStatus::Executing,
                            "failed" => ACStatus::Failed,
                            _ => ACStatus::Pending,
                        };
                        let children = prev_children.remove(&ac_id).unwrap_or_default();
                        let is_atomic = children.is_empty();
                        state.ac_root.push(ACNode {
                            id: ac_id,
                            content,
                            status,
                            depth: 1,
                            is_atomic,
                            children,
                        });
                    }
                }
            }
            "execution.subtask.updated" => {
                let ac_index = ev
                    .payload
                    .get("ac_index")
                    .and_then(|v| v.as_u64())
                    .unwrap_or(0) as usize;
                let sub_id = ev
                    .payload
                    .get("sub_task_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let content = ev
                    .payload
                    .get("content")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let status_str = ev
                    .payload
                    .get("status")
                    .and_then(|v| v.as_str())
                    .unwrap_or("pending");
                let status = match status_str {
                    "completed" | "passed" => ACStatus::Completed,
                    "executing" | "running" => ACStatus::Executing,
                    "failed" => ACStatus::Failed,
                    _ => ACStatus::Pending,
                };
                // Add as child of the parent AC node
                if let Some(parent) = state.ac_root.get_mut(ac_index) {
                    // Update existing or add new
                    if let Some(child) = parent.children.iter_mut().find(|c| c.id == sub_id) {
                        child.status = status;
                        child.content = content;
                    } else {
                        parent.children.push(ACNode {
                            id: sub_id,
                            content,
                            status,
                            depth: 2,
                            is_atomic: true,
                            children: vec![],
                        });
                    }
                    parent.is_atomic = false;
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
                let idx = state.lineages.len();
                state.lineages.push(Lineage {
                    id: ev.aggregate_id.clone(),
                    seed_goal: goal,
                    generations: Vec::new(),
                    current_gen: 0,
                    status: "active".to_string(),
                    convergence_similarity: 0.0,
                    convergence_reason: String::new(),
                    created_at: ev.timestamp.clone(),
                });
                lineage_idx.insert(ev.aggregate_id.clone(), idx);
                lineage_changed = true;
            }
            "lineage.generation.started" => {
                lineage_changed = true;
                if let Some(lin) = lineage_idx.get(&ev.aggregate_id).and_then(|&i| state.lineages.get_mut(i)) {
                    let gen_num = ev
                        .payload
                        .get("generation_number")
                        .and_then(|v| v.as_u64())
                        .unwrap_or(1) as u32;
                    lin.current_gen = gen_num;
                    let phase = ev
                        .payload
                        .get("phase")
                        .and_then(|v| v.as_str())
                        .unwrap_or("executing")
                        .to_string();
                    let seed_id = ev
                        .payload
                        .get("seed_id")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .to_string();
                    lin.generations.push(LineageGeneration {
                        number: gen_num,
                        status: ACStatus::Executing,
                        phase,
                        score: 0.0,
                        ac_pass_count: 0,
                        ac_total: 0,
                        summary: "Executing...".to_string(),
                        seed_id,
                        created_at: ev.timestamp.clone(),
                        highest_stage: 0,
                        drift_score: 0.0,
                        final_approved: false,
                        failure_reason: String::new(),
                        wonder_questions: Vec::new(),
                        ontology_name: String::new(),
                        ontology_fields: Vec::new(),
                        ontology_delta: Vec::new(),
                        execution_output: String::new(),
                    });
                }
            }
            "lineage.generation.completed" => {
                lineage_changed = true;
                if let Some(lin) = lineage_idx.get(&ev.aggregate_id).and_then(|&i| state.lineages.get_mut(i)) {
                    let gen_num = ev
                        .payload
                        .get("generation_number")
                        .and_then(|v| v.as_u64())
                        .unwrap_or(1) as u32;
                    if let Some(gen) = lin.generations.iter_mut().find(|g| g.number == gen_num) {
                        gen.status = ACStatus::Completed;

                        // Extract seed_id
                        if let Some(sid) = ev.payload.get("seed_id").and_then(|v| v.as_str()) {
                            gen.seed_id = sid.to_string();
                        }

                        // Extract evaluation_summary fields
                        if let Some(eval) = ev.payload.get("evaluation_summary") {
                            gen.score = eval
                                .get("score")
                                .and_then(|v| v.as_f64())
                                .unwrap_or(0.0) as f32;
                            gen.final_approved = eval
                                .get("final_approved")
                                .and_then(|v| v.as_bool())
                                .unwrap_or(false);
                            gen.highest_stage = eval
                                .get("highest_stage_passed")
                                .and_then(|v| v.as_u64())
                                .unwrap_or(0) as u8;
                            gen.drift_score = eval
                                .get("drift_score")
                                .and_then(|v| v.as_f64())
                                .unwrap_or(0.0) as f32;
                            gen.failure_reason = eval
                                .get("failure_reason")
                                .and_then(|v| v.as_str())
                                .unwrap_or("")
                                .to_string();

                            // Count AC results
                            if let Some(ac_results) = eval.get("ac_results").and_then(|v| v.as_array()) {
                                gen.ac_total = ac_results.len() as u32;
                                gen.ac_pass_count = ac_results
                                    .iter()
                                    .filter(|r| {
                                        r.get("passed")
                                            .and_then(|v| v.as_bool())
                                            .unwrap_or(false)
                                    })
                                    .count() as u32;
                            }

                            // Build summary from score + approved status
                            gen.summary = if gen.final_approved {
                                format!("Approved ({:.0}%)", gen.score * 100.0)
                            } else if !gen.failure_reason.is_empty() {
                                gen.failure_reason.clone()
                            } else {
                                format!("Score: {:.0}%", gen.score * 100.0)
                            };
                        }

                        // Extract wonder_questions
                        if let Some(wqs) = ev.payload.get("wonder_questions").and_then(|v| v.as_array()) {
                            gen.wonder_questions = wqs
                                .iter()
                                .filter_map(|v| v.as_str().map(String::from))
                                .collect();
                        }

                        // Extract ontology_snapshot
                        if let Some(onto) = ev.payload.get("ontology_snapshot") {
                            gen.ontology_name = onto
                                .get("name")
                                .and_then(|v| v.as_str())
                                .unwrap_or("")
                                .to_string();
                            if let Some(fields) = onto.get("fields").and_then(|v| v.as_array()) {
                                gen.ontology_fields = fields
                                    .iter()
                                    .map(|f| OntologyFieldEntry {
                                        name: f.get("name").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                                        field_type: f.get("type").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                                        description: f.get("description").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                                        required: f.get("required").and_then(|v| v.as_bool()).unwrap_or(false),
                                    })
                                    .collect();
                            }
                        }

                        // Extract execution_output
                        if let Some(output) = ev.payload.get("execution_output").and_then(|v| v.as_str()) {
                            gen.execution_output = output.to_string();
                        }
                    }
                }
            }
            "lineage.generation.phase_changed" => {
                lineage_changed = true;
                if let Some(lin) = lineage_idx.get(&ev.aggregate_id).and_then(|&i| state.lineages.get_mut(i)) {
                    let gen_num = ev
                        .payload
                        .get("generation_number")
                        .and_then(|v| v.as_u64())
                        .unwrap_or(1) as u32;
                    let new_phase = ev
                        .payload
                        .get("phase")
                        .and_then(|v| v.as_str())
                        .unwrap_or("unknown")
                        .to_string();
                    if let Some(gen) = lin.generations.iter_mut().find(|g| g.number == gen_num) {
                        gen.phase = new_phase;
                    }
                }
            }
            "lineage.generation.failed" => {
                lineage_changed = true;
                if let Some(lin) = lineage_idx.get(&ev.aggregate_id).and_then(|&i| state.lineages.get_mut(i)) {
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
                lineage_changed = true;
                if let Some(lin) = lineage_idx.get(&ev.aggregate_id).and_then(|&i| state.lineages.get_mut(i)) {
                    lin.status = "converged".to_string();
                    lin.convergence_reason = ev
                        .payload
                        .get("reason")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .to_string();
                    lin.convergence_similarity = ev
                        .payload
                        .get("similarity")
                        .and_then(|v| v.as_f64())
                        .unwrap_or(0.0) as f32;
                }
            }
            "lineage.stagnated" => {
                lineage_changed = true;
                if let Some(lin) = lineage_idx.get(&ev.aggregate_id).and_then(|&i| state.lineages.get_mut(i)) {
                    lin.status = "stagnated".to_string();
                }
            }
            "lineage.exhausted" => {
                lineage_changed = true;
                if let Some(lin) = lineage_idx.get(&ev.aggregate_id).and_then(|&i| state.lineages.get_mut(i)) {
                    lin.status = "exhausted".to_string();
                }
            }
            "lineage.rewound" => {
                // Log only, no complex rewind tree needed for now
            }
            "lineage.ontology.evolved" => {
                if let Some(lin) = lineage_idx.get(&ev.aggregate_id).and_then(|&i| state.lineages.get_mut(i)) {
                    if let Some(gen) = lin.generations.last_mut() {
                        // Extract delta fields
                        if let Some(delta) = ev.payload.get("delta") {
                            if let Some(added) = delta.get("added_fields").and_then(|v| v.as_array()) {
                                for field in added {
                                    let name = field.get("name").and_then(|v| v.as_str()).unwrap_or("?");
                                    let ftype = field.get("type").and_then(|v| v.as_str()).unwrap_or("?");
                                    gen.ontology_delta.push(format!("+Added: {} ({})", name, ftype));
                                }
                            }
                            if let Some(removed) = delta.get("removed_fields").and_then(|v| v.as_array()) {
                                for field in removed {
                                    let name = field.get("name").and_then(|v| v.as_str())
                                        .or_else(|| field.as_str())
                                        .unwrap_or("?");
                                    gen.ontology_delta.push(format!("-Removed: {}", name));
                                }
                            }
                            if let Some(modified) = delta.get("modified_fields").and_then(|v| v.as_array()) {
                                for field in modified {
                                    let name = field.get("name").and_then(|v| v.as_str()).unwrap_or("?");
                                    let old = field.get("old_type").and_then(|v| v.as_str()).unwrap_or("?");
                                    let new = field.get("new_type").and_then(|v| v.as_str()).unwrap_or("?");
                                    gen.ontology_delta.push(format!("~Modified: {} ({}->{})", name, old, new));
                                }
                            }
                            if let Some(sim) = delta.get("similarity").and_then(|v| v.as_f64()) {
                                gen.drift_score = sim as f32;
                            }
                        }
                    }
                }
            }
            "orchestrator.tool.called" => {
                state._tool_call_count += 1;
                let tool_name = ev
                    .payload
                    .get("tool_name")
                    .and_then(|v| v.as_str())
                    .unwrap_or("unknown")
                    .to_string();
                state
                    .tool_history
                    .entry("orchestrator".to_string())
                    .or_default()
                    .push(ToolHistoryEntry {
                        tool_name,
                        tool_detail: String::new(),
                        duration_secs: 0.0,
                        success: true,
                    });
            }
            "orchestrator.progress.updated" => {
                state._msg_count += 1;
                if let Some(preview) = ev.payload.get("content_preview").and_then(|v| v.as_str()) {
                    state.add_log(
                        LogLevel::Info,
                        "orchestrator",
                        preview,
                    );
                }
                // Track last event timestamp for elapsed calculation
                if let Some(ts) = ev.payload.get("timestamp").and_then(|v| v.as_str()) {
                    state._last_ts = ts.to_string();
                }
            }
            "execution.agent.thinking" => {
                // Each thinking event ≈ 1 API turn, estimate tokens
                state._msg_count += 1;
            }
            "execution.coordinator.started" | "execution.coordinator.completed" |
            "execution.coordinator.thinking" | "execution.coordinator.tool.started" => {
                state._msg_count += 1;
            }
            "execution.decomposition.level_started" | "execution.decomposition.level_completed" => {
                if let Some(level) = ev.payload.get("level").and_then(|v| v.as_u64())
                    .or_else(|| ev.payload.get("level_number").and_then(|v| v.as_u64()))
                {
                    if ev.event_type.contains("started") {
                        state.iteration = state.iteration.max(level as u32 + 1);
                    }
                    state.add_log(
                        LogLevel::Info,
                        "decomposition",
                        &format!("Level {} {}", level,
                            if ev.event_type.contains("started") { "started" } else { "completed" }),
                    );
                }
            }
            _ => {}
        }
    }

    // Cap execution_events after processing all events (batch trim, not per-event)
    if state.execution_events.len() > 500 {
        state.execution_events.drain(..state.execution_events.len() - 500);
    }

    // Derive cost/token estimates from event counts if no explicit cost data
    if state.cost.total_tokens == 0 && state._msg_count > 0 {
        // ~800 tokens per message turn (conservative estimate)
        state.cost.total_tokens = state._msg_count * 800;
    }
    if state.cost.total_cost_usd == 0.0 && state.cost.total_tokens > 0 {
        // Sonnet ~$3/M input + $15/M output, rough avg ~$6/M
        state.cost.total_cost_usd = state.cost.total_tokens as f64 * 6.0 / 1_000_000.0;
        state.cost.history.push_back(state.cost.total_cost_usd);
    }

    // Derive elapsed from start/last timestamps
    if state.elapsed == "00:00" && !state._start_ts.is_empty() && !state._last_ts.is_empty() {
        if let Some(elapsed) = compute_elapsed(&state._start_ts, &state._last_ts) {
            state.elapsed = elapsed;
        }
    }

    // Rebuild lineage list state — preserve selection
    if lineage_changed && !state.lineages.is_empty() {
        let prev_lineage_sel = state.lineage_list.selected;
        state.lineage_list = slt::ListState::new(
            state
                .lineages
                .iter()
                .map(|l| {
                    format!(
                        "{} — {} (gen {}{})",
                        l.id,
                        &l.seed_goal,
                        l.current_gen,
                        if l.status == "converged" { " ✓" } else if l.status == "exhausted" { " ✖" } else { "" }
                    )
                })
                .collect::<Vec<_>>(),
        );
        state.lineage_list.selected = prev_lineage_sel.min(
            state.lineage_list.items.len().saturating_sub(1),
        );
    }

    state.rebuild_tree_state();
}

fn short_payload(payload: &Value) -> String {
    let s = payload.to_string();
    if s.chars().count() > 500 {
        let t: String = s.chars().take(497).collect();
        format!("{t}...")
    } else {
        s
    }
}

/// Parse ISO 8601 timestamps and compute elapsed as "MM:SS" or "HH:MM:SS".
fn compute_elapsed(start: &str, end: &str) -> Option<String> {
    // Extract the datetime portion (YYYY-MM-DDTHH:MM:SS)
    let parse_secs = |ts: &str| -> Option<u64> {
        // Find T separator
        let t_pos = ts.find('T')?;
        let time_part = &ts[t_pos + 1..];
        // Parse HH:MM:SS
        let parts: Vec<&str> = time_part.split(':').collect();
        if parts.len() < 3 {
            return None;
        }
        let h: u64 = parts[0].parse().ok()?;
        let m: u64 = parts[1].parse().ok()?;
        // Seconds may have fractional part or timezone
        let s_str = parts[2].split(|c: char| c == '.' || c == '+' || c == 'Z').next()?;
        let s: u64 = s_str.parse().ok()?;
        // Also need date for multi-day
        let date_part = &ts[..t_pos];
        let date_parts: Vec<&str> = date_part.split('-').collect();
        if date_parts.len() < 3 {
            return None;
        }
        let day: u64 = date_parts[2].parse().ok()?;
        Some(day * 86400 + h * 3600 + m * 60 + s)
    };

    let start_secs = parse_secs(start)?;
    let end_secs = parse_secs(end)?;
    let diff = end_secs.checked_sub(start_secs).unwrap_or(0);

    let hours = diff / 3600;
    let mins = (diff % 3600) / 60;
    let secs = diff % 60;

    if hours > 0 {
        Some(format!("{hours:02}:{mins:02}:{secs:02}"))
    } else {
        Some(format!("{mins:02}:{secs:02}"))
    }
}
