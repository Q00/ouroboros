"""Opt-in check-package wiring around ``ooo run`` (CLI ``_run_orchestrator``)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ouroboros.boundary.acceptance import PackageCriterionStatus, reconcile_acceptance
from ouroboros.boundary.admission import admit_check_package
from ouroboros.boundary.binding import CHECK_DIR
from ouroboros.boundary.events import (
    ACTOR_STARTED,
    ADMISSION_COMPLETED,
    BOUNDARY_AGGREGATE_TYPE,
    CANDIDATE_VERIFIED,
    CONSTRUCTION_FAILED,
    PACKAGE_FROZEN,
    SUPERSEDED,
    RunContract,
    boundary_version_id,
)
from ouroboros.boundary.ledger import (
    BoundaryLeakError,
    BoundaryLedger,
    BoundaryOrderError,
    verify_boundary_order,
)
from ouroboros.boundary.oracle_build import DECLARED_NOT_EXECUTABLE
from ouroboros.boundary.package import seal_package, seed_criterion_keys, seed_digest
from ouroboros.boundary.receipts import CheckStatus, PackageVerdict
from ouroboros.boundary.run_wiring import (
    CheckPackageSettings,
    prepare_check_package,
    render_preparation,
    verify_check_package,
)
from ouroboros.persistence.event_store import EventStore

from .calc_fixtures import (
    BUGFIX_SCRIPT,
    BUGGY,
    FEATURE_GUARDED_SCRIPT,
    FEATURE_UNGUARDED_SCRIPT,
    FIXED,
    INPUT_DIGEST,
    PASSES_ON_BASE_SCRIPT,
    _package,
    _seed,
)
from .fake_constructors import (
    FakeConstructor,
    _ok,
)

CONTRACT = RunContract(check_timeout_seconds=120)


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
# Ordering and verification


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
    verdict = await verify_check_package(state, event_store=store, candidate_checkout=repo)

    # Script-check package: its pass is advisory, so the artifact verdict is
    # "unverified"; the candidate verification itself passed.
    assert verdict.verdict == "unverified"
    events = await store.replay(BOUNDARY_AGGREGATE_TYPE, state.boundary_id)
    assert [e.type for e in events][-1] == CANDIDATE_VERIFIED
    assert verify_boundary_order(events) == ()


async def test_failing_candidate_reports_a_counterexample(
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

    verdict = await verify_check_package(state, event_store=store, candidate_checkout=repo)

    assert verdict.verdict == "fail"
    (example,) = verdict.counterexamples
    assert example.check_id == state.package.checks[0].check_id
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
    from ouroboros.boundary.admission import verify_candidate

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
    assert chr(0x2014) not in prompt


def test_unlinked_criteria_are_recorded_as_uncovered_never_dropped() -> None:
    seed = _seed("add(2, 3) returns 5", "code is readable", "docs mention add")
    package = _package(seed, "repro_add", BUGFIX_SCRIPT, uncovered=(2,))
    keys = seed_criterion_keys(seed)

    reasons = {item.criterion_key: item.reason for item in package.uncovered}
    assert reasons == {keys[1]: DECLARED_NOT_EXECUTABLE, keys[2]: "constructor_omitted"}


# --------------------------------------------------------------------------
# (b) Regeneration: a version that is not admitted is superseded


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
    # Each version is cited by its opaque id, never its unkeyed digest.
    assert "package_sha256" not in v1[0].data
    assert len(v1[0].data["package_id"]) == 64
    assert v1[0].data["package_id"] not in (vacuous.sha256, good.sha256)
    assert v1[-1].data["superseded_by"] == "exec_regen/check_package/v2"
    assert v1[-1].data["successor_package_id"] == state.package.package_id
    assert verify_boundary_order(v1) == ()
    v2 = await store.replay(BOUNDARY_AGGREGATE_TYPE, "exec_regen/check_package/v2")
    assert [e.type for e in v2] == [PACKAGE_FROZEN, ADMISSION_COMPLETED, ACTOR_STARTED]


async def test_a_single_attempt_that_is_not_admitted_leaves_no_package(
    store, repo: Path, tmp_path: Path
) -> None:
    seed = _seed("add(2, 3) returns 5")
    constructor = FakeConstructor(
        _ok(_package(seed, "repro_vacuous", PASSES_ON_BASE_SCRIPT)),
        _ok(_package(seed, "repro_add", BUGFIX_SCRIPT)),
    )

    state = await prepare_check_package(
        seed,
        event_store=store,
        constructor=constructor,
        execution_id="exec_single",
        base_checkout=repo,
        worker_workspace=repo,
        runtime_label="codex",
        settings=CheckPackageSettings(enabled=True, max_construction_attempts=1),
        store_dir=tmp_path / "store",
    )

    assert len(constructor.calls) == 1
    assert not state.admitted
    # The rejected version cannot host a worker, so a final version records
    # that no admitted package exists; the worker binds to it.
    assert state.boundary_id == "exec_single/check_package/v2"
    assert await _types(store, state.boundary_id) == [CONSTRUCTION_FAILED, ACTOR_STARTED]
    verdict = await verify_check_package(state, event_store=store, candidate_checkout=repo)
    # No admitted package: the run falls back to the legacy verifier.
    assert verdict.verdict == "unavailable" and verdict.verdicts == {}


async def test_ledger_keeps_one_seal_per_boundary_id(store, repo: Path, tmp_path: Path) -> None:
    seed = _seed("add(2, 3) returns 5")
    ledger = BoundaryLedger(store)
    package = seal_package(_package(seed, "repro_add", BUGFIX_SCRIPT))
    v1, v2, v3 = (boundary_version_id("b", number) for number in (1, 2, 3))
    await ledger.record_check_package_enabled("b", CONTRACT)
    await ledger.record_package_frozen(v1, package, seed=seed)

    with pytest.raises(BoundaryOrderError):
        await ledger.record_package_frozen(v1, package, seed=seed)
    with pytest.raises(BoundaryOrderError):
        await ledger.record_superseded(v1, superseded_by=v2, reason="x")
    await ledger.record_construction_failed(
        v2, seed_digest=seed_digest(seed), input_digest=INPUT_DIGEST, reason="x"
    )
    await ledger.record_superseded(v1, superseded_by=v2, reason="x")
    with pytest.raises(BoundaryOrderError):
        await ledger.record_superseded(v1, superseded_by=v2, reason="x")
    await ledger.record_actor_started("b", [v2])
    await ledger.record_construction_failed(
        v3, seed_digest=seed_digest(seed), input_digest=INPUT_DIGEST, reason="x"
    )
    with pytest.raises(BoundaryOrderError):
        await ledger.record_superseded(v2, superseded_by=v3, reason="bound")


# --------------------------------------------------------------------------
# Settings and the CLI entrypoint


SEED_DATA = {
    "goal": "Fix add",
    "constraints": [],
    "acceptance_criteria": ["add(2, 3) returns 5"],
    "ontology_schema": {"name": "calc", "description": "calculator", "fields": []},
    "evaluation_principles": [],
    "exit_conditions": [],
    "metadata": {"seed_id": "seed-cli-boundary", "ambiguity_score": 0.1},
}


def _fake_exec(success: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        success=success,
        session_id="sess",
        messages_processed=1,
        duration_seconds=1.0,
        execution_id="exec",
        summary={},
        final_message="done",
    )


def _parallel_result(legacy_success: bool) -> Any:
    from ouroboros.orchestrator.parallel_executor_models import (
        ACExecutionOutcome,
        ACExecutionResult,
        ParallelExecutionResult,
    )

    outcome = ACExecutionOutcome.SUCCEEDED if legacy_success else ACExecutionOutcome.FAILED
    return ParallelExecutionResult(
        results=(
            ACExecutionResult(
                ac_index=0,
                ac_content="add(2, 3) returns 5",
                success=legacy_success,
                outcome=outcome,
                error=None if legacy_success else "evidence form mismatch",
            ),
        ),
        success_count=1 if legacy_success else 0,
        failure_count=0 if legacy_success else 1,
    )


async def _check_package_meta(seen: dict[str, Any]) -> dict[str, Any]:
    """The run's local outcome summary; telemetry carries none of it."""
    sent = seen["telemetry"]["result_meta"]
    assert not {"check_package", "package_verdict", "reconciliation"} & set(sent)
    kwargs = seen.get("kwargs", {})
    return await seen["check_package_run"].outcome_meta(
        seen["store"],
        execution_id=kwargs.get("execution_id"),
        session_id=kwargs.get("session_id"),
        terminal_status=seen["telemetry"]["terminal_status"],
    )


