"""Run-control summaries of the authority's decisions: rendered lines and the closed-value outcome."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ouroboros.boundary.authority import (
    NO_BINDING_AFTER_REQUEST,
    CheckPackageAuthority,
)
from ouroboros.boundary.constructor import ConstructionOutcome
from ouroboros.boundary.run_control import CheckPackageRun
from ouroboros.boundary.run_wiring import (
    CheckPackageSettings,
)
from ouroboros.core.seed import Seed
from ouroboros.persistence.event_store import EventStore

from .test_authority_oracle import (
    BUGGY,
    _a_legacy_accepted_unverified_criterion_exits_zero_scenario,
    _a_package_failure_is_not_masked_by_a_legacy_acceptance_scenario,
    _after_an_authority_error_the_legacy_verdicts_of_sub_acs_decide_uncovered_roots_scenario,
    _an_authority_error_leaves_covered_criteria_undecided_never_accepted_scenario,
    _authority_matrix_and_exit_semantics_scenario,
    _both_verifiers_without_evidence_leave_the_criterion_unverified_scenario,
    _executor,
    _seed,
    _the_outcome_summary_never_reads_an_unavailable_transcript_as_a_rejection_scenario,
    _unattempted_criteria_leave_the_package_undeciding_scenario,
)
from .test_binding_request import _no_declaration_after_the_request_stays_unverified_scenario


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "mathutils.py").write_text(BUGGY)
    return root


@pytest.fixture
async def store():
    event_store = EventStore("sqlite+aiosqlite:///:memory:")
    await event_store.initialize()
    yield event_store
    await event_store.close()


def _run_for_matrix(authority: CheckPackageAuthority) -> CheckPackageRun:
    return CheckPackageRun(
        CheckPackageSettings(enabled=True),
        state=authority.state,
        authority=authority,
        attempted=True,
    )


async def test_authority_matrix_and_exit_semantics_summary(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    authority = await _authority_matrix_and_exit_semantics_scenario(store, repo, tmp_path)
    run = _run_for_matrix(authority)
    lines = run.render_outcome()
    assert any(
        line.startswith(
            "AC 3: not accepted by the legacy verifier (legacy-decided: uncovered:declared_not_executable)"
        )
        for line in lines
    )
    assert any(
        line.startswith(
            "Verified by the check package: 2 of 3 passed; legacy-decided: 2; unverified: 0"
        )
        for line in lines
    )
    assert not any(line.startswith("WARNING: insufficient verification") for line in lines)
    meta = await run.outcome_meta(
        _NoEvents(), execution_id="exec_oracle", session_id="s", terminal_status="failed"
    )  # type: ignore[arg-type]
    assert meta["package_verdict"] == "pass"
    assert meta["legacy_verdict"] == "reject"
    assert meta["reconciliation"] == "legacy_decided_unverified"
    assert meta["verification_coverage"] == "partial"
    assert not {"non_behavioral_count", "label_parse_failure_count"} & set(meta)
    assert meta["legacy_failure_class"] == "evidence_form_mismatch"
    assert not any(key.endswith("_count") for key in meta)


async def test_a_legacy_accepted_unverified_criterion_exits_zero_summary(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    authority = await _a_legacy_accepted_unverified_criterion_exits_zero_scenario(
        store, repo, tmp_path
    )
    meta = await _run_for_matrix(authority).outcome_meta(
        _NoEvents(), execution_id="exec_oracle", session_id="s", terminal_status="completed"
    )  # type: ignore[arg-type]
    assert meta["reconciliation"] == "package_accepted_over_legacy_reject"
    assert meta["verification_coverage"] == "partial"


async def test_a_package_failure_is_not_masked_by_a_legacy_acceptance_summary(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    authority = await _a_package_failure_is_not_masked_by_a_legacy_acceptance_scenario(
        store, repo, tmp_path
    )
    meta = await _run_for_matrix(authority).outcome_meta(
        _NoEvents(), execution_id="exec_oracle", session_id="s", terminal_status="failed"
    )  # type: ignore[arg-type]
    assert meta["reconciliation"] == "agree"


async def test_both_verifiers_without_evidence_leave_the_criterion_unverified_summary(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    authority = await _both_verifiers_without_evidence_leave_the_criterion_unverified_scenario(
        store, repo, tmp_path
    )
    run = _run_for_matrix(authority)
    lines = run.render_outcome()
    assert "- unverified AC 3: uncovered:declared_not_executable" in lines
    assert any(
        line.startswith("WARNING: insufficient verification: the check package decided 2 of 3")
        for line in lines
    )
    meta = await run.outcome_meta(
        _NoEvents(), execution_id="exec_oracle", session_id="s", terminal_status="completed"
    )  # type: ignore[arg-type]
    assert meta["verification_coverage"] == "low"


class _FailingConstructor:
    def __init__(self, **_kwargs: Any) -> None:
        pass

    async def construct(self, seed: Seed, base: Path, *, feedback=()) -> ConstructionOutcome:
        return ConstructionOutcome(None, "constructor_timeout", "1" * 64, "fake")


class _EmptyStore:
    async def query_events(self, **_kwargs: Any) -> list[Any]:
        return []


@pytest.mark.parametrize(("terminal", "legacy"), [("completed", "accept"), ("failed", "reject")])
async def test_a_constructor_outage_leaves_the_legacy_verifier_deciding(
    store: EventStore,
    repo: Path,
    tmp_path: Path,
    terminal: str,
    legacy: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Constructor outage: zero checks admitted. Nothing is installed on the
    # runner, so the executor and the terminal status are the legacy ones;
    # the run exits 0 only when the legacy verifier passed it.
    from types import SimpleNamespace

    monkeypatch.setattr(
        "ouroboros.boundary.run_wiring.default_store_dir",
        lambda execution_id: tmp_path / "store" / execution_id,
    )
    runner = SimpleNamespace(acceptance_authority=None)
    run = CheckPackageRun(
        CheckPackageSettings(
            enabled=True,
            max_construction_attempts=2,
        )
    )
    lines = await run.prepare(
        runner,
        _seed(),
        event_store=store,
        execution_id="exec_outage",
        worker_dir=repo,
        runtime_backend="codex",
        model=None,
        resume=False,
        constructor_factory=_FailingConstructor,
    )
    assert runner.acceptance_authority is None and run.authority is None
    assert any("No admitted package (constructor_timeout)" in line for line in lines)
    assert run.render_outcome() == [
        "Check package unavailable (constructor_timeout); legacy verification decided this run.",
        "WARNING: insufficient verification: the check package decided 0 of 3 criteria "
        "(verification_coverage=low).",
    ]
    meta = await run.outcome_meta(
        _EmptyStore(),
        execution_id="exec_outage",
        session_id="s",
        terminal_status=terminal,  # type: ignore[arg-type]
    )
    assert meta["check_package_status"] == "construction_failed"
    assert meta["reconciliation"] == "fallback_to_legacy"
    assert meta["package_verdict"] == "none"
    assert meta["legacy_verdict"] == legacy
    assert meta["verification_coverage"] == "low"
    # The executor gets no gate: its prompt and retry loop are the legacy ones.
    executor = _executor(repo)
    assert not hasattr(executor, "check_package_gate")


def _run_for(authority: CheckPackageAuthority) -> CheckPackageRun:
    return CheckPackageRun(
        CheckPackageSettings(enabled=True),
        state=authority.state,
        authority=authority,
        attempted=True,
    )


class _NoEvents:
    async def query_events(self, **_kwargs: Any) -> list[Any]:
        return []


async def test_an_authority_error_leaves_covered_criteria_undecided_never_accepted_summary(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # B1: the gate failed criterion 0 on the package; the authority then
    # raises. Nothing the package covers may be accepted because something
    # else failed: covered criteria are indeterminate, and only the uncovered
    # criterion is left to the legacy verifier (resume's no-package rule).
    authority = await _an_authority_error_leaves_covered_criteria_undecided_never_accepted_scenario(
        store, repo, tmp_path, monkeypatch
    )
    run = _run_for(authority)
    assert run.render_outcome()[0] == (
        "Check package could not decide this run (authority_error:OSError); the criteria it "
        "covers are undecided (not accepted), the legacy verifier decided the rest."
    )
    meta = await run.outcome_meta(
        _NoEvents(), execution_id="exec_oracle", session_id="s", terminal_status="failed"
    )  # type: ignore[arg-type]
    assert meta["reconciliation"] == "undecided"
    assert meta["package_verdict"] == "indeterminate"


async def test_unattempted_criteria_are_reported_as_decided_by_the_legacy_verifier_summary(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    # an admitted package whose criteria the worker never attempted decides
    # nothing: the legacy verifier decides the run (fallback_to_legacy).
    authority = await _unattempted_criteria_leave_the_package_undeciding_scenario(
        store, repo, tmp_path
    )
    meta = await _run_for(authority).outcome_meta(
        _NoEvents(), execution_id="exec_oracle", session_id="s", terminal_status="failed"
    )  # type: ignore[arg-type]
    assert meta["reconciliation"] == "fallback_to_legacy"


@pytest.mark.parametrize("via", ["verdict", "annotation"])
async def test_after_an_authority_error_the_legacy_verdicts_of_sub_acs_decide_uncovered_roots_summary(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, via: str
) -> None:
    # a decomposed root carries no legacy rejection itself; its sub-AC
    # does. When the authority fails, the uncovered root is decided by the
    # legacy verdict tree, so it fails and the run does not exit 0.
    authority = await _after_an_authority_error_the_legacy_verdicts_of_sub_acs_decide_uncovered_roots_scenario(
        store, repo, tmp_path, monkeypatch, via
    )
    meta = await _run_for(authority).outcome_meta(
        _NoEvents(), execution_id="exec_oracle", session_id="s", terminal_status="failed"
    )  # type: ignore[arg-type]
    assert meta["legacy_verdict"] == "reject"


async def test_the_outcome_summary_never_reads_an_unavailable_transcript_as_a_rejection_summary(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    # the package passes and the legacy verifier had no transcript:
    # that is agreement with what the switch off decides (accept), never
    # package_accepted_over_legacy_reject.
    authority = (
        await _the_outcome_summary_never_reads_an_unavailable_transcript_as_a_rejection_scenario(
            store, repo, tmp_path
        )
    )
    meta = await _run_for(authority).outcome_meta(
        _NoEvents(), execution_id="exec_oracle", session_id="s", terminal_status="completed"
    )  # type: ignore[arg-type]
    assert meta["package_verdict"] == "pass"
    assert meta["legacy_verdict"] == "accept"
    assert meta["reconciliation"] == "agree"
    assert meta["legacy_failure_class"] == "accepted"


@pytest.fixture
def repo_binding_request(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "mathutils.py").write_text(BUGGY)
    return root


@pytest.fixture
async def store_binding_request():
    event_store = EventStore("sqlite+aiosqlite:///:memory:")
    await event_store.initialize()
    yield event_store
    await event_store.close()


async def test_no_declaration_after_the_request_stays_unverified_summary(
    store_binding_request: EventStore, repo_binding_request: Path, tmp_path: Path
) -> None:
    authority = await _no_declaration_after_the_request_stays_unverified_scenario(
        store_binding_request, repo_binding_request, tmp_path
    )
    run = CheckPackageRun(
        authority.settings, state=authority.state, authority=authority, attempted=True
    )
    assert len(authority.binding_requested) == 1
    lines = run.render_outcome()
    assert any(NO_BINDING_AFTER_REQUEST in line for line in lines)
