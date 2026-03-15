use std::collections::{HashMap, HashSet, VecDeque};

use slt::{
    CommandPaletteState, ListState, PaletteCommand, ScrollState, TableState, TabsState,
    TextInputState,
};

// ─────────────────────────────────────────────────────────────────────────────
// Enums
// ─────────────────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Screen {
    Dashboard,
    Execution,
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
#[allow(dead_code)]
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
    pub history: VecDeque<f64>,
}

#[derive(Debug, Clone, Default)]
pub struct CostMetrics {
    pub total_tokens: u64,
    pub total_cost_usd: f64,
    pub history: VecDeque<f64>,
}

/// Pre-computed flat entry for the AC tree display.
#[derive(Debug, Clone)]
pub struct ACTreeEntry {
    pub prefix: String,
    pub toggle_icon: &'static str,
    pub status: ACStatus,
    pub content: String,
    pub node_id: String,
    pub has_children: bool,
    pub active_tool: Option<String>,
}

#[derive(Debug, Clone)]
#[allow(dead_code)]
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
#[allow(dead_code)]
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

    #[allow(dead_code)]
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
pub struct OntologyFieldEntry {
    pub name: String,
    pub field_type: String,
    pub description: String,
    pub required: bool,
}

#[derive(Debug, Clone)]
pub struct LineageGeneration {
    pub number: u32,
    pub status: ACStatus,
    pub phase: String,
    pub score: f32,
    pub ac_pass_count: u32,
    pub ac_total: u32,
    pub summary: String,
    pub seed_id: String,
    pub created_at: String,
    pub highest_stage: u8,
    pub drift_score: f32,
    pub final_approved: bool,
    pub failure_reason: String,
    pub wonder_questions: Vec<String>,
    pub ontology_name: String,
    pub ontology_fields: Vec<OntologyFieldEntry>,
    pub ontology_delta: Vec<String>,
    pub execution_output: String,
}

#[derive(Debug, Clone)]
pub struct Lineage {
    pub id: String,
    pub seed_goal: String,
    pub generations: Vec<LineageGeneration>,
    pub current_gen: u32,
    pub status: String,
    pub convergence_similarity: f32,
    pub convergence_reason: String,
    pub created_at: String,
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
    pub seed_goal: String,
    pub status: ExecutionStatus,
    pub current_phase: Phase,
    pub iteration: u32,
    pub is_paused: bool,
    pub _start_ts: String,
    pub _last_ts: String,
    pub _msg_count: u64,
    pub _tool_call_count: u64,
    pub elapsed: String,

    // Metrics
    pub drift: DriftMetrics,
    pub cost: CostMetrics,

    // AC Tree
    pub ac_progress_cache: (u32, u32),
    pub ac_root: Vec<ACNode>,
    pub selected_node_id: Option<String>,
    pub prev_selected_node_id: Option<String>,
    pub ac_tree_list: ListState,
    pub ac_tree_entries: Vec<ACTreeEntry>,
    pub ac_expanded: HashSet<String>,
    pub detail_scroll: ScrollState,

    // Tool tracking
    pub active_tools: HashMap<String, ToolInfo>,
    pub tool_history: HashMap<String, Vec<ToolHistoryEntry>>,
    pub thinking: HashMap<String, String>,

    // Logs
    pub logs: Vec<LogEntry>,
    #[allow(dead_code)]
    pub log_scroll: ScrollState,
    pub log_filter: TextInputState,
    pub log_level_filter: Option<LogLevel>,

    // Debug / Info sidebar
    pub raw_events: Vec<RawEvent>,
    #[allow(dead_code)]
    pub debug_scroll: ScrollState,
    pub show_log_panel: bool,

    // Lineage
    pub lineages: Vec<Lineage>,
    pub lineage_list: ListState,
    pub selected_lineage_idx: Option<usize>,
    #[allow(dead_code)]
    pub lineage_scroll: ScrollState,
    pub selected_gen_idx: Option<usize>,
    pub lineage_detail_scroll: ScrollState,
    pub lineage_gen_list: ListState,