async def test_reconciliation_must_follow_a_verification_and_is_single(
    store, repo: Path, tmp_path: Path
) -> None:
    seed = _seed("add(2, 3) returns 5")
    package = _package(seed, "repro_add", BUGFIX_SCRIPT)
    settings = CheckPackageSettings(enabled=True)
    state = await prepare_check_package(
        seed,
        event_store=store,
        constructor=FakeConstructor(_ok(package)),
        execution_id="exec_r",
        base_checkout=repo,
        worker_workspace=repo,
        runtime_label="codex",
        settings=settings,
        store_dir=tmp_path / "store",
    )
    ledger = BoundaryLedger(store)
    assert state.package is not None
    reference = state.package.package_id  # the id the journal cites
    # A decision of exactly the frozen manifest's criteria (the journal
    # gateway refuses any other): the one the verification below produces.
    keys = seed_criterion_keys(seed)
    reconciliation = reconcile_acceptance(
        keys,
        dict.fromkeys(keys, PackageCriterionStatus.UNVERIFIED),
        {},
        existing_run_accepted=True,
        legacy_decides_unverified=True,
    ).to_payload()
    with pytest.raises(BoundaryOrderError, match="verification"):
        await ledger.record_acceptance_reconciled(
            state.boundary_id, package_id=reference, reconciliation=reconciliation
        )
    (repo / "calc.py").write_text(FIXED)
    verdict = await verify_check_package(state, event_store=store, candidate_checkout=repo)
    assert verdict.criteria == {keys[0]: "unverified"}
    await ledger.record_acceptance_reconciled(
        state.boundary_id, package_id=reference, reconciliation=reconciliation
    )
    with pytest.raises(BoundaryOrderError):
        await ledger.record_acceptance_reconciled(
            state.boundary_id, package_id=reference, reconciliation=reconciliation
        )


