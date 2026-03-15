use slt::{Border, Color, Context, KeyCode};

use crate::state::*;

pub fn render(ui: &mut Context, state: &mut AppState) {
    let primary = ui.theme().primary;
    let dim = ui.theme().text_dim;
    let surface = ui.theme().surface;
    let success = ui.theme().success;
    let warning = ui.theme().warning;
    let accent = ui.theme().accent;
    let text = ui.theme().text;
    let error_c = Color::Rgb(235, 111, 146); // Rose Pine love

    // NOTE: rebuild_tree_state() is called in populate_state_from_events(),
    // NOT here. Calling it every frame would destroy expand/collapse state.

    let surface_hover = ui.theme().surface_hover;

    ui.container().grow(1).gap(1).col(|ui| {
        // Phase Outputs — compact horizontal phase cards
        ui.container()
            .bg(surface_hover)
            .px(1)
            .py(0)
            .row(|ui| {
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

                    ui.container()
                        .grow(1)
                        .border(Border::Single)
                        .bg(if active { surface_hover } else { surface })
                        .px(1)
                        .py(0)
                        .col(|ui| {
                            ui.line(|ui| {
                                ui.text(format!("{icon} ")).fg(color);
                                ui.text(phase.label()).fg(color).bold();
                                if done {
                                    ui.text(" ✓").fg(success);
                                } else if active {
                                    ui.text(" …").fg(accent);
                                }
                            });
                            let phase_key = phase.label().to_lowercase();
                            if let Some(outputs) = state.phase_outputs.get(&phase_key) {
                                for line in outputs.iter().rev().take(3) {
                                    ui.line(|ui| {
                                        ui.text("• ").fg(dim);
                                        ui.text_wrap(line).fg(text);
                                    });
                                }
                                if outputs.len() > 3 {
                                    ui.text(format!("  +{} more", outputs.len() - 3)).fg(dim);
                                }
                            }
                        });
                }
                // AC progress + metrics at the end
                ui.container().min_w(18).px(1).py(0).col(|ui| {
                    let (done, total) = state.ac_progress();
                    if total > 0 {
                        let ratio = done as f64 / total as f64;
                        ui.row(|ui| {
                            ui.text(format!("[{done}/{total} AC]")).fg(text).bold();
                        });
                        ui.progress(ratio);
                    }
                    ui.row(|ui| {
                        ui.text(&state.elapsed).fg(dim);
                    });
                    ui.row(|ui| {
                        ui.text(format!("${:.2}", state.cost.total_cost_usd)).fg(success);
                        ui.text(format!(" {}k", state.cost.total_tokens / 1000)).fg(dim);
                    });
                });
            });

        // Main content: AC Tree + Detail
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
                            ui.text("").fg(dim);
                            ui.text("Run ouroboros to generate ACs").fg(dim);
                            ui.text("or use --mock for demo data").fg(dim);
                        });
                    } else {
                        // Split borrows to avoid cloning the entire entries vec every frame
                        let tree_height = state.ac_tree_entries.len().min(30).max(5);
                        let selected_idx = state.ac_tree_list.selected;
                        let entries = &state.ac_tree_entries;
                        let list = &mut state.ac_tree_list;

                        ui.container().grow(1).p(1).col(|ui| {
                            ui.virtual_list(list, tree_height, |ui, idx| {
                                if let Some(entry) = entries.get(idx) {
                                    let is_selected = idx == selected_idx;
                                    let status_color = match entry.status {
                                        ACStatus::Completed => success,
                                        ACStatus::Executing => warning,
                                        ACStatus::Failed | ACStatus::Blocked => error_c,
                                        _ => dim,
                                    };

                                    ui.row(|ui| {
                                        // Selection cursor
                                        if is_selected {
                                            ui.text("▸ ").fg(primary).bold();
                                        } else {
                                            ui.text("  ").fg(dim);
                                        }
                                        // Tree connectors (dimmed)
                                        if !entry.prefix.is_empty() {
                                            ui.text(&entry.prefix).fg(dim);
                                        }
                                        // Expand/collapse icon
                                        ui.text(entry.toggle_icon).fg(text);
                                        // Status icon (colored by status)
                                        ui.text(format!("{} ", entry.status.icon())).fg(status_color);
                                        // Content text
                                        if is_selected {
                                            ui.text_wrap(&entry.content).fg(primary).bold();
                                        } else {
                                            ui.text_wrap(&entry.content).fg(text);
                                        }
                                        // Active tool indicator
                                        if let Some(ref tool) = entry.active_tool {
                                            ui.text(format!("  {tool}")).fg(accent);
                                        }
                                    });
                                }
                            });
                        });

                        // Handle Enter/Space/Right for expand/collapse (AFTER virtual_list consumes j/k)
                        if ui.key_code(KeyCode::Enter) || ui.key_code(KeyCode::Char(' ')) || ui.key_code(KeyCode::Right) {
                            let sel = state.ac_tree_list.selected;
                            if let Some(entry) = state.ac_tree_entries.get(sel) {
                                if entry.has_children {
                                    let nid = entry.node_id.clone();
                                    state.toggle_ac_node(&nid);
                                }
                            }
                        }
                        // Left arrow: collapse current node (or go to parent)
                        if ui.key_code(KeyCode::Left) {
                            let sel = state.ac_tree_list.selected;
                            if let Some(entry) = state.ac_tree_entries.get(sel) {
                                let nid = entry.node_id.clone();
                                if entry.has_children && state.ac_expanded.contains(&nid) {
                                    // Collapse current node
                                    state.toggle_ac_node(&nid);
                                }
                                // If already collapsed or leaf, could navigate to parent
                                // (skip for now — would require parent tracking)
                            }
                        }

                        // Update selection tracking
                        let sel = state.ac_tree_list.selected;
                        if let Some(entry) = state.ac_tree_entries.get(sel) {
                            state.selected_node_id = Some(entry.node_id.clone());
                        }
                        state.check_selection_changed();
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

        // Live Activity Bar
        ui.container()
            .bg(surface_hover)
            .px(2)
            .py(0)
            .row(|ui| {
                if !state.active_tools.is_empty() {
                    ui.text("LIVE").fg(warning).bold();
                    ui.text("  ").fg(dim);
                    for (ac_id, tool) in &state.active_tools {
                        let short = ac_id.replace("sub_ac_", "S").replace("ac_", "AC");
                        ui.text(format!("{short}")).fg(accent);
                        ui.text(format!(" {} {} ", tool.tool_name, &tool.tool_detail)).fg(dim);
                        ui.text("│ ").fg(dim);
                    }
                } else {
                    ui.text("No active tool calls").fg(dim).italic();
                }
            });
    });
}

