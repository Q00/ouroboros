"""Frozen oracle packages: held-out cases, frozen data, isolation, and late binding."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import sys
from typing import Any

import pytest

from ouroboros.boundary.binding import parse_binding
from ouroboros.boundary.oracle import ORACLE_DATA_PATH
from ouroboros.boundary.oracle_build import assemble_package, build_oracle_spec
from ouroboros.boundary.package import (
    CheckPackage,
    CheckPackageError,
    CheckRole,
    PackageFile,
)
from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata

V1_PACKAGE = (
    '{"base_files":[],"checks":[{"argv":["python3",".ouroboros_checks/repro_1.py"],"assertions":'
    '[{"assertion_id":"script_1_1.a1","criterion_key":"ac_1","file":null,"locator":null}],'
    '"check_id":"script_1_1","cwd":".","failure_signature":"OUROBOROS_CHECK_FAILED:repro_1",'
    '"role":"reproduction"}],"criterion_keys":["ac_1","ac_2"],'
    '"files":[{"content":"print(1)\\n",'
    '"path":".ouroboros_checks/repro_1.py","sha256":'
    '"cc42155088fca5730758db72b2a5bca33112a941dfaa2d43098ec422ce4ea213"}],"generated_at":'
    '"2026-09-26T00:00:00Z","generator":"fake","input_digest":"' + "b" * 64 + '",'
    '"schema_version":"ouroboros.check_package.v1","scratch_paths":[],"seed_digest":"'
    + "a" * 64
    + '","uncovered":[{"criterion_key":"ac_2","reason":"not executable"}]}'
)
# Pinned digest of V1_PACKAGE's canonical bytes; it changes only with an
# intentional change to the package schema.
V1_DIGEST = "52de5e8af37b5bd6eb0d246c26ace04751865b3861285c7d754261666bf4b999"

BUGGY = "def clamp(value, low, high):\n    if value > high:\n        return value\n    return max(low, value)\n"
FIXED = "def clamp(value, low, high):\n    return max(low, min(high, value))\n"


def _seed(*criteria: str) -> Seed:
    return Seed(
        goal="clamp keeps values inside the bounds",
        acceptance_criteria=criteria or ("clamp(15, 0, 10) returns 10",),
        ontology_schema=OntologySchema(name="mathutils", description="math helpers"),
        metadata=SeedMetadata(seed_id="seed_oracle", ambiguity_score=0.1),
    )


CASES = [
    {
        "case_id": "c1",
        "held_out": False,
        "args": {"value": 15, "low": 0, "high": 10},
        "expect": {"kind": "returns", "value": 10},
    },
    {
        "case_id": "c2",
        "held_out": True,
        "args": {"value": -3, "low": -2, "high": 4},
        "expect": {"kind": "returns", "value": -2},
    },
    {
        "case_id": "c3",
        "held_out": True,
        "args": {"value": 99, "low": 1, "high": 7},
        "expect": {"kind": "returns", "value": 7},
    },
]


def _package(
    seed: Seed,
    base: Path,
    *,
    symbol: str = "mathutils.clamp",
    role: CheckRole = CheckRole.REPRODUCTION,
    cases: list | None = None,
    params: tuple[str, ...] = ("value", "low", "high"),
    call_kind: str = "function",
    arg_map: dict | None = None,
    named: bool = False,
) -> CheckPackage:
    binding = {"symbol": symbol, **({"arg_map": arg_map} if arg_map else {})}
    spec = build_oracle_spec(
        seed,
        criterion_index=0,
        check_id="oracle_1",
        call_kind=call_kind,
        params=params,
        default_binding=binding,
        cases=cases or CASES,
        target_named_in_criterion=named,
    )
    return assemble_package(
        seed,
        input_digest="1" * 64,
        generator="test",
        oracles=[(spec, role)],
        generated_at=datetime(2026, 9, 26, tzinfo=UTC),
    )


def _oracle_result(check: Any) -> dict[str, Any]:
    assert check.oracle_result is not None
    result: dict[str, Any] = check.oracle_result.model_dump(mode="json")
    return result


def _repo(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True)
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return root


@pytest.fixture
def base(tmp_path: Path) -> Path:
    return _repo(tmp_path / "base", {"mathutils.py": BUGGY})


def test_v1_package_keeps_its_bytes_and_digest() -> None:
    package = CheckPackage.model_validate_json(V1_PACKAGE)
    assert package.to_json_bytes().decode() == V1_PACKAGE
    assert package.sha256 == V1_DIGEST
    assert "oracles" not in package.manifest_summary()


def test_held_out_flags_are_frozen_as_the_constructor_declared_them(base: Path) -> None:
    seed = _seed()
    package = _package(seed, base)
    (spec,) = package.oracles
    marks = {case.case_id: case.held_out for case in spec.cases}
    assert marks == {"c1": False, "c2": True, "c3": True}
    frozen = json.loads(next(f.content for f in package.files if f.path == ORACLE_DATA_PATH))
    assert [c["held_out"] for c in frozen["oracles"][0]["cases"]] == [False, True, True]
    assert package.manifest_summary()["oracles"][0]["held_out_count"] == 2


def test_a_declared_visible_case_stays_visible_whatever_the_seed_text(base: Path) -> None:
    # The Seed states only clamp(15, 0, 10); the product does not second-guess
    # the constructor's declaration in either direction.
    seed = _seed()
    unstated_visible = {**CASES[1], "held_out": False}
    stated_held = {**CASES[0], "held_out": True}
    (spec,) = _package(seed, base, cases=[unstated_visible, stated_held]).oracles
    assert [case.held_out for case in spec.cases] == [False, True]


@pytest.mark.parametrize("flag", [None, "true", 1])
def test_a_case_without_a_boolean_held_out_is_a_construction_error(
    base: Path, flag: object
) -> None:
    case = {key: value for key, value in CASES[0].items() if key != "held_out"}
    if flag is not None:
        case["held_out"] = flag
    with pytest.raises(CheckPackageError, match="held_out"):
        _package(_seed(), base, cases=[case])


def test_oracle_hash_covers_cases_signature_and_grammar(base: Path) -> None:
    seed = _seed()
    package = _package(seed, base)
    data = json.loads(next(f.content for f in package.files if f.path == ORACLE_DATA_PATH))
    assert data["binding_grammar"] == "ouroboros.binding_grammar.v1"
    assert data["oracles"][0]["failure_signature"] == "OUROBOROS_CHECK_FAILED:oracle_1"
    assert package.checks[0].failure_signature == "OUROBOROS_CHECK_FAILED:oracle_1"
    assert package.schema_version == "ouroboros.check_package.v2"
    changed = _package(
        seed, base, cases=[CASES[0], {**CASES[1], "expect": {"kind": "returns", "value": -3}}]
    )
    assert changed.sha256 != package.sha256
    tampered = [
        PackageFile.from_content(ORACLE_DATA_PATH, "{}") if f.path == ORACLE_DATA_PATH else f
        for f in package.files
    ]
    with pytest.raises(ValueError, match="oracle data file"):
        package.model_copy(update={"files": tuple(tampered)}).model_validate(
            package.model_copy(update={"files": tuple(tampered)}).model_dump()
        )


def _forged_interpreter(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(0o755)
    return path


def _declared(package: CheckPackage, raw: dict):
    spec = package.oracles[0]
    return parse_binding(
        raw, criterion_key=spec.criterion_key, params=spec.params, call_kind=spec.call_kind
    )


def test_oracle_data_that_breaks_the_schema_is_a_construction_error(base: Path) -> None:
    seed = _seed()
    with pytest.raises(CheckPackageError, match="default binding"):
        build_oracle_spec(
            seed,
            criterion_index=0,
            check_id="o",
            call_kind="function",
            params=("value",),
            default_binding={"symbol": "m.f", "arg_map": {"value": "lambda: 1"}},
            cases=[
                {
                    "case_id": "c",
                    "held_out": True,
                    "args": {"value": 1},
                    "expect": {"kind": "returns", "value": 1},
                }
            ],
        )
    with pytest.raises(CheckPackageError, match="schema"):
        build_oracle_spec(
            seed,
            criterion_index=0,
            check_id="o",
            call_kind="function",
            params=("value",),
            default_binding={"symbol": "m.f"},
            cases=[
                {
                    "case_id": "c",
                    "held_out": True,
                    "args": {"other": 1},
                    "expect": {"kind": "returns", "value": 1},
                }
            ],
        )


@pytest.mark.parametrize(
    ("expected", "observed", "passes"),
    [
        (10, "10", True),
        (10, "10.0", True),
        (10, "10.0000000001", False),  # an integer expectation is exact
        (0, "1e-13", False),
        (0.0, "1e-13", False),  # no absolute floor: use "approx" for that
        (0.3, "0.1 + 0.2", True),  # within a few ulps
        (0.3, "0.3 + 1e-12", False),
    ],
)
def test_comparison_is_exact_for_integers_and_ulp_bounded_for_floats(
    expected: float, observed: str, passes: bool
) -> None:
    # S8, checked on the comparator's own function.
    from ouroboros.boundary import harness

    assert harness.equal(expected, eval(observed), None) is passes


def test_copies_never_carry_bytecode_caches(tmp_path: Path) -> None:
    # __pycache__ is outside the protected digest, so it is not copied.
    from ouroboros.boundary.tree import copy_checkout

    source = _repo(tmp_path / "src", {"m.py": "x = 1\n", "__pycache__/m.cpython-312.pyc": "x"})
    copy_checkout(source, tmp_path / "copy")
    assert (tmp_path / "copy/m.py").is_file()
    assert not (tmp_path / "copy/__pycache__").exists()
