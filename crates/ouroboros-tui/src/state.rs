#![allow(dead_code)]

use std::collections::HashMap;

use slt::{
    CommandPaletteState, ListState, PaletteCommand, ScrollState, TableState, TabsState,
    TextInputState, TreeNode, TreeState,
};

// ─────────────────────────────────────────────────────────────────────────────
// Enums
// ─────────────────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Screen {
    Dashboard,
    Execution,
    Logs,
    Debug,
    SessionSelector,
    Lineage,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ExecutionStatus {
    Idle,
    Running,
    Paused,
    Completed,
    Failed,
    Cancelled,
}

impl ExecutionStatus {
    pub fn label(self) -> &'static str {
        match self {
            Self::Idle => "IDLE",
            Self::Running => "RUNNING",
            Self::Paused => "PAUSED",
            Self::Completed => "COMPLETED",
            Self::Failed => "FAILED",
            Self::Cancelled => "CANCELLED",
        }
    }

    pub fn icon(self) -> &'static str {
        match self {
            Self::Idle => "○",
            Self::Running => "◐",
            Self::Paused => "⏸",
            Self::Completed => "●",
            Self::Failed => "✖",
            Self::Cancelled => "⊘",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Phase {
    Discover,
    Define,
    Design,
    Deliver,
}

impl Phase {
    pub const ALL: [Phase; 4] = [
        Phase::Discover,
        Phase::Define,
        Phase::Design,
        Phase::Deliver,
    ];

    pub fn label(self) -> &'static str {
        match self {
            Self::Discover => "Discover",
            Self::Define => "Define",
            Self::Design => "Design",
            Self::Deliver => "Deliver",
        }
    }

    pub fn index(self) -> usize {
        match self {
            Self::Discover => 0,
            Self::Define => 1,
            Self::Design => 2,
            Self::Deliver => 3,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ACStatus {
    Pending,
    Executing,
    Completed,
    Failed,
    Blocked,
    Atomic,
    Decomposed,
}

impl ACStatus {
    pub fn icon(self) -> &'static str {
        match self {
            Self::Pending => "○",
            Self::Executing => "◐",
            Self::Completed => "●",
            Self::Failed => "✖",
            Self::Blocked => "⊘",
            Self::Atomic => "◆",
            Self::Decomposed => "◇",
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            Self::Pending => "PENDING",
            Self::Executing => "EXECUTING",
            Self::Completed => "COMPLETED",
            Self::Failed => "FAILED",
            Self::Blocked => "BLOCKED",
            Self::Atomic => "ATOMIC",
            Self::Decomposed => "DECOMPOSED",
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Data Structs
// ─────────────────────────────────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct ACNode {
    pub id: String,
    pub content: String,
    pub status: ACStatus,
    pub depth: u32,
    pub is_atomic: bool,
    pub children: Vec<ACNode>,
}

#[derive(Debug, Clone, Default)]
pub struct DriftMetrics {
    pub goal: f64,
    pub constraint: f64,
    pub ontology: f64,
    pub combined: f64,
    pub history: Vec<f64>,
}

#[derive(Debug, Clone, Default)]
pub struct CostMetrics {
    pub total_tokens: u64,
    pub total_cost_usd: f64,
    pub history: Vec<f64>,
}

#[derive(Debug, Clone)]
pub struct ToolInfo {
    pub tool_name: String,
    pub tool_detail: String,
    pub call_index: u32,
}

#[derive(Debug, Clone)]
pub struct ToolHistoryEntry {
    pub tool_name: String,
    pub tool_detail: String,
    pub duration_secs: f64,
    pub success: bool,
}

#[derive(Debug, Clone)]
pub struct LogEntry {
    pub timestamp: String,
    pub level: LogLevel,
    pub source: String,
    pub message: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LogLevel {
    Debug,
    Info,
    Warning,
    Error,
}

impl LogLevel {
    pub fn label(self) -> &'static str {
        match self {
            Self::Debug => "DEBUG",
            Self::Info => "INFO",
            Self::Warning => "WARN",
            Self::Error => "ERROR",
        }
    }

    pub fn icon(self) -> &'static str {
        match self {
            Self::Debug => "🔍",
            Self::Info => "ℹ",
            Self::Warning => "⚠",
            Self::Error => "✖",
        }
    }
}

#[derive(Debug, Clone)]
pub struct RawEvent {
    pub event_type: String,
    pub aggregate_id: String,
    pub timestamp: String,
    pub data_preview: String,
}

#[derive(Debug, Clone)]
pub struct LineageGeneration {
    pub number: u32,
    pub status: ACStatus,
    pub score: f32,
    pub ac_pass_count: u32,
    pub ac_total: u32,
    pub summary: String,
}

#[derive(Debug, Clone)]
pub struct Lineage {
    pub id: String,
    pub seed_goal: String,
    pub generations: Vec<LineageGeneration>,
    pub current_gen: u32,
    pub converged: bool,
}

// ─────────────────────────────────────────────────────────────────────────────
// Main Application State
// ─────────────────────────────────────────────────────────────────────────────

pub struct AppState {
    // Navigation
    pub screen: Screen,
    pub tabs: TabsState,
    pub command_palette: CommandPaletteState,

    // Execution state
    pub execution_id: String,
    pub session_id: String,
    pub status: ExecutionStatus,
    pub current_phase: Phase,
    pub iteration: u32,
    pub is_paused: bool,
    pub elapsed: String,

    // Metrics
    pub drift: DriftMetrics,
    pub cost: CostMetrics,

    // AC Tree
    pub ac_root: Vec<ACNode>,
    pub selected_node_id: Option<String>,
    pub ac_tree_state: TreeState,
    pub detail_scroll: ScrollState,

    // Tool tracking
    pub active_tools: HashMap<String, ToolInfo>,
    pub tool_history: HashMap<String, Vec<ToolHistoryEntry>>,
    pub thinking: HashMap<String, String>,

    // Logs
    pub logs: Vec<LogEntry>,
    pub log_scroll: ScrollState,
    pub log_filter: TextInputState,
    pub log_level_filter: Option<LogLevel>,

    // Debug
    pub raw_events: Vec<RawEvent>,
    pub debug_scroll: ScrollState,

    // Lineage
    pub lineages: Vec<Lineage>,
    pub lineage_list: ListState,
    pub selected_lineage_idx: Option<usize>,
    pub lineage_scroll: ScrollState,

    // Execution
    pub execution_events: Vec<ExecutionEvent>,
    pub execution_scroll: ScrollState,
    pub phase_outputs: HashMap<String, Vec<String>>,
    pub log_table: TableState,

    // Session selector
    pub sessions: Vec<SessionInfo>,
    pub session_list: ListState,

    // Mock simulation
    pub mock_tick: u64,
    pub auto_simulate: bool,
}

#[derive(Debug, Clone)]
pub struct ExecutionEvent {
    pub timestamp: String,
    pub event_type: String,
    pub detail: String,
    pub phase: Option<String>,
}

#[derive(Debug, Clone)]
pub struct SessionInfo {
    pub aggregate_type: String,
    pub aggregate_id: String,
    pub event_count: usize,
    pub first_ts: String,
}

impl AppState {
    pub fn new() -> Self {
        Self {
            screen: Screen::Dashboard,
            tabs: TabsState::new(vec!["Dashboard", "Execution", "Logs", "Debug", "Lineage"]),
            command_palette: CommandPaletteState::new(vec![
                PaletteCommand::new("Dashboard", "Phase + AC tree + detail (1)"),
                PaletteCommand::new("Execution", "Timeline + tool calls (2)"),
                PaletteCommand::new("Logs", "Filterable log viewer (3/l)"),
                PaletteCommand::new("Debug", "State dump + events (4/d)"),
                PaletteCommand::new("Lineage", "Evolutionary history (e)"),
                PaletteCommand::new("Sessions", "Switch session (s)"),
                PaletteCommand::new("Pause", "Pause execution (p)"),
                PaletteCommand::new("Resume", "Resume execution (r)"),
                PaletteCommand::new("Quit", "Exit (q)"),
            ]),

            execution_id: String::new(),
            session_id: String::new(),
            status: ExecutionStatus::Idle,
            current_phase: Phase::Discover,
            iteration: 0,
            is_paused: false,
            elapsed: String::from("00:00"),

            drift: DriftMetrics::default(),
            cost: CostMetrics::default(),

            ac_root: Vec::new(),
            selected_node_id: None,
            ac_tree_state: TreeState::new(Vec::<TreeNode>::new()),
            detail_scroll: ScrollState::new(),

            active_tools: HashMap::new(),
            tool_history: HashMap::new(),
            thinking: HashMap::new(),

            logs: Vec::new(),
            log_scroll: ScrollState::new(),
            log_filter: TextInputState::with_placeholder("Filter logs..."),
            log_level_filter: None,

            raw_events: Vec::new(),
            debug_scroll: ScrollState::new(),

            lineages: Vec::new(),
            lineage_list: ListState::new(Vec::<&str>::new()),
            selected_lineage_idx: None,
            lineage_scroll: ScrollState::new(),

            execution_events: Vec::new(),
            execution_scroll: ScrollState::new(),
            phase_outputs: HashMap::new(),
            log_table: TableState::new(
                vec!["Time", "Level", "Source", "Message"],
                Vec::<Vec<&str>>::new(),
            ),

            sessions: Vec::new(),
            session_list: ListState::new(Vec::<&str>::new()),

            mock_tick: 0,
            auto_simulate: true,
        }
    }

    /// Find an AC node by ID across the tree.
    pub fn find_node(&self, id: &str) -> Option<&ACNode> {
        fn search<'a>(nodes: &'a [ACNode], id: &str) -> Option<&'a ACNode> {
            for node in nodes {
                if node.id == id {
                    return Some(node);
                }
                if let Some(found) = search(&node.children, id) {
                    return Some(found);
                }
            }
            None
        }
        search(&self.ac_root, id)
    }

    /// Get the selected node (if any).
    pub fn selected_node(&self) -> Option<&ACNode> {
        self.selected_node_id
            .as_deref()
            .and_then(|id| self.find_node(id))
    }

    /// Count completed / total ACs (leaf nodes only).
    pub fn ac_progress(&self) -> (u32, u32) {
        fn count(nodes: &[ACNode]) -> (u32, u32) {
            let mut done = 0u32;
            let mut total = 0u32;
            for node in nodes {
                if node.children.is_empty() {
                    total += 1;
                    if node.status == ACStatus::Completed {
                        done += 1;
                    }
                } else {
                    let (d, t) = count(&node.children);
                    done += d;
                    total += t;
                }
            }
            (done, total)
        }
        count(&self.ac_root)
    }

    pub fn rebuild_tree_state(&mut self) {
        fn to_tree_nodes(
            nodes: &[ACNode],
            active_tools: &HashMap<String, ToolInfo>,
        ) -> Vec<TreeNode> {
            nodes
                .iter()
                .map(|n| {
                    let mut label = format!("{} {}", n.status.icon(), truncate(&n.content, 50));
                    if let Some(tool) = active_tools.get(&n.id) {
                        label = format!("{label}  << {}: {}", tool.tool_name, tool.tool_detail);
                    }
                    if n.children.is_empty() {
                        TreeNode::new(label)
                    } else {
                        TreeNode::new(label)
                            .expanded()
                            .children(to_tree_nodes(&n.children, active_tools))
                    }
                })
                .collect()
        }
        let prev_sel = self.ac_tree_state.selected;
        self.ac_tree_state = TreeState::new(to_tree_nodes(&self.ac_root, &self.active_tools));
        self.ac_tree_state.selected = prev_sel;
    }

    /// Collect flat list of all node IDs in tree order (for mapping tree selection to node ID).
    pub fn flat_node_ids(&self) -> Vec<String> {
        fn collect(nodes: &[ACNode], out: &mut Vec<String>) {
            for node in nodes {
                out.push(node.id.clone());
                collect(&node.children, out);
            }
        }
        let mut ids = Vec::new();
        collect(&self.ac_root, &mut ids);
        ids
    }

    /// Add a log entry.
    pub fn add_log(&mut self, level: LogLevel, source: &str, message: &str) {
        let now = chrono_lite_now();
        self.logs.push(LogEntry {
            timestamp: now.clone(),
            level,
            source: source.to_string(),
            message: message.to_string(),
        });
        if self.logs.len() > 200 {
            self.logs.drain(..self.logs.len() - 200);
        }
        self.log_table.rows.push(vec![
            now,
            level.label().to_string(),
            source.to_string(),
            message.to_string(),
        ]);
        if self.log_table.rows.len() > 200 {
            self.log_table.rows.drain(..self.log_table.rows.len() - 200);
        }
    }

    /// Add a raw event.
    pub fn add_raw_event(&mut self, event_type: &str, aggregate_id: &str, data_preview: &str) {
        let now = chrono_lite_now();
        self.raw_events.push(RawEvent {
            event_type: event_type.to_string(),
            aggregate_id: aggregate_id.to_string(),
            timestamp: now,
            data_preview: data_preview.to_string(),
        });
        if self.raw_events.len() > 100 {
            self.raw_events.drain(..self.raw_events.len() - 100);
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Utilities
// ─────────────────────────────────────────────────────────────────────────────

pub fn truncate(s: &str, max: usize) -> String {
    if s.len() <= max {
        s.to_string()
    } else {
        format!("{}...", &s[..max.saturating_sub(3)])
    }
}

/// Lightweight timestamp without pulling in chrono.
fn chrono_lite_now() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    let h = (secs / 3600) % 24;
    let m = (secs / 60) % 60;
    let s = secs % 60;
    format!("{h:02}:{m:02}:{s:02}")
}
