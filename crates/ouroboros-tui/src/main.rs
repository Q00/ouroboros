mod db;
mod mock;
mod state;
mod views;

use std::path::PathBuf;

use slt::{Border, Color, Context, KeyModifiers, RunConfig, Theme};

use crate::state::*;

fn ouroboros_theme() -> Theme {
    Theme {
        primary: Color::Rgb(196, 167, 231),  // Rose Pine iris
        secondary: Color::Rgb(49, 116, 143), // Rose Pine pine
        accent: Color::Rgb(246, 193, 119),   // Rose Pine gold
        text: Color::Rgb(224, 222, 244),     // Rose Pine text
        text_dim: Color::Rgb(110, 106, 134), // Rose Pine muted
        border: Color::Rgb(38, 35, 58),      // Rose Pine overlay
        bg: Color::Rgb(25, 23, 36),          // Rose Pine base
        success: Color::Rgb(156, 207, 216),  // Rose Pine foam
        warning: Color::Rgb(246, 193, 119),  // Rose Pine gold
        error: Color::Rgb(235, 111, 146),    // Rose Pine love
        selected_bg: Color::Rgb(38, 35, 58), // Rose Pine overlay
        selected_fg: Color::Rgb(224, 222, 244),
        surface: Color::Rgb(31, 29, 46),         // Rose Pine surface
        surface_hover: Color::Rgb(38, 35, 58),   // Rose Pine overlay
        surface_text: Color::Rgb(144, 140, 170), // Rose Pine subtle
    }
}

fn print_help() {
    eprintln!("ouroboros-tui — Rust TUI dashboard for Ouroboros");
    eprintln!();
    eprintln!("USAGE:");
    eprintln!("  ouroboros-tui [monitor] [OPTIONS]");
    eprintln!();
    eprintln!("OPTIONS:");
    eprintln!("  --db-path <PATH>   Path to ouroboros.db (default: ~/.ouroboros/ouroboros.db)");
    eprintln!("  --mock             Use demo data instead of DB");
    eprintln!("  --help, -h         Show this help");
    eprintln!();
    eprintln!("EXAMPLES:");
    eprintln!("  ouroboros-tui                     # default DB");
    eprintln!("  ouroboros-tui monitor             # same as above");
    eprintln!("  ouroboros-tui --mock              # demo mode");
    eprintln!("  ouroboros-tui --db-path /tmp/o.db # custom DB path");
}

