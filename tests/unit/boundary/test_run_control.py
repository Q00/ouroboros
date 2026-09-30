"""The run's check package control (switch, preparation, outcome) as ooo run and execute_seed use it."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ouroboros.boundary.events import RunContract
from ouroboros.boundary.ledger import BoundaryLedger, BoundaryOrderError
from ouroboros.boundary.resume import (
    ResumedCheckPackageAuthority,
)
from ouroboros.boundary.run_control import (
    CheckPackageRun,
    legacy_failure_class_from_events,
    legacy_failure_dimensions,
)
from ouroboros.boundary.run_wiring import (
    CheckPackageSettings,
    forget_live_state,
)
from ouroboros.core.seed import Seed
from ouroboros.persistence.event_store import EventStore

from .calc_fixtures import _seed as _calc_seed
from .test_authority import _authority, _event
from .test_resume import (
    CLAMP_BUGGY,
    CLAMP_FIXED,
    DOUBLE,
    EXECUTION,
    _restored,
    _run_until_the_worker_stops,
    _seed,
)


async def test_the_switch_off_reports_none_of_the_package_fields() -> None:
    run = CheckPackageRun(CheckPackageSettings(enabled=False))

    class _NoEvents:
        async def query_events(self, **_kwargs: Any) -> list[Any]:
            return []

    meta = await run.outcome_meta(
        _NoEvents(), execution_id="e", session_id="s", terminal_status="completed"
    )  # type: ignore[arg-type]
    assert "verification_coverage" not in meta
    assert meta["reconciliation"] == "none"
    assert run.render_outcome() == []


async def _resume(store: EventStore, seed: Seed, repo: Path) -> tuple[CheckPackageRun, Any]:
    run = CheckPackageRun(CheckPackageSettings(enabled=False))  # the switch resolves off now
    runner = SimpleNamespace(acceptance_authority=None)
    lines = await run.prepare(
        runner,
        seed,
        event_store=store,
        execution_id=EXECUTION,
        worker_dir=repo,
        runtime_backend="codex",
        model=None,
        resume=True,
    )
    assert isinstance(runner.acceptance_authority, ResumedCheckPackageAuthority)
    assert runner.acceptance_authority is run.resumed and lines
    return run, runner.acceptance_authority


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "mathutils.py").write_text(CLAMP_BUGGY + DOUBLE)
    return root


@pytest.fixture
async def store():
    event_store = EventStore("sqlite+aiosqlite:///:memory:")
    await event_store.initialize()
    yield event_store
    await event_store.close()


async def test_a_run_never_bound_to_a_package_resumes_as_the_legacy_run(
    store: EventStore, repo: Path
) -> None:
    run = CheckPackageRun(CheckPackageSettings(enabled=False))
    runner = SimpleNamespace(acceptance_authority=None)
    lines = await run.prepare(
        runner,
        _seed(),
        event_store=store,
        execution_id="exec_legacy",
        worker_dir=repo,
        runtime_backend="codex",
        model=None,
        resume=True,
    )
    assert lines == [] and runner.acceptance_authority is None and run.resumed is None


async def test_a_run_that_was_off_resumes_off_under_a_switch_that_is_on_now(
    store: EventStore, repo: Path
) -> None:
    # The run started with the check package off (no enabled record); the
    # switch resolves on now. The resume keeps the run's mode: nothing is
    # installed and every summary field says off.
    run = CheckPackageRun(CheckPackageSettings(enabled=True))
    runner = SimpleNamespace(acceptance_authority=None)
    lines = await run.prepare(
        runner,
        _seed(),
        event_store=store,
        execution_id="exec_was_off",
        worker_dir=repo,
        runtime_backend="codex",
        model=None,
        resume=True,
    )
    assert lines == [] and runner.acceptance_authority is None and run.resumed is None
    assert run.enabled is False
    meta = await run.outcome_meta(
        store, execution_id="exec_was_off", session_id="s", terminal_status="completed"
    )
    assert meta["check_package"] == "off" and meta["reconciliation"] == "none"


async def test_a_resume_takes_its_settings_from_the_run_not_the_live_config(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed, state = await _run_until_the_worker_stops(store, repo, tmp_path, monkeypatch)
    forget_live_state(state)
    recorded = await BoundaryLedger(store).run_contract(EXECUTION)
    assert recorded is not None
    run = CheckPackageRun(CheckPackageSettings(enabled=True, check_timeout_seconds=7))
    runner = SimpleNamespace(acceptance_authority=None)
    await run.prepare(
        runner,
        seed,
        event_store=store,
        execution_id=EXECUTION,
        worker_dir=repo,
        runtime_backend="codex",
        model=None,
        resume=True,
    )
    assert run.settings.check_timeout_seconds == recorded.check_timeout_seconds != 7
    assert run.resumed is not None and run.resumed.boundary.contract == recorded


async def test_resumed_summary_reports_the_original_switch_and_the_package_decision(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On by default, the resumed package fails the run.

    Before the fix the row read ``off``, ``not_run``, ``package_verdict=none``,
    ``reconciliation=none`` and ``legacy_verdict=reject``.
    """
    seed, _state = await _run_until_the_worker_stops(store, repo, tmp_path, monkeypatch)
    # Same process: clamp still broken, package FAIL; the legacy verifier accepted all.
    run, authority = await _resume(store, seed, repo)  # the switch resolves off now
    decided = await authority(seed=seed, execution_id=EXECUTION, parallel_result=_restored())
    assert not decided.all_succeeded and authority.outcome.legacy_run_accepted
    meta = await run.outcome_meta(
        store, execution_id=EXECUTION, session_id="s", terminal_status="failed"
    )
    assert {key: meta[key] for key in list(meta)[:5]} == {
        "check_package": "on",
        "check_package_status": "admitted",
        "package_verdict": "fail",
        "legacy_verdict": "accept",
        "reconciliation": "package_rejected_over_legacy_accept",
    }
    assert meta["legacy_failure_class"] == "accepted"


