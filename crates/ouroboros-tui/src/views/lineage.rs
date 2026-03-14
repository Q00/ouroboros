use slt::{Border, Color, Context};

use crate::state::*;

pub fn render(ui: &mut Context, state: &mut AppState) {
    let dim = ui.theme().text_dim;
    let primary = ui.theme().primary;
    let accent = ui.theme().accent;
    let surface = ui.theme().surface;
    let surface_hover = ui.theme().surface_hover;
    let secondary = ui.theme().secondary;

    ui.container().grow(1).gap(1).row(|ui| {
        ui.container()
            .w(38)
            .mr(1)
            .border(Border::Single)
            .title(" Lineages ")
            .bg(surface)
            .col(|ui| {
                if state.lineages.is_empty() {
                    ui.container().grow(1).center().col(|ui| {
                        ui.text("No lineages").bold().fg(primary);
                        ui.text("Run ouroboros evolve").fg(dim);
                    });
                } else {
                    ui.container().grow(1).p(1).col(|ui| {
                        ui.list(&mut state.lineage_list);
                        state.selected_lineage_idx = Some(state.lineage_list.selected);
                    });
                    ui.container().bg(surface_hover).px(2).py(0).row(|ui| {
                        ui.text(format!("{} lineages", state.lineages.len()))
                            .fg(dim);
                    });
                }
            });

        ui.container().grow(1).col(|ui| {
            if let Some(idx) = state.selected_lineage_idx {
                if let Some(lin) = state.lineages.get(idx) {
                    render_detail(
                        ui,
                        lin,
                        primary,
                        dim,
                        accent,
                        surface,
                        surface_hover,
                        secondary,
                    );
                } else {
                    placeholder(ui, "Invalid", primary, dim);
                }
            } else {
                placeholder(ui, "Select a lineage", primary, dim);
            }
        });
    });
}

fn placeholder(ui: &mut Context, msg: &str, primary: Color, dim: Color) {
    ui.container()
        .grow(1)
        .border(Border::Single)
        .center()
        .col(|ui| {
            ui.text(msg).bold().fg(primary);
            ui.text("← Click on the left panel").fg(dim);
        });
}

#[allow(clippy::too_many_arguments)]
fn render_detail(
    ui: &mut Context,
    lin: &Lineage,
    primary: Color,
    dim: Color,
    _accent: Color,
    surface: Color,
    surface_hover: Color,
    secondary: Color,
) {
    ui.container().grow(1).gap(1).col(|ui| {
        ui.container()
            .border(Border::Single)
            .title(" Info ")
            .bg(surface)
            .p(1)
            .gap(0)
            .col(|ui| {
                ui.row(|ui| {
                    ui.text("ID        ").fg(dim);
                    ui.text(&lin.id).fg(secondary).bold();
                });
                ui.row(|ui| {
                    ui.text("Goal      ").fg(dim);
                    ui.text_wrap(&lin.seed_goal);
                });
                ui.row(|ui| {
                    ui.text("Converged ").fg(dim);
                    if lin.converged {
                        ui.text("● Yes").fg(Color::Green).bold();
                    } else {
                        ui.text("◐ In Progress").fg(Color::Yellow);
                    }
                });
                ui.row(|ui| {
                    ui.text("Gens      ").fg(dim);
                    ui.text(format!("{}", lin.generations.len())).fg(primary);
                });
            });

        ui.container()
            .grow(1)
            .border(Border::Single)
            .title(" Generations ")
            .bg(surface)
            .p(1)
            .gap(1)
            .col(|ui| {
                for (i, gen) in lin.generations.iter().enumerate() {
                    let sc = match gen.status {
                        ACStatus::Completed => Color::Green,
                        ACStatus::Executing => Color::Yellow,
                        ACStatus::Failed => Color::Red,
                        _ => dim,
                    };
                    let bg = if i % 2 == 0 { surface } else { surface_hover };
                    ui.container().bg(bg).border(Border::Single).p(1).col(|ui| {
                        ui.row(|ui| {
                            ui.text(format!("Gen {}", gen.number)).bold().fg(primary);
                            ui.spacer();
                            ui.text(format!("{} {}", gen.status.icon(), gen.status.label()))
                                .fg(sc)
                                .bold();
                        });
                        if gen.score > 0.0 {
                            ui.row(|ui| {
                                ui.text("Score     ").fg(dim);
                                ui.progress(gen.score.min(1.0) as f64);
                                ui.text(format!(" {:.0}%", gen.score * 100.0)).fg(
                                    if gen.score >= 0.9 {
                                        Color::Green
                                    } else if gen.score >= 0.7 {
                                        Color::Yellow
                                    } else {
                                        Color::Red
                                    },
                                );
                            });
                            ui.row(|ui| {
                                ui.text("AC Pass   ").fg(dim);
                                ui.text(format!("{}/{}", gen.ac_pass_count, gen.ac_total))
                                    .fg(if gen.ac_pass_count == gen.ac_total {
                                        Color::Green
                                    } else {
                                        Color::Yellow
                                    });
                            });
                        }
                        ui.row(|ui| {
                            ui.text("Summary   ").fg(dim);
                            ui.text_wrap(&gen.summary);
                        });
                    });
                }
            });
    });
}
