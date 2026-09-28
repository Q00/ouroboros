"""Derived-expectation admission: cases must agree with the constructor's reference.

The round-5 smoke admitted ``clamp(1234, -2345, 3456)`` expected to return
``3456`` (the value is in range, so ``1234`` is right). It failed on the
buggy base as a reproduction case must, so admission kept it, and it then
failed the worker's correct fix on every attempt.
"""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pytest

from ouroboros.boundary.check_env import pin_interpreter
from ouroboros.boundary.events import (
    REFERENCE_CHECK_SCHEMA,
    ReferenceCheckPayload,
    package_frozen_event,
    reference_checked_event,
)
from ouroboros.boundary.ledger import version_state
from ouroboros.boundary.oracle_build import package_from_reply
from ouroboros.boundary.package import seal_package, seed_criterion_keys
from ouroboros.boundary.reference_check import (
    ORACLE_INCONSISTENT,
    REFERENCE_CONTRADICTS_STATED_CASE,
    REFERENCE_UNAVAILABLE,
    check_references,
    references_from_reply,
)
from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata

BUGGY = (
    "def clamp(value, low, high):\n"
    "    if value < low:\n"
    "        return low\n"
    "    if value > high:\n"
    "        return value\n"
    "    return value\n"
)
MARKER = "reference_marker_5b1f"
REFERENCE = f"# {MARKER}\ndef clamp(value, low, high):\n    return max(low, min(value, high))\n"
WRONG_REFERENCE = "def clamp(value, low, high):\n    return value\n"
PROJECT_REFERENCE = "from mathutils import clamp  # the project's code is not reachable\n"


def _seed() -> Seed:
    return Seed(
        goal="Fix mathutils.clamp so that a value above the upper bound is clamped.",
        acceptance_criteria=(
            "clamp(value, low, high) returns high when value > high, low when value < low, "
            "and value otherwise; for example clamp(15, 0, 10) == 10, "
            "clamp(-3, 0, 10) == 0, clamp(7, 0, 10) == 7.",
        ),
        ontology_schema=OntologySchema(name="Clamp", description="clamp helper"),
        metadata=SeedMetadata(seed_id="seed_reference", ambiguity_score=0.1),
    )


def _case(case_id: str, value: int, low: int, high: int, expected: int) -> dict[str, Any]:
    # The constructor declares a case stated by the Seed (``stated_*``) or held out.
    return {
        "case_id": case_id,
        "held_out": not case_id.startswith("stated"),
        "args": {"value": value, "low": low, "high": high},
        "expect": {"kind": "returns", "value": expected},
    }


def _reply(reference: str | None, *cases: dict[str, Any]) -> dict[str, Any]:
    oracle: dict[str, Any] = {
        "criterion": 1,
        "check_id": "c1_clamp",
        "role": "reproduction",
        "call_kind": "function",
        "params": ["value", "low", "high"],
        "default_binding": {"symbol": "mathutils.clamp"},
        "target_named_in_criterion": False,
        "cases": list(cases)
        or [
            _case("stated_above", 15, 0, 10, 10),
            _case("stated_below", -3, 0, 10, 0),
            _case("held_below", -4567, -3210, 5678, -3210),
            # The smoke's slip: in range, so clamp returns 1234, not 3456.
            _case("held_above", 1234, -2345, 3456, 3456),
        ],
    }
    if reference is not None:
        oracle["reference"] = {"source": reference, "symbol": "clamp"}
    return {"oracles": [oracle], "checks": [], "files": [], "uncovered": []}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "mathutils.py").write_text(BUGGY)
    return root


async def _check(reply: dict[str, Any], repo: Path) -> tuple[Any, Any]:
    seed = _seed()
    package = package_from_reply(reply, seed, input_digest="1" * 64, generator="fake")
    return await check_references(
        package,
        references_from_reply(reply),
        seed=seed,
        interpreter=pin_interpreter(sys.executable, "test"),
        timeout_seconds=30,
    )


async def test_the_smoke_case_is_excluded_and_consistent_cases_are_kept(repo: Path) -> None:
    package, report = await _check(_reply(REFERENCE), repo)
    (spec,) = package.oracles
    # Ids are the product's, by position: stated_above c1, stated_below c2,
    # held_below c3, held_above c4.
    assert [(case.case_id, case.held_out) for case in spec.cases] == [
        ("c1", False),
        ("c2", False),
        ("c3", True),
    ]
    # One case excluded from the kept oracle, counted against the package returned.
    assert report.excluded == {"oracle_1": 1}
    assert report.uncovered == {}
    assert report.payload()["excluded_cases"] == [
        {"check_id": "oracle_1", "excluded_count": 1, "reason": ORACLE_INCONSISTENT}
    ]
    # The frozen checks name only the kept cases.
    (check,) = package.checks
    assert [link.assertion_id for link in check.assertions] == [
        "oracle_1.c1",
        "oracle_1.c2",
        "oracle_1.c3",
    ]


