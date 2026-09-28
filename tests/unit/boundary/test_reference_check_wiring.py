"""The reference check inside preparation: only consistent cases are frozen."""

from __future__ import annotations

from collections.abc import Iterator
import json
from pathlib import Path
from typing import Any

import pytest

from ouroboros.boundary.constructor import ConstructionOutcome
from ouroboros.boundary.events import BOUNDARY_AGGREGATE_TYPE
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

from .test_reference_check import BUGGY, MARKER, REFERENCE, _reply, _seed


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
            constructor=_Constructor(seed, repo, _reply(REFERENCE)),
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
        assert state.reference_check.counts()[ORACLE_INCONSISTENT] == 1
        events = await store_events.replay(BOUNDARY_AGGREGATE_TYPE, state.boundary_id)
        types = [event.type for event in events]
        checked = types.index("boundary.oracle.reference_checked")
        assert types.index("boundary.check_package.frozen") < checked
        assert checked < types.index("boundary.actor.started")
        payload = events[checked].data
        assert payload["excluded_cases"] == [
            {"check_id": "oracle_1", "case_ids": ["c4"], "reason": ORACLE_INCONSISTENT}
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
