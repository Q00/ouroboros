"""Everything the check package persists is closed: no model-chosen text but an allowlist.

One run, end to end, from a constructor reply in which every field the model
controls carries a sentinel: the reply is built into a package, reference
checked, sealed, recorded, admitted, verified on a candidate, and its
decision reconciled. Then every byte the run persisted (the store and every
journal event of the run) and every line it printed is walked as data.

- ``SNTLHELD`` marks what must never persist: the held-out case (its inputs,
  expected value and id), identifiers the model chose (check ids, case ids,
  assertion ids), its free-text locator and uncovered reason, and the
  reference implementation. The numbers ``737373`` and ``3 * 737373`` are
  the held-out case's values.
- ``sntlvis`` marks model text that may persist, and only in the fields of
  ``VISIBLE_ALLOWLIST``; each is safe for the reason given there.
"""

from __future__ import annotations

from collections.abc import Iterator
import json
from pathlib import Path
from typing import Any

from ouroboros.boundary.acceptance import ExistingOutcome, reconcile_acceptance
from ouroboros.boundary.constructor import ConstructionOutcome
from ouroboros.boundary.events import BOUNDARY_AGGREGATE_TYPE
from ouroboros.boundary.ledger import BoundaryLedger
from ouroboros.boundary.oracle_build import package_from_reply
from ouroboros.boundary.reference_check import references_from_reply
from ouroboros.boundary.run_wiring import (
    CheckPackageSettings,
    forget_live_state,
    prepare_check_package,
    render_preparation,
    render_verdict,
    verify_check_package,
)
from ouroboros.core.seed import Seed
from ouroboros.persistence.event_store import EventStore

from .clamp_fixtures import BUGGY, FIXED, _seed

HELD = "SNTLHELD"
HELD_NUMBER = 737373
# The held-out input above ``high``: the base (``value > high`` returns
# ``value``) fails the case, so admission keeps the oracle (a held-out case the
# base passes verifies nothing and is excluded).
HELD_VALUE = 3 * HELD_NUMBER
VISIBLE = "sntlvis"

# Model text that may be persisted, by the field that carries it. The package
# record is the product's allowlist projection (``package.manifest_summary``):
# it carries no oracle input name, case, file content or path, so none of
# those fields may carry model text anywhere.
VISIBLE_ALLOWLIST = {
    # The binding's parameter mapping, keyed by the oracle's input names:
    # part of the binding a worker declares and is shown back in repair text.
    "arg_map",
    # A script check's argv in its execution receipt: scripts carry no
    # held-out case and their pass is advisory, never a verified pass.
    "argv",
}

SCRIPT = ".ouroboros_checks/sntlvis_script.py"


def _reply() -> dict[str, Any]:
    names = ["sntlvisvalue", "sntlvislow", "sntlvishigh"]
    return {
        "oracles": [
            {
                "criterion": 1,
                "check_id": f"{HELD}check",
                "role": "reproduction",
                "call_kind": "function",
                "params": names,
                "default_binding": {
                    "symbol": "mathutils.clamp",
                    "arg_map": {name: index for index, name in enumerate(names)},
                },
                "target_named_in_criterion": False,
                "reference": {
                    "source": (
                        f"def clamp(sntlvisvalue, sntlvislow, sntlvishigh):  # {HELD}ref\n"
                        "    return max(sntlvislow, min(sntlvishigh, sntlvisvalue))\n"
                    ),
                    "symbol": "clamp",
                },
                "cases": [
                    {
                        "case_id": f"{HELD}stated",
                        "held_out": False,
                        "args": {"sntlvisvalue": 15, "sntlvislow": 0, "sntlvishigh": 10},
                        "expect": {"kind": "returns", "value": 10},
                    },
                    {
                        "case_id": f"{HELD}case{HELD_NUMBER}",
                        "held_out": True,
                        "args": {
                            "sntlvisvalue": HELD_VALUE,
                            "sntlvislow": -HELD_NUMBER,
                            "sntlvishigh": HELD_NUMBER,
                        },
                        "expect": {"kind": "returns", "value": HELD_NUMBER},
                    },
                ],
            }
        ],
        "checks": [
            {
                "check_id": f"{HELD}script",
                "role": "preservation",
                "argv": ["python3", SCRIPT],
                "cwd": ".",
                "failure_signature": None,
                "target_named_in_criterion": False,
                "assertions": [
                    {
                        "criterion": 2,
                        "assertion_id": f"{HELD}assertion",
                        "locator": f"{HELD}locator {HELD_NUMBER}",
                    }
                ],
            }
        ],
        "files": [
            {
                "path": SCRIPT,
                "content": (
                    "import sys\nsys.path.insert(0, '.')\nfrom mathutils import clamp\n"
                    "assert clamp(5, 0, 10) == 5  # sntlvis\n"
                ),
            }
        ],
        "uncovered": [{"criterion": 3, "reason": f"{HELD}uncovered {HELD_NUMBER}"}],
    }