async def test_resumed_summary_of_an_undecided_run_is_indeterminate(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed, state = await _run_until_the_worker_stops(store, repo, tmp_path, monkeypatch)
    (repo / "mathutils.py").write_text(CLAMP_FIXED + DOUBLE)
    forget_live_state(state)
    run, authority = await _resume(store, seed, repo)
    await authority(seed=seed, execution_id=EXECUTION, parallel_result=_restored())
    meta = await run.outcome_meta(
        store, execution_id=EXECUTION, session_id="s", terminal_status="failed"
    )
    assert (
        meta["check_package"],
        meta["check_package_status"],
        meta["package_verdict"],
        meta["reconciliation"],
    ) == (
        "on",
        "admitted",
        "indeterminate",
        "package_rejected_over_legacy_accept",
    )


class _UnreadableJournal:
    async def replay(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("database is locked")


@pytest.mark.parametrize("enabled", [True, False])
async def test_an_unreadable_journal_refuses_the_resume(repo: Path, enabled: bool) -> None:
    from ouroboros.boundary.ledger import BoundaryOrderError

    run = CheckPackageRun(CheckPackageSettings(enabled=enabled))
    runner = SimpleNamespace(acceptance_authority=None)
    with pytest.raises(BoundaryOrderError, match="could not be read on resume"):
        await run.prepare(
            runner,
            _seed(),
            event_store=_UnreadableJournal(),
            execution_id="exec_x",
            worker_dir=repo,
            runtime_backend="codex",
            model=None,
            resume=True,
        )
    assert runner.acceptance_authority is None and run.resumed is None


async def test_a_fresh_run_with_the_package_on_needs_an_execution_id(
    store: EventStore, repo: Path
) -> None:
    run = CheckPackageRun(CheckPackageSettings(enabled=True))
    with pytest.raises(BoundaryOrderError):
        await run.prepare(
            SimpleNamespace(acceptance_authority=None),
            _seed(),
            event_store=store,
            execution_id="",
            worker_dir=repo,
            runtime_backend="codex",
            model=None,
            resume=False,
        )


async def test_a_resume_without_an_execution_id_is_an_error(store: EventStore, repo: Path) -> None:
    run = CheckPackageRun(CheckPackageSettings(enabled=False))
    runner = SimpleNamespace(acceptance_authority=None)
    with pytest.raises(BoundaryOrderError):
        await run.prepare(
            runner,
            _seed(),
            event_store=store,
            execution_id=None,
            worker_dir=repo,
            runtime_backend="codex",
            model=None,
            resume=True,
        )
    assert runner.acceptance_authority is None


@pytest.mark.parametrize(
    ("recorded", "receipt"),
    [
        (True, {"check_package": "on", "check_package_status": "pending"}),
        (False, {"check_package": "off", "check_package_status": "not_run"}),
    ],
)
async def test_a_resumed_run_loads_its_mode_from_the_journal_before_its_receipt(
    store: EventStore,
    monkeypatch: pytest.MonkeyPatch,
    recorded: bool,
    receipt: dict[str, str],
) -> None:
    """The receipt of a resume says the run's recorded mode, not the live switch.

    Before the fix a resumed receipt left ``check_package`` out.
    """
    monkeypatch.setenv("OUROBOROS_CHECK_PACKAGE", "off" if recorded else "on")
    if recorded:
        await BoundaryLedger(store).record_check_package_enabled(
            "exec_resumed", RunContract(check_timeout_seconds=30)
        )
    run = await CheckPackageRun.load(store, execution_id="exec_resumed", resume=True)
    assert run.pending_meta() == receipt
    assert run.keeps_runner_result()
    assert run.resuming and run.enabled is recorded


@pytest.mark.parametrize("switch", ["on", "off"])
async def test_every_fresh_run_keeps_the_runner_result_that_carries_its_summary(
    store: EventStore, monkeypatch: pytest.MonkeyPatch, switch: str
) -> None:
    """The closed summary is promised in every mode: the job waits for the runner's result.

    Before the fix an off run let the job complete from the terminal event without it.
    """
    monkeypatch.setenv("OUROBOROS_CHECK_PACKAGE", switch)
    run = await CheckPackageRun.load(store, execution_id=None, resume=False)
    assert run.enabled is (switch == "on")
    assert run.keeps_runner_result()


async def test_an_unreadable_journal_refuses_the_resume_before_its_receipt() -> None:
    with pytest.raises(BoundaryOrderError, match="could not be read on resume"):
        await CheckPackageRun.load(
            _UnreadableJournal(),  # type: ignore[arg-type]
            execution_id="exec_x",
            resume=True,
        )
    with pytest.raises(BoundaryOrderError):
        await CheckPackageRun.load(None, execution_id=None, resume=True)  # type: ignore[arg-type]


@pytest.fixture
def repo_authority(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    return root


@pytest.fixture
async def store_authority():
    event_store = EventStore("sqlite+aiosqlite:///:memory:")
    await event_store.initialize()
    yield event_store
    await event_store.close()


def test_legacy_failure_class_takes_the_first_rejected_criterion() -> None:
    events = [
        _event(2, "STALL"),
        _event(1, "EVIDENCE_FORM_MISMATCH"),
        _event(3, "unknown"),
        _event(0, "BLOCKED", session_id="other-session"),
        _event(True, "BLOCKED"),
    ]
    assert legacy_failure_class_from_events(events, session_id="s") == "evidence_form_mismatch"
    assert legacy_failure_class_from_events([_event(0, "unknown")], session_id="s") == "other"
    assert legacy_failure_class_from_events([], session_id="s") == "other"


async def test_legacy_failure_dimensions_for_accepted_and_undecided_runs() -> None:
    class _Store:
        async def query_events(self, **_kwargs: Any) -> list[Any]:
            return [_event(0, "FABRICATION_SUSPECTED"), _event(1, "SCOPE_CREEP")]

    store_authority: Any = _Store()
    assert await legacy_failure_dimensions(
        store_authority, execution_id="e", session_id="s", legacy_verdict="accept"
    ) == {"legacy_failure_class": "accepted"}
    assert await legacy_failure_dimensions(
        store_authority, execution_id="e", session_id="s", legacy_verdict="none"
    ) == {"legacy_failure_class": "none"}
    assert await legacy_failure_dimensions(
        store_authority, execution_id="e", session_id="s", legacy_verdict="reject"
    ) == {"legacy_failure_class": "fabrication_suspected"}


def _run(enabled: bool) -> CheckPackageRun:
    return CheckPackageRun(CheckPackageSettings(enabled=enabled))


async def _meta(run: CheckPackageRun, terminal_status: str, **kwargs: Any) -> dict[str, str]:
    class _Store:
        async def query_events(self, **_kwargs: Any) -> list[Any]:
            return []

    store_authority: Any = _Store()
    return await run.outcome_meta(
        store_authority, execution_id="e", session_id="s", terminal_status=terminal_status, **kwargs
    )


async def test_outcome_meta_when_the_check_package_never_ran() -> None:
    off = await _meta(_run(False), "failed")
    assert off == {
        "check_package": "off",
        "check_package_status": "not_run",
        "package_verdict": "none",
        "legacy_verdict": "reject",
        "reconciliation": "none",
        "legacy_failure_class": "other",
    }
    errored = await _meta(_run(False), "failed", verdict_available=False)
    assert errored["legacy_verdict"] == "none"
    assert errored["legacy_failure_class"] == "none"
    cancelled = await _meta(_run(False), "cancelled")
    assert cancelled["legacy_verdict"] == "none"


async def test_outcome_meta_when_preparation_failed_or_the_hook_never_ran(
    store_authority: EventStore, repo_authority: Path, tmp_path: Path
) -> None:
    run = _run(True)
    run.attempted = True
    run.preparation_error = "OSError"
    meta = await _meta(run, "completed")
    assert meta["check_package_status"] == "construction_failed"
    assert meta["reconciliation"] == "fallback_to_legacy"
    assert meta["package_verdict"] == "none"

    _seed_value, authority = await _authority(store_authority, repo_authority, tmp_path)
    run = _run(True)
    run.attempted = True
    run.state = authority._state  # noqa: SLF001 - test wiring
    run.authority = authority  # installed, but the runner never called it
    meta = await _meta(run, "completed")
    assert meta["check_package_status"] == "admitted"
    assert meta["package_verdict"] == "none"
    assert meta["reconciliation"] == "fallback_to_legacy"
    assert run.render_outcome() == [
        "Check package was not consulted: this execution path does not support it; "
        "the existing verifier decided the run."
    ]


async def test_prepare_with_the_switch_off_does_not_touch_the_runner(tmp_path: Path) -> None:
    runner = SimpleNamespace(acceptance_authority=None)
    run = _run(False)
    lines = await run.prepare(
        runner,
        _calc_seed("add(2, 3) returns 5"),
        event_store=None,  # type: ignore[arg-type]
        execution_id="exec_off",
        worker_dir=tmp_path,
        runtime_backend="codex",
        model=None,
        resume=False,
        constructor_factory=lambda **_kwargs: pytest.fail("constructor must not be built"),
    )
    assert lines == [] and runner.acceptance_authority is None
