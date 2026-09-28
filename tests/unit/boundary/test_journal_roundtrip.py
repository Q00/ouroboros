"""Real receipts round-trip through the journal: nothing here is built as data.

Each case drives the product path of one ``ooo run`` on a tiny buggy
checkout: ``prepare_check_package`` (seal, freeze, admission on the base,
actor start), the per-attempt gate, then ``CheckPackageAuthority`` (final
bindings, candidate verification and its re-run, the decision). Every record
goes through the real ledger, and the journal is replayed
(``verify_boundary_order``, ``recovery_projection``). The authority and the
gate turn any journal refusal into an undecided decision or an unchanged
attempt, so every case also asserts the decision was recorded as made.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

from ouroboros.boundary import check_env
from ouroboros.boundary.admission import admit_check_package
from ouroboros.boundary.authority import CheckPackageAuthority
from ouroboros.boundary.binding import CHECK_DIR
from ouroboros.boundary.constructor import ConstructionOutcome
from ouroboros.boundary.events import (
    ACCEPTANCE_RECONCILED,
    BINDING_RECORDED,
    CANDIDATE_VERIFIED,
    REPLACEMENT_ABANDONED,
    RunContract,
    replacement_abandoned_event,
)
from ouroboros.boundary.ledger import (
    BoundaryLedger,
    BoundaryOrderError,
    RecoveryBound,
    RecoveryUndecidable,
    recovery_projection,
    verify_boundary_order,
)
from ouroboros.boundary.oracle_build import package_from_reply
from ouroboros.boundary.package import seal_package, seed_criterion_keys
from ouroboros.boundary.per_check import HELD_OUT_NOT_DISCRIMINATING
from ouroboros.boundary.run_wiring import (
    BoundaryRunState,
    CheckPackageSettings,
    prepare_check_package,
)
from ouroboros.core.seed import Seed
from ouroboros.orchestrator.parallel_executor_models import (
    ACExecutionOutcome,
    ACExecutionResult,
    ParallelExecutionResult,
    package_settlement_view,
    settle_package_results,
)
from ouroboros.persistence.event_store import EventStore
from ouroboros.runtime.exec_sandbox import SandboxUnavailable, SandboxUnavailableReason

from .clamp_fixtures import BUGGY, FIXED, GOOD_PRESERVE_3, _seed
from .clamp_fixtures import GOOD_REPRO_1 as NON_DISCRIMINATING_REPRO_1

EXECUTION_ID = "exec_roundtrip"
V1 = f"{EXECUTION_ID}/check_package/v1"
V2 = f"{EXECUTION_ID}/check_package/v2"
TIMEOUT_SECONDS = 3

# A held-out case the base fails (7 is above ``high``): only it verifies a fix.
DISCRIMINATING_REPRO_1 = {
    **NON_DISCRIMINATING_REPRO_1,
    "cases": [
        NON_DISCRIMINATING_REPRO_1["cases"][0],
        {
            "case_id": "held",
            "held_out": True,
            "args": {"value": 7, "low": -2, "high": 4},
            "expect": {"kind": "returns", "value": 4},
        },
    ],
}

_SCRIPT_PATH = f"{CHECK_DIR}/keep_2.py"
# A model-written preservation script for criterion 2 (passes on the base).
SCRIPT_CHECK = {
    "check_id": "keep_2",
    "role": "preservation",
    "argv": ["python3", _SCRIPT_PATH],
    "target_named_in_criterion": False,
    "cwd": ".",
    "assertions": [{"criterion": 2, "locator": "main assertion"}],
}
SCRIPT_FILE = {
    "path": _SCRIPT_PATH,
    "content": (
        "import sys\nsys.path.insert(0, '.')\nfrom mathutils import clamp\n"
        "if clamp(5, 0, 10) != 5:\n    print('clamp(5, 0, 10) changed')\n    sys.exit(1)\n"
    ),
}

WRONG = "def clamp(value, low, high):\n    return min(high, value)\n"  # clamp(-5, 0, 10) = -5
# Returns the visible case's value and is otherwise the base.
HARDCODED = (
    "def clamp(value, low, high):\n    if (value, low, high) == (15, 0, 10):\n        return 10\n"
    + (BUGGY.split("\n", 1)[1])
)
# Writes a file every checkout has, whenever it is imported.
MUTATING = "open('data.txt', 'a').write('touched')\n" + FIXED


def _slow(marker: Path, *, once: bool) -> str:
    """A fix whose call of criterion 2's script check sleeps past the timeout.

    A script check that runs out of time is indeterminate (``timeout``) and
    re-run once; an oracle's slow case is that case failing instead
    (``boundary/oracle_run.py``). ``once``: only the first such call sleeps (it
    leaves ``marker`` outside the checkout), so the re-run passes.
    """
    slow = f"value == 5 and not os.path.exists({str(marker)!r})" if once else "value == 5"
    return (
        "import os\nimport time\n\n\ndef clamp(value, low, high):\n"
        f"    if {slow}:\n        open({str(marker)!r}, 'w').close()\n        time.sleep(60)\n"
        "    return max(low, min(high, value))\n"
    )


class _Constructor:
    """The constructor's reply for every call; no replacement call unless ``replacement``."""

    def __init__(self, reply: dict[str, Any], replacement: dict[str, Any] | None = None) -> None:
        self.reply = reply
        if replacement is not None:
            self.replacement = replacement
        else:
            self.construct_replacements = None  # type: ignore[assignment]

    def _outcome(self, seed: Seed, reply: dict[str, Any]) -> ConstructionOutcome:
        package = package_from_reply(reply, seed, input_digest="1" * 64, generator="fake")
        return ConstructionOutcome(package, None, "1" * 64, "fake")

    async def construct(self, seed: Seed, base: Path, *, feedback=()) -> ConstructionOutcome:
        return self._outcome(seed, self.reply)

    async def construct_replacements(self, seed: Seed, base: Path, *, targets):
        if not self.replacement:
            return ConstructionOutcome(None, "constructor_timeout", "2" * 64, "fake")
        return self._outcome(seed, self.replacement)