fn render_detail(ui: &mut Context, state: &mut AppState) {
    let Some(node) = state.selected_node() else {
        let dim = ui.theme().text_dim;
        let primary = ui.theme().primary;
        ui.container().grow(1).center().col(|ui| {
            ui.text("Select a node").fg(primary);
            ui.text("Use ↑↓ to navigate the tree").fg(dim);
        });
        return;
    };

    let accent = ui.theme().accent;
    let dim = ui.theme().text_dim;
    let text_c = ui.theme().text;
    let warn = ui.theme().warning;
    let success = ui.theme().success;
    let secondary = ui.theme().secondary;
    let error_c = Color::Rgb(235, 111, 146);
    let nid = node.id.clone();
    let content = node.content.clone();
    let depth = node.depth;
    let atomic = node.is_atomic;
    let status = node.status;

    // Sub-AC summary
    let children_count = node.children.len();
    let children_completed = node.children.iter().filter(|c| c.status == ACStatus::Completed).count();

    let thinking = state.thinking.get(&nid).cloned();
    let tools = state.tool_history.get(&nid).cloned();
    let active = state.active_tools.get(&nid).cloned();

    ui.scrollable(&mut state.detail_scroll)
        .grow(1)
        .p(1)
        .gap(0)
        .col(|ui| {
            // Node header — ID + status on one prominent line
            ui.row(|ui| {
                let sc = match status {
                    ACStatus::Completed => success,
                    ACStatus::Executing => warn,
                    ACStatus::Failed | ACStatus::Blocked => error_c,
                    ACStatus::Pending => dim,
                    _ => accent,
                };
                ui.text(format!("{} ", status.icon())).fg(sc);
                ui.text(&nid).fg(accent).bold();
                ui.spacer();
                ui.text(status.label()).fg(sc).bold();
            });

            // Content — the most important info
            ui.text("").fg(dim);
            ui.text_wrap(&content).fg(text_c);

            // Metadata
            ui.text("").fg(dim);
            ui.row(|ui| {
                ui.text("depth ").fg(dim);
                ui.text(format!("{depth}")).fg(text_c);
                ui.text("  ").fg(dim);
                if atomic {
                    ui.text("◆ atomic").fg(success);
                } else {
                    ui.text("◇ composite").fg(secondary);
                }
                if children_count > 0 {
                    ui.text("  ").fg(dim);
                    let prog_color = if children_completed == children_count {
                        success
                    } else if children_completed > 0 {
                        warn
                    } else {
                        dim
                    };
                    ui.text(format!("{children_completed}/{children_count} sub-ACs")).fg(prog_color);
                }
            });

            // Active tool — most urgent info, show first
            if let Some(ref a) = active {
                ui.text("").fg(dim);
                ui.row(|ui| {
                    ui.text("● RUNNING ").fg(warn).bold();
                    ui.text(&a.tool_name).fg(accent).bold();
                    ui.text(format!(" {}", a.tool_detail)).fg(text_c);
                });
            }

            // Thinking
            if let Some(ref t) = thinking {
                ui.text("").fg(dim);
                ui.separator();
                ui.text("Thinking").fg(dim).bold();
                ui.text_wrap(t).italic().fg(warn);
            }

            // Tool history
            if let Some(ref h) = tools {
                ui.text("").fg(dim);
                ui.separator();
                ui.row(|ui| {
                    ui.text("Recent Tools").fg(dim).bold();
                    ui.text(format!(" ({})", h.len())).fg(dim);
                });
                for e in h.iter().rev().take(8) {
                    let (m, c) = if e.success {
                        ("✓", success)
                    } else {
                        ("✗", error_c)
                    };
                    ui.row(|ui| {
                        ui.text(format!(" {m} ")).fg(c);
                        ui.text(&e.tool_name).fg(accent);
                        ui.text(" ").fg(dim);
                        ui.text_wrap(&e.tool_detail).fg(text_c);
                        ui.spacer();
                        ui.text(format!("{:.1}s", e.duration_secs)).fg(secondary);
                    });
                }
                if h.len() > 8 {
                    ui.text(format!("  +{} more", h.len() - 8)).fg(dim);
                }
            }
        });
}