async def test_a_consistent_oracle_is_unchanged(repo: Path) -> None:
    reply = _reply(
        REFERENCE,
        _case("stated_above", 15, 0, 10, 10),
        _case("held_above", 1234, -2345, 1000, 1000),
    )
    seed = _seed()
    original = package_from_reply(reply, seed, input_digest="1" * 64, generator="fake")
    package, report = await _check(reply, repo)
    assert package.oracles == original.oracles and package.checks == original.checks
    assert report.excluded == {} and report.uncovered == {}


@pytest.mark.parametrize(
    "reply",
    [
        # The reference does not reproduce a Seed example (clamp(15, 0, 10) == 10).
        _reply(WRONG_REFERENCE),
        # A stated slip on a case whose literals all appear in the Seed text.
        _reply(REFERENCE, _case("stated_above", 15, 0, 10, 15), _case("h", 99, 1, 7, 7)),
    ],
    ids=["reference_misses_example", "stated_value_contradicts_reference"],
)
async def test_a_stated_case_contradiction_makes_the_criterion_uncovered(
    repo: Path, reply: dict[str, Any]
) -> None:
    package, report = await _check(reply, repo)
    key = seed_criterion_keys(_seed())[0]
    assert package.oracles == () and package.checks == ()
    assert [(item.criterion_key, item.reason) for item in package.uncovered] == [
        (key, REFERENCE_CONTRADICTS_STATED_CASE)
    ]
    assert report.uncovered == {key: REFERENCE_CONTRADICTS_STATED_CASE}


@pytest.mark.parametrize(
    "reference", [None, PROJECT_REFERENCE, "def clamp(:\n"], ids=["missing", "project", "syntax"]
)
async def test_a_reference_that_cannot_run_makes_the_criterion_uncovered(
    repo: Path, reference: str | None
) -> None:
    package, report = await _check(_reply(reference), repo)
    key = seed_criterion_keys(_seed())[0]
    assert package.oracles == ()
    assert report.uncovered == {key: REFERENCE_UNAVAILABLE}


async def test_an_oracle_whose_cases_all_disagree_is_uncovered(repo: Path) -> None:
    reply = _reply(REFERENCE, _case("h1", 1234, -2345, 3456, 3456), _case("h2", 5, 6, 9, 5))
    package, report = await _check(reply, repo)
    key = seed_criterion_keys(_seed())[0]
    assert package.oracles == ()
    assert report.uncovered == {key: ORACLE_INCONSISTENT}
    # Dropped whole: no oracle of the returned package to count against.
    assert report.excluded == {}


async def test_an_oracle_left_with_stated_cases_only_is_uncovered(repo: Path) -> None:
    # Every held-out case disagrees with the reference; the stated one agrees.
    # A pass on the stated case alone could verify nothing, so the oracle is
    # not kept and the criterion is uncovered.
    reply = _reply(
        REFERENCE,
        _case("stated_above", 15, 0, 10, 10),
        _case("held_above", 1234, -2345, 3456, 3456),
    )
    package, report = await _check(reply, repo)
    key = seed_criterion_keys(_seed())[0]
    assert package.oracles == ()
    assert report.uncovered == {key: ORACLE_INCONSISTENT}
    assert report.excluded == {}


async def test_a_command_reference_runs_as_a_script(repo: Path) -> None:
    seed = Seed(
        goal="A greeter command.",
        acceptance_criteria=("python greet.py --name Ada prints Hello, Ada",),
        ontology_schema=OntologySchema(name="Greet", description="greeter"),
        metadata=SeedMetadata(seed_id="seed_reference_cli", ambiguity_score=0.1),
    )
    reply = {
        "oracles": [
            {
                "criterion": 1,
                "check_id": "c1_greet",
                "role": "reproduction",
                "call_kind": "cli",
                "params": ["name"],
                "default_binding": {"symbol": "greet.py"},
                "target_named_in_criterion": False,
                "reference": {
                    "source": (
                        "import argparse\n"
                        "parser = argparse.ArgumentParser()\n"
                        "parser.add_argument('--name')\n"
                        "print(f'Hello, {parser.parse_args().name}')\n"
                    ),
                    "symbol": "",
                },
                "cases": [
                    {
                        "case_id": "stated",
                        "held_out": False,
                        "args": {"name": "Ada"},
                        "expect": {"kind": "cli", "exit_code": 0, "stdout_contains": "Hello, Ada"},
                    },
                    {
                        "case_id": "held_ok",
                        "held_out": True,
                        "args": {"name": "Grace"},
                        "expect": {"kind": "cli", "stdout_contains": "Hello, Grace"},
                    },
                    {
                        "case_id": "held_slip",
                        "held_out": True,
                        "args": {"name": "Linus"},
                        "expect": {"kind": "cli", "stdout_contains": "Hello, Linux"},
                    },
                ],
            }
        ],
    }
    package = package_from_reply(reply, seed, input_digest="1" * 64, generator="fake")
    package, report = await check_references(
        package,
        references_from_reply(reply),
        seed=seed,
        interpreter=pin_interpreter(sys.executable, "test"),
        timeout_seconds=30,
    )
    assert report.uncovered == {}
    assert report.excluded == {"oracle_1": 1}  # held_slip
    assert [case.case_id for case in package.oracles[0].cases] == ["c1", "c2"]


