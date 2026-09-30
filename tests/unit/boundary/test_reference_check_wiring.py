"""The reference check inside preparation: only consistent cases are frozen."""

from __future__ import annotations

from collections.abc import Iterator
import json
from pathlib import Path
from typing import Any

import pytest

from ouroboros.boundary.constructor import ConstructionOutcome
from ouroboros.boundary.events import BOUNDARY_AGGREGATE_TYPE
from ouroboros.boundary.ledger import verify_boundary_order
from ouroboros.boundary.oracle_build import package_from_reply
from ouroboros.boundary.reference_check import (
    ORACLE_INCONSISTENT,
    references_from_reply,
)
from ouroboros.boundary.run_wiring import (
    CheckPackageSettings,
    forget_live_state,
    prepare_check_package,
)
from ouroboros.core.seed import Seed
from ouroboros.persistence.event_store import EventStore

from .test_reference_check import BUGGY, MARKER, REFERENCE, _case, _reply, _seed

# Stated cases, a held-out case the base fails (above ``high``, where the base
# returns ``value``) and the smoke's slip (in range, so clamp returns 1234,
# not 3456), which the reference check excludes. A held-out case the base
# already passes would verify nothing, and admission would exclude its oracle.
CASES = (
    _case("stated_above", 15, 0, 10, 10),
    _case("stated_below", -3, 0, 10, 0),
    _case("held_high", 9876, -5432, 8765, 8765),
    _case("held_above", 1234, -2345, 3456, 3456),
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "mathutils.py").write_text(BUGGY)
    return root


class _Constructor:
    def __init__(self, seed: Seed, base: Path, reply: dict[str, Any]) -> None:
        self.outcome = ConstructionOutcome(
            package_from_reply(reply, seed, input_digest="1" * 64, generator="fake"),
            None,
            "1" * 64,
            "fake",
            references=references_from_reply(reply),
        )

    async def construct(self, seed: Seed, base: Path, *, feedback=()) -> ConstructionOutcome:
        return self.outcome


