use slt::{Border, Context};

use crate::state::*;

pub fn render(ui: &mut Context, state: &mut AppState) {
    let dim = ui.theme().text_dim;
    let accent = ui.theme().accent;
    let surface = ui.theme().surface;
    let surface_hover = ui.theme().surface_hover;
    let text = ui.theme().text;

    ui.container().grow(1).gap(0).col(|ui| {
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
            ui.text(format!("{} rows", state.log_table.rows.len()))
                .fg(dim);
        });

        let filter_text = state.log_filter.value.to_lowercase();

        if !filter_text.is_empty() {
            state.log_table.set_filter(&filter_text);
        } else {
            state.log_table.set_filter("");
        }

        if let Some(level) = state.log_level_filter {
            state.log_table.set_filter(level.label());
        }

        state.log_table.page_size = 50;

        ui.container()
            .grow(1)
            .mt(1)
            .border(Border::Single)
            .bg(surface)
            .col(|ui| {
                ui.container().grow(1).col(|ui| {
                    ui.table(&mut state.log_table);
                });
            });

        ui.container().bg(surface_hover).px(3).py(0).row(|ui| {
            let total_pages = state.log_table.total_pages();
            let page = if total_pages > 0 {
                total_pages.min(total_pages)
            } else {
                1
            };
            let total = state.log_table.total_pages().max(1);
            ui.text(format!("Page {page}/{total}")).fg(text);
            ui.text("  ").fg(dim);
            ui.text("Click header to sort").fg(dim);
            ui.text("  ").fg(dim);
            ui.text("PgUp/PgDn to navigate").fg(dim);
        });
    });
}