def _two_oracles(first: str | None, second: str | None) -> dict[str, Any]:
    """Two oracles of the same criterion, with the given references."""
    (one,) = _reply(first)["oracles"]
    (two,) = _reply(second)["oracles"]
    two = {**two, "check_id": "c1_clamp_again"}
    return {"oracles": [one, two], "checks": [], "files": [], "uncovered": []}


async def test_a_criterion_stays_covered_while_one_of_its_oracles_is_valid(repo: Path) -> None:
    # One valid reference and one missing: the criterion keeps its valid
    # oracle, so it is covered in the package and in the report alike.
    package, report = await _check(_two_oracles(REFERENCE, None), repo)
    assert [spec.check_id for spec in package.oracles] == ["oracle_1"]
    assert [check.check_id for check in package.checks] == ["oracle_1"]
    assert package.uncovered == ()
    assert report.uncovered == {}
    assert report.payload()["uncovered"] == []


async def test_a_criterion_is_uncovered_once_none_of_its_oracles_is_valid(repo: Path) -> None:
    package, report = await _check(_two_oracles(None, PROJECT_REFERENCE), repo)
    key = seed_criterion_keys(_seed())[0]
    assert package.oracles == () and package.checks == ()
    assert [(item.criterion_key, item.reason) for item in package.uncovered] == [
        (key, REFERENCE_UNAVAILABLE)
    ]
    assert report.uncovered == {key: REFERENCE_UNAVAILABLE}
    assert report.payload()["uncovered"] == [
        {"criterion_key": key, "reason": REFERENCE_UNAVAILABLE}
    ]


# Case lists for ``_reply``: the smoke slip excluded (c4), or every held-out
# case disagreeing with the reference (the oracle is dropped).
ONE_SLIP = None
ALL_HELD_OUT_WRONG = (
    _case("stated_above", 15, 0, 10, 10),
    _case("held_above", 1234, -2345, 3456, 3456),
)


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        # The earlier oracle is dropped, so the kept one is renamed oracle_1.
        ((None, ONE_SLIP), (REFERENCE, ONE_SLIP), [("oracle_1", 1)]),
        # The later oracle is dropped whole: it is in no record, its
        # criterion stays covered by the first.
        ((REFERENCE, ONE_SLIP), (REFERENCE, ALL_HELD_OUT_WRONG), [("oracle_1", 1)]),
    ],
    ids=["earlier_dropped", "later_dropped_whole"],
)
async def test_the_reference_record_names_the_frozen_package(
    repo: Path, first: tuple, second: tuple, expected: list
) -> None:
    (one,) = _reply(first[0], *(first[1] or ()))["oracles"]
    (two,) = _reply(second[0], *(second[1] or ()))["oracles"]
    reply = {"oracles": [one, {**two, "check_id": "c1_again"}], "checks": [], "files": []}
    reply["uncovered"] = []
    package, report = await _check(reply, repo)
    sealed = seal_package(package)
    assert [spec.check_id for spec in sealed.oracles] == ["oracle_1"]
    assert [case.case_id for case in sealed.oracles[0].cases] == ["c1", "c2", "c3"]
    payload = report.payload()
    assert payload == {
        "schema_version": REFERENCE_CHECK_SCHEMA,
        "excluded_cases": [
            {"check_id": check_id, "excluded_count": count, "reason": ORACLE_INCONSISTENT}
            for check_id, count in expected
        ],
        "uncovered": [],
    }
    frozen = package_frozen_event("boundary_ref", sealed)
    recorded = reference_checked_event(
        "boundary_ref",
        package_id=sealed.package_id,
        payload=ReferenceCheckPayload.model_validate(payload),
    )
    version_state([frozen, recorded])  # the ledger accepts it: no BoundaryOrderError
