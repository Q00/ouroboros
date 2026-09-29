"""ooo run with the check package: flag precedence, admission before dispatch, the exit code."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import typer

from ouroboros.boundary.events import (
    ACCEPTANCE_RECONCILED,
    ACTOR_STARTED,
    ADMISSION_COMPLETED,
    BINDING_RECORDED,
    BOUNDARY_AGGREGATE_TYPE,
    CANDIDATE_VERIFIED,
    PACKAGE_FROZEN,
)
from ouroboros.boundary.ledger import (
    verify_boundary_order,
)
from ouroboros.cli.commands.run import _run_orchestrator
from ouroboros.core.types import Result
from ouroboros.persistence.event_store import EventStore

from .calc_fixtures import (
    BUGFIX_SCRIPT,
    BUGGY,
    FIXED,
)
from .fake_constructors import (
    _constructor_factory,
    _failing_constructor_factory,
)
from .test_run_wiring import SEED_DATA, _check_package_meta, _fake_exec, _parallel_result, _types


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "calc.py").write_text(BUGGY)
    return root


@pytest.fixture
async def store():
    event_store = EventStore("sqlite+aiosqlite:///:memory:")
    await event_store.initialize()
    yield event_store
    await event_store.close()


async def _run_cli(
    tmp_path: Path,
    project: Path,
    *,
    check_package: bool | None,
    constructor_cls: Any,
    worker_edit: str | None,
    monkeypatch: pytest.MonkeyPatch,
    seen: dict[str, Any] | None = None,
    run_success: bool = True,
) -> tuple[EventStore, MagicMock, dict[str, Any]]:
    """Drive ``_run_orchestrator`` with a runner double that honors the hook contract.

    The double calls ``runner.acceptance_authority`` the way
    ``OrchestratorRunner._execute_parallel`` does: once, on the executor's
    result (legacy verdict ``run_success``), and reports the returned
    result's ``all_succeeded`` as the run's success.
    """
    monkeypatch.delenv("OUROBOROS_CHECK_PACKAGE", raising=False)
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
    seen = {} if seen is None else seen
    seen["store"] = store

    async def execute_seed(**kwargs: Any):
        seen["kwargs"] = kwargs
        rows = await store.replay(
            BOUNDARY_AGGREGATE_TYPE, f"{kwargs['execution_id']}/check_package/v1"
        )
        seen["events_at_dispatch"] = [event.type for event in rows]
        if worker_edit is not None:
            (project / "calc.py").write_text(worker_edit)
        parallel_result = _parallel_result(run_success)
        if runner.acceptance_authority is not None:
            parallel_result = await runner.acceptance_authority(
                seed=kwargs["seed"],
                execution_id=kwargs["execution_id"],
                parallel_result=parallel_result,
            )
        seen["parallel_result"] = parallel_result
        return Result.ok(_fake_exec(parallel_result.all_succeeded))

    runner = MagicMock()
    runner.acceptance_authority = None
    runner.execute_seed = AsyncMock(side_effect=execute_seed)
    seen["runner"] = runner
    seed_file = project / "seed.yaml"
    seed_file.write_text("goal: ignored\n")

    def capture(_job_id: str, job_type: str, **kwargs: Any) -> None:
        seen["telemetry"] = {"job_type": job_type, **kwargs}

    from ouroboros.boundary.run_control import CheckPackageRun

    real_resolve = CheckPackageRun.resolve

    def resolve(cli_value: bool | None = None) -> CheckPackageRun:
        seen["check_package_run"] = real_resolve(cli_value)
        return seen["check_package_run"]

    with (
        patch("ouroboros.cli.commands.run._load_seed_from_yaml", return_value=SEED_DATA),
        patch("ouroboros.orchestrator.create_agent_runtime"),
        patch("ouroboros.orchestrator.OrchestratorRunner", return_value=runner),
        patch("ouroboros.persistence.event_store.EventStore", return_value=store),
        patch("ouroboros.boundary.constructor.CheckConstructor", constructor_cls),
        patch(
            "ouroboros.boundary.run_wiring.default_store_dir",
            side_effect=lambda execution_id: tmp_path / "store" / execution_id,
        ),
        patch("ouroboros.telemetry.capture_job_outcome", side_effect=capture),
        patch("ouroboros.boundary.run_control.CheckPackageRun.resolve", side_effect=resolve),
    ):
        await _run_orchestrator(
            seed_file, no_qa=True, project_dir=project, check_package=check_package
        )
    return store, runner, seen


async def test_cli_flag_off_adds_no_model_call_and_no_event(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    from ouroboros.config.models import BoundaryConfig

    with patch("ouroboros.boundary.switch._load_boundary_config", return_value=BoundaryConfig()):
        store, runner, seen = await _run_cli(
            tmp_path,
            repo,
            check_package=False,
            constructor_cls=_constructor_factory(BUGFIX_SCRIPT, calls),
            worker_edit=None,
            monkeypatch=monkeypatch,
        )
    assert calls == []
    assert seen["events_at_dispatch"] == []
    assert set(seen["kwargs"]) == {"seed", "execution_id", "session_id", "parallel"}
    assert runner.acceptance_authority is None
    assert not (tmp_path / "store").exists()
    # No boundary aggregate exists anywhere in the journal.
    async with store._engine.connect() as conn:  # noqa: SLF001 - test-only read
        from sqlalchemy import text

        rows = await conn.execute(
            text("SELECT COUNT(*) FROM events WHERE aggregate_type = :t"),
            {"t": BOUNDARY_AGGREGATE_TYPE},
        )
        assert rows.scalar() == 0
    assert (await _check_package_meta(seen)) == {
        "check_package": "off",
        "check_package_status": "not_run",
        "package_verdict": "none",
        "legacy_verdict": "accept",
        "reconciliation": "none",
        "legacy_failure_class": "accepted",
    }
    await store.close()


async def test_cli_flag_on_admits_before_dispatch_and_verifies_after(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    store, runner, seen = await _run_cli(
        tmp_path,
        repo,
        check_package=True,
        constructor_cls=_constructor_factory(BUGFIX_SCRIPT, calls),
        worker_edit=FIXED,
        monkeypatch=monkeypatch,
    )

    assert len(calls) == 1
    assert calls[0]["runtime_backend"]
    assert seen["events_at_dispatch"] == [PACKAGE_FROZEN, ADMISSION_COMPLETED, ACTOR_STARTED]
    # The worker receives the unchanged Seed and nothing from the package.
    dispatched = seen["kwargs"]["seed"]
    (criterion,) = dispatched.acceptance_criteria
    assert criterion.description == "add(2, 3) returns 5"
    assert criterion.verify_command is None and criterion.output_assertion is None
    assert "OUROBOROS_CHECK_FAILED" not in repr(seen["kwargs"])
    execution_id = seen["kwargs"]["execution_id"]
    final = await _types(store, f"{execution_id}/check_package/v1")
    assert final == [
        PACKAGE_FROZEN,
        ADMISSION_COMPLETED,
        ACTOR_STARTED,
        BINDING_RECORDED,
        CANDIDATE_VERIFIED,
        ACCEPTANCE_RECONCILED,
    ]
    verified = (await store.replay(BOUNDARY_AGGREGATE_TYPE, f"{execution_id}/check_package/v1"))[4]
    assert verified.data["verdict"] == "pass"
    assert (await _check_package_meta(seen)) == {
        "check_package": "on",
        "check_package_status": "admitted",
        "package_verdict": "unverified",
        "legacy_verdict": "accept",
        "reconciliation": "agree",
        "legacy_failure_class": "accepted",
        # The legacy double gives no verifier verdict: neither verifier has
        # evidence, so the criterion stays unverified and coverage is low.
        "verification_coverage": "low",
    }
    await store.close()


async def test_cli_without_any_setting_runs_the_check_package(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On by default: no flag, no variable, no config, telemetry off."""
    from ouroboros.config.models import BoundaryConfig

    monkeypatch.setenv("OUROBOROS_TELEMETRY", "0")
    calls: list[dict[str, Any]] = []
    with patch("ouroboros.boundary.switch._load_boundary_config", return_value=BoundaryConfig()):
        store, runner, seen = await _run_cli(
            tmp_path,
            repo,
            check_package=None,
            constructor_cls=_constructor_factory(BUGFIX_SCRIPT, calls),
            worker_edit=FIXED,
            monkeypatch=monkeypatch,
        )
    assert len(calls) == 1
    assert seen["events_at_dispatch"] == [PACKAGE_FROZEN, ADMISSION_COMPLETED, ACTOR_STARTED]
    assert runner.acceptance_authority is not None
    meta = await _check_package_meta(seen)
    assert meta["check_package"] == "on"
    await store.close()