async def test_the_store_holds_no_constructor_reply_or_its_digest(store, tmp_path: Path) -> None:
    # A constructor reply carries every held-out input and expected value, so
    # neither the reply nor an unkeyed digest of it (which would confirm a
    # guessed value offline) may reach the store a same-user worker can read.
    import hashlib

    from .test_incremental import FULL, _base, _constructor, _Runtime
    from .test_incremental import _seed as incremental_seed

    reply = "```json\n" + json.dumps(FULL) + "\n```"
    runtime = _Runtime({})
    base = _base(tmp_path)
    await prepare_check_package(
        incremental_seed(),
        event_store=store,
        constructor=_constructor(runtime, 5),
        execution_id="exec_reply",
        base_checkout=base,
        worker_workspace=base,
        runtime_label="test",
        settings=CheckPackageSettings(True),
        store_dir=tmp_path / "store",
    )
    assert runtime.prompts, "the constructor must have been called"
    stored = [path for path in sorted((tmp_path / "store").rglob("*")) if path.is_file()]
    haystack = b"\n".join(path.read_bytes() for path in stored)
    assert hashlib.sha256(reply.encode()).hexdigest().encode() not in haystack
    assert json.dumps(FULL["oracles"][0]["cases"][1]["args"]).encode() not in haystack
    assert not any("partial" in path.parts for path in stored)


