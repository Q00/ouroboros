"""Opt-in check-package wiring around ``ooo run`` (CLI ``_run_orchestrator``)."""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import typer

from ouroboros.boundary import (
    BoundaryLeakError,
    BoundaryLedger,
    BoundaryOrderError,
    CheckStatus,
    PackageVerdict,
    admit_check_package,
    seed_criterion_keys,
    seed_digest,
    verify_boundary_order,
)
from ouroboros.boundary.constructor import (
    CHECK_DIR,
    ConstructionOutcome,
    package_from_reply,
)
from ouroboros.boundary.events import (
    ACTOR_STARTED,
    ADMISSION_COMPLETED,
    BOUNDARY_AGGREGATE_TYPE,
    CANDIDATE_VERIFIED,
    CONSTRUCTION_FAILED,
    PACKAGE_FROZEN,
    SELECTION_DECIDED,
    SUPERSEDED,
)
from ouroboros.boundary.run_wiring import (
    CheckPackageSettings,
    RegenerationPolicy,
    prepare_check_package,
    resolve_check_package_settings,
    verify_check_package,
)
from ouroboros.cli.commands.run import _run_orchestrator
from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata
from ouroboros.core.types import Result
from ouroboros.persistence.event_store import EventStore

PY = sys.executable
INPUT_DIGEST = "2" * 64
BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"

BUGFIX_SCRIPT = """import sys
sys.path.insert(0, ".")
SIG = "OUROBOROS_CHECK_FAILED:repro_add"
from calc import add
if add(2, 3) != 5:
    print(SIG)
    print("expected 5, observed", add(2, 3))
    sys.exit(1)
"""

PASSES_ON_BASE_SCRIPT = """print("nothing asserted")
"""

FEATURE_GUARDED_SCRIPT = """import sys
sys.path.insert(0, ".")
SIG = "OUROBOROS_CHECK_FAILED:repro_multiply"
try:
    from calc import multiply
except ImportError:
    print(SIG)
    print("expected calc.multiply to exist; observed: missing")
    sys.exit(1)
if multiply(3, 4) != 12:
    print(SIG)
    print("expected 12, observed", multiply(3, 4))
    sys.exit(1)
"""

FEATURE_UNGUARDED_SCRIPT = """import sys
sys.path.insert(0, ".")
from calc import multiply
if multiply(3, 4) != 12:
    print("OUROBOROS_CHECK_FAILED:repro_multiply")
    sys.exit(1)
"""


def _seed(*criteria: str, score: float | None = 0.05) -> Seed:
    return Seed(
        goal="calc works",
        acceptance_criteria=criteria,
        ontology_schema=OntologySchema(name="calc", description="calculator"),
        metadata=SeedMetadata(seed_id="seed_wiring", ambiguity_score=score),
    )


def _reply(check_id: str, script: str, *, criterion: int = 1, uncovered: tuple = ()) -> dict:
    path = f"{CHECK_DIR}/{check_id}.py"
    return {
        "checks": [
            {
                "check_id": check_id,
                "role": "reproduction",
                "argv": [PY, path],
                "cwd": ".",
                "failure_signature": f"OUROBOROS_CHECK_FAILED:{check_id}",
                "assertions": [{"criterion": criterion, "locator": "main assertion"}],
            }
        ],
        "files": [{"path": path, "content": script}],
        "uncovered": [{"criterion": c, "reason": "not mechanical"} for c in uncovered],
    }


def _package(seed: Seed, check_id: str, script: str, **kwargs: Any):
    return package_from_reply(
        _reply(check_id, script, **kwargs), seed, input_digest=INPUT_DIGEST, generator="fake"
    )


class FakeConstructor:
    """Returns queued outcomes; records every call."""

    def __init__(self, *outcomes: ConstructionOutcome) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, ...]] = []

    async def construct(self, seed: Seed, base: Path, *, feedback=()) -> ConstructionOutcome:
        self.calls.append(tuple(feedback))
        return self.outcomes.pop(0)


def _ok(package) -> ConstructionOutcome:
    return ConstructionOutcome(package, None, package.input_digest, "fake")


@pytest.fixture
async def store():
    event_store = EventStore("sqlite+aiosqlite:///:memory:")
    await event_store.initialize()
    yield event_store
    await event_store.close()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "calc.py").write_text(BUGGY)
    return root


