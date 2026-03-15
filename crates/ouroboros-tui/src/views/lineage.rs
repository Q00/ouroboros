use slt::{Border, Color, Context};

use crate::state::*;

pub fn render(ui: &mut Context, state: &mut AppState) {
    let dim = ui.theme().text_dim;
    let primary = ui.theme().primary;
    let accent = ui.theme().accent;
    let surface = ui.theme().surface;
    let surface_hover = ui.theme().surface_hover;
    let secondary = ui.theme().secondary;
    let text = ui.theme().text;
    let success = ui.theme().success;
    let warning = ui.theme().warning;

    ui.container().grow(1).gap(0).row(|ui| {
        // ── Left panel: Lineage list ──────────────────────────────────────
        ui.container()
            .w(32)
            .mr(1)
            .border(Border::Single)
            .title(" Lineages ")
            .bg(surface)
            .col(|ui| {
                if state.lineages.is_empty() {
                    ui.container().grow(1).center().col(|ui| {
                        ui.text("No lineages").bold().fg(primary);
                        ui.text("").fg(dim);
                        ui.text("Run 'ooo evolve' to start").fg(dim);
                        ui.text("evolutionary development").fg(dim);
                    });
                } else {
                    ui.container().grow(1).p(1).col(|ui| {
                        ui.list(&mut state.lineage_list);
                        let new_idx = state.lineage_list.selected;
                        if state.selected_lineage_idx != Some(new_idx) {
                            state.selected_lineage_idx = Some(new_idx);
                            // Reset generation selection when lineage changes
                            state.selected_gen_idx = None;
                            state.lineage_detail_scroll = slt::ScrollState::new();
                            // Rebuild gen list for the newly selected lineage
                            rebuild_gen_list_by_idx(state, new_idx);
                        }
                    });
                    // Footer
                    ui.container().bg(surface_hover).px(2).py(0).row(|ui| {
                        ui.text(format!("{}", state.lineages.len())).fg(text).bold();
                        ui.text(" lineages").fg(dim);
                        let converged = state.lineages.iter().filter(|l| l.status == "converged").count();
                        if converged > 0 {
                            ui.text(format!("  {} converged", converged)).fg(Color::Green);
                        }
                        let active = state.lineages.iter().filter(|l| l.status == "active").count();
                        if active > 0 {
                            ui.text(format!("  {} active", active)).fg(Color::Yellow);
                        }
                    });
                }
            });

        // ── Right panel: Detail ───────────────────────────────────────────
        ui.container().grow(1).gap(1).col(|ui| {
            if let Some(idx) = state.selected_lineage_idx {
                if idx < state.lineages.len() {
                    render_lineage_detail(
                        ui, state, idx,
                        primary, dim, accent, surface, surface_hover,
                        secondary, text, success, warning,
                    );
                } else {
                    placeholder(ui, "Invalid selection", primary, dim);
                }
            } else {
                placeholder(ui, "Select a lineage", primary, dim);
            }
        });
    });
}

/// Rebuild generation list by lineage index, avoiding a full Lineage clone.
/// Builds the list items from a shared borrow, then assigns to state.
fn rebuild_gen_list_by_idx(state: &mut AppState, lin_idx: usize) {
    let Some(lin) = state.lineages.get(lin_idx) else { return };
    let prev_sel = state.selected_gen_idx;
    let gen_count = lin.generations.len();

    // Build list items from shared borrow (no clone of Lineage)
    let items: Vec<String> = lin.generations.iter().map(|g| {
        let icon = match g.status {
            ACStatus::Completed => {
                if g.final_approved { "●" } else { "○" }
            }
            ACStatus::Executing => "◐",
            ACStatus::Failed => "✖",
            _ => "○",
        };
        let score_str = if g.score > 0.0 {
            format!("{:.0}%", g.score * 100.0)
        } else {
            "--".to_string()
        };
        format!(
            "{} Gen {} | {} | {}/{}AC | {}",
            icon, g.number, score_str, g.ac_pass_count, g.ac_total, &g.summary,
        )
    }).collect();

    // Now mutate state (shared borrow of lineages is dropped)
    state.lineage_gen_list = slt::ListState::new(items);
    if let Some(prev) = prev_sel {
        let clamped = prev.min(gen_count.saturating_sub(1));
        state.lineage_gen_list.selected = clamped;
        state.selected_gen_idx = Some(clamped);
    } else if gen_count > 0 {
        let last = gen_count - 1;
        state.lineage_gen_list.selected = last;
        state.selected_gen_idx = Some(last);
    }
}

