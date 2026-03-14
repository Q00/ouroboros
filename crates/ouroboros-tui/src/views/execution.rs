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
                        .p(1)
                        .col(|ui| {
                            let events = if state.execution_events.is_empty() {
                                state
                                    .raw_events
                                    .iter()
                                    .map(|e| ExecutionEvent {
                                        timestamp: e.timestamp.clone(),
                                        event_type: e.event_type.clone(),
                                        detail: e.data_preview.clone(),
                                        phase: None,
                                    })
                                    .collect::<Vec<_>>()
                            } else {
                                state.execution_events.clone()
                            };

                            for (i, ev) in events.iter().rev().enumerate() {
                                let bg = if i % 2 == 0 { surface } else { surface_hover };
                                let type_color = if ev.event_type.contains("started") {
                                    success
                                } else if ev.event_type.contains("completed") {
                                    secondary
                                } else if ev.event_type.contains("failed")
                                    || ev.event_type.contains("error")
                                {
                                    error
                                } else if ev.event_type.contains("tool") {
                                    accent
                                } else {
                                    dim
                                };

                                ui.container().bg(bg).px(2).py(0).col(|ui| {
                                    ui.row(|ui| {
                                        ui.text(&ev.timestamp).fg(dim);
                                        ui.text("  ").fg(dim);
                                        ui.text(&ev.event_type).fg(type_color).bold();
                                        if let Some(ref p) = ev.phase {
                                            ui.text(format!("  [{}]", p)).fg(secondary);
                                        }
                                    });
                                    if !ev.detail.is_empty() {
                                        ui.text_wrap(&ev.detail).fg(dim);
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

        ui.container()
            .w_pct(35)
            .border(Border::Single)
            .title(" Phase Outputs ")
            .bg(surface)
            .col(|ui| {
                ui.container().grow(1).p(1).gap(1).col(|ui| {
                    for phase in Phase::ALL {
                        let (icon, color) = if phase.index() < state.current_phase.index() {
                            ("●", success)
                        } else if phase == state.current_phase {
                            ("◐", accent)
                        } else {
                            ("○", dim)
                        };

                        ui.container().bg(surface_hover).p(1).col(|ui| {
                            ui.row(|ui| {
                                ui.text(format!("{icon} {}", phase.label()))
                                    .fg(color)
                                    .bold();
                                ui.spacer();
                                if phase.index() < state.current_phase.index() {
                                    ui.text("done").fg(success);
                                } else if phase == state.current_phase {
                                    ui.text("active").fg(accent);
                                }
                            });
                        });
                    }

                    ui.separator();

                    ui.text("Active Tools").fg(text).bold();
                    if state.active_tools.is_empty() {
                        ui.text("  idle").fg(dim).italic();
                    } else {
                        for (ac_id, tool) in &state.active_tools {
                            ui.row(|ui| {
                                ui.text(format!("  {}", ac_id)).fg(secondary);
                                ui.text(format!(" → {} {}", tool.tool_name, tool.tool_detail))
                                    .fg(dim);
                            });
                        }
                    }

                    ui.spacer();

                    ui.text("Metrics").fg(text).bold();
                    ui.row(|ui| {
                        ui.text("  Drift  ").fg(dim);
                        ui.text(format!("{:.3}", state.drift.combined)).fg(text);
                    });
                    ui.row(|ui| {
                        ui.text("  Cost   ").fg(dim);
                        ui.text(format!("${:.2}", state.cost.total_cost_usd))
                            .fg(success);
                    });
                    ui.row(|ui| {
                        ui.text("  Tokens ").fg(dim);
                        ui.text(format!("{}", state.cost.total_tokens)).fg(text);
                    });
                });
            });
    });
}