async def _types(store: EventStore, boundary_id: str) -> list[str]:
    return [event.type for event in await store.replay(BOUNDARY_AGGREGATE_TYPE, boundary_id)]


# --------------------------------------------------------------------------
# Ordering, verification, selection


async def test_package_is_frozen_and_admitted_before_the_actor_starts(
    store, repo: Path, tmp_path: Path
) -> None:
    seed = _seed("add(2, 3) returns 5")
    constructor = FakeConstructor(_ok(_package(seed, "repro_add", BUGFIX_SCRIPT)))
    settings = CheckPackageSettings(enabled=True)

    state = await prepare_check_package(
        seed,
        event_store=store,
        constructor=constructor,
        execution_id="exec_1",
        base_checkout=repo,
        worker_workspace=repo,
        runtime_label="codex",
        settings=settings,
        store_dir=tmp_path / "store",
    )

    assert state.admitted and state.boundary_id == "exec_1/check_package/v1"
    assert await _types(store, state.boundary_id) == [
        PACKAGE_FROZEN,
        ADMISSION_COMPLETED,
        ACTOR_STARTED,
    ]
    assert not (repo / CHECK_DIR).exists()
    assert state.package_path is not None and state.package_path.parent.parent == tmp_path / "store"

    (repo / "calc.py").write_text(FIXED)  # the worker's edit
    verdict = await verify_check_package(
        state, event_store=store, candidate_checkout=repo, settings=settings
    )

    assert verdict.verdict == "pass"
    assert verdict.selection is not None and verdict.selection.replaced
    events = await store.replay(BOUNDARY_AGGREGATE_TYPE, state.boundary_id)
    assert [e.type for e in events][-2:] == [CANDIDATE_VERIFIED, SELECTION_DECIDED]
    assert verify_boundary_order(events) == ()


async def test_failing_candidate_reports_a_counterexample_and_keeps_the_incumbent(
    store, repo: Path, tmp_path: Path
) -> None:
    seed = _seed("add(2, 3) returns 5")
    settings = CheckPackageSettings(enabled=True)
    state = await prepare_check_package(
        seed,
        event_store=store,
        constructor=FakeConstructor(_ok(_package(seed, "repro_add", BUGFIX_SCRIPT))),
        execution_id="exec_2",
        base_checkout=repo,
        worker_workspace=repo,
        runtime_label="codex",
        settings=settings,
        store_dir=tmp_path / "store",
    )
    (repo / "calc.py").write_text("def add(a, b):\n    return a * b\n")

    verdict = await verify_check_package(
        state, event_store=store, candidate_checkout=repo, settings=settings
    )

    assert verdict.verdict == "fail"
    assert verdict.selection is not None and not verdict.selection.replaced
    (example,) = verdict.counterexamples
    assert example.check_id == "repro_add"
    assert "expected 5, observed 6" in example.output_tail


async def test_workspace_holding_a_generated_check_is_refused(
    store, repo: Path, tmp_path: Path
) -> None:
    seed = _seed("add(2, 3) returns 5")
    (repo / CHECK_DIR).mkdir()
    (repo / CHECK_DIR / "repro_add.py").write_text(BUGFIX_SCRIPT)
    # A copy under another name is caught by content digest as well.
    workspace = tmp_path / "worker"
    workspace.mkdir()
    (workspace / "calc.py").write_text(BUGGY)
    (workspace / "renamed.py").write_text(BUGFIX_SCRIPT)
    clean_base = tmp_path / "base"
    clean_base.mkdir()
    (clean_base / "calc.py").write_text(BUGGY)

    with pytest.raises(BoundaryLeakError):
        await prepare_check_package(
            seed,
            event_store=store,
            constructor=FakeConstructor(_ok(_package(seed, "repro_add", BUGFIX_SCRIPT))),
            execution_id="exec_leak",
            base_checkout=clean_base,
            worker_workspace=workspace,
            runtime_label="codex",
            settings=CheckPackageSettings(enabled=True),
            store_dir=tmp_path / "store",
        )
    assert ACTOR_STARTED not in await _types(store, "exec_leak/check_package/v1")


# --------------------------------------------------------------------------
# (a) Bug-fix and feature tasks through the constructor's reply format