fn placeholder(ui: &mut Context, msg: &str, primary: Color, dim: Color) {
    ui.container()
        .grow(1)
        .border(Border::Single)
        .center()
        .col(|ui| {
            ui.text(msg).bold().fg(primary);
            ui.text("Use j/k to navigate").fg(dim);
        });
}

#[allow(clippy::too_many_arguments)]
fn render_lineage_detail(
    ui: &mut Context,
    state: &mut AppState,
    lin_idx: usize,
    primary: Color,
    dim: Color,
    accent: Color,
    surface: Color,
    surface_hover: Color,
    secondary: Color,
    text: Color,
    success: Color,
    warning: Color,
) {
    // Snapshot cheap header fields to avoid cloning the entire Lineage struct.
    // This drops the shared borrow before we need &mut state below.
    let (lin_id, lin_status, lin_seed_goal, lin_current_gen, gen_count,
         best_score, conv_sim, conv_reason, created_at) = {
        let lin = &state.lineages[lin_idx];
        (
            lin.id.clone(),
            lin.status.clone(),
            lin.seed_goal.clone(),
            lin.current_gen,
            lin.generations.len(),
            lin.generations.iter().map(|g| g.score).fold(0.0f32, f32::max),
            lin.convergence_similarity,
            lin.convergence_reason.clone(),
            lin.created_at.clone(),
        )
    };

    // ── Top: Lineage Info + Generation List ───────────────────────────
    ui.container()
        .border(Border::Single)
        .title(" Lineage Info ")
        .bg(surface)
        .p(1)
        .gap(0)
        .col(|ui| {
            // Row 1: ID + Status
            ui.row(|ui| {
                ui.text(&lin_id).fg(secondary).bold();
                ui.spacer();
                match lin_status.as_str() {
                    "converged" => {
                        ui.text("● CONVERGED").fg(Color::Green).bold();
                    }
                    "exhausted" => {
                        ui.text("✖ EXHAUSTED").fg(Color::Red).bold();
                    }
                    "aborted" => {
                        ui.text("⊘ ABORTED").fg(Color::Red).bold();
                    }
                    _ => {
                        ui.text(format!("◐ ACTIVE (gen {})", lin_current_gen)).fg(Color::Yellow).bold();
                    }
                }
            });
            // Row 2: Goal
            ui.text("Goal").fg(dim);
            ui.text_wrap(&lin_seed_goal);
            // Row 3: Metrics
            ui.row(|ui| {
                ui.text(format!("{} gens", gen_count)).fg(accent);
                if best_score > 0.0 {
                    ui.text("  best: ").fg(dim);
                    let error_c = Color::Rgb(235, 111, 146);
                    ui.text(format!("{:.0}%", best_score * 100.0)).fg(score_color(best_score, success, warning, error_c));
                }
                if conv_sim > 0.0 {
                    ui.text("  sim: ").fg(dim);
                    ui.text(format!("{:.0}%", conv_sim * 100.0)).fg(secondary);
                }
                if !conv_reason.is_empty() {
                    ui.text("  ").fg(dim);
                    ui.text_wrap(&conv_reason).fg(dim);
                }
                if !created_at.is_empty() {
                    ui.spacer();
                    ui.text(&created_at).fg(dim);
                }
            });
        });

    // ── Middle: Generation List (selectable) ──────────────────────────
    ui.container()
        .border(Border::Single)
        .title(" Generations ")
        .bg(surface)
        .col(|ui| {
            if gen_count == 0 {
                ui.container().grow(1).center().col(|ui| {
                    ui.text("No generations yet").fg(dim);
                });
            } else {
                // Rebuild gen list if count changed (handles new generations)
                if state.lineage_gen_list.items.len() != gen_count {
                    rebuild_gen_list_by_idx(state, lin_idx);
                }
                ui.container().p(1).col(|ui| {
                    ui.list(&mut state.lineage_gen_list);
                    state.selected_gen_idx = Some(state.lineage_gen_list.selected);
                });
            }
        });

    // ── Bottom: Selected Generation Detail ────────────────────────────
    // Clone only the selected generation, not the entire Lineage
    let gen_opt = state.selected_gen_idx.and_then(|gi| {
        state.lineages.get(lin_idx)
            .and_then(|lin| lin.generations.get(gi))
            .cloned()
    });

    if let Some(gen) = gen_opt {
        ui.container()
            .grow(1)
            .border(Border::Single)
            .title(format!(" Gen #{} Detail ", gen.number))
            .bg(surface)
            .col(|ui| {
                ui.scrollable(&mut state.lineage_detail_scroll)
                    .grow(1)
                    .p(1)
                    .gap(0)
                    .col(|ui| {
                        render_gen_detail(
                            ui, &gen, primary, dim, accent, surface_hover,
                            secondary, text, success, warning,
                        );
                    });
            });
    } else {
        ui.container()
            .grow(1)
            .border(Border::Single)
            .title(" Generation Detail ")
            .bg(surface)
            .center()
            .col(|ui| {
                ui.text("Select a generation above").fg(dim);
            });
    }
}

