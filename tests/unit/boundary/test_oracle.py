"""Frozen oracle packages: held-out cases, frozen data, isolation, and late binding."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any

import pytest

from ouroboros.boundary.admission import (
    CandidateVerdict,
    CheckStatus,
    PackageVerdict,
    admit_binding,
    admit_check_package,
    verify_candidate,
)
from ouroboros.boundary.binding import CallKind, parse_binding
from ouroboros.boundary.oracle import (
    ORACLE_DATA_PATH,
    failed_heldout_only,
    repair_lines,
    worker_visible_seed_text,
)
from ouroboros.boundary.oracle_build import assemble_package, build_oracle_spec
from ouroboros.boundary.package import (
    CheckPackage,
    CheckPackageError,
    CheckRole,
    PackageFile,
    load_check_package,
)
from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata

V1_PACKAGE = (
    '{"base_files":[],"checks":[{"argv":["python3",".ouroboros_checks/repro_1.py"],"assertions":'
    '[{"assertion_id":"repro_1.a1","criterion_key":"ac_1","file":null,"locator":null}],'
    '"check_id":"repro_1","cwd":".","failure_signature":"OUROBOROS_CHECK_FAILED:repro_1",'
    '"role":"reproduction"}],"criterion_keys":["ac_1","ac_2"],"files":[{"content":"print(1)\\n",'
    '"path":".ouroboros_checks/repro_1.py","sha256":'
    '"cc42155088fca5730758db72b2a5bca33112a941dfaa2d43098ec422ce4ea213"}],"generated_at":'
    '"2026-09-26T00:00:00Z","generator":"fake","input_digest":"' + "b" * 64 + '",'
    '"schema_version":"ouroboros.check_package.v1","scratch_paths":[],"seed_digest":"'
    + "a" * 64
    + '","uncovered":[{"criterion_key":"ac_2","reason":"not executable"}]}'
)
# Digest of V1_PACKAGE as computed by the package module at 93c144e82.
V1_DIGEST = "c21403e5bf89eed5ab8c705d25ceaed571602909c2618acc9c3ebd4e6e6fdaa7"

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
        "case_id": "stated",
        "args": {"value": 15, "low": 0, "high": 10},
        "expect": {"kind": "returns", "value": 10},
    },
    {
        "case_id": "held_1",
        "args": {"value": -3, "low": -2, "high": 4},
        "expect": {"kind": "returns", "value": -2},
    },
    {
        "case_id": "held_2",
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
        base_checkout=base,
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
    return check.oracle_result


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


def test_v1_package_keeps_its_bytes_and_digest(tmp_path: Path) -> None:
    package = CheckPackage.model_validate_json(V1_PACKAGE)
    assert package.to_json_bytes().decode() == V1_PACKAGE
    assert package.sha256 == V1_DIGEST
    stored = tmp_path / f"{V1_DIGEST}.json"
    stored.write_text(V1_PACKAGE)
    assert load_check_package(stored, expected_sha256=V1_DIGEST).sha256 == V1_DIGEST
    assert "oracles" not in package.manifest_summary()


def test_held_out_cases_are_in_the_oracle_and_absent_from_the_seed_text(base: Path) -> None:
    seed = _seed()
    package = _package(seed, base)
    (spec,) = package.oracles
    marks = {case.case_id: case.held_out for case in spec.cases}
    assert marks == {"stated": False, "held_1": True, "held_2": True}
    text = worker_visible_seed_text(seed)
    for case in spec.cases:
        if case.held_out:
            assert not all(str(v) in text for v in (*case.args.values(), case.expect.value))
    frozen = json.loads(next(f.content for f in package.files if f.path == ORACLE_DATA_PATH))
    assert [c["held_out"] for c in frozen["oracles"][0]["cases"]] == [False, True, True]
    assert package.manifest_summary()["oracles"][0]["held_out_count"] == 2
    # The constructor's own flag is not trusted: the product rule decides.
    claimed = [{**CASES[0], "held_out": True}]
    (relabelled,) = _package(seed, base, cases=claimed).oracles
    assert relabelled.cases[0].held_out is False


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


async def test_tier_a_bugfix_admits_then_passes_or_fails_with_a_counterexample(
    base: Path, tmp_path: Path
) -> None:
    seed = _seed()
    package = _package(seed, base)
    assert package.oracles[0].default_resolves  # symbol exists at the base: tier A
    admission = await admit_check_package(package, base, reject_unsafe_checks=True)
    assert admission.verdict is PackageVerdict.ADMITTED
    assert admission.checks[0].reason == "reached_failing_assertion"

    fixed = _repo(tmp_path / "fixed", {"mathutils.py": FIXED})
    passed = await verify_candidate(package, fixed)
    assert passed.verdict is CandidateVerdict.PASS
    assert _oracle_result(passed.checks[0])["binding_source"] == "default"

    wrong = await verify_candidate(package, base)
    assert wrong.verdict is CandidateVerdict.FAIL
    result = _oracle_result(wrong.checks[0])
    assert "clamp(value=15, low=0, high=10): expected 10, observed 15" in [
        c["detail"] for c in result["cases"]
    ]
    # The journal keeps case pass/fail only; inputs and observations stay in the receipt.
    journal = wrong.event_summary()["checks"][0]["oracle_result"]
    assert "detail" not in json.dumps(journal) and journal["cases"][0]["passed"] is False


async def test_a_missing_target_is_the_expected_failure_on_the_base(tmp_path: Path) -> None:
    base = _repo(tmp_path / "base", {"mathutils.py": BUGGY})
    seed = _seed("mathutils.lerp(a, b, t) interpolates: lerp(0, 10, 0.5) returns 5")
    package = _package(
        seed,
        base,
        symbol="mathutils.lerp",
        params=("a", "b", "t"),
        cases=[
            {
                "case_id": "stated",
                "args": {"a": 0, "b": 10, "t": 0.5},
                "expect": {"kind": "returns", "value": 5, "approx": 1e-9},
            },
            {
                "case_id": "held",
                "args": {"a": 2, "b": 4, "t": 0.25},
                "expect": {"kind": "returns", "value": 2.5, "approx": 1e-9},
            },
        ],
    )
    admission = await admit_check_package(package, base)
    assert admission.verdict is PackageVerdict.ADMITTED
    assert _oracle_result(admission.checks[0])["resolve"] == "missing"


async def test_an_unrelated_import_error_is_never_a_detected_failure(tmp_path: Path) -> None:
    base = _repo(tmp_path / "base", {"mathutils.py": "import not_installed_dependency\n" + BUGGY})
    package = _package(_seed(), base)
    admission = await admit_check_package(package, base)
    assert admission.verdict is PackageVerdict.INDETERMINATE
    assert admission.checks[0].reason == "failure_signature_absent"
    assert _oracle_result(admission.checks[0])["resolve"] == "import_error"


async def test_workspace_code_editing_the_check_files_is_detected(
    base: Path, tmp_path: Path
) -> None:
    tamper = (
        "import glob, os, stat\n"
        "_parent = os.path.dirname(os.getcwd())\n"
        "for _path in glob.glob(os.path.join(_parent, '*.ctrl', '.ouroboros_checks', 'oracle', '*')):\n"
        "    try:\n"
        "        open(_path, 'a').write('# edited')\n"
        "    except OSError:\n"
        "        os.chmod(_path, stat.S_IRUSR | stat.S_IWUSR)\n"
        "        open(_path, 'a').write('# edited')\n"
    )
    package = _package(_seed(), base)
    candidate = _repo(tmp_path / "cand", {"mathutils.py": tamper + FIXED})
    result = await verify_candidate(package, candidate)
    (check,) = result.checks
    assert check.status is CheckStatus.INDETERMINATE
    assert check.reason == "protected_bytes_mutated"
    assert any(path.startswith("ctrl:") for path in check.mutated_paths)
    assert result.verdict is CandidateVerdict.INDETERMINATE
    # The check files never live inside the checkout copy.
    assert not (candidate / ".ouroboros_checks").exists()


async def test_import_time_monkeypatch_cannot_reach_the_comparison(
    base: Path, tmp_path: Path
) -> None:
    # The target process forges every observation as the stated example's
    # answer. The comparison runs in the harness process, against frozen
    # expectations the target never receives, so held-out cases still fail.
    forge = (
        "import __main__\n"
        "def _forged(target, kind, call):\n"
        "    return {'case_id': call['case_id'], 'outcome': 'returned', 'value': 10,\n"
        "            'encodable': True, 'repr': '10'}\n"
        "__main__._run = _forged\n"
    )
    package = _package(_seed(), base)
    candidate = _repo(tmp_path / "cand", {"mathutils.py": forge + BUGGY})
    result = await verify_candidate(package, candidate)
    assert result.verdict is CandidateVerdict.FAIL
    oracle_result = _oracle_result(result.checks[0])
    assert [c["passed"] for c in oracle_result["cases"]] == [True, False, False]
    assert failed_heldout_only(oracle_result)
    shown = repair_lines(oracle_result)
    assert shown == [
        "- 2 held-out case(s) also failed (inputs withheld; they test the criterion's "
        "general rule, not only the examples in its text)"
    ]
    assert "-3" not in "".join(shown) and "99" not in "".join(shown)


def _declared(package: CheckPackage, raw: dict):
    spec = package.oracles[0]
    return parse_binding(
        raw, criterion_key=spec.criterion_key, params=spec.params, call_kind=spec.call_kind
    )


async def test_late_binding_to_new_code_is_admitted_on_the_base(tmp_path: Path) -> None:
    base = _repo(tmp_path / "base", {"mathutils.py": BUGGY})
    package = _package(
        _seed("values are limited to the bounds, for example 15 in [0, 10] gives 10"),
        base,
        symbol="mathutils.bound",
    )
    assert not package.oracles[0].default_resolves
    binding = _declared(
        package, {"symbol": "limits.limit", "arg_map": {"value": 0, "low": 1, "high": 2}}
    )
    admitted = await admit_binding(package, "oracle_1", binding, base)
    assert admitted.valid and admitted.reason == "binding_admitted_on_base"
    assert admitted.execution.signature_seen

    candidate = _repo(
        tmp_path / "cand",
        {
            "mathutils.py": BUGGY,
            "limits.py": "def limit(v, lo, hi):\n    return max(lo, min(hi, v))\n",
        },
    )
    result = await verify_candidate(
        package, candidate, bindings={"oracle_1": binding}, check_tiers={"oracle_1": "A_prime"}
    )
    assert result.verdict is CandidateVerdict.PASS
    assert result.checks[0].tier == "A_prime"
    assert (result.checks[0].binding or {}).get("symbol") == "limits.limit"
    assert result.bindings == {"oracle_1": binding.to_dict()}


async def test_late_binding_to_base_code_that_already_passes_is_invalid(tmp_path: Path) -> None:
    base = _repo(tmp_path / "base", {"mathutils.py": BUGGY, "safe.py": FIXED})
    package = _package(_seed("values are limited to the bounds"), base, symbol="mathutils.bound")
    binding = _declared(package, {"symbol": "safe.clamp"})
    admitted = await admit_binding(package, "oracle_1", binding, base)
    assert not admitted.valid and not admitted.indeterminate
    assert admitted.reason == "binding_invalid:binding_passes_on_base"


async def test_preservation_binding_must_pass_on_the_base(tmp_path: Path) -> None:
    base = _repo(tmp_path / "base", {"mathutils.py": BUGGY})
    keep = [
        {
            "case_id": "inside",
            "args": {"value": 5, "low": 0, "high": 10},
            "expect": {"kind": "returns", "value": 5},
        }
    ]
    package = _package(
        _seed("values inside the bounds are unchanged"),
        base,
        symbol="mathutils.bound",
        role=CheckRole.PRESERVATION,
        cases=keep,
    )
    broken = _declared(
        package, {"symbol": "mathutils.clamp", "arg_map": {"value": 2, "low": 0, "high": 1}}
    )
    admitted = await admit_binding(package, "oracle_1", broken, base)
    assert admitted.reason == "binding_invalid:binding_fails_on_base"


async def test_binding_admission_timeout_is_indeterminate_and_not_retried(tmp_path: Path) -> None:
    base = _repo(
        tmp_path / "base",
        {
            "mathutils.py": BUGGY,
            "slow.py": "import time\ntime.sleep(30)\ndef f(v, lo, hi):\n    return v\n",
        },
    )
    package = _package(_seed("values are limited"), base, symbol="mathutils.bound")
    binding = _declared(package, {"symbol": "slow.f"})
    admitted = await admit_binding(package, "oracle_1", binding, base, timeout_seconds=1)
    assert admitted.indeterminate and admitted.reason == "binding_admission_timeout"
    assert admitted.execution.timed_out


async def test_cli_oracle_through_a_declared_script(tmp_path: Path) -> None:
    base = _repo(tmp_path / "base", {"README.md": "tool\n"})
    seed = _seed("the tool prints the larger of two numbers")
    spec = build_oracle_spec(
        seed,
        criterion_index=0,
        check_id="oracle_1",
        call_kind="cli",
        params=("a", "b"),
        default_binding={"symbol": "max_tool.py", "call_kind": "cli"},
        cases=[
            {
                "case_id": "c1",
                "args": {"a": 3, "b": 8},
                "expect": {"kind": "cli", "exit_code": 0, "stdout": "8"},
            }
        ],
        base_checkout=base,
    )
    package = assemble_package(
        seed, input_digest="1" * 64, generator="t", oracles=[(spec, CheckRole.REPRODUCTION)]
    )
    assert (await admit_check_package(package, base)).verdict is PackageVerdict.ADMITTED
    script = "import sys\nprint(max(int(sys.argv[1]), int(sys.argv[2])))\n"
    candidate = _repo(tmp_path / "cand", {"bin/pick.py": script})
    binding = parse_binding(
        {"symbol": "bin/pick.py", "call_kind": "cli", "arg_map": {"a": 0, "b": 1}},
        criterion_key=spec.criterion_key,
        params=spec.params,
        call_kind=CallKind.CLI,
    )
    result = await verify_candidate(package, candidate, bindings={"oracle_1": binding})
    assert result.verdict is CandidateVerdict.PASS


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
                {"case_id": "c", "args": {"value": 1}, "expect": {"kind": "returns", "value": 1}}
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
                {"case_id": "c", "args": {"other": 1}, "expect": {"kind": "returns", "value": 1}}
            ],
        )


async def test_float_results_compare_within_a_few_ulps(tmp_path: Path) -> None:
    base = _repo(tmp_path / "base", {"mathutils.py": "def mix(a, b, t):\n    return a\n"})
    seed = _seed("mix(a, b, t) interpolates linearly")
    cases = [
        {
            "case_id": "c",
            "args": {"a": 2, "b": 8, "t": 0.1},
            "expect": {"kind": "returns", "value": 2.6},
        },
        {
            "case_id": "d",
            "args": {"a": 0, "b": 3, "t": 0.1},
            "expect": {"kind": "returns", "value": 0.3},
        },
    ]
    package = _package(seed, base, symbol="mathutils.mix", params=("a", "b", "t"), cases=cases)
    fixed = _repo(
        tmp_path / "fixed", {"mathutils.py": "def mix(a, b, t):\n    return a + (b - a) * t\n"}
    )
    result = await verify_candidate(package, fixed)
    assert result.verdict is CandidateVerdict.PASS  # 0 + 3 * 0.1 is 0.30000000000000004
    off = _repo(
        tmp_path / "off", {"mathutils.py": "def mix(a, b, t):\n    return a + (b - a) * t + 1e-6\n"}
    )
    assert (await verify_candidate(package, off)).verdict is CandidateVerdict.FAIL
