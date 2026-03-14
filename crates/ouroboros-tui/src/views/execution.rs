use slt::{Border, Context};

use crate::state::*;

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

    ui.container().grow(1).gap(1).row(|ui| {
        ui.container().grow(1).gap(1).col(|ui| {
            ui.container()
                .border(Border::Single)
                .title(" Phase Outputs ")
                .bg(surface)
                .grow(1)
                .col(|ui| {
                    ui.scrollable(&mut state.execution_scroll)
                        .grow(1)
                        .p(1)
                        .gap(1)
                        .col(|ui| {
                            for phase in Phase::ALL {
                                let done = phase.index() < state.current_phase.index();
                                let active = phase == state.current_phase;
                                let (icon, color) = if done {
                                    ("●", success)
                                } else if active {
                                    ("◐", accent)
                                } else {
                                    ("○", dim)
                                };

                                let phase_key = phase.label().to_lowercase();

                                ui.container()
                                    .border(Border::Single)
                                    .bg(surface_hover)
                                    .p(1)
                                    .col(|ui| {
                                        ui.line(|ui| {
                                            ui.text(format!("{icon} ")).fg(color);
                                            ui.text(format!("{} ", phase.label())).fg(color).bold();
                                            if done {
                                                ui.text("(Converge)").fg(dim);
                                            } else if active {
                                                ui.text("(Diverge)").fg(dim);
                                            }
                                        });

                                        if let Some(outputs) = state.phase_outputs.get(&phase_key) {
                                            for line in outputs.iter().rev().take(5) {
                                                ui.line(|ui| {
                                                    ui.text("  • ").fg(dim);
                                                    ui.text(line).fg(text);
                                                });
                                            }
                                        } else if done || active {
                                            ui.text("  No output recorded").fg(dim).italic();
                                        }
                                    });
                            }
                        });
                });

            ui.container()
                .border(Border::Single)
                .title(" Metrics ")
                .bg(surface)
                .p(1)
                .gap(0)
                .col(|ui| {
                    ui.row(|ui| {
                        ui.line(|ui| {
                            ui.text("Drift ").fg(dim);
                            ui.text(format!("{:.3} ", state.drift.combined))
                                .fg(drift_color(state.drift.combined));
                        });
                        if !state.drift.history.is_empty() {
                            ui.sparkline(&state.drift.history, 14);
                        }
                        ui.text("    ").fg(dim);
                        ui.line(|ui| {
                            ui.text("Cost ").fg(dim);
                            ui.text(format!("${:.2} ", state.cost.total_cost_usd))
                                .fg(success);
                        });
                        if !state.cost.history.is_empty() {
                            ui.sparkline(&state.cost.history, 14);
                        }
                        ui.text("    ").fg(dim);
                        ui.line(|ui| {
                            ui.text("Iter ").fg(dim);
                            ui.text(format!("{}", state.iteration)).fg(primary);
                        });
                    });
                });
        });

        ui.container()
            .w_pct(40)
            .border(Border::Single)
            .title(" Event Timeline ")
            .bg(surface)
            .col(|ui| {
                if state.execution_events.is_empty() && state.raw_events.is_empty() {
                    ui.container().grow(1).center().col(|ui| {
                        ui.text("No events yet").fg(dim);
                    });
                } else {
                    ui.scrollable(&mut state.detail_scroll).grow(1).col(|ui| {
                        let events: Vec<ExecutionEvent> = if state.execution_events.is_empty() {
                            state
                                .raw_events
                                .iter()
                                .map(|e| ExecutionEvent {
                                    timestamp: e.timestamp.clone(),
                                    event_type: e.event_type.clone(),
                                    detail: e.data_preview.clone(),
                                    phase: None,
                                })
                                .collect()
                        } else {
                            state.execution_events.clone()
                        };

                        for (i, ev) in events.iter().rev().enumerate() {
                            let bg = if i % 2 == 0 { surface } else { surface_hover };
                            let (icon, type_color) = event_visual(
                                &ev.event_type,
                                success,
                                secondary,
                                error,
                                accent,
                                dim,
                            );

                            ui.container().bg(bg).px(1).py(0).col(|ui| {
                                ui.line(|ui| {
                                    ui.text(format!("{icon} ")).fg(type_color);
                                    ui.text(&ev.timestamp).fg(dim);
                                    ui.text("  ").fg(dim);
                                    ui.text(&ev.event_type).fg(type_color).bold();
                                });
                                if !ev.detail.is_empty() {
                                    ui.text_wrap(truncate(&ev.detail, 60)).fg(dim);
                                }
                            });
                        }
                    });
                }

                ui.container().bg(surface_hover).px(2).py(0).row(|ui| {
                    let count = if state.execution_events.is_empty() {
                        state.raw_events.len()
                    } else {
                        state.execution_events.len()
                    };
                    ui.line(|ui| {
                        ui.text(format!("{count}")).fg(text);
                        ui.text(" events").fg(dim);
                    });
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
            });
    });
}

fn event_visual(
    event_type: &str,
    success: slt::Color,
    secondary: slt::Color,
    error: slt::Color,
    accent: slt::Color,
    dim: slt::Color,
) -> (&'static str, slt::Color) {
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

fn drift_color(v: f64) -> slt::Color {
    if v < 0.1 {
        slt::Color::Green
    } else if v < 0.2 {
        slt::Color::Yellow
    } else {
        slt::Color::Red
    }
}
