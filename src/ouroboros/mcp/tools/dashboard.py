"""AC Compliance Dashboard — per-AC visibility across generations.

Formats AC pass/fail data from lineage history into human-readable
tables with trend analysis.

Three display modes:
- summary: Latest generation with trend dots (default)
- full: AC x Generation matrix
- ac: Single AC detailed timeline
"""

from __future__ import annotations

from ouroboros.core.lineage import ACResult, OntologyLineage


def _extract_ac_history(
    lineage: OntologyLineage,
) -> dict[int, list[tuple[int, bool | None]]]:
    """Extract per-AC pass/fail history across all generations.

    Returns:
        Dict mapping ac_index → list of (generation_number, passed_or_None).
    """
    history: dict[int, list[tuple[int, bool | None]]] = {}

    for gen in lineage.generations:
        es = gen.evaluation_summary
        if es is None or not es.ac_results:
            continue

        for ac in es.ac_results:
            if ac.ac_index not in history:
                history[ac.ac_index] = []
            history[ac.ac_index].append((gen.generation_number, ac.passed))

    return history


def _trend_dots(results: list[tuple[int, bool | None]], max_dots: int = 5) -> str:
    """Render pass/fail trend as P/F letters.

    Returns e.g. "PPPFP (4/5)" where P = pass, F = fail.
    """
    recent = results[-max_dots:]
    dots = ""
    for _, passed in recent:
        dots += "P" if passed else "F"

    passed_count = sum(1 for _, p in recent if p)
    return f"{dots} ({passed_count}/{len(recent)})"


def _classify_ac(results: list[tuple[int, bool | None]]) -> str:
    """Classify AC stability: stable, flaky, failing, new."""
    if not results:
        return "new"

    recent = results[-3:]  # Last 3 generations
    all_pass = all(p for _, p in recent)
    all_fail = all(not p for _, p in recent)

    if all_pass and len(results) >= 2:
        return "stable"
    elif all_fail:
        return "failing"
    else:
        return "flaky"


def format_summary(lineage: OntologyLineage) -> str:
    """Format summary mode: latest generation with trends.

    Attention-first ordering: failing/flaky ACs at top, stable collapsed.
    """
    if not lineage.generations:
        return "No generations in lineage."

    latest_gen = lineage.generations[-1]
    es = latest_gen.evaluation_summary
    history = _extract_ac_history(lineage)

    lines = [
        f"## AC Dashboard: {lineage.lineage_id}",
        "",
    ]

    if es:
        score_str = f"{es.score:.2f}" if es.score is not None else "N/A"
        status = "APPROVED" if es.final_approved else "REJECTED"
        lines.append(f"### Gen {latest_gen.generation_number} — Score: {score_str} | {status}")
    else:
        lines.append(f"### Gen {latest_gen.generation_number} — No evaluation")

    if not es or not es.ac_results:
        lines.append("")
        lines.append("No per-AC data available. Run with Phase 1+ evaluation.")
        return "\n".join(lines)

    lines.append("")

    # Classify and sort: failing → flaky → stable
    ac_data: list[tuple[ACResult, str, list[tuple[int, bool | None]]]] = []
    for ac in es.ac_results:
        ac_history = history.get(ac.ac_index, [])
        classification = _classify_ac(ac_history)
        ac_data.append((ac, classification, ac_history))

    order = {"failing": 0, "flaky": 1, "new": 2, "stable": 3}
    ac_data.sort(key=lambda x: (order.get(x[1], 99), x[0].ac_index))

    # Render table
    lines.append("| AC | Status | Description | Trend |")
    lines.append("|---:|--------|-------------|-------|")

    stable_count = 0
    for ac, classification, ac_history in ac_data:
        if classification == "stable" and len(ac_data) > 10:
            stable_count += 1
            continue

        status = "PASS" if ac.passed else "FAIL"
        desc = ac.ac_content[:50] + ("..." if len(ac.ac_content) > 50 else "")
        trend = _trend_dots(ac_history) if ac_history else "-"
        lines.append(f"| {ac.ac_index + 1} | {status} | {desc} | {trend} |")

    if stable_count > 0:
        lines.append(f"| | | *...{stable_count} stable ACs (all passing)* | |")

    return "\n".join(lines)


def format_full(lineage: OntologyLineage) -> str:
    """Format full mode: AC x Generation matrix."""
    if not lineage.generations:
        return "No generations in lineage."

    history = _extract_ac_history(lineage)
    if not history:
        return "No per-AC data available across generations."

    # Get all generation numbers that have AC data
    gen_numbers: list[int] = []
    for gen in lineage.generations:
        if gen.evaluation_summary and gen.evaluation_summary.ac_results:
            gen_numbers.append(gen.generation_number)

    if not gen_numbers:
        return "No per-AC data available across generations."

    lines = [
        f"## AC Dashboard (Full): {lineage.lineage_id}",
        "",
    ]

    # Header
    gen_header = "".join(f"  Gen{g:<3}" for g in gen_numbers)
    lines.append(f"{'AC':<8}{gen_header}  Status")
    lines.append("-" * (8 + len(gen_numbers) * 7 + 10))

    # Build per-AC rows
    all_indices = sorted(history.keys())
    for ac_idx in all_indices:
        ac_results = history[ac_idx]
        results_by_gen = dict(ac_results)

        row = f"AC {ac_idx + 1:<4}"
        for g in gen_numbers:
            if g in results_by_gen:
                status = "[P]" if results_by_gen[g] else "[F]"
            else:
                status = "[ ]"
            row += f"  {status:<5}"

        classification = _classify_ac(ac_results)
        row += f"  {classification}"
        lines.append(row)

    return "\n".join(lines)


def format_single_ac(
    lineage: OntologyLineage,
    ac_index: int,
) -> str:
    """Format single AC mode: detailed timeline for one AC."""
    history = _extract_ac_history(lineage)
    ac_history = history.get(ac_index, [])

    lines = [
        f"## AC {ac_index + 1} History: {lineage.lineage_id}",
        "",
    ]

    if not ac_history:
        lines.append(f"No data for AC {ac_index + 1}.")
        return "\n".join(lines)

    # Get AC text from latest generation
    ac_text = ""
    for gen in reversed(lineage.generations):
        if gen.evaluation_summary:
            for ac in gen.evaluation_summary.ac_results:
                if ac.ac_index == ac_index:
                    ac_text = ac.ac_content
                    break
        if ac_text:
            break

    if ac_text:
        lines.append(f"**AC**: {ac_text}")
        lines.append("")

    classification = _classify_ac(ac_history)
    passed_total = sum(1 for _, p in ac_history if p)
    lines.append(
        f"**Classification**: {classification} | **Pass rate**: {passed_total}/{len(ac_history)}"
    )
    lines.append("")

    # Timeline
    lines.append("| Generation | Status | Evidence |")
    lines.append("|------------|--------|----------|")

    for gen_num, passed in ac_history:
        status = "PASS" if passed else "FAIL"
        evidence = ""
        for gen in lineage.generations:
            if gen.generation_number == gen_num and gen.evaluation_summary:
                for ac in gen.evaluation_summary.ac_results:
                    if ac.ac_index == ac_index:
                        evidence = ac.evidence[:60] if ac.evidence else ""
                        break
        lines.append(f"| Gen {gen_num} | {status} | {evidence} |")

    return "\n".join(lines)