class _Constructor:
    async def construct(self, seed: Seed, base: Path, *, feedback: Any = ()) -> ConstructionOutcome:
        reply = _reply()
        package = package_from_reply(reply, seed, input_digest="1" * 64, generator="fake")
        return ConstructionOutcome(
            package, None, "1" * 64, "fake", references=references_from_reply(reply)
        )


def _walk(value: Any, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], Any]]:
    """Every dict key and scalar leaf with the field names above it; JSON text is opened."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield (*path, str(key)), str(key)
            yield from _walk(item, (*path, str(key)))
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _walk(item, path)
    elif isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            opened = json.loads(value)
        except ValueError:
            yield path, value
        else:
            yield path, value
            yield from _walk(opened, path)
    else:
        yield path, value


async def test_every_persisted_byte_is_closed_to_model_text_but_the_allowlist(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mathutils.py").write_text(BUGGY)
    seed = _seed()
    store_dir = tmp_path / "store"
    events_db = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
    await events_db.initialize()
    try:
        state = await prepare_check_package(
            seed,
            event_store=events_db,
            constructor=_Constructor(),  # type: ignore[arg-type]
            execution_id="exec_closure",
            base_checkout=repo,
            worker_workspace=repo,
            runtime_label="test",
            settings=CheckPackageSettings(True, max_construction_attempts=1),
            store_dir=store_dir,
        )
        assert state.admitted and state.package is not None
        (repo / "mathutils.py").write_text(FIXED)
        verdict = await verify_check_package(state, event_store=events_db, candidate_checkout=repo)
        keys = state.package.criterion_keys
        # The held-out case ran and passed: this run exercised it.
        assert verdict.verdicts[keys[0]].status.value == "pass"
        legacy = {
            index: ExistingOutcome(index, "succeeded", "accepted", "completed")
            for index in range(len(keys))
        }
        reconciliation = reconcile_acceptance(
            keys,
            verdict.verdicts,
            legacy,
            existing_run_accepted=True,
            legacy_decides_unverified=True,
        )
        await BoundaryLedger(events_db).record_acceptance_reconciled(
            state.boundary_id,
            package_id=state.package.package_id,
            reconciliation=reconciliation.to_payload(),
        )
        documents: list[Any] = []
        for aggregate in (state.execution_id, *state.versions):
            documents += [
                event.data for event in await events_db.replay(BOUNDARY_AGGREGATE_TYPE, aggregate)
            ]
        stored = [path for path in store_dir.rglob("*") if path.is_file()]
        assert any(path.parent.name == "packages" for path in stored)
        assert any(path.parent.name == "receipts" for path in stored)
        for path in stored:
            text = path.read_text(errors="replace")
            if path.suffix == ".json":
                documents.append(json.loads(text))
            else:  # a base snapshot file: no value of the package at all
                assert HELD not in text and str(HELD_NUMBER) not in text, path
                assert str(HELD_VALUE) not in text, path
        printed = "\n".join([*render_preparation(state), *render_verdict(verdict)]) + json.dumps(
            verdict.summary()
        )
        forget_live_state(state)
    finally:
        await events_db.close()

    visible_fields: set[str] = set()
    for path, leaf in (item for document in documents for item in _walk(document)):
        if isinstance(leaf, bool):
            continue
        if isinstance(leaf, int | float):
            assert abs(leaf) not in (HELD_NUMBER, HELD_VALUE), path
            continue
        if not isinstance(leaf, str):
            continue
        assert HELD not in leaf, path
        if VISIBLE in leaf.lower():
            assert set(path) & VISIBLE_ALLOWLIST, path
            visible_fields |= set(path) & VISIBLE_ALLOWLIST
    assert HELD not in printed and str(HELD_NUMBER) not in printed
    assert str(HELD_VALUE) not in printed
    # The allowlist is exercised, not vacuous.
    assert visible_fields == VISIBLE_ALLOWLIST