#[allow(clippy::too_many_arguments)]
fn render_gen_detail(
    ui: &mut Context,
    gen: &LineageGeneration,
    primary: Color,
    dim: Color,
    accent: Color,
    surface_hover: Color,
    secondary: Color,
    text: Color,
    success: Color,
    warning: Color,
) {
    // ── Double Diamond: Phase Progress ─────────────────────────────
    render_double_diamond(ui, &gen.phase, dim, accent, success);

    // ── Header: Status + Score ───────────────────────────────────
    let error = Color::Rgb(235, 111, 146); // Rose Pine error
    let sc = status_color(gen.status, success, warning, error, dim);
    ui.row(|ui| {
        ui.text(format!("{} {}", gen.status.icon(), gen.status.label())).fg(sc).bold();
        if gen.highest_stage > 0 {
            ui.text(format!("  Stage {}/3", gen.highest_stage)).fg(accent);
        }
        if gen.final_approved {
            ui.text("  APPROVED").fg(success).bold();
        }
        ui.spacer();
        if gen.score > 0.0 {
            ui.text(format!("Score: {:.0}%", gen.score * 100.0)).fg(score_color(gen.score, success, warning, error)).bold();
        }
        if !gen.seed_id.is_empty() {
            ui.text("  ").fg(dim);
            ui.text(&gen.seed_id).fg(dim);
        }
    });

    // ── Evaluation: AC Results + Drift ────────────────────────────
    if gen.ac_total > 0 || gen.drift_score > 0.0 {
        ui.text("").fg(dim);
        ui.container().bg(surface_hover).px(1).py(0).col(|ui| {
            ui.row(|ui| {
                ui.text("Evaluation").fg(primary).bold();
            });
            if gen.ac_total > 0 {
                ui.row(|ui| {
                    ui.text("  AC ").fg(dim);
                    let ac_color = if gen.ac_pass_count == gen.ac_total {
                        success
                    } else if gen.ac_pass_count > 0 {
                        warning
                    } else {
                        error
                    };
                    ui.text(format!("{}/{} passed", gen.ac_pass_count, gen.ac_total)).fg(ac_color);
                    // Visual bar
                    ui.text("  ").fg(dim);
                    let bar_len = 10;
                    let filled = if gen.ac_total > 0 {
                        (gen.ac_pass_count as usize * bar_len) / gen.ac_total as usize
                    } else {
                        0
                    };
                    let bar: String = (0..bar_len)
                        .map(|i| if i < filled { '█' } else { '░' })
                        .collect();
                    ui.text(bar).fg(ac_color);
                });
            }
            if gen.drift_score > 0.0 {
                ui.row(|ui| {
                    ui.text("  Drift ").fg(dim);
                    let dc = if gen.drift_score > 0.3 {
                        error
                    } else if gen.drift_score > 0.15 {
                        warning
                    } else {
                        success
                    };
                    ui.text(format!("{:.2}", gen.drift_score)).fg(dc);
                });
            }
        });
    }

    // ── Failure Reason ────────────────────────────────────────────
    if !gen.failure_reason.is_empty() {
        ui.text("").fg(dim);
        ui.row(|ui| {
            ui.text("Failure ").fg(error).bold();
            ui.text_wrap(&gen.failure_reason);
        });
    }

    // ── Summary ───────────────────────────────────────────────────
    if !gen.summary.is_empty() {
        ui.text("").fg(dim);
        ui.row(|ui| {
            ui.text("Summary ").fg(dim);
            ui.text_wrap(&gen.summary);
        });
    }

    // ── Ontology ──────────────────────────────────────────────────
    if !gen.ontology_name.is_empty() || !gen.ontology_fields.is_empty() {
        ui.text("").fg(dim);
        ui.container().bg(surface_hover).px(1).py(0).col(|ui| {
            ui.row(|ui| {
                ui.text("Ontology").fg(primary).bold();
                if !gen.ontology_name.is_empty() {
                    ui.text(format!("  {}", gen.ontology_name)).fg(accent);
                }
            });
            for field in &gen.ontology_fields {
                ui.row(|ui| {
                    let marker = if field.required { "*" } else { " " };
                    ui.text(format!(" {marker}")).fg(if field.required { warning } else { dim });
                    ui.text(&field.name).fg(text).bold();
                    ui.text(format!(" : {}", field.field_type)).fg(secondary);
                    if !field.description.is_empty() {
                        ui.text_wrap(format!("  {}", &field.description)).fg(dim);
                    }
                });
            }
        });
    }

    // ── Ontology Delta ────────────────────────────────────────────
    if !gen.ontology_delta.is_empty() {
        ui.text("").fg(dim);
        ui.row(|ui| {
            ui.text("Ontology Changes").fg(primary).bold();
            ui.text(format!(" ({})", gen.ontology_delta.len())).fg(dim);
        });
        for delta in &gen.ontology_delta {
            let (marker_color, icon) = if delta.starts_with('+') {
                (success, "+")
            } else if delta.starts_with('-') {
                (error, "-")
            } else if delta.starts_with('~') {
                (warning, "~")
            } else {
                (dim, " ")
            };
            ui.row(|ui| {
                ui.text(format!("  {icon} ")).fg(marker_color);
                // Skip the prefix character in the display text
                let display = if delta.len() > 1 { &delta[1..] } else { delta };
                ui.text(display.trim_start()).fg(text);
            });
        }
    }

    // ── Wonder Questions ──────────────────────────────────────────
    if !gen.wonder_questions.is_empty() {
        ui.text("").fg(dim);
        ui.row(|ui| {
            ui.text("Wonder Questions").fg(primary).bold();
            ui.text(format!(" ({})", gen.wonder_questions.len())).fg(dim);
        });
        for (i, q) in gen.wonder_questions.iter().enumerate() {
            ui.text(format!("  {}. ", i + 1)).fg(accent);
            ui.text_wrap(format!("     {q}"));
        }
    }

    // ── Execution Output ──────────────────────────────────────────
    if !gen.execution_output.is_empty() {
        ui.text("").fg(dim);
        ui.separator();
        ui.text("Execution Output").fg(dim).bold();
        for line in gen.execution_output.lines() {
            if line.is_empty() {
                ui.text("").fg(dim);
            } else {
                ui.text_wrap(line).fg(success);
            }
        }
    }

    // ── Timestamp ─────────────────────────────────────────────────
    if !gen.created_at.is_empty() {
        ui.text("").fg(dim);
        ui.row(|ui| {
            ui.spacer();
            ui.text(&gen.created_at).fg(dim);
        });
    }
}

