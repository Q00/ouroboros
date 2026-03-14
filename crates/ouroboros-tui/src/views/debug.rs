use slt::{Border, Color, Context};

use crate::state::*;

pub fn render(ui: &mut Context, state: &mut AppState) {
    let dim = ui.theme().text_dim;
    let accent = ui.theme().accent;
    let primary = ui.theme().primary;
    let surface = ui.theme().surface;
    let surface_hover = ui.theme().surface_hover;
    let success = ui.theme().success;
    let secondary = ui.theme().secondary;

    ui.container().grow(1).gap(1).row(|ui| {
        ui.container().w_pct(38).mr(1).gap(1).col(|ui| {
            ui.container()
                .grow(1)
                .border(Border::Single)
                .title(" Execution ")
                .bg(surface)
                .p(1)
                .gap(0)
                .col(|ui| {
                    kv(ui, "Exec ID   ", &state.execution_id, secondary, dim);
                    kv(ui, "Session   ", &state.session_id, secondary, dim);
                    ui.row(|ui| {
                        ui.text("Status    ").fg(dim);
                        let sc = match state.status {
                            ExecutionStatus::Running => Color::Green,
                            ExecutionStatus::Paused => Color::Yellow,
                            ExecutionStatus::Failed => Color::Red,
                            _ => Color::Cyan,
                        };
                        ui.text(format!("{} {}", state.status.icon(), state.status.label()))
                            .fg(sc)
                            .bold();
                    });
                    kv(ui, "Phase     ", state.current_phase.label(), primary, dim);
                    kv(ui, "Iteration ", &state.iteration.to_string(), primary, dim);
                    kv(
                        ui,
                        "Paused    ",
                        if state.is_paused { "Yes" } else { "No" },
                        if state.is_paused {
                            Color::Yellow
                        } else {
                            success
                        },
                        dim,
                    );
                });

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
                            ui.sparkline(&state.drift.history, 20);
                        });
                    }
                });

            ui.container()
                .grow(1)
                .border(Border::Single)
                .title(" Cost ")
                .bg(surface)
                .p(1)
                .gap(0)
                .col(|ui| {
                    kv(
                        ui,
                        "Tokens    ",
                        &state.cost.total_tokens.to_string(),
                        primary,
                        dim,
                    );
                    kv(
                        ui,
                        "Cost USD  ",
                        &format!("${:.4}", state.cost.total_cost_usd),
                        success,
                        dim,
                    );
                    kv(
                        ui,
                        "Tools     ",
                        &state.active_tools.len().to_string(),
                        primary,
                        dim,
                    );
                    if !state.cost.history.is_empty() {
                        ui.row(|ui| {
                            ui.text("History   ").fg(dim);
                            ui.sparkline(&state.cost.history, 20);
                        });
                    }
                });
        });

        ui.container()
            .grow(1)
            .border(Border::Single)
            .title(" Event Stream ")
            .bg(surface)
            .col(|ui| {
                if state.raw_events.is_empty() {
                    ui.container().grow(1).center().col(|ui| {
                        ui.text("No events yet").bold().fg(primary);
                        ui.text("Waiting...").fg(dim);
                    });
                } else {
                    ui.scrollable(&mut state.debug_scroll)
                        .grow(1)
                        .p(1)
                        .col(|ui| {
                            for (i, ev) in state.raw_events.iter().rev().enumerate() {
                                let bg = if i % 2 == 0 { surface } else { surface_hover };
                                ui.container().bg(bg).px(1).py(0).col(|ui| {
                                    ui.row(|ui| {
                                        ui.text(&ev.timestamp).fg(dim);
                                        ui.text("  ").fg(dim);
                                        ui.text(&ev.event_type).fg(secondary).bold();
                                        ui.spacer();
                                        ui.text(&ev.aggregate_id).fg(accent);
                                    });
                                    ui.text_wrap(&ev.data_preview).fg(dim);
                                });
                            }
                        });
                }

                ui.container().bg(surface_hover).px(3).py(0).row(|ui| {
                    ui.text(format!("{} events", state.raw_events.len()))
                        .fg(dim);
                });
            });
    });
}

fn kv(ui: &mut Context, label: &str, value: &str, vc: Color, dim: Color) {
    ui.row(|ui| {
        ui.text(label).fg(dim);
        ui.text(value).fg(vc);
    });
}

fn drift_kv(ui: &mut Context, label: &str, val: f64, dim: Color) {
    let c = if val < 0.1 {
        Color::Green
    } else if val < 0.2 {
        Color::Yellow
    } else {
        Color::Red
    };
    ui.row(|ui| {
        ui.text(label).fg(dim);
        ui.text(format!("{:.4}", val)).fg(c);
    });
}