    // Execution
    pub execution_events: Vec<ExecutionEvent>,
    pub event_timeline_scroll: ScrollState,
    pub phase_outputs: HashMap<String, Vec<String>>,
    pub log_table: TableState,

    // Session selector
    pub sessions: Vec<SessionInfo>,
    pub session_list: ListState,
    pub session_table: TableState,

    // Mock simulation
    pub mock_tick: u64,
    pub auto_simulate: bool,
}

#[derive(Debug, Clone)]
#[allow(dead_code)]
pub struct ExecutionEvent {
    pub timestamp: String,
    pub event_type: String,
    pub detail: String,
    pub phase: Option<String>,
}

#[derive(Debug, Clone)]
#[allow(dead_code)]
pub struct SessionInfo {
    pub aggregate_type: String,
    pub aggregate_id: String,
    pub event_count: usize,
    pub first_ts: String,
    pub goal: String,
    pub status: String,
}

impl AppState {
    pub fn new() -> Self {
        Self {
            screen: Screen::Dashboard,
            tabs: TabsState::new(vec!["Dashboard", "Execution", "Lineage", "Sessions"]),
            command_palette: CommandPaletteState::new(vec![
                PaletteCommand::new("Dashboard", "Phase + AC tree + detail (1)"),
                PaletteCommand::new("Execution", "Diamond + events + info/logs (2)"),
                PaletteCommand::new("Lineage", "Evolutionary history (3/e)"),
                PaletteCommand::new("Sessions", "Switch session (s)"),
                PaletteCommand::new("Pause", "Pause execution (p)"),
                PaletteCommand::new("Resume", "Resume execution (r)"),
                PaletteCommand::new("Quit", "Exit (q)"),
            ]),

            execution_id: String::new(),
            session_id: String::new(),
            seed_goal: String::new(),
            status: ExecutionStatus::Idle,
            current_phase: Phase::Discover,
            iteration: 0,
            is_paused: false,
            _start_ts: String::new(),
            _last_ts: String::new(),
            _msg_count: 0,
            _tool_call_count: 0,
            elapsed: String::from("00:00"),

            drift: DriftMetrics::default(),
            cost: CostMetrics::default(),

            ac_progress_cache: (0, 0),
            ac_root: Vec::new(),
            selected_node_id: None,
            prev_selected_node_id: None,
            ac_tree_list: ListState::new(Vec::<&str>::new()),
            ac_tree_entries: Vec::new(),
            ac_expanded: HashSet::new(),
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
            show_log_panel: false,

            lineages: Vec::new(),
            lineage_list: ListState::new(Vec::<&str>::new()),
            selected_lineage_idx: None,
            lineage_scroll: ScrollState::new(),
            selected_gen_idx: None,
            lineage_detail_scroll: ScrollState::new(),
            lineage_gen_list: ListState::new(Vec::<&str>::new()),

            execution_events: Vec::new(),
            event_timeline_scroll: ScrollState::new(),
            phase_outputs: HashMap::new(),
            log_table: TableState::new(
                vec!["Time", "Level", "Source", "Message"],
                Vec::<Vec<&str>>::new(),
            ),

            sessions: Vec::new(),
            session_list: ListState::new(Vec::<&str>::new()),
            session_table: TableState::new(
                vec!["", "Goal", "ID", "Time", "Events"],
                Vec::<Vec<&str>>::new(),
            ),

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

    /// Count completed / total ACs (leaf nodes only). Returns cached value
    /// computed in `rebuild_tree_state()`.
    pub fn ac_progress(&self) -> (u32, u32) {
        self.ac_progress_cache
    }

    /// Recompute ac_progress from the tree (called by rebuild_tree_state).
    fn compute_ac_progress(nodes: &[ACNode]) -> (u32, u32) {
        let mut done = 0u32;
        let mut total = 0u32;
        for node in nodes {
            if node.children.is_empty() {
                total += 1;
                if node.status == ACStatus::Completed {
                    done += 1;
                }
            } else {
                let (d, t) = Self::compute_ac_progress(&node.children);
                done += d;
                total += t;
            }
        }
        (done, total)
    }

    pub fn rebuild_tree_state(&mut self) {
        // On first build, expand all nodes by default
        if self.ac_expanded.is_empty() && !self.ac_root.is_empty() {
            fn collect_ids(nodes: &[ACNode], set: &mut HashSet<String>) {
                for n in nodes {
                    if !n.children.is_empty() {
                        set.insert(n.id.clone());
                        collect_ids(&n.children, set);
                    }
                }
            }
            collect_ids(&self.ac_root, &mut self.ac_expanded);
        }

        // Preserve selection by node ID
        let prev_node_id = self
            .ac_tree_entries
            .get(self.ac_tree_list.selected)
            .map(|e| e.node_id.clone());

        // Flatten tree with connectors
        let mut entries = Vec::new();
        let mut guides: Vec<bool> = Vec::new();

        fn flatten(
            nodes: &[ACNode],
            expanded: &HashSet<String>,
            active_tools: &HashMap<String, ToolInfo>,
            guides: &mut Vec<bool>,
            entries: &mut Vec<ACTreeEntry>,
        ) {
            let count = nodes.len();
            for (i, node) in nodes.iter().enumerate() {
                let is_last = i == count - 1;
                let has_children = !node.children.is_empty();
                let is_expanded = has_children && expanded.contains(&node.id);

                // Build tree prefix from guide stack
                let mut prefix = String::new();
                for &has_line in guides.iter() {
                    prefix.push_str(if has_line { "│  " } else { "   " });
                }
                // Add connector for this node
                if !guides.is_empty() || count > 1 {
                    prefix.push_str(if is_last { "└─ " } else { "├─ " });
                }

                let toggle_icon = if has_children {
                    if is_expanded { "▾ " } else { "▸ " }
                } else {
                    "  "
                };

                let active_tool = active_tools.get(&node.id).map(|t| {
                    format!("« {}: {}", t.tool_name, &t.tool_detail)
                });

                entries.push(ACTreeEntry {
                    prefix,
                    toggle_icon,
                    status: node.status,
                    content: node.content.clone(),
                    node_id: node.id.clone(),
                    has_children,
                    active_tool,
                });

                // Recurse into children if expanded
                if is_expanded {
                    guides.push(!is_last);
                    flatten(&node.children, expanded, active_tools, guides, entries);
                    guides.pop();
                }
            }
        }

        flatten(
            &self.ac_root,
            &self.ac_expanded,
            &self.active_tools,
            &mut guides,
            &mut entries,
        );

        // Build ListState items (used by virtual_list for navigation only)
        let list_items: Vec<String> = entries.iter().map(|e| e.node_id.clone()).collect();
        self.ac_tree_list = ListState::new(list_items);
        self.ac_tree_entries = entries;

        // Restore selection by node ID
        if let Some(ref id) = prev_node_id {
            if let Some(pos) = self.ac_tree_entries.iter().position(|e| e.node_id == *id) {
                self.ac_tree_list.selected = pos;
            }
        }

        // Cache AC progress so it doesn't recompute every frame
        self.ac_progress_cache = Self::compute_ac_progress(&self.ac_root);
    }

    /// Reset detail scroll when the selected node changes.
    pub fn check_selection_changed(&mut self) {
        if self.selected_node_id != self.prev_selected_node_id {
            self.detail_scroll = ScrollState::new();
            self.prev_selected_node_id = self.selected_node_id.clone();
        }
    }

    /// Toggle expand/collapse for a node by ID.
    pub fn toggle_ac_node(&mut self, node_id: &str) {
        if self.ac_expanded.contains(node_id) {
            self.ac_expanded.remove(node_id);
        } else {
            self.ac_expanded.insert(node_id.to_string());
        }
        self.rebuild_tree_state();
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
        if self.raw_events.len() > 500 {
            self.raw_events.drain(..self.raw_events.len() - 500);
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Utilities
// ─────────────────────────────────────────────────────────────────────────────

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