def _reply(repro: dict[str, Any] = DISCRIMINATING_REPRO_1) -> dict[str, Any]:
    return {"oracles": [repro, GOOD_PRESERVE_3], "checks": [SCRIPT_CHECK], "files": [SCRIPT_FILE]}


@pytest.fixture
async def store():
    event_store = EventStore("sqlite+aiosqlite:///:memory:")
    await event_store.initialize()
    yield event_store
    await event_store.close()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git checkout holding the buggy ``clamp`` and a file no check may change."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "mathutils.py").write_text(BUGGY)
    (root / "data.txt").write_text("kept\n")
    git = ("git", "-c", "user.name=t", "-c", "user.email=t@example.com")
    subprocess.run([*git, "init", "-q"], cwd=root, check=True)
    subprocess.run([*git, "add", "."], cwd=root, check=True)
    subprocess.run([*git, "commit", "-qm", "base"], cwd=root, check=True)
    return root


SETTINGS = CheckPackageSettings(enabled=True, check_timeout_seconds=TIMEOUT_SECONDS)
SETTINGS_CONTRACT = RunContract(check_timeout_seconds=TIMEOUT_SECONDS)


async def _prepare(
    store: EventStore, repo: Path, tmp_path: Path, constructor: Any
) -> tuple[Seed, BoundaryRunState, CheckPackageAuthority]:
    seed = _seed()
    state = await prepare_check_package(
        seed,
        event_store=store,
        constructor=constructor,
        execution_id=EXECUTION_ID,
        base_checkout=repo,
        worker_workspace=repo,
        runtime_label="codex",
        settings=SETTINGS,
        store_dir=tmp_path / "store",
    )
    authority = CheckPackageAuthority(state, SETTINGS, event_store=store, candidate_checkout=repo)
    # The runner's executor: the gate replaces the legacy verifier's rejection.
    authority.install(SimpleNamespace(_ac_retry_attempts=0))
    return seed, state, authority


def _succeeded(index: int) -> ACExecutionResult:
    return ACExecutionResult(
        ac_index=index,
        ac_content=f"criterion {index}",
        success=True,
        outcome=ACExecutionOutcome.SUCCEEDED,
    )


def _parallel(results: list[ACExecutionResult]) -> ParallelExecutionResult:
    return ParallelExecutionResult(
        results=tuple(results),
        success_count=sum(r.outcome is ACExecutionOutcome.SUCCEEDED for r in results),
        failure_count=sum(r.outcome is ACExecutionOutcome.FAILED for r in results),
    )


