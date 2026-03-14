mod db;
mod mock;
mod state;
mod views;

use std::path::PathBuf;

use slt::{Color, Context, KeyModifiers, RunConfig, Theme};

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
                        });
                    }
                    state.session_list = slt::ListState::new(
                        sessions
                            .iter()
                            .map(|s| {
                                let goal_short = if s.goal.len() > 50 {
                                    format!("{}...", &s.goal[..47])
                                } else if s.goal.is_empty() {
                                    "(no goal)".to_string()
                                } else {
                                    s.goal.clone()
                                };
                                format!(
                                    "[{}] {} ({} events)",
                                    s.status, goal_short, s.event_count
                                )
                            })
                            .collect::<Vec<_>>(),
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
                    0 => state.screen = Screen::Dashboard,
                    1 => state.screen = Screen::Execution,
                    2 => state.screen = Screen::Logs,
                    3 => state.screen = Screen::Debug,
                    4 => state.screen = Screen::Lineage,
                    5 => state.screen = Screen::SessionSelector,
                    6 => {
                        state.is_paused = true;
                        state.status = ExecutionStatus::Paused;
                    }
                    7 => {
                        state.is_paused = false;
                        state.status = ExecutionStatus::Running;
                    }
                    8 => ui.quit(),
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
                    Screen::Logs => views::logs::render(ui, &mut state),
                    Screen::Debug => views::debug::render(ui, &mut state),
                    Screen::Lineage => views::lineage::render(ui, &mut state),
                    Screen::SessionSelector => {
                        if let Some(idx) = views::session_selector::render(ui, &mut state) {
                            if let Some(session) = state.sessions.get(idx) {
                                let agg_id = session.aggregate_id.clone();
                                if let Some(ref mut conn) = ouro_db {
                                    // Reset state for new session
                                    let mut new_state = AppState::new();
                                    // Preserve sessions list
                                    new_state.sessions = state.sessions.clone();
                                    new_state.session_list = slt::ListState::new(
                                        state.session_list.items.clone(),
                                    );
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

                render_footer(ui);
            });

            poll_counter += 1;
            if let Some(ref mut conn) = ouro_db {
                if poll_counter % 30 == 0 {
                    let new_events = conn.read_new_events();
                    if !new_events.is_empty() {
                        db::populate_state_from_events(&mut state, &new_events);
                    }
                }
            } else if state.auto_simulate && !state.is_paused {
                mock::tick_mock(&mut state);
            }
        },
    )
}

fn handle_global_keys(ui: &mut Context, state: &mut AppState) {
    let on_logs = state.tabs.selected == 2;

    if ui.key('q') {
        ui.quit();
    }
    if ui.key_mod('p', KeyModifiers::CONTROL) {
        state.command_palette.open = !state.command_palette.open;
    }
    if ui.key('p') && !state.command_palette.open && !on_logs {
        state.is_paused = true;
        state.status = ExecutionStatus::Paused;
        state.add_log(LogLevel::Info, "tui", "Execution paused by user");
    }
    if ui.key('r') && !state.command_palette.open && !on_logs {
        state.is_paused = false;
        state.status = ExecutionStatus::Running;
        state.add_log(LogLevel::Info, "tui", "Execution resumed");
    }
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
    if ui.key('5') {
        state.tabs.selected = 4;
    }
    if ui.key('l') && !on_logs {
        state.tabs.selected = 2;
    }
    if ui.key('d') && !on_logs {
        state.tabs.selected = 3;
    }
    if ui.key('e') && !on_logs {
        state.tabs.selected = 4;
    }
    if ui.key('s') && !state.command_palette.open && !on_logs {
        state.screen = Screen::SessionSelector;
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

    ui.container().bg(surface).px(3).py(1).col(|ui| {
        ui.row(|ui| {
            ui.text("◆ OUROBOROS").bold().fg(accent);

            ui.spacer();

            if !state.session_id.is_empty() {
                let sid: String = state.session_id.chars().take(14).collect();
                ui.text(&sid).fg(secondary);
                ui.text("  ").fg(dim);
            }

            ui.text(format!("iter {}  ", state.iteration)).fg(dim);
            ui.text(&state.elapsed).fg(dim);
            ui.text("  ").fg(dim);
            ui.text(format!("${:.2}", state.cost.total_cost_usd))
                .fg(success);
            ui.text(format!("  {}k", state.cost.total_tokens / 1000))
                .fg(dim);

            ui.text("    ").fg(dim);

            let (sc, sl) = match state.status {
                ExecutionStatus::Running => (success, "● RUN"),
                ExecutionStatus::Paused => (accent, "⏸ PAUSE"),
                ExecutionStatus::Completed => (success, "✓ DONE"),
                ExecutionStatus::Failed => (error, "✖ FAIL"),
                _ => (dim, "○ IDLE"),
            };
            ui.text(sl).fg(sc).bold();

            let (done, total) = state.ac_progress();
            if total > 0 {
                ui.text(format!("  {done}/{total}")).fg(text);
            }
        });
    });
}

fn render_tab_bar(ui: &mut Context, state: &mut AppState) {
    let surface = ui.theme().surface;
    let accent = ui.theme().accent;
    let dim = ui.theme().text_dim;
    let text = ui.theme().text;
    let tabs = ["Dashboard", "Execution", "Logs", "Debug", "Lineage"];
    ui.container().bg(surface).px(2).py(0).row(|ui| {
        for (i, label) in tabs.iter().enumerate() {
            let active = state.tabs.selected == i;
            let resp = ui.container().px(2).py(0).row(|ui| {
                if active {
                    ui.text("▸ ").fg(accent);
                    ui.text(*label).fg(text).bold();
                } else {
                    ui.text("  ").fg(dim);
                    ui.text(*label).fg(dim);
                }
            });
            if resp.clicked {
                state.tabs.selected = i;
            }
        }
        ui.spacer();
    });

    if state.screen != Screen::SessionSelector {
        state.screen = match state.tabs.selected {
            0 => Screen::Dashboard,
            1 => Screen::Execution,
            2 => Screen::Logs,
            3 => Screen::Debug,
            4 => Screen::Lineage,
            _ => Screen::Dashboard,
        };
    }
}

fn render_footer(ui: &mut Context) {
    let surface = ui.theme().surface;
    let dim = ui.theme().text_dim;
    let accent = ui.theme().accent;
    let text = ui.theme().text;

    ui.container().bg(surface).px(3).py(0).row(|ui| {
        let keys: &[(&str, &str)] = &[
            ("q", "Quit"),
            ("p/r", "Pause"),
            ("^P", "Palette"),
            ("↑↓", "Nav"),
        ];
        for (i, (key, desc)) in keys.iter().enumerate() {
            ui.text(*key).fg(accent);
            ui.text(format!(" {} ", desc)).fg(dim);
            if i < keys.len() - 1 {
                ui.text("  ").fg(text);
            }
        }
    });
}