/// Map a GenerationPhase string to a 0-4 index for the evolutionary Double Diamond.
///
/// First Diamond (Problem Space):  Wonder(0) → Reflect(1) → Seed(2)
/// Second Diamond (Solution Space): Execute(3) → Evaluate(4)
fn gen_phase_to_index(phase: &str) -> usize {
    let p = phase.to_lowercase();
    if p.contains("evaluat") {
        4
    } else if p.contains("execut") {
        3
    } else if p.contains("seed") {
        2
    } else if p.contains("reflect") {
        1
    } else {
        // "wondering", empty, or any initial/unknown phase
        0
    }
}

/// Is this phase terminal (all done)?
fn is_gen_phase_done(phase: &str) -> bool {
    let p = phase.to_lowercase();
    p.contains("completed") || p.contains("converged") || p.contains("failed") || p.contains("cancelled")
}

/// Get color for a phase slot based on current progress.
fn gen_phase_color(slot: usize, current: usize, done: bool, dim: Color, accent: Color, success: Color) -> Color {
    if done {
        success
    } else if slot < current {
        success
    } else if slot == current {
        accent
    } else {
        dim
    }
}

/// Render the evolutionary Double Diamond visualization (3 lines) for a generation.
///
/// ```text
/// ◇━━━━◆━━━━◇  ◇━━━━━━━━━◇
/// WONDER REFLECT SEED  EXECUTE EVAL
///   diverge→converge   diverge→converge
/// ```
fn render_double_diamond(ui: &mut Context, phase: &str, dim: Color, accent: Color, success: Color) {
    let idx = gen_phase_to_index(phase);
    let done = is_gen_phase_done(phase);

    ui.container().px(1).py(0).col(|ui| {
        // Line 1: Diamond shapes
        // Diamond 1 spans phases 0-2 (Wonder → Reflect → Seed)
        // Diamond 2 spans phases 3-4 (Execute → Evaluate)
        let d1_color = if done || idx > 2 { success } else if idx <= 2 { accent } else { dim };
        let d2_color = if done { success } else if idx >= 3 { accent } else { dim };

        ui.line(|ui| {
            // First diamond: Wonder ◇━━◆━━◇ Seed
            let c0 = gen_phase_color(0, idx, done, dim, accent, success);
            let c1 = gen_phase_color(1, idx, done, dim, accent, success);
            let c2 = gen_phase_color(2, idx, done, dim, accent, success);
            ui.text("◇").fg(c0);
            ui.text("━━━━").fg(d1_color);
            ui.text("◆").fg(c1);
            ui.text("━━━━").fg(d1_color);
            ui.text("◇").fg(c2);

            ui.text("  ").fg(dim);

            // Second diamond: Execute ◇━━━━━━━◇ Evaluate
            let c3 = gen_phase_color(3, idx, done, dim, accent, success);
            let c4 = gen_phase_color(4, idx, done, dim, accent, success);
            ui.text("◇").fg(c3);
            ui.text("━━━━━━━━━").fg(d2_color);
            ui.text("◇").fg(c4);
        });

        // Line 2: Phase labels (aligned under diamond key points)
        ui.line(|ui| {
            // Labels: WONDER(6) REFLECT(7) SEED(4)  EXECUTE(7) EVAL(4)
            let labels = ["WONDER", "REFLECT", "SEED", "EXECUTE", "EVAL"];
            let gaps = [" ", " ", "  ", " "];
            for (i, label) in labels.iter().enumerate() {
                let c = gen_phase_color(i, idx, done, dim, accent, success);
                if !done && i == idx {
                    ui.text(*label).fg(c).bold();
                } else {
                    ui.text(*label).fg(c);
                }
                if i < 4 {
                    ui.text(gaps[i]).fg(dim);
                }
            }
        });

        // Line 3: Sub-labels
        ui.line(|ui| {
            ui.text(" diverge→converge").fg(d1_color);
            ui.text("  ").fg(dim);
            ui.text("diverge→converge").fg(d2_color);
        });
    });
}

fn score_color(score: f32, success: Color, warning: Color, error: Color) -> Color {
    if score >= 0.9 {
        success
    } else if score >= 0.7 {
        warning
    } else {
        error
    }
}

fn status_color(status: ACStatus, success: Color, warning: Color, error: Color, dim: Color) -> Color {
    match status {
        ACStatus::Completed => success,
        ACStatus::Executing => warning,
        ACStatus::Failed => error,
        _ => dim,
    }
}