async def test_bugfix_check_is_admitted(repo: Path, tmp_path: Path) -> None:
    seed = _seed("add(2, 3) returns 5")
    result = await admit_check_package(
        _package(seed, "repro_add", BUGFIX_SCRIPT), repo, work_dir=tmp_path / "w"
    )
    assert result.verdict is PackageVerdict.ADMITTED


async def test_feature_check_guarding_the_missing_symbol_is_admitted(
    repo: Path, tmp_path: Path
) -> None:
    seed = _seed("multiply(3, 4) returns 12")
    package = _package(seed, "repro_multiply", FEATURE_GUARDED_SCRIPT)

    base = await admit_check_package(package, repo, work_dir=tmp_path / "w1")
    assert base.verdict is PackageVerdict.ADMITTED
    assert base.checks[0].reason == "reached_failing_assertion"

    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "calc.py").write_text(BUGGY + "\n\ndef multiply(a, b):\n    return a * b\n")
    from ouroboros.boundary import verify_candidate

    verified = await verify_candidate(package, candidate, work_dir=tmp_path / "w2")
    assert verified.verdict.value == "pass"


async def test_unguarded_feature_import_is_indeterminate_which_is_why_the_prompt_guards(
    repo: Path, tmp_path: Path
) -> None:
    seed = _seed("multiply(3, 4) returns 12")
    package = _package(seed, "repro_multiply", FEATURE_UNGUARDED_SCRIPT)

    result = await admit_check_package(package, repo, work_dir=tmp_path / "w")

    assert result.verdict is PackageVerdict.INDETERMINATE
    assert result.checks[0].status is CheckStatus.INDETERMINATE
    assert result.checks[0].reason == "failure_signature_absent"


def test_constructor_prompt_requires_guarded_feature_checks() -> None:
    from ouroboros.agents.loader import load_agent_prompt

    prompt = load_agent_prompt("check-constructor")
    assert "Feature tasks" in prompt
    assert "OUROBOROS_CHECK_FAILED:<check_id>" in prompt
    assert "—" not in prompt


def test_unlinked_criteria_are_recorded_as_uncovered_never_dropped() -> None:
    seed = _seed("add(2, 3) returns 5", "code is readable", "docs mention add")
    package = _package(seed, "repro_add", BUGFIX_SCRIPT, uncovered=(2,))
    keys = seed_criterion_keys(seed)

    reasons = {item.criterion_key: item.reason for item in package.uncovered}
    assert reasons == {keys[1]: "not mechanical", keys[2]: "constructor_omitted"}


# --------------------------------------------------------------------------
# (b) Regeneration: product supersedes, study keeps exactly one


async def test_product_regeneration_supersedes_the_rejected_version(
    store, repo: Path, tmp_path: Path
) -> None:
    seed = _seed("add(2, 3) returns 5")
    vacuous = _package(seed, "repro_vacuous", PASSES_ON_BASE_SCRIPT)
    good = _package(seed, "repro_add", BUGFIX_SCRIPT)
    constructor = FakeConstructor(_ok(vacuous), _ok(good))

    state = await prepare_check_package(
        seed,
        event_store=store,
        constructor=constructor,
        execution_id="exec_regen",
        base_checkout=repo,
        worker_workspace=repo,
        runtime_label="codex",
        settings=CheckPackageSettings(enabled=True, max_construction_attempts=2),
        store_dir=tmp_path / "store",
    )

    assert state.admitted and state.boundary_id == "exec_regen/check_package/v2"
    assert state.package is not None and state.package.sha256 == good.sha256
    assert any("reproduction_passed_on_base" in item for item in constructor.calls[1])
    v1 = await store.replay(BOUNDARY_AGGREGATE_TYPE, "exec_regen/check_package/v1")
    assert [e.type for e in v1] == [PACKAGE_FROZEN, ADMISSION_COMPLETED, SUPERSEDED]
    assert v1[0].data["package_sha256"] == vacuous.sha256
    assert v1[-1].data["superseded_by"] == "exec_regen/check_package/v2"
    assert v1[-1].data["successor_package_sha256"] == good.sha256
    assert verify_boundary_order(v1) == ()
    v2 = await store.replay(BOUNDARY_AGGREGATE_TYPE, "exec_regen/check_package/v2")
    assert [e.type for e in v2] == [PACKAGE_FROZEN, ADMISSION_COMPLETED, ACTOR_STARTED]


