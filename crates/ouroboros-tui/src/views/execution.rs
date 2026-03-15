use slt::{Border, Color, Context};

use crate::state::*;
use crate::views;

pub fn render(ui: &mut Context, state: &mut AppState) {
    let dim = ui.theme().text_dim;
    let surface = ui.theme().surface;
    let surface_hover = ui.theme().surface_hover;
    let text = ui.theme().text;
    let secondary = ui.theme().secondary;
    let accent = ui.theme().accent;
    let success = ui.theme().success;
    let error = ui.theme().error;
    let primary = ui.theme().primary;

    ui.container().grow(1).gap(0).col(|ui| {
        // ── Top: Execution / Drift / Cost — horizontal cards ──
        ui.container().gap(0).row(|ui| {
            // Execution info
            ui.container()
                .grow(1)
                .border(Border::Single)
                .title(" Execution ")
                .bg(surface)
                .p(1)
                .gap(0)
                .col(|ui| {
                    ui.row(|ui| {
                        ui.text("Exec ID   ").fg(dim);
                        ui.text(&state.execution_id).fg(secondary);
                    });
                    ui.row(|ui| {
                        ui.text("Session   ").fg(dim);
                        ui.text(&state.session_id).fg(secondary);
                    });
                    ui.row(|ui| {
                        ui.text("Status    ").fg(dim);
                        let sc = match state.status {
                            ExecutionStatus::Running => success,
                            ExecutionStatus::Paused => accent,
                            ExecutionStatus::Failed => error,
                            ExecutionStatus::Completed => success,
                            _ => dim,
                        };
                        ui.text(format!("{} {}", state.status.icon(), state.status.label()))
                            .fg(sc)
                            .bold();
                    });
                    ui.row(|ui| {
                        ui.text("Phase     ").fg(dim);
                        ui.text(state.current_phase.label()).fg(primary).bold();
                    });
                    ui.row(|ui| {
                        ui.text("Iteration ").fg(dim);
                        ui.text(format!("{}", state.iteration)).fg(accent);
                    });
                });

            // Drift
            ui.container()
                .grow(1)
                .border(Border::Single)
                .title(" Drift ")
                .bg(surface)
                .p(1)
                .gap(0)
                .col(|ui| {
                    drift_kv(ui, "Combined  ", state.drift.combined, dim);
                    drift_kv(ui, "Goal      ", state.drift.goal, dim);
                    drift_kv(ui, "Constraint", state.drift.constraint, dim);
                    drift_kv(ui, "Ontology  ", state.drift.ontology, dim);
                    if !state.drift.history.is_empty() {
                        ui.row(|ui| {
                            ui.text("History   ").fg(dim);
                            ui.sparkline(state.drift.history.make_contiguous(), 20);
                        });
                    }
                });

            // Cost
            ui.container()
                .grow(1)
                .border(Border::Single)
                .title(" Cost ")
                .bg(surface)
                .p(1)
                .gap(0)
                .col(|ui| {
                    ui.row(|ui| {
                        ui.text("Tokens    ").fg(dim);
                        if state.cost.total_tokens > 0 {
                            ui.text(format!("~{}", state.cost.total_tokens)).fg(accent);
                        } else {
                            ui.text("—").fg(dim);
                        }
                    });
                    ui.row(|ui| {
                        ui.text("Cost USD  ").fg(dim);
                        if state.cost.total_cost_usd > 0.0 {
                            ui.text(format!("~${:.2}", state.cost.total_cost_usd)).fg(success);
                        } else {
                            ui.text("—").fg(dim);
                        }
                    });
                    ui.row(|ui| {
                        ui.text("Tools     ").fg(dim);
                        ui.text(format!("{} calls", state._tool_call_count)).fg(primary);
                    });
                    ui.row(|ui| {
                        ui.text("Messages  ").fg(dim);
                        ui.text(format!("{}", state._msg_count)).fg(accent);
                    });
                    ui.row(|ui| {
                        ui.text("Events    ").fg(dim);
                        ui.text(format!("{}", state.raw_events.len())).fg(accent);
                    });
                    if !state.cost.history.is_empty() {
                        ui.row(|ui| {
                            ui.text("History   ").fg(dim);
                            ui.sparkline(state.cost.history.make_contiguous(), 20);
                        });
                    }
                });
        });

        // ── Event Timeline ──
        ui.container()
            .grow(1)
            .border(Border::Single)
            .title(" Event Timeline ")
            .bg(surface)
            .col(|ui| {
                render_event_timeline(
                    ui, state, dim, surface, surface_hover, text, secondary,
                    accent, success, error, primary,
                );
            });

        // Bottom: Log panel (conditional)
        if state.show_log_panel {
            ui.container().h_pct(40).mt(1).col(|ui| {
                views::logs::render_log_panel(ui, state);
            });
        }
    });
}

