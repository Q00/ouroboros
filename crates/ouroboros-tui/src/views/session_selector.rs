use slt::{Border, Context, KeyCode};

use crate::state::*;

/// Returns Some(selected_index) when Enter is pressed, None otherwise.
pub fn render(ui: &mut Context, state: &mut AppState) -> Option<usize> {
    let dim = ui.theme().text_dim;
    let surface = ui.theme().surface;
    let surface_hover = ui.theme().surface_hover;
    let text = ui.theme().text;
    let accent = ui.theme().accent;
    let secondary = ui.theme().secondary;
    let success = ui.theme().success;

    // Esc returns to Dashboard
    if ui.key_code(KeyCode::Esc) {
        state.tabs.selected = 0;
    }

    let mut selected = None;

    // Enter selects the currently highlighted row
    if ui.key_code(KeyCode::Enter) && !state.sessions.is_empty() {
        let idx = state.session_table.selected.min(state.sessions.len().saturating_sub(1));
        selected = Some(idx);
    }

    // Left/Right arrow keys for page navigation
    if ui.key_code(KeyCode::Left) && state.session_table.page > 0 {
        state.session_table.page -= 1;
    }
    if ui.key_code(KeyCode::Right) {
        let max_page = state.session_table.total_pages().saturating_sub(1);
        if state.session_table.page < max_page {
            state.session_table.page += 1;
        }
    }

    ui.container().grow(1).gap(0).col(|ui| {
        // Header
        ui.container().bg(surface_hover).px(3).py(1).col(|ui| {
            ui.row(|ui| {
                ui.text("◆ Session Selector").fg(text).bold();
                ui.spacer();
                ui.text(format!("{} sessions", state.sessions.len())).fg(dim);
            });
            ui.row(|ui| {
                ui.text("Enter").fg(accent);
                ui.text(" select  ").fg(dim);
                ui.text("Esc").fg(accent);
                ui.text(" back  ").fg(dim);
                ui.text("←/→").fg(accent);
                ui.text(" pages  ").fg(dim);
                ui.text("Click header").fg(accent);
                ui.text(" sort").fg(dim);
            });
        });

        // Session table
        ui.container()
            .grow(1)
            .mt(1)
            .border(Border::Single)
            .title(" Sessions ")
            .bg(surface)
            .col(|ui| {
                if state.sessions.is_empty() {
                    ui.container().grow(1).center().col(|ui| {
                        ui.text("No sessions found").fg(dim).bold();
                        ui.text("").fg(dim);
                        ui.text("Run ouroboros workflows to create sessions").fg(dim);
                        ui.text("or use --mock for demo data").fg(dim);
                    });
                } else {
                    state.session_table.page_size = 50;
                    ui.container().grow(1).col(|ui| {
                        ui.table(&mut state.session_table);
                    });
                }
            });

        // Footer
        if !state.sessions.is_empty() {
            ui.container().bg(surface_hover).px(3).py(0).row(|ui| {
                let total_pages = state.session_table.total_pages().max(1);
                let page = (state.session_table.page + 1).min(total_pages);
                ui.text(format!("Page {page}/{total_pages}")).fg(text);
                ui.text("  ").fg(dim);
                ui.text("Click row to select").fg(dim);
                ui.spacer();
                // Show selected session ID in footer
                let sel_idx = state.session_table.selected;
                if let Some(session) = state.sessions.get(sel_idx) {
                    ui.text(&session.aggregate_id).fg(accent);
                    ui.text(format!("  {} events", session.event_count)).fg(success);
                    ui.text("  ").fg(dim);
                    ui.text(&session.first_ts).fg(secondary);
                }
            });
        }
    });

    selected
}