async def test_the_store_and_journal_hold_no_held_out_value_or_unkeyed_digest(
    store, tmp_path: Path
) -> None:
    # After prepare and verify, nothing a same-user worker can read (the store,
    # the journal, the printed lines) carries a held-out case's values or an
    # unkeyed digest of the package that would confirm a guessed value.
    from .test_package_identity import _seed as identity_seed
    from .test_package_identity import guess_probe, held_out_package

    clamp_buggy = "def clamp(value, low, high):\n    if value > high:\n        return value\n    return max(low, value)\n"
    clamp_fixed = "def clamp(value, low, high):\n    return max(low, min(high, value))\n"
    repo = tmp_path / "clamp"
    repo.mkdir()
    (repo / "mathutils.py").write_text(clamp_buggy)
    seed = identity_seed()
    settings = CheckPackageSettings(True)
    state = await prepare_check_package(
        seed,
        event_store=store,
        constructor=FakeConstructor(_ok(held_out_package(seed, repo))),
        execution_id="exec_identity",
        base_checkout=repo,
        worker_workspace=repo,
        runtime_label="test",
        settings=settings,
        store_dir=tmp_path / "store",
    )
    package = state.package
    assert package is not None and package.sealed
    assert [case.held_out for case in package.oracles[0].cases] == [False, True, True]
    (repo / "mathutils.py").write_text(clamp_fixed)
    verdict = await verify_check_package(state, event_store=store, candidate_checkout=repo)
    assert verdict.verdict == "pass"

    events = await store.replay(BOUNDARY_AGGREGATE_TYPE, state.boundary_id)
    stored = [path for path in sorted((tmp_path / "store").rglob("*")) if path.is_file()]
    haystack = b"\n".join(
        (
            *(path.read_bytes() for path in stored),
            json.dumps([event.data for event in events], sort_keys=True).encode(),
            "\n".join(render_preparation(state)).encode(),
            json.dumps(verdict.summary()).encode(),
        )
    )
    assert guess_probe(package, haystack) == []
    assert package.sha256.encode()[:16] not in haystack
    assert all(not key.endswith("package_sha256") for e in events for key in e.data)
    assert verify_boundary_order(events) == ()

    def held_out_entries(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, dict):
            found = [value] if value.get("case_id") in {"c2", "c3"} else []
            return found + [e for item in value.values() for e in held_out_entries(item)]
        if isinstance(value, list):
            return [e for item in value for e in held_out_entries(item)]
        if isinstance(value, str) and value.lstrip().startswith("{"):
            try:
                return held_out_entries(json.loads(value))
            except ValueError:
                return []
        return []

    documents: list[Any] = [event.data for event in events]
    documents += [json.loads(path.read_bytes()) for path in stored if path.suffix == ".json"]
    entries = [entry for document in documents for entry in held_out_entries(document)]
    assert entries, "the probe must see the held-out case ids"
    for entry in entries:
        assert set(entry) <= {"case_id", "held_out", "passed"}, entry


async def test_the_final_verification_runs_under_the_recorded_run_contract(
    store, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review probe: the run records a check timeout of 37 before its worker
    # starts; the final verification must use 37 whatever is configured later.
    import ouroboros.boundary.run_wiring as run_wiring

    seed = _seed("add(2, 3) returns 5")
    state = await prepare_check_package(
        seed,
        event_store=store,
        constructor=FakeConstructor(_ok(_package(seed, "repro_add", BUGFIX_SCRIPT))),
        execution_id="exec_contract",
        base_checkout=repo,
        worker_workspace=repo,
        runtime_label="codex",
        settings=CheckPackageSettings(enabled=True, check_timeout_seconds=37),
        store_dir=tmp_path / "store",
    )
    assert state.contract == RunContract(check_timeout_seconds=37)
    assert await BoundaryLedger(store).run_contract("exec_contract") == state.contract
    seen: list[float] = []
    verify = run_wiring.verify_with_bindings

    async def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs["contract"].check_timeout_seconds)
        return await verify(*args, **kwargs)

    monkeypatch.setattr(run_wiring, "verify_with_bindings", spy)
    (repo / "calc.py").write_text(FIXED)
    await verify_check_package(state, event_store=store, candidate_checkout=repo)
    assert seen == [37]


