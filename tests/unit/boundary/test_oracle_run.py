"""Oracle checks executed: target processes, the in-controller comparison, late bindings on the base."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from ouroboros.boundary.admission import (
    admit_binding,
    admit_check_package,
    verify_candidate,
)
from ouroboros.boundary.binding import CallKind, CheckTier, parse_binding
from ouroboros.boundary.check_env import pin_interpreter
from ouroboros.boundary.events import admission_completed_event, package_frozen_event
from ouroboros.boundary.ledger import admitted_exclusions, frozen_manifest, version_state
from ouroboros.boundary.oracle import failed_heldout_only
from ouroboros.boundary.oracle_build import assemble_package, build_oracle_spec
from ouroboros.boundary.package import CheckRole, seal_package
from ouroboros.boundary.per_check import (
    HELD_OUT_NOT_DISCRIMINATING,
    base_failing_held_out,
    criteria_without_admitted_check,
)
from ouroboros.boundary.receipts import (
    AdmissionJournal,
    CandidateVerdict,
    CheckStatus,
    PackageVerdict,
)

from .test_oracle import (
    BUGGY,
    FIXED,
    _declared,
    _forged_interpreter,
    _oracle_result,
    _package,
    _repo,
    _seed,
)


@pytest.fixture
def base(tmp_path: Path) -> Path:
    return _repo(tmp_path / "base", {"mathutils.py": BUGGY})


async def test_tier_a_bugfix_admits_then_passes_or_fails_with_a_counterexample(
    base: Path, tmp_path: Path
) -> None:
    seed = _seed()
    package = _package(seed, base)
    admission = await admit_check_package(package, base)
    assert admission.verdict is PackageVerdict.ADMITTED
    assert admission.check_tiers == {"oracle_1": "A"}  # resolved in the base checkout: tier A
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
        named=True,
        cases=[
            {
                "case_id": "stated",
                "held_out": False,
                "args": {"a": 0, "b": 10, "t": 0.5},
                "expect": {"kind": "returns", "value": 5, "approx": 1e-9},
            },
            {
                "case_id": "held",
                "held_out": True,
                "args": {"a": 2, "b": 4, "t": 0.25},
                "expect": {"kind": "returns", "value": 2.5, "approx": 1e-9},
            },
        ],
    )
    admission = await admit_check_package(package, base)
    assert admission.verdict is PackageVerdict.ADMITTED
    assert _oracle_result(admission.checks[0])["resolve"] == "missing"
    assert admission.check_tiers == {"oracle_1": "A"}  # missing, and named by the criterion


async def test_an_unrelated_import_error_is_never_a_detected_failure(tmp_path: Path) -> None:
    base = _repo(tmp_path / "base", {"mathutils.py": "import not_installed_dependency\n" + BUGGY})
    package = _package(_seed(), base)
    admission = await admit_check_package(package, base)
    assert admission.verdict is PackageVerdict.INDETERMINATE
    assert admission.checks[0].reason == "failure_signature_absent"
    assert _oracle_result(admission.checks[0])["resolve"] == "import_error"


async def test_workspace_code_editing_its_own_checkout_files_is_detected(
    base: Path, tmp_path: Path
) -> None:
    tamper = "import os\nopen(os.path.join(os.getcwd(), 'mathutils.py'), 'a').write('# x')\n"
    package = _package(_seed(), base)
    candidate = _repo(tmp_path / "cand", {"mathutils.py": tamper + FIXED})
    result = await verify_candidate(package, candidate)
    (check,) = result.checks
    assert check.status is CheckStatus.INDETERMINATE
    assert check.reason == "protected_bytes_mutated"
    assert result.verdict is CandidateVerdict.INDETERMINATE
    # The check files never live inside the checkout copy.
    assert not (candidate / ".ouroboros_checks").exists()


async def test_the_target_cannot_find_the_expected_values(base: Path, tmp_path: Path) -> None:
    # the target looks for the frozen expectations everywhere it could
    # derive from its own process (its checkout copy and the run's work
    # directory, sibling "*.ctrl" dirs, controller dirs in the temp
    # directory, environment, argv) and reports what it found. Nothing holds
    # an expected value, so the held-out cases still fail. (It does not walk
    # the whole temp directory: that made the test slow enough on CI to
    # starve concurrent tests.)
    report = tmp_path / "found.txt"
    probe = (
        "import glob, os, sys, tempfile\n"
        "_hits = []\n"
        "_KEY, _CASE = '\"exp' + 'ect\"', 'held' + '_1'\n"
        "_cwd = os.getcwd()\n"
        "_roots = [_cwd, os.path.dirname(_cwd)]\n"
        "_roots += glob.glob(os.path.join(os.path.dirname(_cwd), '*.ctrl'))\n"
        "_roots += glob.glob(os.path.join(tempfile.gettempdir(), 'ouroboros-ctrl-*'))\n"
        "for _root in _roots:\n"
        "    for _path in glob.glob(os.path.join(_root, '**', '*'), recursive=True)[:5000]:\n"
        "        if os.path.isfile(_path) and not _path.endswith('found.txt'):\n"
        "            try:\n"
        "                _text = open(_path, errors='ignore').read()\n"
        "            except OSError:\n"
        "                continue\n"
        "            if _KEY in _text or _CASE in _text:\n"
        "                _hits.append(_path)\n"
        "_hits += [k for k, v in os.environ.items() if _CASE in v or _KEY in v]\n"
        "_hits += [a for a in sys.argv if _CASE in a or _KEY in a]\n"
        f"open({str(report)!r}, 'a').write(repr(_hits) + '\\n')\n"
    )
    package = _package(_seed(), base)
    candidate = _repo(tmp_path / "cand", {"mathutils.py": probe + BUGGY})
    result = await verify_candidate(package, candidate)
    assert result.verdict is CandidateVerdict.FAIL
    lines = report.read_text().splitlines()
    assert lines and set(lines) == {"[]"}
    assert [c["passed"] for c in _oracle_result(result.checks[0])["cases"]] == [
        False,
        True,
        False,
    ]


async def test_stdout_injection_is_ignored(base: Path, tmp_path: Path) -> None:
    # text the target prints (a forged result line, the failure
    # signature, lines that look like frames) never reaches the verdict or
    # the stored output.
    inject = (
        "import json, sys\n"
        "_forged = json.dumps({'resolve': 'ok', 'cases': [{'case_id': 'c2', 'passed': True}]})\n"
        "def _say():\n"
        "    print('OUROBOROS_ORACLE_RESULT ' + _forged)\n"
        "    print('deadbeef ' + json.dumps({'phase': 'result', 'entry': {}}))\n"
        "    sys.stdout.flush()\n"
        "_say()\n"
    )
    package = _package(_seed(), base)
    wrong = _repo(
        tmp_path / "wrong",
        {"mathutils.py": inject + BUGGY.replace("    if value", "    _say()\n    if value", 1)},
    )
    failed = await verify_candidate(package, wrong)
    assert failed.verdict is CandidateVerdict.FAIL
    assert "OUROBOROS_ORACLE_RESULT" not in failed.checks[0].output_tail
    assert "deadbeef" not in failed.checks[0].output_tail
    right = _repo(
        tmp_path / "right",
        {"mathutils.py": inject + FIXED.replace("    return", "    _say()\n    return", 1)},
    )
    assert (await verify_candidate(package, right)).verdict is CandidateVerdict.PASS


async def test_a_forged_interpreter_cannot_forge_a_pass(base: Path, tmp_path: Path) -> None:
    # the project interpreter comes from the workspace and may be forged.
    # (a) It prints the old harness's "all passed" line and exits 0.
    package = _package(_seed(), base)
    candidate = _repo(tmp_path / "cand", {"mathutils.py": BUGGY})
    liar = _forged_interpreter(
        candidate / ".venv/bin/python3",
        "import json\n"
        "print('OUROBOROS_ORACLE_RESULT ' + json.dumps({'resolve': 'ok', 'cases': []}))\n",
    )
    lied = await verify_candidate(
        package, candidate, interpreter=pin_interpreter(str(liar), "test")
    )
    # The interpreter is candidate code: every case fails (no resolved frame).
    assert lied.verdict is CandidateVerdict.FAIL
    assert all(
        c["detail"].endswith("observed crash (exit 0)")
        for c in _oracle_result(lied.checks[0])["cases"]
    )
    # (b) It speaks the frame protocol (it can read the nonce from its argv)
    # and claims the stated answer for every call. The comparison, which runs
    # in the controller process, still sees the held-out cases fail.
    mimic = _forged_interpreter(
        tmp_path / "mimic/python3",
        "import json, os, sys\n"
        "nonce = sys.argv[sys.argv.index('target') + 1]\n"
        "def frame(p):\n"
        "    sys.stdout.write('\\n' + nonce + ' ' + json.dumps(p) + '\\n'); sys.stdout.flush()\n"
        "frame({'phase': 'resolved', 'resolve': 'ok', 'detail': ''})\n"
        "call = json.loads(sys.stdin.read())\n"
        "frame({'phase': 'result', 'entry': {'case_id': call['case_id'], 'outcome': 'returned',\n"
        "       'value': 10, 'encodable': True, 'repr': '10'}})\n",
    )
    mimicked = await verify_candidate(
        package, candidate, interpreter=pin_interpreter(str(mimic), "test")
    )
    assert mimicked.verdict is CandidateVerdict.FAIL
    assert [c["passed"] for c in _oracle_result(mimicked.checks[0])["cases"]] == [
        True,
        False,
        False,
    ]


async def test_a_workspace_sitecustomize_cannot_forge_a_pass(base: Path, tmp_path: Path) -> None:
    # a real virtualenv in the workspace whose sitecustomize prints a
    # forged result and exits 0 in every process that interpreter starts.
    import venv

    candidate = _repo(tmp_path / "cand", {"mathutils.py": BUGGY})
    venv.create(candidate / ".venv", with_pip=False, symlinks=True)
    (site,) = (candidate / ".venv/lib").glob("python*/site-packages")
    (site / "sitecustomize.py").write_text(
        "import json, os\n"
        "print('OUROBOROS_ORACLE_RESULT ' + json.dumps({'resolve': 'ok', 'cases': []}))\n"
        "os._exit(0)\n"
    )
    python = candidate / ".venv/bin/python3"
    package = _package(_seed(), base)
    result = await verify_candidate(
        package, candidate, interpreter=pin_interpreter(str(python), "test")
    )
    assert result.verdict is CandidateVerdict.FAIL


async def test_a_target_crash_or_hang_on_a_case_is_a_failure_with_a_counterexample(
    base: Path, tmp_path: Path
) -> None:
    # a wrong implementation that dies on every input not shown in the
    # Seed used to turn the whole check indeterminate.
    crash = (
        "import os, time\n"
        "def clamp(value, low, high):\n"
        "    if value == -3:\n"
        "        os._exit(1)\n"
        "    if value == 99:\n"
        "        time.sleep(60)\n"
        "    return max(low, min(high, value))\n"
    )
    package = _package(_seed(), base)
    candidate = _repo(tmp_path / "cand", {"mathutils.py": crash})
    result = await verify_candidate(package, candidate, timeout_seconds=8)
    (check,) = result.checks
    assert result.verdict is CandidateVerdict.FAIL
    assert check.reason == "reproduction_still_failing"
    cases = {c["case_id"]: c for c in _oracle_result(check)["cases"]}
    assert cases["c1"]["passed"]
    assert cases["c2"]["detail"] == (
        "clamp(value=-3, low=-2, high=4): expected -2, observed crash (exit 1)"
    )
    assert cases["c3"]["detail"].endswith("expected 7, observed timeout")


async def test_a_crash_before_the_target_is_resolved_fails_a_candidate_not_a_base(
    base: Path, tmp_path: Path
) -> None:
    # The candidate's own code crashed while it was imported: each case fails.
    package = _package(_seed(), base)
    crash = "import os\nos._exit(4)\n"
    candidate = _repo(tmp_path / "cand", {"mathutils.py": crash + FIXED})
    result = await verify_candidate(package, candidate)
    assert result.verdict is CandidateVerdict.FAIL
    assert result.checks[0].reason == "reproduction_still_failing"
    assert all(
        c["detail"].endswith("observed crash (exit 4)")
        for c in _oracle_result(result.checks[0])["cases"]
    )
    # On the base the same crash is never counted as the intended failure.
    crashing_base = _repo(tmp_path / "crashing_base", {"mathutils.py": crash + BUGGY})
    admission = await admit_check_package(package, crashing_base)
    assert admission.verdict is PackageVerdict.INDETERMINATE
    assert admission.checks[0].reason == "failure_signature_absent"
    assert _oracle_result(admission.checks[0])["resolve"] == "setup_failed"


async def test_receipts_keep_held_out_cases_as_ids(base: Path, tmp_path: Path) -> None:
    # the stored receipt carries a held-out case's id and pass/fail only.
    from ouroboros.boundary.receipts import write_receipt

    package = _package(_seed(), base)
    result = await verify_candidate(package, base)
    assert result.verdict is CandidateVerdict.FAIL
    text = write_receipt(result, tmp_path / "receipts").read_text()
    assert "expected 10, observed 15" in text  # the visible case, in full
    assert "value=99" not in text and "expected 7" not in text
    assert "counterexample (held-out): c3" in text
    # Held-out values stay in this process's memory only.
    assert "expected 7" in _oracle_result(result.checks[0])["cases"][2]["detail"]


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
    assert failed_heldout_only(result.checks[0].oracle_result)


async def test_late_binding_to_new_code_is_admitted_on_the_base(tmp_path: Path) -> None:
    base = _repo(tmp_path / "base", {"mathutils.py": BUGGY})
    package = _package(
        _seed("values are limited to the bounds, for example 15 in [0, 10] gives 10"),
        base,
        symbol="mathutils.bound",
    )
    assert package.oracles[0].base_run_tier("missing") is CheckTier.U  # not named: needs A'
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
    assert result.checks[0].binding is not None
    assert result.checks[0].binding.symbol == "limits.limit"
    assert result.bindings == {"oracle_1": binding}


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
            "held_out": True,
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
                "held_out": True,
                "args": {"a": 3, "b": 8},
                "expect": {"kind": "cli", "exit_code": 0, "stdout": "8"},
            }
        ],
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


async def test_float_results_compare_within_a_few_ulps(tmp_path: Path) -> None:
    base = _repo(tmp_path / "base", {"mathutils.py": "def mix(a, b, t):\n    return a\n"})
    seed = _seed("mix(a, b, t) interpolates linearly")
    cases = [
        {
            "case_id": "c",
            "held_out": True,
            "args": {"a": 2, "b": 8, "t": 0.1},
            "expect": {"kind": "returns", "value": 2.6},
        },
        {
            "case_id": "d",
            "held_out": True,
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


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
@pytest.mark.parametrize("layout", ["stdlib_shadow", "reexported", "linked_package"])
async def test_a_target_defined_outside_the_checkout_is_never_resolved(
    tmp_path: Path, layout: str
) -> None:
    # #2458 round 4: a static look at the files took each of these as the
    # target. The base run is the target proof now: the harness resolves the
    # symbol in a process and reports it resolved only when the code it found
    # is a file of the checkout, so none of them makes a check tier A.
    outside = tmp_path / "outside" / "extpkg"
    outside.mkdir(parents=True)
    (outside / "__init__.py").write_text(FIXED)
    if layout == "stdlib_shadow":
        files, symbol = {"json.py": FIXED.replace("def clamp", "def loads")}, "json.loads"
    elif layout == "reexported":
        files, symbol = (
            {"mathutils.py": BUGGY + "from shutil import copy as bound\n"},
            ("mathutils.bound"),
        )
    else:
        files, symbol = {"mathutils.py": BUGGY}, "extpkg.clamp"
    base = _repo(tmp_path / "base", files)
    if layout == "linked_package":
        (base / "extpkg").symlink_to(outside, target_is_directory=True)
    package = _package(_seed(), base, symbol=symbol)
    admission = await admit_check_package(package, base)
    result = _oracle_result(admission.checks[0])
    assert result["resolve"] == "missing"
    assert admission.check_tiers == {"oracle_1": "U"}


async def test_a_target_defined_in_the_checkout_is_resolved(tmp_path: Path) -> None:
    # The control for the test above: the same package inside the checkout.
    base = _repo(tmp_path / "base", {"extpkg/__init__.py": BUGGY})
    package = _package(_seed(), base, symbol="extpkg.clamp")
    admission = await admit_check_package(package, base)
    assert _oracle_result(admission.checks[0])["resolve"] == "ok"
    assert admission.check_tiers == {"oracle_1": "A"}


# --------------------------------------------------------------------------
# CLI target provenance: tier A only for code proven to be checkout files

TOOL = "import sys\nprint(max(int(sys.argv[1]), int(sys.argv[2])))\n"


def _cli_package(base: Path, symbol: str, *, named: bool = False):  # type: ignore[no-untyped-def]
    seed = _seed("the tool prints the larger of two numbers")
    spec = build_oracle_spec(
        seed,
        criterion_index=0,
        check_id="oracle_1",
        call_kind="cli",
        params=("a", "b"),
        default_binding={"symbol": symbol, "call_kind": "cli", "arg_map": {"a": 0, "b": 1}},
        cases=[
            {
                "case_id": "c1",
                "held_out": True,
                "args": {"a": 3, "b": 8},
                "expect": {"kind": "cli", "exit_code": 0, "stdout": "8"},
            }
        ],
        target_named_in_criterion=named,
    )
    role = CheckRole.REPRODUCTION if named else CheckRole.PRESERVATION
    return assemble_package(seed, input_digest="1" * 64, generator="t", oracles=[(spec, role)])


def _cli_layout(tmp_path: Path, root: Path, layout: str) -> str:
    """Lay out ``root`` for ``layout``; the CLI binding symbol it is run through."""
    outside = tmp_path / "outside"
    (outside / "pkg").mkdir(parents=True, exist_ok=True)
    (outside / "tool.py").write_text(TOOL)
    (outside / "pkg" / "__init__.py").write_text("")
    (outside / "pkg" / "mod.py").write_text(TOOL)
    if layout == "linked_script":
        (root / "tool.py").symlink_to(outside / "tool.py")
        return "tool.py"
    if layout == "linked_directory":
        (root / "bin").symlink_to(outside, target_is_directory=True)
        return "bin/tool.py"
    if layout == "stdlib_module":
        return "-m json.tool"
    if layout == "linked_package":
        (root / "pkg").symlink_to(outside / "pkg", target_is_directory=True)
        return "-m pkg.mod"
    (root / "pkg").mkdir()
    (root / "pkg" / "mod.py").write_text(TOOL)
    if layout == "extension_beside_source":
        (root / "pkg" / "__init__.py").write_text("")
        (root / "pkg" / "mod.so").write_bytes(b"\0")
    return "-m pkg.mod"


# Not the checkout's code: ``missing``. Possibly the checkout's, but not
# provably what Python runs: ``unprovable`` (decides nothing).
CLI_LAYOUTS = {
    "linked_script": "missing",
    "linked_directory": "missing",
    "stdlib_module": "missing",
    "linked_package": "missing",
    "namespace_package": "unprovable",
    "extension_beside_source": "unprovable",
}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
@pytest.mark.parametrize("layout", sorted(CLI_LAYOUTS))
async def test_a_cli_target_outside_the_checkout_is_never_tier_a(
    tmp_path: Path, layout: str
) -> None:
    # #2463 round 4: ``is_file()`` followed a link out of the checkout, and a
    # ``-m`` binding was never checked, so both were resolved as tier A.
    base = _repo(tmp_path / "base", {"README.md": "tool\n"})
    symbol = _cli_layout(tmp_path, base, layout)
    admission = await admit_check_package(_cli_package(base, symbol), base)
    (check,) = admission.checks
    assert _oracle_result(check)["resolve"] == CLI_LAYOUTS[layout]
    assert admission.check_tiers == {"oracle_1": "U"}
    if CLI_LAYOUTS[layout] == "unprovable":
        assert check.status is CheckStatus.INDETERMINATE


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
@pytest.mark.parametrize("layout", sorted(CLI_LAYOUTS))
async def test_a_candidate_cli_target_outside_the_checkout_never_passes(
    tmp_path: Path, layout: str
) -> None:
    # Admitted on a base whose checkout has the tool; the candidate's is not
    # provably its own: a failure when it is not the checkout's, undecided
    # when it cannot be told.
    base = _repo(
        tmp_path / "base",
        {"tool.py": TOOL, "bin/tool.py": TOOL, "pkg/__init__.py": "", "pkg/mod.py": TOOL},
    )
    candidate = _repo(tmp_path / "cand", {"README.md": "tool\n"})
    symbol = _cli_layout(tmp_path, candidate, layout)
    package = _cli_package(base, symbol)
    if layout != "stdlib_module":
        assert (await admit_check_package(package, base)).verdict is PackageVerdict.ADMITTED
    result = await verify_candidate(package, candidate)
    expected = (
        CandidateVerdict.FAIL
        if CLI_LAYOUTS[layout] == "missing"
        else CandidateVerdict.INDETERMINATE
    )
    assert result.verdict is expected


@pytest.mark.parametrize("symbol", ["tool.py", "-m pkg.mod", "-m pkg"])
async def test_a_cli_target_in_the_checkout_is_tier_a(tmp_path: Path, symbol: str) -> None:
    base = _repo(
        tmp_path / "base",
        {"tool.py": TOOL, "pkg/__init__.py": "", "pkg/mod.py": TOOL, "pkg/__main__.py": TOOL},
    )
    admission = await admit_check_package(_cli_package(base, symbol), base)
    assert _oracle_result(admission.checks[0])["resolve"] == "ok"
    assert admission.check_tiers == {"oracle_1": "A"}
    assert admission.verdict is PackageVerdict.ADMITTED


async def test_an_unprovable_target_is_named_and_undecided(tmp_path: Path) -> None:
    # The harness reports ``unprovable`` where the host cannot prove the
    # target is a checkout file without following links; the controller
    # keeps that name (it is not a malformed frame) and decides nothing.
    forge = (
        "import os, stat, sys\n"
        "for fd in range(3, 64):\n"
        "    try:\n"
        "        if stat.S_ISFIFO(os.fstat(fd).st_mode):\n"
        "            os.write(fd, b'\\n' + sys.argv[2].encode() + b' {\"phase\": \"resolved\", '\n"
        '                     b\'"resolve": "unprovable", "detail": "no dir_fd"}\\n\')\n'
        "            break\n"
        "    except OSError:\n"
        "        pass\n"
        "os._exit(0)\n"
    )
    base = _repo(tmp_path / "base", {"mathutils.py": forge})
    admission = await admit_check_package(_package(_seed(), base), base)
    (check,) = admission.checks
    assert _oracle_result(check)["resolve"] == "unprovable"
    assert check.status is CheckStatus.INDETERMINATE
    assert admission.check_tiers == {"oracle_1": "U"}


# --------------------------------------------------------------------------
# A named target that can never be checkout code is not tier A


@pytest.mark.parametrize("call_kind", ["function", "cli"])
async def test_a_named_standard_library_target_is_never_tier_a(
    tmp_path: Path, call_kind: str
) -> None:
    # Missing from the checkout and named by the criterion is tier A only
    # when the worker can add it there. A standard library module is
    # imported before the checkout's code, so no candidate could ever pass
    # such a check: it is tier U (legacy-decided unless a binding reaches
    # checkout code), never an admitted check that always fails.
    base = _repo(tmp_path / "base", {"README.md": "x\n"})
    if call_kind == "cli":
        package = _cli_package(base, "-m json.tool", named=True)
    else:
        package = _package(
            _seed("json.loads parses a JSON list"),
            base,
            symbol="json.loads",
            params=("s",),
            named=True,
            cases=[
                {
                    "case_id": "held",
                    "held_out": True,
                    "args": {"s": "[1]"},
                    "expect": {"kind": "returns", "value": [1]},
                }
            ],
        )
    admission = await admit_check_package(package, base)
    assert _oracle_result(admission.checks[0])["resolve"] == "missing"
    assert admission.check_tiers == {"oracle_1": "U"}


# --------------------------------------------------------------------------
# A held-out case counts only if it failed on the base


def _two_criteria_package():  # type: ignore[no-untyped-def]
    """Criterion 1's only held-out case passes on the buggy base; criterion 2's fails there."""
    seed = _seed("clamp(15, 0, 10) returns 10", "clamp(99, 1, 7) returns 7")
    visible = {
        "case_id": "stated",
        "held_out": False,
        "args": {"value": 15, "low": 0, "high": 10},
        "expect": {"kind": "returns", "value": 10},
    }
    below = {
        "case_id": "held_below",
        "held_out": True,
        "args": {"value": -3, "low": -2, "high": 4},
        "expect": {"kind": "returns", "value": -2},
    }
    above = {
        "case_id": "held_above",
        "held_out": True,
        "args": {"value": 99, "low": 1, "high": 7},
        "expect": {"kind": "returns", "value": 7},
    }
    specs = [
        build_oracle_spec(
            seed,
            criterion_index=index,
            check_id=f"oracle_{index + 1}",
            call_kind="function",
            params=("value", "low", "high"),
            default_binding={"symbol": "mathutils.clamp"},
            cases=cases,
        )
        for index, cases in enumerate([[visible, below], [below, above]])
    ]
    package = assemble_package(
        seed,
        input_digest="1" * 64,
        generator="t",
        oracles=[(spec, CheckRole.REPRODUCTION) for spec in specs],
    )
    return seed, specs, seal_package(package)


async def test_a_held_out_case_the_base_already_passes_discriminates_nothing(
    base: Path,
) -> None:
    # Adversarial finding B2: the only held-out case of criterion 1
    # (clamp(-3, -2, 4) == -2) already passes on the buggy base, so a
    # candidate special-casing the visible input "passed" it. Such an oracle
    # is not admitted; its criterion is uncovered (legacy-decided).
    seed, specs, package = _two_criteria_package()
    admission = await admit_check_package(
        package, base, interpreter=pin_interpreter(sys.executable, "test")
    )

    assert admission.verdict is PackageVerdict.ADMITTED
    by_id = {check.check_id: check for check in admission.checks}
    assert by_id["oracle_1"].status is CheckStatus.VIOLATED
    assert by_id["oracle_1"].reason == HELD_OUT_NOT_DISCRIMINATING
    assert admission.excluded_checks == {"oracle_1": HELD_OUT_NOT_DISCRIMINATING}
    assert admission.check_tiers == {"oracle_1": "C", "oracle_2": "A"}
    assert criteria_without_admitted_check(package, admission.excluded_checks) == {
        specs[0].criterion_key: HELD_OUT_NOT_DISCRIMINATING
    }
    # Which held-out cases failed on the base, by id only, from the receipt
    # and from its journal form alike.
    journal = AdmissionJournal.model_validate(admission.event_summary())
    excluded = admission.excluded_checks or {}
    assert base_failing_held_out(admission.checks, excluded) == {"oracle_2": frozenset({"c2"})}
    assert base_failing_held_out(journal.checks, excluded) == {"oracle_2": frozenset({"c2"})}
    # The ledger accepts the record admission wrote, and its replay gives
    # the same base-failing held-out cases (the rule a verified pass needs).
    frozen = package_frozen_event("boundary_b2", package)
    manifest = frozen_manifest(frozen.data)
    assert admitted_exclusions(manifest, admission.event_summary()) == frozenset({"oracle_1"})
    replayed = version_state([frozen, admission_completed_event("boundary_b2", admission)])
    assert replayed.base_failing == base_failing_held_out(admission.checks, excluded)