/// Render the unified event timeline (merging execution events + raw events).
fn render_event_timeline(
    ui: &mut Context,
    state: &mut AppState,
    dim: Color,
    surface: Color,
    surface_hover: Color,
    text: Color,
    secondary: Color,
    accent: Color,
    success: Color,
    error: Color,
    primary: Color,
) {
    if state.execution_events.is_empty() && state.raw_events.is_empty() {
        ui.container().grow(1).center().col(|ui| {
            ui.text("No events yet").fg(dim);
        });
        return;
    }

    // Event count badge
    let count = if state.execution_events.is_empty() {
        state.raw_events.len()
    } else {
        state.execution_events.len()
    };
    ui.container().bg(surface_hover).px(2).py(0).row(|ui| {
        ui.text(format!("{count}")).fg(text).bold();
        ui.text(" events").fg(dim);
        ui.spacer();
        if !state.active_tools.is_empty() {
            for tool in state.active_tools.values() {
                ui.line(|ui| {
                    ui.text("● ").fg(accent);
                    ui.text(&tool.tool_name).fg(text);
                });
            }
        }
    });

    ui.scrollable(&mut state.event_timeline_scroll)
        .grow(1)
        .p(1)
        .col(|ui| {
            if !state.execution_events.is_empty() {
                // Use execution events
                for (i, ev) in state.execution_events.iter().rev().take(200).enumerate() {
                    let bg = if i % 2 == 0 { surface } else { surface_hover };
                    let (icon, type_color) =
                        event_visual(&ev.event_type, success, secondary, error, accent, dim);

                    let ts = compact_timestamp(&ev.timestamp);

                    ui.container().bg(bg).px(1).py(0).col(|ui| {
                        ui.line(|ui| {
                            ui.text(format!("{icon} ")).fg(type_color);
                            ui.text(&ts).fg(dim);
                            ui.text(" ").fg(dim);
                            ui.text(&ev.event_type).fg(type_color).bold();
                        });
                        if !ev.detail.is_empty() {
                            ui.text_wrap(&ev.detail).fg(dim);
                        }
                    });
                }
            } else {
                // Fall back to raw events (with richer rendering from debug.rs)
                for (i, ev) in state.raw_events.iter().rev().take(200).enumerate() {
                    let bg = if i % 2 == 0 { surface } else { surface_hover };
                    let ts = compact_timestamp(&ev.timestamp);
                    let type_color = event_type_color(
                        &ev.event_type,
                        success,
                        secondary,
                        error,
                        accent,
                        primary,
                        dim,
                    );
                    let icon = event_category_icon(&ev.event_type);

                    ui.container().bg(bg).px(1).py(0).col(|ui| {
                        ui.row(|ui| {
                            ui.text(&ts).fg(dim);
                            ui.text(" ").fg(dim);
                            ui.text(format!("{icon} ")).fg(type_color);
                            ui.text(&ev.event_type).fg(type_color).bold();
                        });
                        if !ev.data_preview.is_empty()
                            && ev.data_preview != "{}"
                            && ev.data_preview.len() <= 200
                            && !ev.data_preview.starts_with("{\"")
                        {
                            ui.text_wrap(&ev.data_preview).fg(dim);
                        }
                    });
                }
            }
        });
}

// ─────────────────────────────────────────────────────────────────────────────
// Helper functions (migrated from debug.rs + execution.rs)
// ─────────────────────────────────────────────────────────────────────────────

fn compact_timestamp(ts: &str) -> String {
    if ts.len() > 8 {
        ts.chars().skip(ts.len().saturating_sub(8)).collect()
    } else {
        ts.to_string()
    }
}

fn event_visual(
    event_type: &str,
    success: Color,
    secondary: Color,
    error: Color,
    accent: Color,
    dim: Color,
) -> (&'static str, Color) {
    if event_type.contains("started") {
        ("▶", success)
    } else if event_type.contains("completed") {
        ("✓", secondary)
    } else if event_type.contains("failed") || event_type.contains("error") {
        ("✗", error)
    } else if event_type.contains("tool") {
        ("⚡", accent)
    } else if event_type.contains("phase") {
        ("◆", secondary)
    } else if event_type.contains("drift") {
        ("↕", accent)
    } else if event_type.contains("cost") || event_type.contains("token") {
        ("$", success)
    } else {
        ("·", dim)
    }
}

fn event_type_color(
    event_type: &str,
    success: Color,
    secondary: Color,
    error: Color,
    accent: Color,
    primary: Color,
    dim: Color,
) -> Color {
    if event_type.starts_with("orchestrator.") {
        primary
    } else if event_type.starts_with("execution.tool") {
        accent
    } else if event_type.starts_with("execution.") {
        secondary
    } else if event_type.starts_with("observability.drift") {
        Color::Yellow
    } else if event_type.starts_with("observability.cost") {
        success
    } else if event_type.starts_with("workflow.") {
        secondary
    } else if event_type.starts_with("lineage.") {
        primary
    } else if event_type.contains("failed") || event_type.contains("error") {
        error
    } else {
        dim
    }
}

fn event_category_icon(event_type: &str) -> &'static str {
    if event_type.starts_with("orchestrator.") {
        "◆"
    } else if event_type.starts_with("execution.tool") {
        "⚡"
    } else if event_type.starts_with("execution.") {
        "▶"
    } else if event_type.starts_with("observability.drift") {
        "↕"
    } else if event_type.starts_with("observability.cost") {
        "$"
    } else if event_type.starts_with("workflow.") {
        "◇"
    } else if event_type.starts_with("lineage.") {
        "∞"
    } else {
        "·"
    }
}

fn drift_kv(ui: &mut Context, label: &str, val: f64, dim: Color) {
    ui.row(|ui| {
        ui.text(label).fg(dim);
        if val > 0.0 {
            let c = if val < 0.1 {
                Color::Green
            } else if val < 0.2 {
                Color::Yellow
            } else {
                Color::Red
            };
            ui.text(format!("{:.4}", val)).fg(c);
        } else {
            ui.text("—").fg(dim);
        }
    });
}