async def test_late_binding_uses_the_recorded_run_contract(
    store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review probe: the run records a check timeout of 37; the late binding a
    # worker declares after it stops is admitted (one base run) under that
    # recorded value, never under the live default, and so is every check.
    from ouroboros.boundary import binding_flow

    from .test_binding_flow import BUGGY as FLOW_BUGGY
    from .test_binding_flow import FIXED as FLOW_FIXED
    from .test_binding_flow import _Constructor
    from .test_binding_flow import _seed as flow_seed

    repo = tmp_path / "flow_repo"
    repo.mkdir()
    (repo / "mathutils.py").write_text(FLOW_BUGGY)
    seed = flow_seed()
    state = await prepare_check_package(
        seed,
        event_store=store,
        constructor=_Constructor(seed, repo),
        execution_id="exec_late_contract",
        base_checkout=repo,
        worker_workspace=repo,
        runtime_label="codex",
        settings=CheckPackageSettings(True, check_timeout_seconds=37, max_construction_attempts=1),
        store_dir=tmp_path / "store",
    )
    assert state.admission is not None and state.admission.check_tiers["oracle_2"] == "U"
    assert await BoundaryLedger(store).run_contract("exec_late_contract") == RunContract(
        check_timeout_seconds=37
    )
    seen: dict[str, list[int]] = {"binding": [], "candidate": []}
    admit, verify = binding_flow.admit_binding, binding_flow.verify_candidate

    async def admit_spy(*args: Any, **kwargs: Any) -> Any:
        seen["binding"].append(kwargs["timeout_seconds"])
        return await admit(*args, **kwargs)

    async def verify_spy(*args: Any, **kwargs: Any) -> Any:
        seen["candidate"].append(kwargs["timeout_seconds"])
        return await verify(*args, **kwargs)

    monkeypatch.setattr(binding_flow, "admit_binding", admit_spy)
    monkeypatch.setattr(binding_flow, "verify_candidate", verify_spy)
    (repo / "mathutils.py").write_text(
        FLOW_FIXED + "\ndef lerp(a, b, t):\n    return a + (b - a) * t\n"
    )
    keys = seed_criterion_keys(seed)
    verdict = await verify_check_package(
        state,
        event_store=store,
        candidate_checkout=repo,
        declared_entry_points={keys[1]: [{"symbol": "mathutils.lerp"}]},
    )
    assert verdict.assignments["oracle_2"].tier.value == "A_prime"
    assert seen["binding"] == [37]
    assert seen["candidate"] and set(seen["candidate"]) == {37}


async def test_the_product_store_root_resolves_a_symlinked_config_dir(
    store, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A user whose config dir (or home) is a symlink: the product resolves its
    # own store root once, so the no-follow publication below that trusted
    # anchor works and lands in the real directory.
    real = tmp_path / "real_config"
    real.mkdir()
    link = tmp_path / "linked_config"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: link)
    seed = _seed("add(2, 3) returns 5")
    state = await prepare_check_package(
        seed,
        event_store=store,
        constructor=FakeConstructor(_ok(_package(seed, "repro_add", BUGFIX_SCRIPT))),
        execution_id="exec_linked_home",
        base_checkout=repo,
        worker_workspace=repo,
        runtime_label="codex",
        settings=CheckPackageSettings(enabled=True),
    )
    assert state.admitted and state.package is not None
    assert state.store_dir == real.resolve() / "boundary" / "exec_linked_home"
    (record,) = (state.store_dir / "packages").iterdir()
    assert record.name == f"{state.package.package_id}.json" and not record.is_symlink()
    assert list((real / "boundary" / "exec_linked_home" / "receipts").iterdir())


async def test_no_record_is_written_for_a_package_not_bound_to_the_seed(
    store, repo: Path, tmp_path: Path
) -> None:
    # The constructor returns a package built for another Seed: the product
    # refuses it before anything reaches the store, not only at the freeze.
    from ouroboros.boundary.package import CheckPackageError

    seed = _seed("add(2, 3) returns 5")
    other = _seed("sub(5, 3) returns 2")
    with pytest.raises(CheckPackageError):
        await prepare_check_package(
            seed,
            event_store=store,
            constructor=FakeConstructor(_ok(_package(other, "repro_sub", BUGFIX_SCRIPT))),
            execution_id="exec_foreign",
            base_checkout=repo,
            worker_workspace=repo,
            runtime_label="codex",
            settings=CheckPackageSettings(enabled=True),
            store_dir=tmp_path / "store",
        )
    packages = tmp_path / "store" / "packages"
    assert not packages.exists() or list(packages.iterdir()) == []