async def test_study_policy_never_regenerates(store, repo: Path, tmp_path: Path) -> None:
    seed = _seed("add(2, 3) returns 5")
    constructor = FakeConstructor(
        _ok(_package(seed, "repro_vacuous", PASSES_ON_BASE_SCRIPT)),
        _ok(_package(seed, "repro_add", BUGFIX_SCRIPT)),
    )

    state = await prepare_check_package(
        seed,
        event_store=store,
        constructor=constructor,
        execution_id="exec_study",
        base_checkout=repo,
        worker_workspace=repo,
        runtime_label="codex",
        settings=CheckPackageSettings(
            enabled=True, max_construction_attempts=5, policy=RegenerationPolicy.STUDY
        ),
        store_dir=tmp_path / "store",
    )

    assert len(constructor.calls) == 1
    assert not state.admitted
    # The rejected version cannot host a worker, so a final version records
    # that no admitted package exists; the worker binds to it.
    assert state.boundary_id == "exec_study/check_package/v2"
    assert await _types(store, state.boundary_id) == [CONSTRUCTION_FAILED, ACTOR_STARTED]
    verdict = await verify_check_package(
        state, event_store=store, candidate_checkout=repo, settings=CheckPackageSettings(True)
    )
    assert verdict.verdict == "indeterminate"


async def test_ledger_keeps_one_seal_per_boundary_id(store, repo: Path, tmp_path: Path) -> None:
    seed = _seed("add(2, 3) returns 5")
    ledger = BoundaryLedger(store)
    package = _package(seed, "repro_add", BUGFIX_SCRIPT)
    await ledger.record_package_frozen("b/v1", package, seed=seed)

    with pytest.raises(BoundaryOrderError):
        await ledger.record_package_frozen("b/v1", package, seed=seed)
    with pytest.raises(BoundaryOrderError):
        await ledger.record_superseded("b/v1", superseded_by="b/v2", reason="x")
    await ledger.record_construction_failed(
        "b/v2", seed_digest=seed_digest(seed), input_digest=INPUT_DIGEST, reason="x"
    )
    await ledger.record_superseded("b/v1", superseded_by="b/v2", reason="x")
    with pytest.raises(BoundaryOrderError):
        await ledger.record_superseded("b/v1", superseded_by="b/v2", reason="x")
    await ledger.record_actor_started("actor", ["b/v2"])
    await ledger.record_construction_failed(
        "b/v3", seed_digest=seed_digest(seed), input_digest=INPUT_DIGEST, reason="x"
    )
    with pytest.raises(BoundaryOrderError):
        await ledger.record_superseded("b/v2", superseded_by="b/v3", reason="bound")


# --------------------------------------------------------------------------
# (c) Optional ambiguity score


async def test_unscored_seed_runs_through_the_boundary(store, repo: Path, tmp_path: Path) -> None:
    seed = _seed("add(2, 3) returns 5", score=None)
    state = await prepare_check_package(
        seed,
        event_store=store,
        constructor=FakeConstructor(_ok(_package(seed, "repro_add", BUGFIX_SCRIPT))),
        execution_id="exec_unscored",
        base_checkout=repo,
        worker_workspace=repo,
        runtime_label="codex",
        settings=CheckPackageSettings(enabled=True),
        store_dir=tmp_path / "store",
    )
    assert state.admitted
    assert state.seed_digest == seed_digest(seed)


# --------------------------------------------------------------------------
# Settings and the CLI entrypoint


def test_switch_precedence_cli_then_env_then_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OUROBOROS_CHECK_PACKAGE", raising=False)
    config = SimpleNamespace(
        boundary=SimpleNamespace(
            check_package="off",
            constructor_timeout_seconds=300,
            check_timeout_seconds=60,
            max_construction_attempts=3,
        )
    )
    with patch("ouroboros.boundary.run_wiring._load_boundary_config", return_value=config.boundary):
        assert resolve_check_package_settings(None).enabled is False
        monkeypatch.setenv("OUROBOROS_CHECK_PACKAGE", "on")
        settings = resolve_check_package_settings(None)
        assert settings.enabled is True and settings.max_construction_attempts == 3
        assert resolve_check_package_settings(False).enabled is False
        monkeypatch.setenv("OUROBOROS_CHECK_PACKAGE", "off")
        assert resolve_check_package_settings(True).enabled is True


