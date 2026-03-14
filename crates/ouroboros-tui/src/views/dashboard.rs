use slt::{Border, Color, Context};

use crate::state::*;

pub fn render(ui: &mut Context, state: &mut AppState) {
    let primary = ui.theme().primary;
    let dim = ui.theme().text_dim;
    let surface = ui.theme().surface;
    let success = ui.theme().success;

    state.rebuild_tree_state();

    let surface_hover = ui.theme().surface_hover;

    ui.container().grow(1).gap(1).col(|ui| {
        ui.container()
            .border(Border::Single)
            .bg(surface_hover)
            .px(2)
            .py(0)
            .row(|ui| {
                for (i, phase) in Phase::ALL.iter().enumerate() {
                    if *phase == state.current_phase {
                        ui.text(format!(" ◆ {} ", phase.label())).bold().fg(success);
                    } else if phase.index() < state.current_phase.index() {
                        ui.text(format!(" ● {} ", phase.label())).fg(primary);
                    } else {
                        ui.text(format!(" ◇ {} ", phase.label())).fg(dim);
                    }
                    if i < 3 {
                        ui.text(" → ").fg(dim);
                    }
                }
                ui.spacer();
                if !state.drift.history.is_empty() {
                    ui.text("drift ").fg(dim);
                    ui.sparkline(&state.drift.history, 10);
                    ui.text(format!(" {:.2}", state.drift.combined))
                        .fg(drift_color(state.drift.combined));
                }
            });

        ui.container().grow(1).gap(0).row(|ui| {
            ui.container()
                .grow(3)
                .border(Border::Single)
                .title(" AC Tree ")
                .bg(surface)
                .col(|ui| {
                    if state.ac_root.is_empty() {
                        ui.container().grow(1).center().col(|ui| {
                            ui.text("No AC data").bold().fg(primary);
                            ui.text("Run ouroboros to generate").fg(dim);
                            ui.text("or use --mock for demo").fg(dim);
                        });
                    } else {
                        ui.container().grow(1).p(1).col(|ui| {
                            ui.tree(&mut state.ac_tree_state);
                        });
                        let flat_ids = state.flat_node_ids();
                        let sel: usize = state.ac_tree_state.selected;
                        if sel < flat_ids.len() {
                            state.selected_node_id = Some(flat_ids[sel].clone());
                        }
                    }
                });

            ui.container()
                .grow(2)
                .min_w(38)
                .ml(1)
                .border(Border::Single)
                .title(" Detail ")
                .bg(surface)
                .col(|ui| {
                    render_detail(ui, state);
                });
        });

        ui.container()
            .border(Border::Single)
            .bg(surface_hover)
            .px(2)
            .py(0)
            .row(|ui| {
                if !state.active_tools.is_empty() {
                    for (ac_id, tool) in &state.active_tools {
                        let short = ac_id.replace("sub_ac_", "S").replace("ac_", "AC");
                        ui.text(format!("◐ {short}")).fg(Color::Yellow);
                        ui.text(format!(" → {} ", tool.tool_detail)).fg(dim);
                    }
                } else {
                    ui.text("idle").fg(dim).italic();
                }
                ui.spacer();
                ui.text(format!("${:.2}", state.cost.total_cost_usd))
                    .fg(success);
                ui.text(format!("  {}k tok", state.cost.total_tokens / 1000))
                    .fg(dim);
            });
    });
}

fn render_detail(ui: &mut Context, state: &mut AppState) {
    let Some(node) = state.selected_node() else {
        let dim = ui.theme().text_dim;
        ui.container().grow(1).center().col(|ui| {
            ui.text("← Select a node").fg(dim);
        });
        return;
    };

    let accent = ui.theme().accent;
    let dim = ui.theme().text_dim;
    let text_c = ui.theme().text;
    let warn = ui.theme().warning;
    let nid = node.id.clone();
    let content = node.content.clone();
    let depth = node.depth;
    let atomic = node.is_atomic;
    let children = node.children.len();
    let status = node.status;

    let thinking = state.thinking.get(&nid).cloned();
    let tools = state.tool_history.get(&nid).cloned();
    let active = state.active_tools.get(&nid).cloned();

    ui.scrollable(&mut state.detail_scroll)
        .grow(1)
        .p(1)
        .gap(0)
        .col(|ui| {
            ui.line(|ui| {
                ui.text("ID        ").fg(dim);
                ui.text(&nid).fg(accent);
            });
            ui.line(|ui| {
                ui.text("Status    ").fg(dim);
                let sc = match status {
                    ACStatus::Completed => Color::Green,
                    ACStatus::Executing => Color::Yellow,
                    ACStatus::Failed | ACStatus::Blocked => Color::Red,
                    ACStatus::Pending => dim,
                    _ => Color::Cyan,
                };
                ui.text(format!("{} {}", status.icon(), status.label()))
                    .fg(sc)
                    .bold();
            });
            ui.line(|ui| {
                ui.text("Depth     ").fg(dim);
                ui.text(format!("{depth}")).fg(text_c);
            });
            ui.line(|ui| {
                ui.text("Atomic    ").fg(dim);
                if atomic {
                    ui.text("Yes").fg(Color::Green);
                } else {
                    ui.text("No").fg(dim);
                }
            });
            if children > 0 {
                ui.line(|ui| {
                    ui.text("Children  ").fg(dim);
                    ui.text(format!("{children}")).fg(accent);
                });
            }

            ui.separator();
            ui.line_wrap(|ui| {
                ui.text(&content).fg(text_c);
            });

            if let Some(ref t) = thinking {
                ui.separator();
                ui.text("Thinking:").fg(dim);
                ui.text_wrap(truncate(t, 300)).italic().fg(warn);
            }

            if let Some(ref h) = tools {
                ui.separator();
                ui.text("Tools:").fg(dim);
                for e in h.iter().rev().take(10) {
                    let (m, c) = if e.success {
                        ("✓", Color::Green)
                    } else {
                        ("✗", Color::Red)
                    };
                    ui.row(|ui| {
                        ui.text(format!(" {m} ")).fg(c);
                        ui.text(format!("{:<6}", e.tool_name)).fg(accent);
                        ui.text(&e.tool_detail);
                        ui.spacer();
                        ui.text(format!("{:.1}s", e.duration_secs)).fg(dim);
                    });
                }
            }

            if let Some(ref a) = active {
                ui.separator();
                ui.row(|ui| {
                    ui.text(" ● ").fg(Color::Yellow).bold();
                    ui.text(format!("{} {}", a.tool_name, a.tool_detail))
                        .fg(Color::Yellow);
                });
            }
        });
}

fn drift_color(v: f64) -> Color {
    if v < 0.1 {
        Color::Green
    } else if v < 0.2 {
        Color::Yellow
    } else {
        Color::Red
    }
}
