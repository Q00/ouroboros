use slt::{Border, Context};

use crate::state::*;

pub fn render(ui: &mut Context, state: &mut AppState) {
    let dim = ui.theme().text_dim;
    let surface = ui.theme().surface;
    let surface_hover = ui.theme().surface_hover;
    let text = ui.theme().text;
    let secondary = ui.theme().secondary;
    let accent = ui.theme().accent;
    let error = ui.theme().error;

    let filter_text = state.log_filter.value.to_lowercase();
    let filtered: Vec<&LogEntry> = state
        .logs
        .iter()
        .filter(|l| {
            if let Some(lv) = state.log_level_filter {
                if l.level != lv {
                    return false;
                }
            }
            if !filter_text.is_empty()
                && !l.message.to_lowercase().contains(&filter_text)
                && !l.source.to_lowercase().contains(&filter_text)
            {
                return false;
            }
            true
        })
        .collect();

    ui.container().grow(1).gap(0).col(|ui| {
        ui.container()
            .grow(1)
            .border(Border::Single)
            .bg(surface)
            .col(|ui| {
                if filtered.is_empty() {
                    ui.container().grow(1).center().col(|ui| {
                        ui.text("No log entries").fg(dim);
                    });
                } else {
                    ui.scrollable(&mut state.log_scroll).grow(1).col(|ui| {
                        for (i, e) in filtered.iter().rev().enumerate() {
                            let lc = match e.level {
                                LogLevel::Debug => dim,
                                LogLevel::Info => secondary,
                                LogLevel::Warning => accent,
                                LogLevel::Error => error,
                            };

                            let bg = if i % 2 == 0 { surface } else { surface_hover };
                            ui.container().bg(bg).px(2).py(0).row(|ui| {
                                ui.text(&e.timestamp).fg(dim);
                                ui.text("  ").fg(dim);
                                ui.text(format!("{:<4}", e.level.label())).fg(lc);
                                ui.text("  ").fg(dim);
                                ui.text(&e.message).fg(text);
                            });
                        }
                    });
                }
            });

        ui.container().bg(surface_hover).px(3).py(0).row(|ui| {
            ui.text("Filter ").fg(dim);
            ui.container().grow(1).mr(2).row(|ui| {
                ui.text_input(&mut state.log_filter);
            });
            for (label, level) in [
                ("All", None),
                ("Err", Some(LogLevel::Error)),
                ("Wrn", Some(LogLevel::Warning)),
                ("Inf", Some(LogLevel::Info)),
            ] {
                let active = state.log_level_filter == level;
                let resp = ui.container().px(1).py(0).row(|ui| {
                    if active {
                        ui.text(label).fg(accent).bold();
                    } else {
                        ui.text(label).fg(dim);
                    }
                });
                if resp.clicked {
                    state.log_level_filter = if active { None } else { level };
                }
            }
            ui.text("  ").fg(dim);
            ui.text(format!("{}/{}", filtered.len(), state.logs.len()))
                .fg(dim);
        });
    });
}