def _scalars(value: Any) -> Iterator[Any]:
    """Every scalar inside a JSON-like value (dict values, list items, the value itself)."""
    if isinstance(value, dict):
        for item in value.values():
            yield from _scalars(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _scalars(item)
    else:
        yield value


async def test_preparation_freezes_only_consistent_cases_and_records_ids(
    repo: Path, tmp_path: Path
) -> None:
    store_events = EventStore("sqlite+aiosqlite:///:memory:")
    await store_events.initialize()
    try:
        seed = _seed()
        state = await prepare_check_package(
            seed,
            event_store=store_events,
            constructor=_Constructor(seed, repo, _reply(REFERENCE, *CASES)),
            execution_id="exec_reference",
            base_checkout=repo,
            worker_workspace=repo,
            runtime_label="codex",
            settings=CheckPackageSettings(True, max_construction_attempts=1),
            store_dir=tmp_path / "store",
        )
        assert state.admitted and state.package is not None
        assert [case.case_id for case in state.package.oracles[0].cases] == ["c1", "c2", "c3"]
        assert state.reference_check is not None
        assert state.reference_check.excluded == {"oracle_1": 1}
        events = await store_events.replay(BOUNDARY_AGGREGATE_TYPE, state.boundary_id)
        types = [event.type for event in events]
        checked = types.index("boundary.oracle.reference_checked")
        assert types.index("boundary.check_package.frozen") < checked
        assert checked < types.index("boundary.actor.started")
        payload = events[checked].data
        # A count against the frozen oracle's id, never a case id or value.
        assert payload["excluded_cases"] == [
            {"check_id": "oracle_1", "excluded_count": 1, "reason": ORACLE_INCONSISTENT}
        ]
        assert payload["package_id"] == state.package.package_id
        # Neither the excluded values nor the reference reach the journal or the store.
        # Values are compared as JSON scalars, so a timestamp that happens to
        # contain the same digits is not a false alarm.
        documents = [event.data for event in events] + [
            json.loads(path.read_text(errors="replace"))
            for path in (tmp_path / "store").rglob("*.json")
        ]
        scalars = [value for document in documents for value in _scalars(document)]
        assert 3456 not in scalars and 2345 not in scalars
        assert not any(isinstance(value, str) and MARKER in value for value in scalars)
        for path in (tmp_path / "store").rglob("*"):
            if path.is_file():
                assert MARKER not in path.read_text(errors="replace")
        forget_live_state(state)
    finally:
        await store_events.close()


def _clamp_oracle(criterion: int, *cases: dict[str, Any]) -> dict[str, Any]:
    return {
        "criterion": criterion,
        "check_id": f"model_{criterion}_{len(cases)}",
        "role": "reproduction",
        "call_kind": "function",
        "params": ["value", "low", "high"],
        "default_binding": {"symbol": "mathutils.clamp"},
        "target_named_in_criterion": False,
        "reference": {"source": REFERENCE, "symbol": "clamp"},
        "cases": list(cases),
    }


class _ReplacingConstructor:
    """``construct`` and ``construct_replacements`` return their replies with references."""

    def __init__(self, seed: Seed, reply: dict[str, Any], replacement: dict[str, Any]) -> None:
        self.seed, self.reply, self.replacement = seed, reply, replacement

    def _outcome(self, reply: dict[str, Any]) -> ConstructionOutcome:
        package = package_from_reply(reply, self.seed, input_digest="1" * 64, generator="fake")
        return ConstructionOutcome(
            package, None, "1" * 64, "fake", references=references_from_reply(reply)
        )

    async def construct(self, seed: Seed, base: Path, *, feedback=()) -> ConstructionOutcome:
        return self._outcome(self.reply)

    async def construct_replacements(self, seed: Seed, base: Path, *, targets):
        return self._outcome(self.replacement)


async def test_a_replacement_version_records_its_reference_check_under_its_own_ids(
    repo: Path, tmp_path: Path
) -> None:
    # Criterion 1 has two oracles: the first passes on the base (excluded at
    # admission), the second loses one inconsistent case to the reference
    # check. Criterion 2's only oracle passes on the base, so it is the
    # replacement target. The merged version re-mints every id: the kept
    # second oracle of criterion 1 becomes ``oracle_1``. Its reference check
    # is recorded against the package actually sealed, and the journal
    # accepts every record of that version.
    seed = _seed()
    seed = seed.model_copy(
        update={
            "acceptance_criteria": (
                seed.acceptance_criteria[0],
                "clamp(5, 0, 10) returns 5 and clamp(20, 0, 10) returns 10",
            )
        }
    )
    passes_on_base = _clamp_oracle(
        1, _case("stated_in", 7, 0, 10, 7), _case("held_in", 3, 0, 10, 3)
    )
    good = _clamp_oracle(1, *CASES)
    target = _clamp_oracle(2, _case("stated_in", 5, 0, 10, 5), _case("held_in", 4, 0, 10, 4))
    replacement = _clamp_oracle(
        2, _case("stated_high", 20, 0, 10, 10), _case("held_high", 9876, -5432, 8765, 8765)
    )
    constructor = _ReplacingConstructor(
        seed,
        {"oracles": [passes_on_base, good, target], "checks": [], "files": [], "uncovered": []},
        {"oracles": [replacement], "checks": [], "files": [], "uncovered": []},
    )
    events_db = EventStore("sqlite+aiosqlite:///:memory:")
    await events_db.initialize()
    try:
        state = await prepare_check_package(
            seed,
            event_store=events_db,
            constructor=constructor,
            execution_id="exec_replaced_reference",
            base_checkout=repo,
            worker_workspace=repo,
            runtime_label="codex",
            settings=CheckPackageSettings(True, max_construction_attempts=1),
            store_dir=tmp_path / "store",
        )
        v1 = await events_db.replay(BOUNDARY_AGGREGATE_TYPE, state.versions[0])
        v1_checked = next(e for e in v1 if e.type == "boundary.oracle.reference_checked")
        assert v1_checked.data["excluded_cases"] == [
            {"check_id": "oracle_1_2", "excluded_count": 1, "reason": ORACLE_INCONSISTENT}
        ]
        assert state.admitted and state.replacement_outcome == "admitted"
        assert state.boundary_id == state.versions[1]
        assert state.package is not None
        assert [check.check_id for check in state.package.checks] == ["oracle_1", "oracle_2"]
        # The kept oracle keeps its count, under the id the merged package gave it.
        assert state.reference_check is not None
        assert state.reference_check.excluded == {"oracle_1": 1}
        v2 = await events_db.replay(BOUNDARY_AGGREGATE_TYPE, state.boundary_id)
        checked = next(e for e in v2 if e.type == "boundary.oracle.reference_checked")
        assert checked.data["package_id"] == state.package.package_id
        assert checked.data["excluded_cases"] == [
            {"check_id": "oracle_1", "excluded_count": 1, "reason": ORACLE_INCONSISTENT}
        ]
        assert checked.data["uncovered"] == []
        assert verify_boundary_order(v2) == ()
        forget_live_state(state)
    finally:
        await events_db.close()
