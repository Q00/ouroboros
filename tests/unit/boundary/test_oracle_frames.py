"""Oracle frames from a hostile target: bounded parsing, per-case failures, in-process comparison.

Threat model: the code under test runs as the same user as the controller
and can write anything to its frame pipe (it can read the nonce from its own
argv). Whatever it writes, one bad case fails that case with a counterexample;
it never makes the check undecided and never reaches the comparison as code.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

import pytest

from ouroboros.boundary import admission as admission_module
from ouroboros.boundary import check_env
from ouroboros.boundary.admission import verify_candidate
from ouroboros.boundary.check_env import CheckCommand
from ouroboros.boundary.oracle_run import parse_frame, valid_entry
from ouroboros.boundary.receipts import CandidateVerdict, CheckStatus
from tests.unit.boundary.test_oracle import BUGGY, FIXED, _oracle_result, _package, _repo, _seed


@pytest.fixture
def base(tmp_path: Path) -> Path:
    return _repo(tmp_path / "base", {"mathutils.py": BUGGY})


MALFORMED = "observed malformed or oversized output"

# Candidate helper: write raw bytes as a frame on the target's frame pipe.
FORGE = (
    "import os, stat, sys\n"
    "def _frame_fd():\n"
    "    for fd in range(3, 64):\n"
    "        try:\n"
    "            if stat.S_ISFIFO(os.fstat(fd).st_mode):\n"
    "                return fd\n"
    "        except OSError:\n"
    "            pass\n"
    "def _forge(payload):\n"
    "    os.write(_frame_fd(), b'\\n' + sys.argv[2].encode() + b' ' + payload + b'\\n')\n"
)


def _result_frame(entry: dict[str, Any]) -> bytes:
    return json.dumps({"phase": "result", "entry": entry}).encode()


# Payloads forged in place of the result of the held-out case ``c3``
# (clamp(99, 1, 7) == 7); the other cases are computed correctly.
PAYLOADS = {
    "deep_nesting": b"[" * 200_000 + b"]" * 200_000,
    "huge_float": _result_frame(
        {"case_id": "c3", "outcome": "returned", "value": 7, "encodable": True, "repr": "7"}
    ).replace(b'"value": 7', b'"value": 1e999'),
    "missing_repr": _result_frame(
        {"case_id": "c3", "outcome": "returned", "value": 7, "encodable": True}
    ),
    "garbage_bytes": b"\xff\xfe\x00 not json \x80",
    "oversized": b'"' + b"x" * (9 * 1024 * 1024) + b'"',
}


@pytest.mark.parametrize("kind", sorted(PAYLOADS))
async def test_a_hostile_frame_fails_its_own_case_only(
    kind: str, base: Path, tmp_path: Path
) -> None:
    payload = PAYLOADS[kind]
    candidate_code = (
        FORGE
        + f"_PAYLOAD = {payload!r}\n"
        + "def clamp(value, low, high):\n"
        + "    if value == 99:\n"
        + "        _forge(_PAYLOAD)\n"
        + "        os._exit(0)\n"
        + "    return max(low, min(high, value))\n"
    )
    package = _package(_seed(), base)
    candidate = _repo(tmp_path / "cand", {"mathutils.py": candidate_code})
    result = await verify_candidate(package, candidate)
    (check,) = result.checks
    assert result.verdict is CandidateVerdict.FAIL
    assert check.status is CheckStatus.VIOLATED
    assert check.reason == "reproduction_still_failing"
    cases = {c["case_id"]: c for c in _oracle_result(check)["cases"]}
    assert cases["c1"]["passed"] and cases["c2"]["passed"]
    assert not cases["c3"]["passed"]
    assert cases["c3"]["detail"] == f"clamp(value=99, low=1, high=7): expected 7, {MALFORMED}"


async def test_a_deep_frame_written_at_import_fails_every_case(base: Path, tmp_path: Path) -> None:
    # the frame arrives before the target is resolved. The candidate's
    # code has already run, so each case fails; the check is not undecided.
    deep = FORGE + "_forge(b'[' * 200000 + b']' * 200000)\n"
    package = _package(_seed(), base)
    candidate = _repo(tmp_path / "cand", {"mathutils.py": deep + FIXED})
    result = await verify_candidate(package, candidate)
    assert result.verdict is CandidateVerdict.FAIL
    cases = _oracle_result(result.checks[0])["cases"]
    assert [c["passed"] for c in cases] == [False, False, False]
    assert all(c["detail"].endswith(MALFORMED) for c in cases)


async def test_an_out_of_range_value_fails_only_its_case(tmp_path: Path) -> None:
    # an integer too large for a float expectation used to raise
    # OverflowError in the comparator and turn the whole check indeterminate.
    seed = _seed("mathutils.lerp(a, b, t) interpolates: lerp(0, 10, 0.5) returns 5.5")
    lerp_base = _repo(tmp_path / "lerp_base", {"mathutils.py": BUGGY})
    package = _package(
        seed,
        lerp_base,
        symbol="mathutils.lerp",
        params=("a", "b", "t"),
        cases=[
            {
                "case_id": "stated",
                "held_out": False,
                "args": {"a": 0, "b": 10, "t": 0.5},
                "expect": {"kind": "returns", "value": 5.5},
            },
            {
                "case_id": "held",
                "held_out": True,
                "args": {"a": 2, "b": 4, "t": 0.25},
                "expect": {"kind": "returns", "value": 2.5},
            },
        ],
    )
    candidate = _repo(
        tmp_path / "cand",
        {
            "mathutils.py": (
                "def lerp(a, b, t):\n"
                "    if a == 2:\n"
                "        return 10 ** 400\n"
                "    return a + (b - a) * t + 0.5\n"
            )
        },
    )
    result = await verify_candidate(package, candidate)
    assert result.verdict is CandidateVerdict.FAIL
    cases = {c["case_id"]: c for c in _oracle_result(result.checks[0])["cases"]}
    assert cases["c1"]["passed"]
    assert cases["c2"]["detail"].endswith(MALFORMED)


def test_the_frame_parser_is_bounded() -> None:
    assert parse_frame(b'{"a": [1, 2.5, "x", null, true]}') == {"a": [1, 2.5, "x", None, True]}
    assert parse_frame(b"[" * 200_000 + b"]" * 200_000) is None
    assert parse_frame(b'{"a": ' + b"[" * 70 + b"]" * 70 + b"}") is None  # depth cap
    assert parse_frame(b'{"a": 1e999}') is None
    assert parse_frame(b'{"a": NaN}') is None
    assert parse_frame(b'{"a": ' + b"9" * 1001 + b"}") is None
    assert parse_frame(b'{"a": ' + b"9" * 400 + b"}") is not None
    assert parse_frame(b"\xff\xfe") is None
    assert parse_frame(b"[1]") is None  # not an object
    assert valid_entry(
        {"case_id": "c", "outcome": "raised", "exception": ["ValueError"], "repr": "x"}, "c"
    )
    assert not valid_entry({"case_id": "c", "outcome": "returned", "repr": "x"}, "c")
    assert not valid_entry(
        {"case_id": "d", "outcome": "returned", "encodable": False, "repr": "x"}, "c"
    )


def test_an_overlong_repr_or_exception_list_is_malformed() -> None:
    # the harness truncates repr to 300 characters; a longer one is
    # forged and never reaches receipts or repair text.
    ok = {"case_id": "c", "outcome": "returned", "encodable": False, "repr": "x" * 300}
    assert valid_entry(ok, "c")
    assert not valid_entry({**ok, "repr": "x" * 301}, "c")
    raised = {"case_id": "c", "outcome": "raised", "exception": ["ValueError"], "repr": "x"}
    assert not valid_entry({**raised, "exception": ["E"] * 65}, "c")
    assert not valid_entry({**raised, "exception": ["E" * 201]}, "c")


async def test_the_comparison_runs_in_the_controller_with_modules_loaded_before_targets(
    base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the only processes an oracle check starts are target processes,
    # and nothing is imported between the first target start and the verdict.
    # The candidate plants a forged ``json`` package in a directory at the
    # front of the controller's sys.path (a stand-in for a writable stdlib).
    planted = tmp_path / "planted"
    planted.mkdir()
    monkeypatch.syspath_prepend(str(planted))
    tamper = (
        "import os\n"
        f"_dir = os.path.join({str(planted)!r}, 'json')\n"
        "os.makedirs(_dir, exist_ok=True)\n"
        "open(os.path.join(_dir, '__init__.py'), 'w').write("
        "'def loads(*a, **k):\\n    return {\"cases\": []}\\n')\n"
    )
    launched: list[list[str]] = []
    modules: dict[str, set[str]] = {}
    # Every subprocess an event loop starts goes through ``subprocess_exec``.
    real_exec = asyncio.base_events.BaseEventLoop.subprocess_exec

    async def recording_exec(self: Any, factory: Any, *argv: Any, **kwargs: Any) -> Any:
        if not launched:
            modules["before"] = set(sys.modules)
        launched.append([str(item) for item in argv])
        return await real_exec(self, factory, *argv, **kwargs)

    real_run = admission_module.run_oracle_check

    async def run_and_snapshot(*args: Any, **kwargs: Any) -> Any:
        outcome = await real_run(*args, **kwargs)
        modules["after"] = set(sys.modules)
        return outcome

    json_module = sys.modules["json"]
    monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "subprocess_exec", recording_exec)
    monkeypatch.setattr(admission_module, "run_oracle_check", run_and_snapshot)
    package = _package(_seed(), base)
    candidate = _repo(tmp_path / "cand", {"mathutils.py": tamper + BUGGY})
    result = await verify_candidate(package, candidate)

    assert (planted / "json" / "__init__.py").is_file()  # the tamper happened
    assert result.verdict is CandidateVerdict.FAIL
    assert [c["passed"] for c in _oracle_result(result.checks[0])["cases"]] == [
        False,
        True,
        False,
    ]
    # Each starts the pinned interpreter (after ``--``) in the harness target role.
    assert len(launched) == 3 and all(argv[argv.index("--") + 5] == "target" for argv in launched)
    assert modules["after"] - modules["before"] == set()
    assert sys.modules["json"] is json_module


async def test_a_target_s_children_are_killed_after_the_target_exits(tmp_path: Path) -> None:
    # the target leader has already exited (and been reaped) when the
    # case ends; the child it left in its process group is still killed. (A
    # double fork plus setsid can escape the group; it cannot change the
    # verdict, which is decided in memory from frame bytes already read.)
    from ouroboros.boundary import oracle_run

    marker = tmp_path / "survivor.txt"
    child = (
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import sys, time; time.sleep(1.5); "
        'open(sys.argv[1], "a").write("alive")\', sys.argv[1]])\n'
    )
    process = await check_env.spawn_check_process(
        CheckCommand((sys.executable, "-c", child, str(marker)), dict(os.environ), str(tmp_path)),
        stdin=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    # The leader is gone; its child (holding the leader's stdout) is not.
    while process.returncode is None:
        await asyncio.sleep(0.05)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await oracle_run.reap_check_process(process, started)
    assert loop.time() - started <= oracle_run.REAP_MARGIN_SECONDS + 0.5
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        await asyncio.sleep(0.25)
    assert not marker.exists()
