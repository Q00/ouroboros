"""AC Compliance Dashboard — per-AC visibility across generations.

Formats AC pass/fail data from lineage history into human-readable
tables with trend analysis.

Three display modes:
- summary: Latest generation with trend dots (default)
- full: AC x Generation matrix
- ac: Single AC detailed timeline
"""

from __future__ import annotations

from ouroboros.core.acceptance import AcceptanceState
from ouroboros.core.lineage import ACAuthorityState, ACResult, OntologyLineage

# Unverified is not a rejection: no executed verification minted a verdict.
_GENERATION_LABELS: dict[AcceptanceState, str] = {
    AcceptanceState.APPROVED: "APPROVED",
    AcceptanceState.REJECTED: "REJECTED",
    AcceptanceState.UNVERIFIED: "NOT APPROVED (unverified)",
}

# An unresolved AC has no authoritative verdict; it is marked apart from a fail.
_AC_MARKS: dict[ACAuthorityState, str] = {
    ACAuthorityState.PASS: "P",
    ACAuthorityState.FAIL: "F",
    ACAuthorityState.UNRESOLVED: "U",
}

_ACHistory = list[tuple[int, ACAuthorityState]]


def _extract_ac_history(
    lineage: OntologyLineage,
) -> dict[int, _ACHistory]:
    """Extract per-AC authority-state history across all generations.

    Returns:
        Dict mapping ac_index to a list of (generation_number, authority_state).
    """
    history: dict[int, _ACHistory] = {}

    for gen in lineage.generations:
        es = gen.evaluation_summary
        if es is None or not es.ac_results:
            continue

        for ac in es.ac_results:
            if ac.ac_index not in history:
                history[ac.ac_index] = []
            history[ac.ac_index].append((gen.generation_number, ac.authority_state))

    return history


def _trend_dots(results: _ACHistory, max_dots: int = 5) -> str:
    """Render the authority-state trend as P/F/U letters.

    Returns e.g. "PPUFP (3/5)" where P = pass, F = fail, U = unresolved.
    """
    recent = results[-max_dots:]
    dots = "".join(_AC_MARKS[state] for _, state in recent)

    passed_count = sum(1 for _, state in recent if state is ACAuthorityState.PASS)
    return f"{dots} ({passed_count}/{len(recent)})"


def _classify_ac(results: _ACHistory) -> str:
    """Classify AC stability: stable, failing, unresolved, flaky, new.

    ``failing`` needs every recent verdict to be an authoritative fail, and
    ``unresolved`` means no recent generation produced an authoritative verdict.
    """
    if not results:
        return "new"

    recent_states = {state for _, state in results[-3:]}  # Last 3 generations

    if recent_states == {ACAuthorityState.PASS} and len(results) >= 2:
        return "stable"
    if recent_states == {ACAuthorityState.FAIL}:
        return "failing"
    if recent_states == {ACAuthorityState.UNRESOLVED}:
        return "unresolved"
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
        status = _GENERATION_LABELS[es.acceptance_state]
        lines.append(f"### Gen {latest_gen.generation_number} | Score: {score_str} | {status}")
    else:
        lines.append(f"### Gen {latest_gen.generation_number} | No evaluation")

    if not es or not es.ac_results:
        lines.append("")
        lines.append("No per-AC data available. Run with Phase 1+ evaluation.")
        return "\n".join(lines)

    lines.append("")

    # Classify and sort: failing, flaky, unresolved, new, stable
    ac_data: list[tuple[ACResult, str, _ACHistory]] = []
    for ac in es.ac_results:
        ac_history = history.get(ac.ac_index, [])
        classification = _classify_ac(ac_history)
        ac_data.append((ac, classification, ac_history))

    order = {"failing": 0, "flaky": 1, "unresolved": 2, "new": 3, "stable": 4}
    ac_data.sort(key=lambda x: (order.get(x[1], 99), x[0].ac_index))

    # Render table
    lines.append("| AC | Status | Description | Trend |")
    lines.append("|---:|--------|-------------|-------|")

    stable_count = 0
    for ac, classification, ac_history in ac_data:
        if classification == "stable" and len(ac_data) > 10:
            stable_count += 1
            continue

        status = ac.authority_state.upper()
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
                status = f"[{_AC_MARKS[results_by_gen[g]]}]"
            else:
                status = "[ ]"
            row += f"  {status:<5}"

        classification = _classify_ac(ac_results)
        row += f"  {classification}"
        lines.append(row)

    lines.append("")
    lines.append("P = pass, F = fail, U = unresolved (no authoritative verdict), [ ] = no data")
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
    passed_total = sum(1 for _, state in ac_history if state is ACAuthorityState.PASS)
    lines.append(
        f"**Classification**: {classification} | **Pass rate**: {passed_total}/{len(ac_history)}"
    )
    lines.append("")

    # Timeline
    lines.append("| Generation | Status | Evidence |")
    lines.append("|------------|--------|----------|")

    for gen_num, state in ac_history:
        status = state.upper()
        evidence = ""
        for gen in lineage.generations:
            if gen.generation_number == gen_num and gen.evaluation_summary:
                for ac in gen.evaluation_summary.ac_results:
                    if ac.ac_index == ac_index:
                        evidence = ac.evidence[:60] if ac.evidence else ""
                        break
        lines.append(f"| Gen {gen_num} | {status} | {evidence} |")

    return "\n".join(lines)
