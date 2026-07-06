"""Tests for ``ouroboros harness`` — A3 experience-store CLI.

These pin the thin read-only surface over A2's interview traces
(:mod:`ouroboros.auto.trace_export`):

* ``list`` enumerates exported traces plus persisted-but-unexported runs.
* ``show`` renders ``summary.md`` + key ``outcome.json`` fields, and
  projects a trace on-demand when it is missing on disk.
* ``trace --grep`` greps the JSONL streams with ``file:line:`` prefixes.
* ``diff`` renders deterministic side-by-side metrics + stream deltas.
* ``frontier --metric`` ranks traced runs; an unknown metric errors cleanly.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from ouroboros.auto.state import AutoPipelineState, AutoStore
from ouroboros.auto.trace_export import (
    AMBIGUITY_FILE,
    DECISIONS_FILE,
    OUTCOME_FILE,
    QUESTIONS_FILE,
    SUMMARY_FILE,
)
from ouroboros.cli.main import app

runner = CliRunner(env={"COLUMNS": "240"})


def _write_trace(
    traces_root: Path,
    run_id: str,
    *,
    grade: str = "A",
    phase: str = "complete",
    provenance: dict[str, int] | None = None,
    ambiguity_scores: list[float] | None = None,
    counts: dict[str, int] | None = None,
    questions: list[str] | None = None,
    unverified: int = 0,
) -> Path:
    """Materialize a trace directory in the shape the A2 exporter writes."""
    trace_dir = traces_root / run_id
    trace_dir.mkdir(parents=True, exist_ok=True)

    provenance = provenance or {"user_provided": 3, "model_inferred": 1}
    ambiguity_scores = ambiguity_scores if ambiguity_scores is not None else [0.8, 0.4, 0.15]
    counts = counts or {
        "questions": len(questions or ["q1", "q2"]),
        "decisions": 4,
        "promoted": 3,
        "rejected": 1,
        "gated": 1,
        "ambiguity": len(ambiguity_scores),
        "lateral": 0,
        "flags": 2,
    }

    outcome = {
        "run_id": run_id,
        "auto_session_id": run_id,
        "status": phase,
        "phase": phase,
        "grade": grade,
        "seed_id": f"seed_{run_id}",
        "seed_origin": "generated",
        "qa": {
            "verdict": "PASS",
            "score": 0.92,
            "passed": True,
            "differences": [],
            "suggestions": [],
        },
        "provenance_histogram": provenance,
        "unverified_provenance_findings": [
            {"code": "unverified_provenance", "target": f"t{i}", "message": "m"}
            for i in range(unverified)
        ],
        "blocker": None,
        "stop_reason_code": None,
        "degraded": False,
        "counts": counts,
    }
    (trace_dir / OUTCOME_FILE).write_text(json.dumps(outcome, indent=2, sort_keys=True) + "\n")

    # summary.md
    (trace_dir / SUMMARY_FILE).write_text(
        f"# Interview trace — {run_id}\n\n- Status: **{phase}**\n- Grade: {grade}\n"
    )

    # ambiguity.jsonl (time-ordered trajectory)
    amb_rows = [
        {
            "type": "ambiguity",
            "event": "auto.round",
            "round": i + 1,
            "ambiguity_score": s,
            "at": f"2026-07-06T00:0{i}:00",
        }
        for i, s in enumerate(ambiguity_scores)
    ]
    (trace_dir / AMBIGUITY_FILE).write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in amb_rows) + "\n"
    )

    # questions.jsonl
    q_rows = [
        {"type": "question", "round": i + 1, "question": q, "answer": f"a{i}"}
        for i, q in enumerate(questions or ["What is the goal?", "Which backend?"])
    ]
    (trace_dir / QUESTIONS_FILE).write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in q_rows) + "\n"
    )

    # decisions.jsonl
    d_rows = [
        {
            "type": "decision",
            "section": "goal",
            "key": "primary",
            "value": "ship",
            "promoted": True,
        },
    ]
    (trace_dir / DECISIONS_FILE).write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in d_rows) + "\n"
    )
    return trace_dir


# --------------------------------------------------------------------------- #
# list                                                                        #
# --------------------------------------------------------------------------- #
def test_list_shows_exported_and_unexported(tmp_path: Path) -> None:
    traces_root = tmp_path / "traces"
    data_root = tmp_path / "data"
    data_root.mkdir(parents=True)

    _write_trace(traces_root, "auto_aaa111", grade="A")

    # A persisted-but-unexported run (auto state present, no trace dir).
    store = AutoStore(root=data_root)
    state = AutoPipelineState(goal="Build a thing", cwd=str(tmp_path))
    state.auto_session_id = "auto_bbb222"
    store.save(state)

    result = runner.invoke(
        app,
        ["harness", "list", "--traces-root", str(traces_root), "--data-root", str(data_root)],
    )
    assert result.exit_code == 0, result.output
    assert "auto_aaa111" in result.output
    assert "exported" in result.output
    assert "auto_bbb222" in result.output
    assert "not exported" in result.output
    # Provenance digest is data-derived from outcome.json.
    assert "user_provided=3" in result.output


def test_list_empty(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "harness",
            "list",
            "--traces-root",
            str(tmp_path / "traces"),
            "--data-root",
            str(tmp_path / "data"),
        ],
    )
    assert result.exit_code == 0
    assert "No traces found." in result.output


# --------------------------------------------------------------------------- #
# show                                                                        #
# --------------------------------------------------------------------------- #
def test_show_renders_summary_and_outcome(tmp_path: Path) -> None:
    traces_root = tmp_path / "traces"
    _write_trace(traces_root, "auto_ccc333", grade="B", unverified=2)

    result = runner.invoke(
        app,
        ["harness", "show", "auto_ccc333", "--traces-root", str(traces_root)],
    )
    assert result.exit_code == 0, result.output
    assert "Interview trace — auto_ccc333" in result.output
    assert "grade: B" in result.output
    assert "final_ambiguity: 0.150" in result.output
    assert "unverified_provenance: 2" in result.output


def test_show_unknown_run_errors(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "harness",
            "show",
            "auto_missing",
            "--traces-root",
            str(tmp_path / "traces"),
            "--data-root",
            str(tmp_path / "data"),
            "--db-path",
            str(tmp_path / "nope.db"),
        ],
    )
    assert result.exit_code == 2


def test_show_exports_on_demand_from_persisted_state(tmp_path: Path) -> None:
    """No trace on disk, but persisted auto state → project on-demand."""
    traces_root = tmp_path / "traces"
    data_root = tmp_path / "data"
    data_root.mkdir(parents=True)

    store = AutoStore(root=data_root)
    state = AutoPipelineState(goal="Ship the harness CLI", cwd=str(tmp_path))
    state.auto_session_id = "auto_ondemand"
    state.last_grade = "A"
    store.save(state)

    assert not (traces_root / "auto_ondemand").exists()

    result = runner.invoke(
        app,
        [
            "harness",
            "show",
            "auto_ondemand",
            "--traces-root",
            str(traces_root),
            "--data-root",
            str(data_root),
            "--db-path",
            str(tmp_path / "absent.db"),  # no EventStore → export from state alone
        ],
    )
    assert result.exit_code == 0, result.output
    assert "auto_ondemand" in result.output
    # The trace was materialized on disk as a side effect.
    assert (traces_root / "auto_ondemand" / OUTCOME_FILE).is_file()


# --------------------------------------------------------------------------- #
# trace --grep                                                                #
# --------------------------------------------------------------------------- #
def test_trace_grep_matches_with_file_line_prefix(tmp_path: Path) -> None:
    traces_root = tmp_path / "traces"
    _write_trace(
        traces_root,
        "auto_grep1",
        questions=["Which backend do we target?", "What is the deadline?"],
    )

    result = runner.invoke(
        app,
        ["harness", "trace", "auto_grep1", "--grep", "backend", "--traces-root", str(traces_root)],
    )
    assert result.exit_code == 0, result.output
    assert f"{QUESTIONS_FILE}:1:" in result.output
    assert "backend" in result.output


def test_trace_grep_stream_filter_and_no_match(tmp_path: Path) -> None:
    traces_root = tmp_path / "traces"
    _write_trace(traces_root, "auto_grep2")

    # Restrict to ambiguity stream; questions text won't appear there.
    result = runner.invoke(
        app,
        [
            "harness",
            "trace",
            "auto_grep2",
            "--grep",
            "ambiguity_score",
            "--stream",
            "ambiguity",
            "--traces-root",
            str(traces_root),
        ],
    )
    assert result.exit_code == 0, result.output
    assert f"{AMBIGUITY_FILE}:1:" in result.output

    no_match = runner.invoke(
        app,
        [
            "harness",
            "trace",
            "auto_grep2",
            "--grep",
            "zzz_never_present_zzz",
            "--traces-root",
            str(traces_root),
        ],
    )
    assert no_match.exit_code == 0
    assert "No matches" in no_match.output


def test_trace_unknown_stream_errors(tmp_path: Path) -> None:
    traces_root = tmp_path / "traces"
    _write_trace(traces_root, "auto_grep3")
    result = runner.invoke(
        app,
        [
            "harness",
            "trace",
            "auto_grep3",
            "--grep",
            "x",
            "--stream",
            "bogus",
            "--traces-root",
            str(traces_root),
        ],
    )
    assert result.exit_code == 64


# --------------------------------------------------------------------------- #
# diff                                                                        #
# --------------------------------------------------------------------------- #
def test_diff_two_runs(tmp_path: Path) -> None:
    traces_root = tmp_path / "traces"
    _write_trace(
        traces_root,
        "auto_diffa",
        grade="B",
        ambiguity_scores=[0.9, 0.5],
        counts={
            "questions": 5,
            "decisions": 4,
            "promoted": 3,
            "rejected": 1,
            "gated": 1,
            "ambiguity": 2,
            "lateral": 0,
            "flags": 3,
        },
    )
    _write_trace(
        traces_root,
        "auto_diffb",
        grade="A",
        ambiguity_scores=[0.9, 0.4, 0.1],
        counts={
            "questions": 3,
            "decisions": 4,
            "promoted": 4,
            "rejected": 0,
            "gated": 0,
            "ambiguity": 3,
            "lateral": 1,
            "flags": 1,
        },
    )

    result = runner.invoke(
        app,
        ["harness", "diff", "auto_diffa", "auto_diffb", "--traces-root", str(traces_root)],
    )
    assert result.exit_code == 0, result.output
    assert "diff A=auto_diffa B=auto_diffb" in result.output
    assert "final_ambiguity" in result.output
    # questions delta B-A = 3-5 = -2
    assert "-2" in result.output
    assert "Stream line counts" in result.output


# --------------------------------------------------------------------------- #
# frontier                                                                    #
# --------------------------------------------------------------------------- #
def test_frontier_ranks_by_final_ambiguity(tmp_path: Path) -> None:
    traces_root = tmp_path / "traces"
    _write_trace(traces_root, "auto_hi", ambiguity_scores=[0.5, 0.45])  # final 0.45
    _write_trace(traces_root, "auto_lo", ambiguity_scores=[0.5, 0.10])  # final 0.10

    result = runner.invoke(
        app,
        ["harness", "frontier", "--metric", "final_ambiguity", "--traces-root", str(traces_root)],
    )
    assert result.exit_code == 0, result.output
    lines = [ln for ln in result.output.splitlines() if "auto_" in ln]
    # Ascending: the lower final ambiguity ranks first.
    assert lines[0].split()[1] == "auto_lo"
    assert lines[1].split()[1] == "auto_hi"


def test_frontier_unknown_metric_lists_available(tmp_path: Path) -> None:
    traces_root = tmp_path / "traces"
    _write_trace(traces_root, "auto_fr1")
    result = runner.invoke(
        app,
        ["harness", "frontier", "--metric", "bogus_metric", "--traces-root", str(traces_root)],
    )
    assert result.exit_code == 2
    assert "Available metrics:" in result.output
    assert "final_ambiguity" in result.output
    assert "unverified_provenance" in result.output


def test_frontier_no_traces(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["harness", "frontier", "--metric", "rounds", "--traces-root", str(tmp_path / "traces")],
    )
    assert result.exit_code == 0
    assert "No traced runs found." in result.output