fn main() -> std::io::Result<()> {
    let args: Vec<String> = std::env::args().collect();

    if args.iter().any(|a| a == "--help" || a == "-h") {
        print_help();
        return Ok(());
    }

    let use_mock = args.iter().any(|a| a == "--mock");

    let db_path = args
        .iter()
        .position(|a| a == "--db-path")
        .and_then(|i| args.get(i + 1))
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            let home = std::env::var("HOME")
                .or_else(|_| std::env::var("USERPROFILE"))
                .unwrap_or_else(|_| ".".into());
            PathBuf::from(home).join(".ouroboros/ouroboros.db")
        });

    let mut state = AppState::new();
    let mut ouro_db: Option<db::OuroborosDb> = None;
    let mut poll_counter: u64 = 0;

    if use_mock {
        mock::init_mock_state(&mut state);
        state.add_log(LogLevel::Info, "tui", "Running in mock/demo mode");
    } else {
        match db::OuroborosDb::open(&db_path) {
            Ok(mut conn) => {
                let sessions = conn.distinct_sessions();
                let event_count = conn.event_count();

                if event_count == 0 {
                    state.add_log(LogLevel::Warning, "db", "DB empty — loading demo data");
                    mock::init_mock_state(&mut state);
                } else {
                    state.add_log(
                        LogLevel::Info,
                        "db",
                        &format!("{} events, {} sessions", event_count, sessions.len()),
                    );
                    for s in &sessions {
                        state.sessions.push(state::SessionInfo {
                            aggregate_type: "session".into(),
                            aggregate_id: s.aggregate_id.clone(),
                            event_count: s.event_count,
                            first_ts: s.timestamp.clone(),
                            goal: s.goal.clone(),
                            status: s.status.clone(),
                        });
                    }
                    // Build session table rows
                    let table_rows: Vec<Vec<String>> = sessions
                        .iter()
                        .map(|s| {
                            let status_icon = match s.status.as_str() {
                                "done" => "✓",
                                "failed" => "✗",
                                "running" => "▶",
                                "paused" => "⏸",
                                "cancelled" => "⊘",
                                _ => "?",
                            };
                            let goal_short: String = if s.goal.is_empty() {
                                "(no goal)".into()
                            } else {
                                s.goal.clone()
                            };
                            let id_short: String = s.aggregate_id.chars().skip(
                                s.aggregate_id.len().saturating_sub(12)
                            ).collect();
                            let ts_compact: String = match (
                                s.timestamp.get(5..7),
                                s.timestamp.get(8..10),
                                s.timestamp.get(11..16),
                            ) {
                                (Some(month), Some(day), Some(time)) => {
                                    format!("{}/{} {}", month, day, time)
                                }
                                _ => s.timestamp.clone(),
                            };
                            vec![
                                status_icon.to_string(),
                                goal_short,
                                format!("..{id_short}"),
                                ts_compact,
                                format!("{} events", s.event_count),
                            ]
                        })
                        .collect();
                    state.session_table = slt::TableState::new(
                        vec!["", "Goal", "ID", "Time", "Events"],
                        table_rows,
                    );
                    // Keep list for fallback selection tracking
                    state.session_list = slt::ListState::new(
                        sessions.iter().map(|s| {
                            let status_icon = match s.status.as_str() {
                                "done" => "✓", "failed" => "✗", "running" => "▶",
                                "paused" => "⏸", "cancelled" => "⊘", _ => "?",
                            };
                            format!("{} {}", status_icon, &s.goal)
                        }).collect::<Vec<_>>(),
                    );
                    // Load only the most recent session instead of all events
                    if let Some(latest) = state.sessions.first() {
                        let session_events =
                            conn.read_events_for_session(&latest.aggregate_id);
                        state.add_log(
                            LogLevel::Info,
                            "db",
                            &format!(
                                "Loaded session: {} ({} events)",
                                latest.aggregate_id,
                                session_events.len()
                            ),
                        );
                        db::populate_state_from_events(&mut state, &session_events);
                    }
                    // Load lineage events separately (they have independent aggregate_ids)
                    let lineage_events = conn.read_all_lineage_events();
                    if !lineage_events.is_empty() {
                        state.add_log(
                            LogLevel::Info,
                            "db",
                            &format!("Loaded {} lineage events", lineage_events.len()),
                        );
                        db::populate_state_from_events(&mut state, &lineage_events);
                    }
                    ouro_db = Some(conn);
                }
            }
            Err(e) => {
                state.add_log(LogLevel::Error, "db", &format!("DB failed: {e}"));
                mock::init_mock_state(&mut state);
            }
        }
    }

    slt::run_with(
        RunConfig {
            mouse: true,
            theme: ouroboros_theme(),
            ..Default::default()
        },
        |ui: &mut Context| {
            handle_global_keys(ui, &mut state);

            if let Some(cmd_idx) = ui.command_palette(&mut state.command_palette) {
                match cmd_idx {
                    0 => state.tabs.selected = 0, // Dashboard
                    1 => state.tabs.selected = 1, // Execution
                    2 => state.tabs.selected = 2, // Lineage
                    3 => state.tabs.selected = 3, // Sessions
                    4 => {
                        state.is_paused = true;
                        state.status = ExecutionStatus::Paused;
                    }
                    5 => {
                        state.is_paused = false;
                        state.status = ExecutionStatus::Running;
                    }
                    6 => ui.quit(),
                    _ => {}
                }
            }

            let bg = ui.theme().bg;
            ui.container().grow(1).bg(bg).col(|ui| {
                render_header(ui, &state);
                render_tab_bar(ui, &mut state);

                ui.container().grow(1).p(1).col(|ui| match state.screen {
                    Screen::Dashboard => views::dashboard::render(ui, &mut state),
                    Screen::Execution => views::execution::render(ui, &mut state),
                    Screen::Lineage => views::lineage::render(ui, &mut state),
                    Screen::SessionSelector => {
                        if let Some(idx) = views::session_selector::render(ui, &mut state) {
                            if let Some(session) = state.sessions.get(idx) {
                                let agg_id = session.aggregate_id.clone();
                                if let Some(ref mut conn) = ouro_db {
                                    // Reset state for new session
                                    let mut new_state = AppState::new();
                                    // Preserve sessions list and table
                                    new_state.sessions = state.sessions.clone();
                                    new_state.session_table = slt::TableState::new(
                                        state.session_table.headers.clone(),
                                        state.session_table.rows.clone(),
                                    );
                                    new_state.session_list = slt::ListState::new(
                                        state.session_list.items.clone(),
                                    );
                                    // Preserve lineage state (global, doesn't change per session)
                                    new_state.lineages = state.lineages.clone();
                                    new_state.lineage_list = slt::ListState::new(state.lineage_list.items.clone());
                                    new_state.selected_lineage_idx = state.selected_lineage_idx;
                                    let events = conn.read_events_for_session(&agg_id);
                                    db::populate_state_from_events(&mut new_state, &events);
                                    new_state.add_log(
                                        LogLevel::Info,
                                        "tui",
                                        &format!("Loaded session: {agg_id}"),
                                    );
                                    state = new_state;
                                }
                                state.screen = Screen::Dashboard;
                                state.tabs.selected = 0;
                            }
                        }
                    }
                });

                render_footer(ui, &state);
            });

            poll_counter += 1;
            if let Some(ref mut conn) = ouro_db {
                if poll_counter % 30 == 0 {
                    let new_events = conn.read_new_events();
                    if !new_events.is_empty() {
                        // Filter to events belonging to current session or its execution
                        let new_events: Vec<_> = new_events.into_iter().filter(|ev| {
                            ev.aggregate_id == state.session_id
                                || (!state.execution_id.is_empty() && ev.aggregate_id.starts_with(&state.execution_id))
                                || ev.event_type.starts_with("lineage.")
                                || ev.event_type.starts_with("observability.")
                        }).collect();
                        if !new_events.is_empty() {
                            db::populate_state_from_events(&mut state, &new_events);
                        }
                    }
                }
            } else if state.auto_simulate && !state.is_paused {
                mock::tick_mock(&mut state);
            }
        },
    )
}