def test_config_accepts_yaml_boolean_spelling() -> None:
    from ouroboros.config.models import BoundaryConfig, OuroborosConfig

    assert BoundaryConfig.model_validate({"check_package": True}).check_package == "on"
    assert BoundaryConfig.model_validate({"check_package": False}).check_package == "off"
    assert OuroborosConfig().boundary.check_package == "off"


SEED_DATA = {
    "goal": "Fix add",
    "constraints": [],
    "acceptance_criteria": ["add(2, 3) returns 5"],
    "ontology_schema": {"name": "calc", "description": "calculator", "fields": []},
    "evaluation_principles": [],
    "exit_conditions": [],
    "metadata": {"seed_id": "seed-cli-boundary", "ambiguity_score": 0.1},
}


def _fake_exec() -> SimpleNamespace:
    return SimpleNamespace(
        success=True,
        session_id="sess",
        messages_processed=1,
        duration_seconds=1.0,
        execution_id="exec",
        summary={},
        final_message="done",
    )


async def _run_cli(
    tmp_path: Path,
    project: Path,
    *,
    check_package: bool | None,
    constructor_cls: Any,
    worker_edit: str | None,
    monkeypatch: pytest.MonkeyPatch,
    seen: dict[str, Any] | None = None,
) -> tuple[EventStore, MagicMock, dict[str, Any]]:
    monkeypatch.delenv("OUROBOROS_CHECK_PACKAGE", raising=False)
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
    seen = {} if seen is None else seen

    async def execute_seed(**kwargs: Any):
        seen["kwargs"] = kwargs
        rows = await store.replay(
            BOUNDARY_AGGREGATE_TYPE, f"{kwargs['execution_id']}/check_package/v1"
        )
        seen["events_at_dispatch"] = [event.type for event in rows]
        if worker_edit is not None:
            (project / "calc.py").write_text(worker_edit)
        return Result.ok(_fake_exec())

    runner = MagicMock()
    runner.execute_seed = AsyncMock(side_effect=execute_seed)
    seen["runner"] = runner
    seed_file = project / "seed.yaml"
    seed_file.write_text("goal: ignored\n")
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
    ):
        await _run_orchestrator(
            seed_file, no_qa=True, project_dir=project, check_package=check_package
        )
    return store, runner, seen


def _constructor_factory(script: str, calls: list[dict[str, Any]]) -> Any:
    def factory(**kwargs: Any) -> Any:
        calls.append(kwargs)

        class _Constructor:
            async def construct(self, seed: Seed, base: Path, *, feedback=()):
                return _ok(_package(seed, "repro_add", script))

        return _Constructor()

    return factory


async def test_cli_flag_off_adds_no_model_call_and_no_event(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    from ouroboros.config.models import BoundaryConfig

    with patch(
        "ouroboros.boundary.run_wiring._load_boundary_config", return_value=BoundaryConfig()
    ):
        store, runner, seen = await _run_cli(
            tmp_path,
            repo,
            check_package=None,
            constructor_cls=_constructor_factory(BUGFIX_SCRIPT, calls),
            worker_edit=None,
            monkeypatch=monkeypatch,
        )
    assert calls == []
    assert seen["events_at_dispatch"] == []
    assert set(seen["kwargs"]) == {"seed", "execution_id", "session_id", "parallel"}
    assert not (tmp_path / "store").exists()
    # No boundary aggregate exists anywhere in the journal.
    async with store._engine.connect() as conn:  # noqa: SLF001 - test-only read
        from sqlalchemy import text

        rows = await conn.execute(
            text("SELECT COUNT(*) FROM events WHERE aggregate_type = :t"),
            {"t": BOUNDARY_AGGREGATE_TYPE},
        )
        assert rows.scalar() == 0
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
        CANDIDATE_VERIFIED,
        SELECTION_DECIDED,
    ]
    verified = (await store.replay(BOUNDARY_AGGREGATE_TYPE, f"{execution_id}/check_package/v1"))[3]
    assert verified.data["verdict"] == "pass"
    await store.close()


async def test_cli_flag_on_exits_non_zero_when_the_candidate_fails(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(typer.Exit) as exit_info:
        await _run_cli(
            tmp_path,
            repo,
            check_package=True,
            constructor_cls=_constructor_factory(BUGFIX_SCRIPT, []),
            worker_edit=None,
            monkeypatch=monkeypatch,
        )
    assert exit_info.value.exit_code == 1


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