async def _run(
    seed: Seed, authority: CheckPackageAuthority, *, gate: bool
) -> list[ACExecutionResult]:
    """The worker's attempts (each judged by the gate when ``gate``), settled, then decided."""
    results = [_succeeded(index) for index in range(len(seed.acceptance_criteria))]
    if gate:
        results = [
            await authority.gate(
                seed=seed, ac_index=r.ac_index, result=r, execution_id=EXECUTION_ID
            )
            for r in results
        ]
        results = settle_package_results(results, [package_settlement_view(r) for r in results])
    decided = await authority(
        seed=seed, execution_id=EXECUTION_ID, parallel_result=_parallel(results)
    )
    return list(decided.results)


async def _journal(store: EventStore, state: BoundaryRunState) -> dict[str, list[Any]]:
    """Replay every version of the run; the whole journal is one the product writes."""
    ledger = BoundaryLedger(store)
    run_events = await ledger.events(EXECUTION_ID)
    versions = {version: await ledger.events(version) for version in state.versions}
    for events in versions.values():
        assert verify_boundary_order(events, run_events=run_events) == ()
    projection = recovery_projection(
        EXECUTION_ID, run_events, await ledger.run_versions(EXECUTION_ID)
    )
    assert isinstance(projection, RecoveryBound), projection
    assert projection.boundary_id == state.boundary_id
    return versions


def _decision(
    authority: CheckPackageAuthority, versions: dict[str, list[Any]], state: BoundaryRunState
) -> dict[str, dict[str, Any]]:
    """The one recorded decision, made (never undecided), by criterion key."""
    outcome = authority.outcome
    assert outcome is not None and outcome.error is None, outcome
    decisions = [e for e in versions[state.boundary_id] if e.type == ACCEPTANCE_RECONCILED]
    assert len(decisions) == 1
    assert decisions[0].data.get("undecided_reason") is None
    return {item["criterion_key"]: item for item in decisions[0].data["criteria"]}


def _verifications(versions: dict[str, list[Any]], state: BoundaryRunState) -> list[Any]:
    return [e for e in versions[state.boundary_id] if e.type == CANDIDATE_VERIFIED]


def _statuses(decision: dict[str, dict[str, Any]]) -> list[str]:
    return [decision[key]["package_status"] for key in seed_criterion_keys(_seed())]


# ----------------------------------------------------------------------
# a, b: the gate on every attempt, then the terminal decision