async def test_cli_flag_on_exits_non_zero_when_the_candidate_fails(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}
    with pytest.raises(typer.Exit) as exit_info:
        await _run_cli(
            tmp_path,
            repo,
            check_package=True,
            constructor_cls=_constructor_factory(BUGFIX_SCRIPT, []),
            worker_edit=None,
            monkeypatch=monkeypatch,
            seen=seen,
        )
    assert exit_info.value.exit_code == 1
    # The legacy verifier accepted the unchanged (still buggy) tree; the
    # package's counterexample rejects it, and the durable result says so.
    (result,) = seen["parallel_result"].results
    assert result.success is False and result.outcome.value == "failed"
    assert seen["telemetry"]["terminal_status"] == "failed"
    meta = await _check_package_meta(seen)
    assert meta["package_verdict"] == "fail"
    assert meta["legacy_verdict"] == "accept"
    assert meta["reconciliation"] == "package_rejected_over_legacy_accept"


async def test_cli_refuses_to_dispatch_into_a_leaking_workspace(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A renamed copy of the generated check sits in the worker's workspace.
    (repo / "leaked_check.py").write_text(BUGFIX_SCRIPT)
    seen: dict[str, Any] = {}
    with pytest.raises(typer.Exit) as exit_info:
        await _run_cli(
            tmp_path,
            repo,
            check_package=True,
            constructor_cls=_constructor_factory(BUGFIX_SCRIPT, []),
            worker_edit=None,
            monkeypatch=monkeypatch,
            seen=seen,
        )
    assert exit_info.value.exit_code == 1
    assert seen["runner"].execute_seed.await_count == 0


async def test_cli_unverified_criterion_with_a_legacy_rejection_exits_non_zero(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A script-only criterion is unverified; the legacy rejection decides it (2026-09-27).

    Before this rule the run was accepted as unverified (exit 0); now the
    legacy verifier decides every criterion the package cannot verify.
    """
    seen: dict[str, Any] = {}
    with pytest.raises(typer.Exit) as exit_info:
        await _run_cli(
            tmp_path,
            repo,
            check_package=True,
            constructor_cls=_constructor_factory(BUGFIX_SCRIPT, []),
            worker_edit=FIXED,
            monkeypatch=monkeypatch,
            run_success=False,
            seen=seen,
        )
    assert exit_info.value.exit_code == 1
    store = seen["store"]
    boundary_id = f"{seen['kwargs']['execution_id']}/check_package/v1"
    events = await store.replay(BOUNDARY_AGGREGATE_TYPE, boundary_id)
    reconciled = events[-1]
    assert reconciled.type == ACCEPTANCE_RECONCILED
    assert reconciled.data["schema_version"] == "ouroboros.acceptance_reconciliation.v3"
    assert reconciled.data["run_accepted"] is False
    assert reconciled.data["existing_run_accepted"] is False
    (criterion,) = reconciled.data["criteria"]
    assert criterion["governed_by"] == "existing_verifier"
    assert criterion["package_status"] == "unverified"
    assert criterion["existing_outcome"] == "failed"
    assert verify_boundary_order(events) == ()
    (result,) = seen["parallel_result"].results
    assert result.success is False and result.outcome.value == "failed"
    assert seen["telemetry"]["terminal_status"] == "failed"
    meta = await _check_package_meta(seen)
    assert meta["legacy_verdict"] == "reject"
    assert meta["reconciliation"] == "legacy_decided_unverified"
    assert meta["verification_coverage"] == "low"
    assert meta["legacy_failure_class"] == "other"  # no recovery record in this double
    await store.close()


async def test_cli_package_fail_keeps_a_rejected_run_failed(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}
    with pytest.raises(typer.Exit) as exit_info:
        await _run_cli(
            tmp_path,
            repo,
            check_package=True,
            constructor_cls=_constructor_factory(BUGFIX_SCRIPT, []),
            worker_edit=None,
            monkeypatch=monkeypatch,
            run_success=False,
            seen=seen,
        )
    assert exit_info.value.exit_code == 1
    assert (await _check_package_meta(seen))["reconciliation"] == "agree"


async def test_cli_flag_off_keeps_a_rejected_run_failed(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ouroboros.config.models import BoundaryConfig

    calls: list[dict[str, Any]] = []
    seen: dict[str, Any] = {}
    with (
        patch(
            "ouroboros.boundary.switch._load_boundary_config",
            return_value=BoundaryConfig(check_package="off"),
        ),
        pytest.raises(typer.Exit) as exit_info,
    ):
        await _run_cli(
            tmp_path,
            repo,
            check_package=None,
            constructor_cls=_constructor_factory(BUGFIX_SCRIPT, calls),
            worker_edit=FIXED,
            monkeypatch=monkeypatch,
            run_success=False,
            seen=seen,
        )
    assert exit_info.value.exit_code == 1
    assert calls == []
    meta = await _check_package_meta(seen)
    assert meta["check_package"] == "off"
    assert meta["legacy_verdict"] == "reject"
    assert meta["reconciliation"] == "none"


@pytest.mark.parametrize("legacy_passes", [True, False])
async def test_a_cli_constructor_outage_leaves_the_legacy_verifier_deciding(
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    legacy_passes: bool,
) -> None:
    # Constructor outage: no package is admitted, so zero checks exist. The
    # run must not exit 0 unless the legacy verifier passed it.
    run = _run_cli(
        tmp_path,
        repo,
        check_package=True,
        constructor_cls=_failing_constructor_factory("constructor_timeout"),
        worker_edit=None,
        monkeypatch=monkeypatch,
        run_success=legacy_passes,
    )
    if legacy_passes:
        store, _runner, _seen = await run
        await store.close()
    else:
        with pytest.raises(typer.Exit) as exit_info:
            await run
        assert exit_info.value.exit_code == 1
    import re

    raw = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)
    text = " ".join(re.sub(r"[│╭╮╰╯─]", " ", raw).split())
    assert "Check package unavailable (constructor_timeout)" in text
    assert "legacy verification decided this run" in text