fn handle_global_keys(ui: &mut Context, state: &mut AppState) {
    let on_execution = state.screen == Screen::Execution;
    // Log panel has text input, so avoid consuming keys when it's active
    let log_input_active = on_execution && state.show_log_panel;

    if ui.key('q') {
        ui.quit();
    }
    if ui.key_mod('p', KeyModifiers::CONTROL) {
        state.command_palette.open = !state.command_palette.open;
    }
    if ui.key('p') && !state.command_palette.open && !log_input_active {
        state.is_paused = true;
        state.status = ExecutionStatus::Paused;
        state.add_log(LogLevel::Info, "tui", "Execution paused by user");
    }
    if ui.key('r') && !state.command_palette.open && !log_input_active {
        state.is_paused = false;
        state.status = ExecutionStatus::Running;
        state.add_log(LogLevel::Info, "tui", "Execution resumed");
    }
    // Tab navigation: 1=Dashboard, 2=Execution, 3=Lineage, s=Sessions
    if ui.key('1') {
        state.tabs.selected = 0;
    }
    if ui.key('2') {
        state.tabs.selected = 1;
    }
    if ui.key('3') {
        state.tabs.selected = 2;
    }
    if ui.key('4') {
        state.tabs.selected = 3;
    }
    if ui.key('e') && !log_input_active {
        state.tabs.selected = 2; // Lineage shortcut
    }
    if ui.key('s') && !state.command_palette.open && !log_input_active {
        state.tabs.selected = 3; // Sessions
    }
    // Execution-specific toggles (only on Execution tab)
    if on_execution && !log_input_active {
        if ui.key('l') {
            state.show_log_panel = !state.show_log_panel;
        }
    }
}

fn render_header(ui: &mut Context, state: &AppState) {
    let text = ui.theme().text;
    let dim = ui.theme().text_dim;
    let success = ui.theme().success;
    let error = ui.theme().error;
    let accent = ui.theme().accent;
    let secondary = ui.theme().secondary;
    let surface = ui.theme().surface;
    let surface_hover = ui.theme().surface_hover;

    ui.container().bg(surface).px(3).py(0).col(|ui| {
        // Line 1: Logo + Status + Metrics
        ui.row(|ui| {
            ui.text("◆ OUROBOROS").bold().fg(accent);
            ui.text("  ").fg(dim);

            let (sc, sl) = match state.status {
                ExecutionStatus::Running => (success, "● RUN"),
                ExecutionStatus::Paused => (accent, "⏸ PAUSE"),
                ExecutionStatus::Completed => (success, "✓ DONE"),
                ExecutionStatus::Failed => (error, "✖ FAIL"),
                _ => (dim, "○ IDLE"),
            };
            ui.text(sl).fg(sc).bold();

            ui.spacer();

            let (done, total) = state.ac_progress();
            if total > 0 {
                ui.text(format!("[{done}/{total} AC]")).fg(text).bold();
                ui.text("  ").fg(dim);
            }
            ui.text(&state.elapsed).fg(dim);
            ui.text("  ").fg(dim);
            ui.text(format!("${:.2}", state.cost.total_cost_usd)).fg(success);
            ui.text(format!("  {}k tok", state.cost.total_tokens / 1000)).fg(dim);
            ui.text("  ").fg(dim);
            ui.text(format!("iter {}", state.iteration)).fg(dim);
        });
        // Line 2: Session Goal (most important context — prominent)
        if !state.seed_goal.is_empty() {
            ui.container().bg(surface_hover).px(0).py(0).row(|ui| {
                ui.text("Goal ").fg(dim);
                ui.text_wrap(&state.seed_goal).fg(text).bold();
                ui.spacer();
                if !state.session_id.is_empty() {
                    let sid_short: String = if state.session_id.len() > 12 {
                        format!("..{}", &state.session_id[state.session_id.len().saturating_sub(10)..])
                    } else {
                        state.session_id.clone()
                    };
                    ui.text(&sid_short).fg(secondary);
                }
            });
        }
    });
}

