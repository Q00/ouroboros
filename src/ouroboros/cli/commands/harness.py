"""Experience-store CLI (A3 / run-metaharness plan).

A thin, read-only surface over the greppable interview traces that A2
(:mod:`ouroboros.auto.trace_export`) projects under
``<cwd>/.ouroboros/traces/<run_id>/``. Each run directory holds one JSONL
file per stream (``questions``/``ambiguity``/``lateral``/``decisions``/
``flags``), an ``outcome.json`` of key metrics, and a human ``summary.md``.

This module never *writes* run state. Its only mutation is a best-effort,
on-demand **projection**: when a requested run has persisted auto state but
no trace on disk yet, it calls
:func:`ouroboros.auto.trace_export.export_interview_trace` to materialize the
same deterministic files the pipeline finalize hook would have written. All
rendered output is plain deterministic text (no wall-clock stamps — any
timestamp shown is derived from persisted state or stored events).

Subcommands::

    ouroboros harness list
    ouroboros harness show <run_id>
    ouroboros harness trace <run_id> --grep <pattern> [--stream <name>]
    ouroboros harness diff <a> <b>
    ouroboros harness frontier --metric <name>
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
from pathlib import Path
import re
from typing import Annotated, Any

import typer

from ouroboros.auto.state import AutoStore
from ouroboros.auto.trace_export import (
    AMBIGUITY_FILE,
    DECISIONS_FILE,
    FLAGS_FILE,
    LATERAL_FILE,
    OUTCOME_FILE,
    QUESTIONS_FILE,
    SUMMARY_FILE,
    export_interview_trace,
)
from ouroboros.cli.formatters.panels import print_error

app = typer.Typer(
    name="harness",
    help="Inspect A2 interview traces (list/show/trace/diff/frontier).",
    no_args_is_help=True,
)

# Exit codes (mirror the `status run` convention: 0 ok, 2 unknown, 64 malformed).
EXIT_OK = 0
EXIT_UNKNOWN = 2
EXIT_MALFORMED = 64

# The five greppable JSONL streams, in a stable render/scan order.
_STREAM_FILES: dict[str, str] = {
    "questions": QUESTIONS_FILE,
    "ambiguity": AMBIGUITY_FILE,
    "lateral": LATERAL_FILE,
    "decisions": DECISIONS_FILE,
    "flags": FLAGS_FILE,
}


# --------------------------------------------------------------------------- #
# Path / store resolution                                                     #
# --------------------------------------------------------------------------- #
def _default_traces_root() -> Path:
    """Return ``<cwd>/.ouroboros/traces`` — where the pipeline writes traces."""
    return Path.cwd() / ".ouroboros" / "traces"


def _default_db_path() -> Path:
    """Return the canonical EventStore path used by the running CLI."""
    return Path.home() / ".ouroboros" / "ouroboros.db"


def _auto_store(data_root: Path | None) -> AutoStore:
    return AutoStore(root=data_root) if data_root is not None else AutoStore()


def _trace_dir(traces_root: Path, run_id: str) -> Path:
    return traces_root / run_id


def _is_exported(trace_dir: Path) -> bool:
    """A trace is materialized once its ``outcome.json`` exists."""
    return (trace_dir / OUTCOME_FILE).is_file()


# --------------------------------------------------------------------------- #
# Read helpers (deterministic, no wall-clock)                                 #
# --------------------------------------------------------------------------- #
def _read_outcome(trace_dir: Path) -> dict[str, Any]:
    path = trace_dir / OUTCOME_FILE
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows


def _final_ambiguity(trace_dir: Path) -> float | None:
    """Return the last recorded ambiguity score, or ``None`` if absent.

    Derived from the ``ambiguity.jsonl`` trajectory (already time-ordered by
    the exporter), so it is data-derived and deterministic.
    """
    last: float | None = None
    for row in _iter_jsonl(trace_dir / AMBIGUITY_FILE):
        score = row.get("ambiguity_score")
        if isinstance(score, (int, float)):
            last = float(score)
    return last


def _raw_auto_state(auto_store: AutoStore, run_id: str) -> dict[str, Any]:
    """Light raw read of a persisted auto-state file (no schema validation).

    Used only for cheap columns (timestamp/phase/grade) in ``list`` so we do
    not pay ``AutoPipelineState.from_dict`` validation per row and so pruned or
    partially-written state never aborts the listing.
    """
    try:
        path = auto_store.path_for(run_id)
    except ValueError:
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _derived_timestamp(trace_dir: Path, raw_state: dict[str, Any]) -> str:
    """A data-derived timestamp for a run (never wall-clock-at-render).

    Priority: persisted ``created_at`` on the auto state, else the earliest
    ``at`` stamp found across the stored trace streams, else ``n/a``.
    """
    created = raw_state.get("created_at")
    if isinstance(created, str) and created:
        return created
    stamps: list[str] = []
    for stream_file in _STREAM_FILES.values():
        for row in _iter_jsonl(trace_dir / stream_file):
            at = row.get("at")
            if isinstance(at, str) and at:
                stamps.append(at)
    return min(stamps) if stamps else "n/a"


def _histogram_digest(histogram: dict[str, Any]) -> str:
    """Compact, deterministic one-line digest of a provenance histogram."""
    if not histogram:
        return "-"
    parts = [f"{key}={histogram[key]}" for key in sorted(histogram)]
    return ",".join(parts)


def _stream_counts(trace_dir: Path) -> dict[str, int]:
    """Per-stream line counts straight from disk (independent of outcome)."""
    return {
        name: len(_iter_jsonl(trace_dir / filename)) for name, filename in _STREAM_FILES.items()
    }


# --------------------------------------------------------------------------- #
# On-demand export fallback                                                    #
# --------------------------------------------------------------------------- #
async def _open_event_store(db_path: Path):
    """Open the EventStore read-only, or ``None`` if the DB is absent.

    Read-only is enforced at the SQLite layer so this recovery/inspection
    surface can never mutate the durable event log.
    """
    if not db_path.exists():
        return None
    from ouroboros.persistence.event_store import EventStore

    store = EventStore(f"sqlite+aiosqlite:///{db_path}", read_only=True)
    try:
        await store.initialize()
    except Exception:
        try:
            await store.close()
        finally:
            raise
    return store


async def _export_on_demand(
    run_id: str,
    *,
    traces_root: Path,
    auto_store: AutoStore,
    db_path: Path,
) -> Path | None:
    """Project ``run_id`` into ``traces_root/run_id`` from persisted state.

    Returns the trace directory, or ``None`` if the run is unknown to the auto
    store (no persisted state to project from).
    """
    event_store = await _open_event_store(db_path)
    try:
        return await export_interview_trace(
            run_id,
            auto_store=auto_store,
            event_store=event_store,
            out_root=_trace_dir(traces_root, run_id),
        )
    except ValueError:
        # Unknown / corrupt auto session — nothing to project.
        return None
    finally:
        if event_store is not None:
            await event_store.close()


def _ensure_trace(
    run_id: str,
    *,
    traces_root: Path,
    auto_store: AutoStore,
    db_path: Path,
) -> Path | None:
    """Resolve a run's trace dir, projecting on-demand when not yet exported.

    Filesystem first: an already-exported trace is returned untouched. When
    missing, fall back to A2's :func:`export_interview_trace`. Returns ``None``
    when the run can be neither found on disk nor projected from state.
    """
    trace_dir = _trace_dir(traces_root, run_id)
    if _is_exported(trace_dir):
        return trace_dir
    return asyncio.run(
        _export_on_demand(
            run_id,
            traces_root=traces_root,
            auto_store=auto_store,
            db_path=db_path,
        )
    )


# --------------------------------------------------------------------------- #
# Text rendering (plain, deterministic — no Rich/ANSI)                         #
# --------------------------------------------------------------------------- #
def _render_table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    lines.append("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return "\n".join(lines)


def _fmt_ambiguity(value: float | None) -> str:
    return f"{value:.3f}" if value is not None else "n/a"


# --------------------------------------------------------------------------- #
# Shared CLI options                                                           #
# --------------------------------------------------------------------------- #
TracesRootOpt = Annotated[
    Path | None,
    typer.Option(
        "--traces-root",
        help="Trace directory root (default: <cwd>/.ouroboros/traces).",
    ),
]
DataRootOpt = Annotated[
    Path | None,
    typer.Option(
        "--data-root",
        help="Auto-store data root (default: ~/.ouroboros/data).",
    ),
]
DbPathOpt = Annotated[
    Path | None,
    typer.Option(
        "--db-path",
        help="EventStore path for on-demand export (default: ~/.ouroboros/ouroboros.db).",
    ),
]


def _resolve_roots(
    traces_root: Path | None,
    data_root: Path | None,
    db_path: Path | None,
) -> tuple[Path, AutoStore, Path]:
    root = traces_root if traces_root is not None else _default_traces_root()
    store = _auto_store(data_root)
    db = db_path if db_path is not None else _default_db_path()
    return root, store, db


# --------------------------------------------------------------------------- #
# Commands                                                                     #
# --------------------------------------------------------------------------- #
@app.command(name="list")
def list_runs(
    traces_root: TracesRootOpt = None,
    data_root: DataRootOpt = None,
) -> None:
    """List runs that have traces, plus persisted runs not yet exported.

    Columns are all data-derived: run_id, a persisted timestamp (never
    wall-clock at render), export status, terminal phase / grade, final
    ambiguity, and a provenance-histogram digest.
    """
    root = traces_root if traces_root is not None else _default_traces_root()
    store = _auto_store(data_root)

    exported: set[str] = set()
    if root.is_dir():
        for child in root.iterdir():
            if child.is_dir() and _is_exported(child):
                exported.add(child.name)

    available: set[str] = set()
    if store.root.is_dir():
        for child in store.root.glob("auto_*.json"):
            available.add(child.stem)

    run_ids = sorted(exported | available)
    if not run_ids:
        typer.echo("No traces found.")
        return

    rows: list[list[str]] = []
    for run_id in run_ids:
        trace_dir = _trace_dir(root, run_id)
        raw_state = _raw_auto_state(store, run_id)
        is_exported = run_id in exported
        if is_exported:
            outcome = _read_outcome(trace_dir)
            phase = str(outcome.get("phase") or raw_state.get("phase") or "n/a")
            grade = str(outcome.get("grade") or raw_state.get("last_grade") or "n/a")
            ambiguity = _fmt_ambiguity(_final_ambiguity(trace_dir))
            digest = _histogram_digest(outcome.get("provenance_histogram") or {})
            status = "exported"
        else:
            phase = str(raw_state.get("phase") or "n/a")
            grade = str(raw_state.get("last_grade") or "n/a")
            ambiguity = "-"
            digest = "-"
            status = "not exported"
        timestamp = _derived_timestamp(trace_dir, raw_state)
        rows.append([run_id, timestamp, status, phase, grade, ambiguity, digest])

    headers = ["run_id", "timestamp", "status", "phase", "grade", "ambiguity", "provenance"]
    typer.echo(_render_table(headers, rows))


@app.command()
def show(
    run_id: Annotated[str, typer.Argument(help="Run id (auto_<hex>) to render.")],
    traces_root: TracesRootOpt = None,
    data_root: DataRootOpt = None,
    db_path: DbPathOpt = None,
) -> None:
    """Render a run's ``summary.md`` plus key ``outcome.json`` fields.

    Exports the trace on-demand when it is missing on disk.
    """
    root, store, db = _resolve_roots(traces_root, data_root, db_path)
    trace_dir = _ensure_trace(run_id, traces_root=root, auto_store=store, db_path=db)
    if trace_dir is None:
        print_error(f"Run not found (no trace on disk and no persisted state): {run_id}")
        raise typer.Exit(EXIT_UNKNOWN)

    summary_path = trace_dir / SUMMARY_FILE
    if summary_path.is_file():
        typer.echo(summary_path.read_text(encoding="utf-8").rstrip())
    else:
        typer.echo(f"# Interview trace — {run_id}\n(summary.md unavailable)")

    outcome = _read_outcome(trace_dir)
    if not outcome:
        return

    counts = outcome.get("counts") or {}
    qa = outcome.get("qa") or {}
    typer.echo("")
    typer.echo("## Outcome")
    typer.echo("")
    fields: list[tuple[str, Any]] = [
        ("run_id", outcome.get("run_id") or run_id),
        ("status", outcome.get("status")),
        ("phase", outcome.get("phase")),
        ("grade", outcome.get("grade")),
        ("seed_id", outcome.get("seed_id")),
        ("seed_origin", outcome.get("seed_origin")),
        ("degraded", outcome.get("degraded")),
        ("final_ambiguity", _fmt_ambiguity(_final_ambiguity(trace_dir))),
        ("qa_verdict", qa.get("verdict")),
        ("qa_score", qa.get("score")),
        ("qa_passed", qa.get("passed")),
        ("questions", counts.get("questions")),
        ("decisions", counts.get("decisions")),
        ("promoted", counts.get("promoted")),
        ("rejected", counts.get("rejected")),
        ("gated", counts.get("gated")),
        ("ambiguity_points", counts.get("ambiguity")),
        ("lateral", counts.get("lateral")),
        ("flags", counts.get("flags")),
        (
            "unverified_provenance",
            len(outcome.get("unverified_provenance_findings") or []),
        ),
        ("blocker", outcome.get("blocker")),
        ("stop_reason_code", outcome.get("stop_reason_code")),
    ]
    for label, value in fields:
        typer.echo(f"- {label}: {value if value is not None else 'n/a'}")

    digest = _histogram_digest(outcome.get("provenance_histogram") or {})
    typer.echo(f"- provenance_histogram: {digest}")


@app.command()
def trace(
    run_id: Annotated[str, typer.Argument(help="Run id (auto_<hex>) to grep.")],
    grep: Annotated[
        str,
        typer.Option("--grep", help="Regex (fallback: literal) to match stream lines."),
    ],
    stream: Annotated[
        str | None,
        typer.Option(
            "--stream",
            help="Restrict to one stream: questions|ambiguity|lateral|decisions|flags.",
        ),
    ] = None,
    traces_root: TracesRootOpt = None,
    data_root: DataRootOpt = None,
    db_path: DbPathOpt = None,
) -> None:
    """Grep across a run's JSONL streams; print ``file:line:`` matches.

    Deterministic: streams are scanned in a fixed order and lines in file
    order. Exports the trace on-demand when missing.
    """
    if stream is not None and stream not in _STREAM_FILES:
        print_error(f"Unknown stream: {stream}. Available: {', '.join(_STREAM_FILES)}.")
        raise typer.Exit(EXIT_MALFORMED)

    try:
        pattern = re.compile(grep)
    except re.error:
        # Invalid regex → treat the pattern as a literal substring.
        pattern = re.compile(re.escape(grep))

    root, store, db = _resolve_roots(traces_root, data_root, db_path)
    trace_dir = _ensure_trace(run_id, traces_root=root, auto_store=store, db_path=db)
    if trace_dir is None:
        print_error(f"Run not found (no trace on disk and no persisted state): {run_id}")
        raise typer.Exit(EXIT_UNKNOWN)

    stream_names = [stream] if stream is not None else list(_STREAM_FILES)
    match_count = 0
    for name in stream_names:
        path = trace_dir / _STREAM_FILES[name]
        if not path.is_file():
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if pattern.search(line):
                typer.echo(f"{_STREAM_FILES[name]}:{lineno}: {line}")
                match_count += 1

    if match_count == 0:
        typer.echo(f"No matches for /{grep}/ in {run_id}.")


@app.command()
def diff(
    run_a: Annotated[str, typer.Argument(metavar="A", help="First run id.")],
    run_b: Annotated[str, typer.Argument(metavar="B", help="Second run id.")],
    traces_root: TracesRootOpt = None,
    data_root: DataRootOpt = None,
    db_path: DbPathOpt = None,
) -> None:
    """Side-by-side of two runs' key metrics + per-stream line deltas.

    Deterministic text. Exports either trace on-demand when missing.
    """
    root, store, db = _resolve_roots(traces_root, data_root, db_path)

    dirs: dict[str, Path] = {}
    for label, run_id in (("A", run_a), ("B", run_b)):
        trace_dir = _ensure_trace(run_id, traces_root=root, auto_store=store, db_path=db)
        if trace_dir is None:
            print_error(f"Run {label} not found (no trace, no persisted state): {run_id}")
            raise typer.Exit(EXIT_UNKNOWN)
        dirs[label] = trace_dir

    outcome_a = _read_outcome(dirs["A"])
    outcome_b = _read_outcome(dirs["B"])
    counts_a = outcome_a.get("counts") or {}
    counts_b = outcome_b.get("counts") or {}

    def _num(value: Any) -> str:
        return str(value) if value is not None else "n/a"

    def _delta(a: Any, b: Any) -> str:
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            d = b - a
            return f"{d:+g}"
        return "-"

    amb_a = _final_ambiguity(dirs["A"])
    amb_b = _final_ambiguity(dirs["B"])

    rows: list[list[str]] = [
        ["grade", _num(outcome_a.get("grade")), _num(outcome_b.get("grade")), "-"],
        [
            "final_ambiguity",
            _fmt_ambiguity(amb_a),
            _fmt_ambiguity(amb_b),
            _delta(amb_a, amb_b),
        ],
    ]
    for key in (
        "questions",
        "decisions",
        "promoted",
        "rejected",
        "gated",
        "ambiguity",
        "lateral",
        "flags",
    ):
        va = counts_a.get(key)
        vb = counts_b.get(key)
        rows.append([key, _num(va), _num(vb), _delta(va, vb)])

    hist_a = _histogram_digest(outcome_a.get("provenance_histogram") or {})
    hist_b = _histogram_digest(outcome_b.get("provenance_histogram") or {})
    rows.append(["provenance_histogram", hist_a, hist_b, "-"])

    typer.echo(f"# diff A={run_a} B={run_b}")
    typer.echo("")
    typer.echo(_render_table(["metric", "A", "B", "delta (B-A)"], rows))

    # Per-stream line-count deltas straight from disk (independent of outcome).
    disk_a = _stream_counts(dirs["A"])
    disk_b = _stream_counts(dirs["B"])
    stream_rows = [
        [name, str(disk_a[name]), str(disk_b[name]), _delta(disk_a[name], disk_b[name])]
        for name in _STREAM_FILES
    ]
    typer.echo("")
    typer.echo("## Stream line counts")
    typer.echo("")
    typer.echo(_render_table(["stream", "A", "B", "delta (B-A)"], stream_rows))


# --------------------------------------------------------------------------- #
# frontier metrics                                                             #
# --------------------------------------------------------------------------- #
# Each metric extracts a comparable value from a trace directory. ``ascending``
# controls rank order (True = smaller is better/ranked first). Runs whose value
# is ``None`` sort last regardless of direction.
_FrontierExtractor = Callable[[Path, dict[str, Any]], float | None]


def _count_metric(key: str) -> _FrontierExtractor:
    def _extract(_trace_dir: Path, outcome: dict[str, Any]) -> float | None:
        value = (outcome.get("counts") or {}).get(key)
        return float(value) if isinstance(value, (int, float)) else None

    return _extract


def _unverified_metric(_trace_dir: Path, outcome: dict[str, Any]) -> float | None:
    findings = outcome.get("unverified_provenance_findings")
    if isinstance(findings, list):
        return float(len(findings))
    return None


def _ambiguity_metric(trace_dir: Path, _outcome: dict[str, Any]) -> float | None:
    return _final_ambiguity(trace_dir)


_FRONTIER_METRICS: dict[str, tuple[_FrontierExtractor, bool, str]] = {
    "final_ambiguity": (_ambiguity_metric, True, "last ambiguity score (lower is better)"),
    "rounds": (_count_metric("questions"), True, "question rounds (fewer to converge)"),
    "decisions": (_count_metric("decisions"), False, "total ledger decisions"),
    "promoted": (_count_metric("promoted"), False, "promoted decisions"),
    "rejected": (_count_metric("rejected"), True, "rejected/superseded decisions"),
    "gated": (_count_metric("gated"), True, "low-ambiguity-gated decisions"),
    "ambiguity_points": (_count_metric("ambiguity"), False, "ambiguity trajectory points"),
    "lateral": (_count_metric("lateral"), True, "lateral/unstuck records"),
    "flags": (_count_metric("flags"), True, "timeout/fallback/degraded flags"),
    "unverified_provenance": (_unverified_metric, True, "unverified-provenance gate findings"),
}


def _available_metrics_text() -> str:
    lines = ["Available metrics:"]
    for name in sorted(_FRONTIER_METRICS):
        _, ascending, description = _FRONTIER_METRICS[name]
        order = "asc" if ascending else "desc"
        lines.append(f"  {name} ({order}) — {description}")
    return "\n".join(lines)


@app.command()
def frontier(
    metric: Annotated[
        str,
        typer.Option("--metric", help="Metric to rank traced runs by."),
    ],
    traces_root: TracesRootOpt = None,
) -> None:
    """Rank all traced runs by a metric from ``outcome.json``.

    Ranks only runs already exported on disk (does not bulk-project every
    persisted run). Unknown metric → clean error listing the available ones.
    """
    if metric not in _FRONTIER_METRICS:
        print_error(f"Unknown metric: {metric}")
        typer.echo(_available_metrics_text())
        raise typer.Exit(EXIT_UNKNOWN)

    extractor, ascending, _description = _FRONTIER_METRICS[metric]
    root = traces_root if traces_root is not None else _default_traces_root()

    traced: list[str] = []
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if child.is_dir() and _is_exported(child):
                traced.append(child.name)

    if not traced:
        typer.echo("No traced runs found.")
        return

    scored: list[tuple[str, float | None, str, str]] = []
    for run_id in traced:
        trace_dir = _trace_dir(root, run_id)
        outcome = _read_outcome(trace_dir)
        value = extractor(trace_dir, outcome)
        grade = str(outcome.get("grade") or "n/a")
        phase = str(outcome.get("phase") or "n/a")
        scored.append((run_id, value, grade, phase))

    # None values sort last; among present values honor the metric direction.
    # Secondary key on run_id keeps ties deterministic.
    def _sort_key(item: tuple[str, float | None, str, str]):
        run_id, value, _grade, _phase = item
        missing = value is None
        primary = 0.0 if value is None else value
        if not ascending:
            primary = -primary
        return (missing, primary, run_id)

    scored.sort(key=_sort_key)

    rows: list[list[str]] = []
    for rank, (run_id, value, grade, phase) in enumerate(scored, start=1):
        if value is None:
            rendered = "n/a"
        elif metric == "final_ambiguity":
            rendered = f"{value:.3f}"
        else:
            rendered = f"{value:g}"
        rows.append([str(rank), run_id, rendered, grade, phase])

    order = "asc" if ascending else "desc"
    typer.echo(f"# frontier by {metric} ({order})")
    typer.echo("")
    typer.echo(_render_table(["rank", "run_id", metric, "grade", "phase"], rows))


__all__ = ["app"]