async def test_a_correct_candidate_is_a_verified_pass(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    seed, state, authority = await _prepare(store, repo, tmp_path, _Constructor(_reply()))
    assert state.admitted and not state.admission.excluded_checks
    (repo / "mathutils.py").write_text(FIXED)
    results = await _run(seed, authority, gate=True)
    versions = await _journal(store, state)
    repairs = [
        e
        for e in versions[state.boundary_id]
        if e.type == BINDING_RECORDED and e.data["phase"] == "repair"
    ]
    assert len(repairs) == 3  # the gate judged every attempt and recorded it
    decision = _decision(authority, versions, state)
    assert _statuses(decision) == ["pass", "unverified", "unverified"]
    assert [event.data["verdict"] for event in _verifications(versions, state)] == ["pass"]
    assert all(result.success for result in results)


async def test_b_wrong_candidate_fails(store: EventStore, repo: Path, tmp_path: Path) -> None:
    seed, state, authority = await _prepare(store, repo, tmp_path, _Constructor(_reply()))
    (repo / "mathutils.py").write_text(WRONG)
    results = await _run(seed, authority, gate=True)
    versions = await _journal(store, state)
    decision = _decision(authority, versions, state)
    assert _statuses(decision)[2] == "fail"
    assert [event.data["verdict"] for event in _verifications(versions, state)] == ["fail"]
    assert not results[2].success


# ----------------------------------------------------------------------
# c: a check that times out, and the one re-run


@pytest.mark.parametrize(
    ("once", "expected"),
    [
        (True, ["pass", "unverified", "unverified"]),
        (False, ["pass", "indeterminate", "unverified"]),
    ],
    ids=["times_out_then_passes", "times_out_twice"],
)
async def test_c_timeout_and_rerun_are_journaled(
    store: EventStore, repo: Path, tmp_path: Path, once: bool, expected: list[str]
) -> None:
    seed, state, authority = await _prepare(store, repo, tmp_path, _Constructor(_reply()))
    (repo / "mathutils.py").write_text(_slow(tmp_path / "slept", once=once))
    await _run(seed, authority, gate=False)
    versions = await _journal(store, state)
    first, rerun = _verifications(versions, state)
    assert first.data["verdict"] == "indeterminate"
    assert [c["check_id"] for c in first.data["checks"] if c["reason"] == "timeout"] == [
        "script_2_1"
    ]
    # The re-run runs only the check that timed out.
    assert [check["check_id"] for check in rerun.data["checks"]] == ["script_2_1"]
    assert rerun.data["verdict"] == ("pass" if once else "indeterminate")
    assert _statuses(_decision(authority, versions, state)) == expected


# ----------------------------------------------------------------------
# d, e: a mutation, and no sandbox, are undecided


async def test_d_a_candidate_that_mutates_a_protected_file_is_indeterminate(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    seed, state, authority = await _prepare(store, repo, tmp_path, _Constructor(_reply()))
    (repo / "mathutils.py").write_text(MUTATING)
    await _run(seed, authority, gate=False)
    versions = await _journal(store, state)
    verifications = _verifications(versions, state)
    assert len(verifications) == 2  # a mutation is transient: one re-run, mutated again
    for event in verifications:
        assert event.data["verdict"] == "indeterminate"
        assert event.data["protected_bytes_mutated"] is True
    decision = _decision(authority, versions, state)
    assert _statuses(decision) == ["indeterminate"] * 3
    assert (repo / "data.txt").read_text() == "kept\n"


async def test_e_an_unavailable_sandbox_is_indeterminate(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed, state, authority = await _prepare(store, repo, tmp_path, _Constructor(_reply()))
    (repo / "mathutils.py").write_text(FIXED)

    def refuse(*_args: Any, **_kwargs: Any) -> SandboxUnavailable:
        return SandboxUnavailable(SandboxUnavailableReason.SANDBOX_UNAVAILABLE)

    monkeypatch.setattr(check_env, "confine", refuse)
    await _run(seed, authority, gate=False)
    versions = await _journal(store, state)
    (verification,) = _verifications(versions, state)
    assert verification.data["verdict"] == "indeterminate"
    assert {check["reason"] for check in verification.data["checks"]} == {"sandbox_unavailable"}
    decision = _decision(authority, versions, state)
    assert _statuses(decision) == ["indeterminate"] * 3


# ----------------------------------------------------------------------
# f: a held-out case the base passes verifies nothing


async def test_f_a_non_discriminating_oracle_is_excluded_and_verifies_nothing(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    constructor = _Constructor(_reply(NON_DISCRIMINATING_REPRO_1))
    seed, state, authority = await _prepare(store, repo, tmp_path, constructor)
    assert state.admitted
    assert state.admission.excluded_checks == {"oracle_1": HELD_OUT_NOT_DISCRIMINATING}
    (repo / "mathutils.py").write_text(HARDCODED)
    await _run(seed, authority, gate=False)
    versions = await _journal(store, state)
    decision = _decision(authority, versions, state)
    assert _statuses(decision) == ["uncovered", "unverified", "unverified"]
    first = decision[seed_criterion_keys(seed)[0]]
    assert first["reason"] == f"uncovered:{HELD_OUT_NOT_DISCRIMINATING}"


# A replacement script check for criterion 1 that exits without its signature:
# indeterminate on the base, so the replacement version is not admitted.
UNSIGNED = {
    "checks": [
        {
            **SCRIPT_CHECK,
            "check_id": "repro_1",
            "role": "reproduction",
            "argv": ["python3", f"{CHECK_DIR}/repro_1.py"],
            "failure_signature": "OUROBOROS_CHECK_FAILED:repro_1",
            "assertions": [{"criterion": 1, "locator": "main assertion"}],
        }
    ],
    "files": [{"path": f"{CHECK_DIR}/repro_1.py", "content": "import sys\nsys.exit(1)\n"}],
}


@pytest.mark.parametrize(
    ("replacement", "outcome"),
    [({}, "construction_failed"), (UNSIGNED, "not_admitted")],
    ids=["call_failed", "not_admitted"],
)
async def test_f_an_unadmitted_replacement_leaves_a_run_the_journal_recovers(
    store: EventStore, repo: Path, tmp_path: Path, replacement: dict[str, Any], outcome: str
) -> None:
    """The excluded oracle's criterion gets one replacement call; its version is not bound.

    The product records v2 abandoned in favor of v1 before the worker starts
    on v1, so the recovery projection binds v1.
    """
    constructor = _Constructor(_reply(NON_DISCRIMINATING_REPRO_1), replacement=replacement)
    seed, state, authority = await _prepare(store, repo, tmp_path, constructor)
    assert state.replacement_outcome == outcome
    assert state.boundary_id == V1 and state.versions == (V1, V2)
    (repo / "mathutils.py").write_text(HARDCODED)
    await _run(seed, authority, gate=False)
    versions = await _journal(store, state)
    assert versions[V2][-1].type == REPLACEMENT_ABANDONED
    assert versions[V2][-1].data["bound"] == V1
    assert "pass" not in _statuses(_decision(authority, versions, state))
    # A later version nothing abandoned is not one the product writes.
    ledger = BoundaryLedger(store)
    await ledger.record_construction_failed(
        f"{EXECUTION_ID}/check_package/v3", seed_digest="0" * 64, input_digest="0" * 64, reason="x"
    )
    assert isinstance(await _projection(ledger), RecoveryUndecidable)


async def _projection(ledger: BoundaryLedger) -> Any:
    return recovery_projection(
        EXECUTION_ID, await ledger.events(EXECUTION_ID), await ledger.run_versions(EXECUTION_ID)
    )


async def test_a_worker_starts_only_once_a_later_version_is_abandoned(
    store: EventStore, repo: Path
) -> None:
    """Real admission, then the ledger's own order: v2 must be closed before v1 starts."""
    seed = _seed()
    package = seal_package(
        package_from_reply(_reply(), seed, input_digest="1" * 64, generator="fake")
    )
    ledger = BoundaryLedger(store)
    await ledger.record_check_package_enabled(EXECUTION_ID, SETTINGS_CONTRACT)
    await ledger.record_package_frozen(V1, package, seed=seed)
    admission = await admit_check_package(
        package,
        repo,
        timeout_seconds=TIMEOUT_SECONDS,
        interpreter=check_env.resolve_check_interpreter(repo),
    )
    await ledger.record_admission(V1, admission)
    await ledger.record_construction_failed(
        V2, seed_digest=package.seed_digest, input_digest="0" * 64, reason="replacement_failed:x"
    )
    with pytest.raises(BoundaryOrderError, match="neither superseded nor abandoned"):
        await ledger.record_actor_started(EXECUTION_ID, [V1])
    await ledger.record_replacement_abandoned(V2, bound=V1)
    await ledger.record_actor_started(EXECUTION_ID, [V1])
    assert isinstance(await _projection(ledger), RecoveryBound)


async def test_nothing_is_abandoned_after_the_worker_started(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    _, state, _ = await _prepare(store, repo, tmp_path, _Constructor(_reply()))
    ledger = BoundaryLedger(store)
    await ledger.record_construction_failed(
        V2, seed_digest=state.seed_digest, input_digest="0" * 64, reason="replacement_failed:x"
    )
    assert isinstance(await _projection(ledger), RecoveryUndecidable)
    with pytest.raises(BoundaryOrderError, match="abandoned only while"):
        await ledger.record_replacement_abandoned(V2, bound=V1)
    # Written past the ledger, the late abandonment still leaves v2 standing on replay.
    await store.append(replacement_abandoned_event(V2, bound=V1, package_id=None))
    assert isinstance(await _projection(ledger), RecoveryUndecidable)


def test_hardcoded_candidate_is_the_base_but_for_the_visible_case() -> None:
    namespace: dict[str, Any] = {}
    exec(HARDCODED, namespace)  # noqa: S102 - test data
    clamp = namespace["clamp"]
    assert clamp(15, 0, 10) == 10 and clamp(16, 0, 10) == 16
