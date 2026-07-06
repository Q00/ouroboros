"""Greppable per-run interview trace artifact (A2 / run-metaharness plan).

This module is a **projection**, not a store. It reads from two durable
sources that already exist by the time an ``ooo auto`` run finalizes:

* the auto :class:`~ouroboros.auto.ledger.SeedDraftLedger` — question history,
  every decided contract field with its ``source`` / ``status`` /
  ``provenance`` (A1 / #1579), and the decision-origin histogram; and
* the :class:`~ouroboros.persistence.event_store.EventStore` — the interview
  event stream (ambiguity-score trajectory, lateral advisories, and the
  timeout / fallback / degraded lifecycle events the driver appends).

The projection writes plain, grep-able files under
``<cwd>/.ouroboros/traces/<run_id>/`` (``run_id`` == ``auto_session_id``):
one JSONL file per stream plus a human ``summary.md``. Empty streams omit
their file. Every JSONL line is self-describing via a ``type`` field and is
derived purely from persisted state + stored events (no wall-clock stamps),
so re-export is **byte-idempotent**: it overwrites deterministically.

Two entry points:

* :func:`export_interview_trace` — manual / A3-CLI entry that loads a past
  run from :class:`~ouroboros.auto.state.AutoStore` + the EventStore and
  projects it. Callable for any ``run_id`` whose state is persisted.
* :func:`best_effort_export_trace` — the pipeline-finalize hook. Wrapped so
  a projection failure only logs and never raises into the run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from ouroboros.auto.ledger import (
    DecisionProvenance,
    LedgerStatus,
    SeedDraftLedger,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ouroboros.auto.state import AutoPipelineState, AutoStore
    from ouroboros.persistence.event_store import EventStore

log = structlog.get_logger(__name__)

# --- stream filenames (stable public contract for A3 / grep tooling) --------
QUESTIONS_FILE = "questions.jsonl"
AMBIGUITY_FILE = "ambiguity.jsonl"
LATERAL_FILE = "lateral.jsonl"
DECISIONS_FILE = "decisions.jsonl"
FLAGS_FILE = "flags.jsonl"
OUTCOME_FILE = "outcome.json"
SUMMARY_FILE = "summary.md"

_ALL_STREAM_FILES: tuple[str, ...] = (
    QUESTIONS_FILE,
    AMBIGUITY_FILE,
    LATERAL_FILE,
    DECISIONS_FILE,
    FLAGS_FILE,
    OUTCOME_FILE,
    SUMMARY_FILE,
)

# Entry statuses that mean a decision was *superseded / unresolved* — the
# "rejected" half of promoted-vs-rejected. Mirrors ``ledger._INACTIVE_STATUSES``.
_REJECTED_STATUSES: frozenset[LedgerStatus] = frozenset(
    {LedgerStatus.WEAK, LedgerStatus.CONFLICTING, LedgerStatus.BLOCKED}
)

# Decision origins that must pass the A1 low-ambiguity gate before executing.
_GATED_PROVENANCE: frozenset[DecisionProvenance] = frozenset(
    {DecisionProvenance.MODEL_INFERRED, DecisionProvenance.TIMEOUT_DEFAULT}
)

# Event-type substrings that classify an event into the ``lateral`` stream.
_LATERAL_MARKERS: tuple[str, ...] = ("lateral", "unstuck", "stagnation")

# Event-type substrings that classify an event into the ``flags`` stream —
# timeout / fallback / degraded lifecycle signals worth an audit line.
_FLAG_MARKERS: tuple[str, ...] = (
    "timeout",
    "timed_out",
    "fallback",
    "failed",
    "deadline",
    "degraded",
    "safe_default",
    "closure",
    "blocked",
    "intent_guard",
    "parent_handoff",
    "nonclosure",
    "synthesis_failed",
    "persistence_probe",
    "unsafe",
)

# Cap projected string payloads so a runaway answer/decision cannot bloat a
# trace file. The ledger already truncates its own text; this bounds anything
# passed straight through from event payloads.
_MAX_TEXT = 2000


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _MAX_TEXT:
        return value[: _MAX_TEXT - 15].rstrip() + " ... (truncated)"
    return value


def _clip_data(data: dict[str, Any]) -> dict[str, Any]:
    return {key: _clip(val) for key, val in data.items()}


def _event_iso(event: Any) -> str | None:
    ts = getattr(event, "timestamp", None)
    if ts is None:
        return None
    isoformat = getattr(ts, "isoformat", None)
    return isoformat() if callable(isoformat) else str(ts)


async def _gather_events(
    event_store: EventStore | None,
    aggregate_ids: tuple[str, ...],
) -> list[Any]:
    """Return stored events for ``aggregate_ids`` ordered oldest-first.

    Best-effort: any per-aggregate query failure is swallowed so a partial
    event stream still produces a partial trace instead of no trace.
    """
    if event_store is None:
        return []
    collected: list[Any] = []
    for aggregate_id in aggregate_ids:
        if not aggregate_id:
            continue
        try:
            events = await event_store.query_events(aggregate_id=aggregate_id, limit=2000)
        except Exception as exc:  # pragma: no cover - defensive; store may be closed
            log.warning(
                "auto.trace_export.event_query_failed",
                aggregate_id=aggregate_id,
                error=str(exc),
            )
            continue
        collected.extend(events)
    # ``query_events`` returns newest-first; a stable ascending sort by
    # timestamp gives a readable, deterministic trajectory.
    collected.sort(key=lambda e: (getattr(e, "timestamp", None) is None, _event_iso(e) or ""))
    return collected


def _classify_event(event_type: str) -> str:
    """Route an event type to a stream: ``lateral`` | ``flag`` | ``other``.

    Ambiguity classification is handled separately (payload-driven) so an
    event carrying an ``ambiguity_score`` lands in the ambiguity stream
    regardless of its type name.
    """
    lowered = event_type.lower()
    if any(marker in lowered for marker in _LATERAL_MARKERS):
        return "lateral"
    if any(marker in lowered for marker in _FLAG_MARKERS):
        return "flag"
    return "other"


def _build_question_lines(
    ledger: SeedDraftLedger,
    events: list[Any],
) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for index, qa in enumerate(ledger.question_history, start=1):
        lines.append(
            {
                "type": "question",
                "round": index,
                "question": _clip(qa.get("question", "")),
                "answer": _clip(qa.get("answer", "")),
            }
        )
    # Enrich with event-store response records (the true round numbers, and the
    # only Q/A surface for cross-provider runs whose ledger history is sparse).
    for event in events:
        if getattr(event, "type", "") != "interview.response.recorded":
            continue
        data = getattr(event, "data", {}) or {}
        lines.append(
            {
                "type": "response_event",
                "round": data.get("round_number"),
                "question_preview": _clip(data.get("question_preview", "")),
                "response_preview": _clip(data.get("response_preview", "")),
                "at": _event_iso(event),
            }
        )
    return lines


def _build_ambiguity_lines(events: list[Any]) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for event in events:
        data = getattr(event, "data", {}) or {}
        if "ambiguity_score" not in data:
            continue
        lines.append(
            {
                "type": "ambiguity",
                "event": getattr(event, "type", ""),
                "at": _event_iso(event),
                "round": data.get("round_number"),
                "ambiguity_score": data.get("ambiguity_score"),
                "data": _clip_data(data),
            }
        )
    return lines


def _build_lateral_lines(
    state: AutoPipelineState,
    events: list[Any],
) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for event in events:
        data = getattr(event, "data", {}) or {}
        if "ambiguity_score" in data:
            # Already captured as an ambiguity trajectory point.
            continue
        if _classify_event(getattr(event, "type", "")) != "lateral":
            continue
        lines.append(
            {
                "type": "lateral",
                "event": getattr(event, "type", ""),
                "at": _event_iso(event),
                "data": _clip_data(data),
            }
        )
    if state.last_lateral_persona or state.last_lateral_text:
        lines.append(
            {
                "type": "lateral_final",
                "persona": state.last_lateral_persona,
                "approach_summary": _clip(state.last_lateral_approach_summary or ""),
                "text": _clip(state.last_lateral_text or ""),
            }
        )
    return lines


def _build_decision_lines(ledger: SeedDraftLedger) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for section_name, section in ledger.sections.items():
        for entry in section.entries:
            status = entry.status
            status_value = status.value if isinstance(status, LedgerStatus) else str(status)
            source = entry.source
            source_value = getattr(source, "value", None) or str(source)
            provenance = entry.effective_provenance
            promoted = status not in _REJECTED_STATUSES
            lines.append(
                {
                    "type": "decision",
                    "section": section_name,
                    "key": entry.key,
                    "value": _clip(entry.value),
                    "source": source_value,
                    "provenance": provenance.value,
                    "status": status_value,
                    "promoted": promoted,
                    "gated": provenance in _GATED_PROVENANCE,
                    "confidence": entry.confidence,
                }
            )
    return lines


def _build_flag_lines(
    state: AutoPipelineState,
    ledger: SeedDraftLedger,
    events: list[Any],
) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for event in events:
        data = getattr(event, "data", {}) or {}
        if "ambiguity_score" in data:
            continue
        event_type = getattr(event, "type", "")
        classification = _classify_event(event_type)
        if classification != "flag":
            continue
        lines.append(
            {
                "type": "event_flag",
                "event": event_type,
                "at": _event_iso(event),
                "data": _clip_data(data),
            }
        )
    # Derived state flags — the durable end-of-run signals that are not
    # individual events.
    lines.append({"type": "state_flag", "kind": "terminal_phase", "value": state.phase.value})
    if state.interview_closure_mode:
        lines.append(
            {
                "type": "state_flag",
                "kind": "interview_closure_mode",
                "value": state.interview_closure_mode,
            }
        )
    seed_meta = _seed_metadata(state)
    if seed_meta.get("degraded"):
        lines.append(
            {
                "type": "state_flag",
                "kind": "degraded_seed",
                "value": True,
                "recovery_reason": seed_meta.get("recovery_reason"),
                "unresolved_slots": list(seed_meta.get("unresolved_slots", ()) or ()),
            }
        )
    if state.last_error:
        lines.append(
            {
                "type": "state_flag",
                "kind": "blocker",
                "value": _clip(state.last_error),
                "stop_reason_code": state.last_error_code,
            }
        )
    open_gaps = ledger.open_gaps()
    if open_gaps:
        lines.append({"type": "state_flag", "kind": "open_gaps", "value": list(open_gaps)})
    return lines


def _seed_metadata(state: AutoPipelineState) -> dict[str, Any]:
    artifact = state.seed_artifact or {}
    if not isinstance(artifact, dict):
        return {}
    meta = artifact.get("metadata", {})
    return meta if isinstance(meta, dict) else {}


def _gate_findings(state: AutoPipelineState) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for finding in state.findings:
        if isinstance(finding, dict):
            findings.append(finding)
    return findings


def _build_outcome(
    state: AutoPipelineState,
    ledger: SeedDraftLedger,
    counts: dict[str, int],
) -> dict[str, Any]:
    seed_meta = _seed_metadata(state)
    findings = _gate_findings(state)
    return {
        "run_id": state.auto_session_id,
        "auto_session_id": state.auto_session_id,
        "status": state.phase.value,
        "phase": state.phase.value,
        "grade": state.last_grade,
        "seed_id": state.seed_id,
        "seed_path": state.seed_path,
        "seed_origin": state.seed_origin.value,
        "interview_session_id": state.interview_session_id,
        "interview_closure_mode": state.interview_closure_mode,
        "qa": {
            "verdict": state.last_qa_verdict,
            "score": state.last_qa_score,
            "passed": state.last_qa_passed,
            "differences": list(state.last_qa_differences),
            "suggestions": list(state.last_qa_suggestions),
        },
        "provenance_histogram": ledger.provenance_histogram(),
        "seed_decision_provenance": seed_meta.get("decision_provenance", {}),
        "gate_findings": findings,
        "unverified_provenance_findings": [
            f for f in findings if f.get("code") == "unverified_provenance"
        ],
        "assumptions": [_clip(v) for v in ledger.assumptions()],
        "non_goals": [_clip(v) for v in ledger.non_goals()],
        "open_gaps": ledger.open_gaps(),
        "blocker": _clip(state.last_error) if state.last_error else None,
        "stop_reason_code": state.last_error_code,
        "degraded": bool(seed_meta.get("degraded")),
        "counts": counts,
    }


def _build_summary_md(outcome: dict[str, Any], counts: dict[str, int]) -> str:
    lines: list[str] = []
    lines.append(f"# Interview trace — {outcome['run_id']}")
    lines.append("")
    lines.append(f"- Status: **{outcome['status']}**")
    lines.append(f"- Grade: {outcome['grade'] or 'n/a'}")
    lines.append(f"- Seed: {outcome['seed_id'] or 'n/a'} (origin: {outcome['seed_origin']})")
    if outcome["interview_closure_mode"]:
        lines.append(f"- Interview closure mode: {outcome['interview_closure_mode']}")
    qa = outcome["qa"]
    if qa["verdict"] is not None or qa["score"] is not None:
        lines.append(
            f"- Evaluate/QA: verdict={qa['verdict'] or 'n/a'} "
            f"score={qa['score'] if qa['score'] is not None else 'n/a'} "
            f"passed={qa['passed']}"
        )
    if outcome["blocker"]:
        lines.append(f"- Blocker: {outcome['blocker']}")
    lines.append("")
    lines.append("## Counts")
    lines.append("")
    lines.append(f"- Questions: {counts['questions']}")
    lines.append(
        f"- Decisions: {counts['decisions']} "
        f"(promoted {counts['promoted']}, rejected {counts['rejected']}, "
        f"gated {counts['gated']})"
    )
    lines.append(f"- Ambiguity points: {counts['ambiguity']}")
    lines.append(f"- Lateral records: {counts['lateral']}")
    lines.append(f"- Flags: {counts['flags']}")
    lines.append("")
    lines.append("## Decision provenance histogram")
    lines.append("")
    histogram = outcome["provenance_histogram"]
    if histogram:
        for provenance, count in histogram.items():
            lines.append(f"- {provenance}: {count}")
    else:
        lines.append("- (none)")
    unverified = outcome["unverified_provenance_findings"]
    if unverified:
        lines.append("")
        lines.append("## Unverified provenance findings (gate)")
        lines.append("")
        for finding in unverified:
            lines.append(f"- {finding.get('target', '')}: {finding.get('message', '')}")
    if outcome["open_gaps"]:
        lines.append("")
        lines.append("## Open gaps")
        lines.append("")
        for gap in outcome["open_gaps"]:
            lines.append(f"- {gap}")
    lines.append("")
    return "\n".join(lines)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    payload = "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows)
    path.write_text(payload + "\n", encoding="utf-8")


def _remove_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _resolve_out_root(state: AutoPipelineState, out_root: Path | None) -> Path:
    if out_root is not None:
        return Path(out_root)
    base = Path(state.cwd).expanduser() if state.cwd else Path.cwd()
    return base / ".ouroboros" / "traces" / state.auto_session_id


async def export_trace_from_state(
    state: AutoPipelineState,
    ledger: SeedDraftLedger,
    *,
    event_store: EventStore | None,
    out_root: Path | None = None,
) -> Path:
    """Project a completed run's state + ledger + events into trace files.

    Writes into ``out_root`` (default ``<cwd>/.ouroboros/traces/<run_id>/``)
    and returns that directory. Empty streams omit (and clear any stale) file.
    Idempotent: content is derived only from persisted state and stored
    events, so re-export is byte-identical.
    """
    aggregate_ids: tuple[str, ...] = tuple(
        agg for agg in (state.auto_session_id, state.interview_session_id) if agg
    )
    events = await _gather_events(event_store, aggregate_ids)

    question_lines = _build_question_lines(ledger, events)
    ambiguity_lines = _build_ambiguity_lines(events)
    lateral_lines = _build_lateral_lines(state, events)
    decision_lines = _build_decision_lines(ledger)
    flag_lines = _build_flag_lines(state, ledger, events)

    counts = {
        "questions": len(question_lines),
        "decisions": len(decision_lines),
        "promoted": sum(1 for line in decision_lines if line["promoted"]),
        "rejected": sum(1 for line in decision_lines if not line["promoted"]),
        "gated": sum(1 for line in decision_lines if line["gated"]),
        "ambiguity": len(ambiguity_lines),
        "lateral": len(lateral_lines),
        "flags": len(flag_lines),
    }
    outcome = _build_outcome(state, ledger, counts)
    summary_md = _build_summary_md(outcome, counts)

    out_dir = _resolve_out_root(state, out_root)
    out_dir.mkdir(parents=True, exist_ok=True)

    streams: dict[str, list[dict[str, Any]]] = {
        QUESTIONS_FILE: question_lines,
        AMBIGUITY_FILE: ambiguity_lines,
        LATERAL_FILE: lateral_lines,
        DECISIONS_FILE: decision_lines,
        FLAGS_FILE: flag_lines,
    }
    for filename, rows in streams.items():
        path = out_dir / filename
        if rows:
            _write_jsonl(path, rows)
        else:
            _remove_if_exists(path)

    (out_dir / OUTCOME_FILE).write_text(
        json.dumps(outcome, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (out_dir / SUMMARY_FILE).write_text(summary_md, encoding="utf-8")

    log.info(
        "auto.trace_export.written",
        auto_session_id=state.auto_session_id,
        out_dir=str(out_dir),
        **counts,
    )
    return out_dir


async def export_interview_trace(
    run_id: str,
    *,
    auto_store: AutoStore,
    event_store: EventStore | None,
    out_root: Path | None = None,
) -> Path | None:
    """Manual / A3-CLI entry: project a *past* run from persisted state.

    Loads ``run_id`` from ``auto_store`` (raising :class:`ValueError` for an
    unknown or corrupt session, matching :meth:`AutoStore.load`), reconstructs
    its ledger, and delegates to :func:`export_trace_from_state`. Returns the
    trace directory, or ``None`` if the persisted state carries no ledger and
    no goal to seed one from.
    """
    state = auto_store.load(run_id)
    ledger = (
        SeedDraftLedger.from_dict(state.ledger)
        if state.ledger
        else SeedDraftLedger.from_goal(state.goal)
    )
    return await export_trace_from_state(
        state,
        ledger,
        event_store=event_store,
        out_root=out_root,
    )


async def best_effort_export_trace(
    state: AutoPipelineState,
    ledger: SeedDraftLedger,
    *,
    event_store: EventStore | None,
    out_root: Path | None = None,
) -> Path | None:
    """Pipeline-finalize hook: export the trace, never raising into the run.

    Any failure — event-store query, filesystem, serialization — is logged and
    swallowed so trace generation cannot affect the auto pipeline's outcome.
    """
    try:
        return await export_trace_from_state(
            state,
            ledger,
            event_store=event_store,
            out_root=out_root,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort by contract
        log.warning(
            "auto.trace_export.failed",
            auto_session_id=getattr(state, "auto_session_id", None),
            error=str(exc),
        )
        return None
