"""Tests for L3-1 runtime evidence substrate."""

from __future__ import annotations

from pathlib import Path

import pytest

from ouroboros.auto.task_classes import TaskClass
from ouroboros.orchestrator.runtime_evidence import (
    HEADLESS_RUN_PROBE_KIND,
    DeferredProbe,
    HeadlessRunProbe,
    RuntimeEvidence,
    probes_for_task_class,
)

# ---------------------------------------------------------------------------
# RuntimeEvidence dataclass
# ---------------------------------------------------------------------------


def test_runtime_evidence_shape() -> None:
    evidence = RuntimeEvidence(
        probe_kind="headless_run",
        passed=True,
        summary="ok",
        duration_seconds=0.42,
        payload={"exit_code": 0},
    )
    assert evidence.probe_kind == "headless_run"
    assert evidence.passed is True
    assert evidence.summary == "ok"
    assert evidence.duration_seconds == 0.42
    assert evidence.payload["exit_code"] == 0


def test_runtime_evidence_rejects_negative_duration() -> None:
    with pytest.raises(ValueError, match=">= 0"):
        RuntimeEvidence(
            probe_kind="headless_run",
            passed=False,
            summary="bogus",
            duration_seconds=-0.1,
        )


def test_runtime_evidence_rejects_empty_probe_kind() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        RuntimeEvidence(probe_kind="", passed=True, summary="")


def test_runtime_evidence_is_frozen() -> None:
    evidence = RuntimeEvidence(probe_kind="headless_run", passed=True, summary="ok")
    with pytest.raises(Exception):  # noqa: BLE001 - frozen
        evidence.passed = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# HeadlessRunProbe — happy paths and failure modes
# ---------------------------------------------------------------------------


def test_headless_run_success(tmp_path: Path) -> None:
    """``true`` (POSIX) exits 0 → probe passes; duration > 0."""
    probe = HeadlessRunProbe()
    evidence = probe.run(cwd=tmp_path, command=["python", "-c", "import sys; sys.exit(0)"])
    assert evidence.probe_kind == HEADLESS_RUN_PROBE_KIND
    assert evidence.passed is True
    assert evidence.payload["exit_code"] == 0
    assert evidence.payload["outcome"] == "completed"
    assert evidence.duration_seconds >= 0
    assert "exit_code=0" in evidence.summary


def test_headless_run_nonzero_exit_fails(tmp_path: Path) -> None:
    probe = HeadlessRunProbe()
    evidence = probe.run(cwd=tmp_path, command=["python", "-c", "import sys; sys.exit(7)"])
    assert evidence.passed is False
    assert evidence.payload["exit_code"] == 7
    assert "exit_code=7" in evidence.summary


def test_headless_run_captures_stdout(tmp_path: Path) -> None:
    probe = HeadlessRunProbe()
    evidence = probe.run(
        cwd=tmp_path,
        command=["python", "-c", "print('hello world')"],
    )
    assert evidence.passed is True
    assert "hello world" in evidence.payload["stdout_preview"]


def test_headless_run_captures_stderr(tmp_path: Path) -> None:
    probe = HeadlessRunProbe()
    evidence = probe.run(
        cwd=tmp_path,
        command=[
            "python",
            "-c",
            "import sys; sys.stderr.write('errline'); sys.exit(2)",
        ],
    )
    assert evidence.passed is False
    assert "errline" in evidence.payload["stderr_preview"]


def test_headless_run_string_command_is_tokenized(tmp_path: Path) -> None:
    """A shell-style string command is tokenized via ``shlex.split`` —
    *not* run through a shell — so it stays free of injection
    surprises."""
    probe = HeadlessRunProbe()
    evidence = probe.run(cwd=tmp_path, command="python -c 'print(\"shlex ok\")'")
    assert evidence.passed is True
    assert "shlex ok" in evidence.payload["stdout_preview"]


def test_headless_run_timeout(tmp_path: Path) -> None:
    """A command that exceeds *timeout_seconds* yields a probe-FAIL with
    ``outcome = "timeout"``. Uses a tiny budget so the test is fast."""
    probe = HeadlessRunProbe()
    evidence = probe.run(
        cwd=tmp_path,
        command=["python", "-c", "import time; time.sleep(5)"],
        timeout_seconds=0.5,
    )
    assert evidence.passed is False
    assert evidence.payload["outcome"] == "timeout"
    assert "timed out" in evidence.summary


def test_headless_run_missing_cwd_raises(tmp_path: Path) -> None:
    probe = HeadlessRunProbe()
    with pytest.raises(FileNotFoundError):
        probe.run(cwd=tmp_path / "does_not_exist", command=["echo", "x"])


def test_headless_run_empty_command_rejected(tmp_path: Path) -> None:
    probe = HeadlessRunProbe()
    with pytest.raises(ValueError, match="at least one argv token"):
        probe.run(cwd=tmp_path, command=[])


# ---------------------------------------------------------------------------
# DeferredProbe — placeholder for non-v1 probe kinds
# ---------------------------------------------------------------------------


def test_deferred_probe_returns_non_passing_evidence(tmp_path: Path) -> None:
    """A :class:`DeferredProbe` represents a catalog-declared probe kind
    that L3 v1 has not yet implemented (e.g. ``sim_trace``). Calling
    :meth:`run` must succeed with a clearly-marked ``outcome ==
    "deferred"`` rather than crashing the verifier."""
    probe = DeferredProbe(kind="sim_trace")
    evidence = probe.run(cwd=tmp_path, command=())
    assert evidence.probe_kind == "sim_trace"
    assert evidence.passed is False
    assert evidence.payload["outcome"] == "deferred"
    assert "deferred" in evidence.summary


# ---------------------------------------------------------------------------
# L1 binding registry
# ---------------------------------------------------------------------------


def test_probes_for_cli_uses_headless_run() -> None:
    """The CLI catalog declares ``headless_run`` first — the binding
    returns a :class:`HeadlessRunProbe` for that slot."""
    probes = probes_for_task_class(TaskClass.CLI)
    assert any(isinstance(p, HeadlessRunProbe) for p in probes)


def test_every_task_class_resolves_without_crash() -> None:
    """Every L1-a catalog entry must resolve via :func:`probes_for_task_class`.
    Either the probe kind is implemented (real probe) or it falls
    through to :class:`DeferredProbe`. KeyError here means a catalog
    growth landed without updating the L3 registry."""
    for task_class in TaskClass:
        probes = probes_for_task_class(task_class)
        # At minimum, every PRODUCT-class declares at least one probe.
        # ``library`` declares ``import_smoke`` / ``unit_tests`` which
        # both fall through to ``DeferredProbe`` in v1.
        for probe in probes:
            assert isinstance(probe, (HeadlessRunProbe, DeferredProbe))


def test_deferred_kinds_route_to_deferred_probe() -> None:
    """``sim_trace`` is declared in the catalog for ``game_2d`` but not
    implemented in v1 — must route to :class:`DeferredProbe`."""
    probes = probes_for_task_class(TaskClass.GAME_2D)
    sim_probes = [p for p in probes if p.kind == "sim_trace"]
    assert sim_probes  # the catalog still declares it
    assert all(isinstance(p, DeferredProbe) for p in sim_probes)


def test_headless_run_kind_constant() -> None:
    """Pin the constant — L3-2 (verifier integration) imports it."""
    assert HEADLESS_RUN_PROBE_KIND == "headless_run"