fn render_tab_bar(ui: &mut Context, state: &mut AppState) {
    let surface_hover = ui.theme().surface_hover;
    let accent = ui.theme().accent;
    let dim = ui.theme().text_dim;
    let text = ui.theme().text;
    let selected_bg = ui.theme().selected_bg;
    let tabs = [
        ("1", "Dashboard"),
        ("2", "Execution"),
        ("3", "Lineage"),
        ("4", "Sessions"),
    ];
    ui.container().bg(surface_hover).px(1).py(0).row(|ui| {
        for (i, (key, label)) in tabs.iter().enumerate() {
            let active = state.tabs.selected == i;
            let resp = if active {
                ui.container()
                    .bg(selected_bg)
                    .border(Border::Single)
                    .px(1)
                    .py(0)
                    .row(|ui| {
                        ui.text(*key).fg(accent).bold();
                        ui.text(" ").fg(dim);
                        ui.text(*label).fg(text).bold();
                    })
            } else {
                ui.container().px(2).py(0).row(|ui| {
                    ui.text(*key).fg(dim);
                    ui.text(" ").fg(dim);
                    ui.text(*label).fg(dim);
                })
            };
            if resp.clicked {
                state.tabs.selected = i;
            }
        }
        ui.spacer();
        // Drift sparkline in tab bar (always visible)
        let drift_success = ui.theme().success;
        let drift_warning = ui.theme().warning;
        let drift_error = ui.theme().error;
        if !state.drift.history.is_empty() {
            ui.text("drift ").fg(dim);
            ui.sparkline(state.drift.history.make_contiguous(), 8);
            ui.text(format!(" {:.2}", state.drift.combined)).fg(
                if state.drift.combined < 0.1 { drift_success }
                else if state.drift.combined < 0.2 { drift_warning }
                else { drift_error }
            );
        }
    });

    state.screen = match state.tabs.selected {
        0 => Screen::Dashboard,
        1 => Screen::Execution,
        2 => Screen::Lineage,
        3 => Screen::SessionSelector,
        _ => Screen::Dashboard,
    };
}

fn render_footer(ui: &mut Context, state: &AppState) {
    let surface = ui.theme().surface;
    let dim = ui.theme().text_dim;
    let accent = ui.theme().accent;

    // Tab-specific hints
    let extra_keys: &[(&str, &str)] = match state.screen {
        Screen::Dashboard => &[("↑↓", "Navigate tree"), ("Enter", "Expand/Collapse")],
        Screen::Execution => &[("l", "Log panel"), ("↑↓", "Scroll")],
        Screen::Lineage => &[("↑↓", "Select lineage")],
        Screen::SessionSelector => &[("Enter", "Load session"), ("←→", "Page"), ("Esc", "Back")],
    };

    ui.container().bg(surface).px(3).py(0).row(|ui| {
        // Global keys
        for (key, desc) in &[("q", "Quit"), ("p/r", "Pause/Resume"), ("^P", "Palette")] {
            ui.text(*key).fg(accent);
            ui.text(format!(" {}  ", desc)).fg(dim);
        }
        ui.text("│").fg(dim);
        ui.text(" ").fg(dim);
        // Tab-specific keys
        for (key, desc) in extra_keys {
            ui.text(*key).fg(accent);
            ui.text(format!(" {}  ", desc)).fg(dim);
        }
    });
}
