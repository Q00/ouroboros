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
        ui.container()
            .grow(1)
            .border(Border::Single)
            .title(" Timeline ")
            .bg(surface)
            .col(|ui| {
                if state.execution_events.is_empty() && state.raw_events.is_empty() {
                    ui.container().grow(1).center().col(|ui| {
                        ui.text("No execution events").fg(dim);
                        ui.text("Start a workflow to see timeline").fg(dim);
                    });
                } else {
                    ui.scrollable(&mut state.execution_scroll)
                        .grow(1)
                        .col(|ui| {
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

                                ui.container().bg(bg).px(2).py(0).row(|ui| {
                                    ui.text(format!(" {icon} ")).fg(type_color);
                                    ui.text(&ev.timestamp).fg(dim);
                                    ui.text("  ").fg(dim);
                                    ui.text(&ev.event_type).fg(type_color).bold();
                                    if let Some(ref p) = ev.phase {
                                        ui.text(format!(" [{p}]")).fg(secondary);
                                    }
                                    if !ev.detail.is_empty() {
                                        ui.spacer();
                                        ui.text(truncate(&ev.detail, 40)).fg(dim);
                                    }
                                });
                            }
                        });
                }

                ui.container().bg(surface_hover).px(3).py(0).row(|ui| {
                    let count = if state.execution_events.is_empty() {
                        state.raw_events.len()
                    } else {
                        state.execution_events.len()
                    };
                    ui.text(format!("{count} events")).fg(dim);
                });
            });

        ui.container().w_pct(35).gap(1).col(|ui| {
            ui.container()
                .border(Border::Single)
                .title(" Phase ")
                .bg(surface)
                .p(1)
                .gap(0)
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

                        ui.row(|ui| {
                            ui.text(format!(" {icon} {:<10}", phase.label())).fg(color);
                            if done {
                                ui.text("  ✓").fg(success);
                            } else if active {
                                ui.text("  ...").fg(accent);
                            }
                        });
                    }

                    ui.separator();

                    let (done, total) = state.ac_progress();
                    if total > 0 {
                        ui.progress(done as f64 / total as f64);
                        ui.text(format!("  {done}/{total} AC")).fg(text);
                    }
                });

            ui.container()
                .border(Border::Single)
                .title(" Tools ")
                .bg(surface)
                .p(1)
                .gap(0)
                .col(|ui| {
                    if state.active_tools.is_empty() {
                        ui.text("  idle").fg(dim).italic();
                    } else {
                        for (ac_id, tool) in &state.active_tools {
                            ui.row(|ui| {
                                ui.text(" ● ").fg(accent);
                                ui.text(ac_id).fg(secondary);
                                ui.text(format!(" {} ", tool.tool_name)).fg(text);
                                ui.text(&tool.tool_detail).fg(dim);
                            });
                        }
                    }

                    let total_tools: usize = state.tool_history.values().map(|v| v.len()).sum();
                    if total_tools > 0 {
                        ui.separator();
                        ui.text(format!("  {total_tools} total calls")).fg(dim);
                    }
                });

            ui.container()
                .grow(1)
                .border(Border::Single)
                .title(" Metrics ")
                .bg(surface)
                .p(1)
                .gap(0)
                .col(|ui| {
                    ui.row(|ui| {
                        ui.text("  Drift    ").fg(dim);
                        ui.text(format!("{:.3}", state.drift.combined))
                            .fg(drift_color(state.drift.combined));
                    });
                    if !state.drift.history.is_empty() {
                        ui.row(|ui| {
                            ui.text("           ").fg(dim);
                            ui.sparkline(&state.drift.history, 18);
                        });
                    }

                    ui.separator();

                    ui.row(|ui| {
                        ui.text("  Cost     ").fg(dim);
                        ui.text(format!("${:.2}", state.cost.total_cost_usd))
                            .fg(success);
                    });
                    ui.row(|ui| {
                        ui.text("  Tokens   ").fg(dim);
                        ui.text(format!("{}", state.cost.total_tokens)).fg(text);
                    });
                    if !state.cost.history.is_empty() {
                        ui.row(|ui| {
                            ui.text("           ").fg(dim);
                            ui.sparkline(&state.cost.history, 18);
                        });
                    }

                    ui.separator();

                    ui.row(|ui| {
                        ui.text("  Iter     ").fg(dim);
                        ui.text(format!("{}", state.iteration)).fg(primary);
                    });
                    ui.row(|ui| {
                        ui.text("  Elapsed  ").fg(dim);
                        ui.text(&state.elapsed).fg(text);
                    });
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
    if event_type.contains("started") || event_type.contains("started") {
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
