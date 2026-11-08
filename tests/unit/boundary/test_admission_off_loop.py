"""Admission copies, digests and removes checkouts off the event loop, with unchanged receipts."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import contextvars
import functools
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import threading
from typing import Any

import pytest

from ouroboros.boundary import admission
from ouroboros.boundary.admission import admit_binding, admit_check_package, verify_candidate
from ouroboros.boundary.package import BaseFileRef, CheckPackage
from ouroboros.boundary.receipts import CandidateVerdict, PackageVerdict

from .conftest import SIGNATURE, build_package, make_seed
from .test_oracle import BUGGY, _declared, _repo
from .test_oracle import _package as _oracle_package
from .test_oracle import _seed as _oracle_seed

_WAIT = 30.0
"""Upper bound for every wait on a worker thread here (never reached when the tests pass)."""

_COPY_ROOT = re.compile(r"^\d{3}-")
_VOLATILE = frozenset({"started_at", "completed_at", "duration_seconds"})


def _on_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _spy(calls: list[bool], real: Callable[..., Any]) -> Callable[..., Any]:
    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(_on_loop())
        return real(*args, **kwargs)

    return spy


def _spy_checkout_removals(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Record, for every removal of a check copy or a work root, whether it ran on the loop.

    A check process's own scratch directory (``check_env.check_scratch``) is
    not a checkout copy and is not counted.
    """
    removals: list[bool] = []
    real = shutil.rmtree

    def rmtree(path: Any, *args: Any, **kwargs: Any) -> None:
        name = Path(path).name
        if _COPY_ROOT.match(name) or (
            name.startswith(("ouroboros-check-", "ouroboros-binding-"))
            and not name.startswith("ouroboros-check-env-")
        ):
            removals.append(_on_loop())
        real(path, *args, **kwargs)

    monkeypatch.setattr(admission.shutil, "rmtree", rmtree)
    return removals


# ``_run_off_loop`` itself: a blocking call in a thread, never orphaned by a cancellation.


async def _until(event: threading.Event) -> None:
    assert await asyncio.to_thread(event.wait, _WAIT), "the worker thread never started"


async def test_the_call_runs_in_a_thread_with_its_arguments_and_context() -> None:
    variable: contextvars.ContextVar[str] = contextvars.ContextVar(
        "admission_test", default="unset"
    )
    variable.set("caller")

    def work(a: int, *, b: int) -> tuple[bool, str, int]:
        return _on_loop(), variable.get(), a + b

    assert await admission._run_off_loop(work, 2, b=3) == (False, "caller", 5)


async def test_the_call_raises_the_very_exception_a_direct_call_raises() -> None:
    error = FileExistsError("copy_root")

    def work() -> None:
        raise error

    with pytest.raises(FileExistsError) as raised:
        await admission._run_off_loop(work)
    assert raised.value is error


@pytest.mark.parametrize("cancellations", [1, 3])
async def test_a_cancelled_caller_waits_for_the_thread_before_it_is_cancelled(
    cancellations: int,
) -> None:
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def work() -> str:
        started.set()
        assert release.wait(_WAIT)
        finished.set()
        return "done"

    task = asyncio.create_task(admission._run_off_loop(work))
    await _until(started)
    for _ in range(cancellations):
        # Each cancellation reaches the waiting caller; none may end it early.
        task.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, _WAIT)
    assert finished.is_set()


async def test_a_cancelled_caller_still_runs_its_cleanup_after_the_thread() -> None:
    started, release = threading.Event(), threading.Event()
    order: list[str] = []

    def work() -> None:
        started.set()
        assert release.wait(_WAIT)
        order.append("work")

    async def caller() -> None:
        try:
            await admission._run_off_loop(work)
        finally:
            order.append("cleanup")

    task = asyncio.create_task(caller())
    await _until(started)
    task.cancel()
    for _ in range(5):
        await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, _WAIT)
    assert order == ["work", "cleanup"]


async def test_a_timeout_around_the_call_is_still_a_timeout() -> None:
    started, release = threading.Event(), threading.Event()

    def work() -> None:
        started.set()
        assert release.wait(_WAIT)

    async def bounded() -> None:
        async with asyncio.timeout(0.01):
            await admission._run_off_loop(work)

    task = asyncio.create_task(bounded())
    await _until(started)
    async with asyncio.timeout(_WAIT):
        while not task.cancelling():  # the deadline fired while the thread still runs
            await asyncio.sleep(0.005)
    assert not task.done()
    release.set()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(task, _WAIT)


@functools.cache
def _oracle_package_and_binding() -> tuple[CheckPackage, Any]:
    # Built once: the Seed's creation time is part of its digest.
    package = _oracle_package(
        _oracle_seed("values are limited to the bounds, for example 15 in [0, 10] gives 10"),
        Path("unused"),
        symbol="mathutils.bound",
    )
    binding = _declared(
        package, {"symbol": "limits.limit", "arg_map": {"value": 0, "low": 1, "high": 2}}
    )
    return package, binding


def _oracle_case(tmp_path: Path) -> tuple[CheckPackage, Any, Path]:
    base = _repo(tmp_path / "oracle-base", {"mathutils.py": BUGGY})
    return (*_oracle_package_and_binding(), base)


async def test_admission_and_verification_touch_every_checkout_off_the_event_loop(
    tmp_path: Path, base_checkout: Path, fixed_checkout: Path, package: CheckPackage, monkeypatch
) -> None:
    copies: list[bool] = []
    manifests: list[bool] = []
    monkeypatch.setattr(admission, "copy_checkout", _spy(copies, admission.copy_checkout))
    monkeypatch.setattr(admission, "tree_manifest", _spy(manifests, admission.tree_manifest))
    removals = _spy_checkout_removals(monkeypatch)

    admitted = await admit_check_package(package, base_checkout, work_dir=tmp_path / "a")
    # No work_dir: the run owns its temporary root and removes it too.
    verified = await verify_candidate(package, fixed_checkout)

    assert admitted.verdict is PackageVerdict.ADMITTED
    assert verified.verdict is CandidateVerdict.PASS
    # Per run of two checks: the source before and after, and each check's
    # copy before and after its command; one copy per check.
    assert copies == [False] * 4
    assert manifests == [False] * 12
    # Two check copies per run, plus the root the second run owned.
    assert removals == [False] * 5


async def test_a_binding_admission_touches_every_checkout_off_the_event_loop(
    tmp_path: Path, monkeypatch
) -> None:
    package, binding, base = _oracle_case(tmp_path)
    copies: list[bool] = []
    manifests: list[bool] = []
    monkeypatch.setattr(admission, "copy_checkout", _spy(copies, admission.copy_checkout))
    monkeypatch.setattr(admission, "tree_manifest", _spy(manifests, admission.tree_manifest))
    removals = _spy_checkout_removals(monkeypatch)

    admitted = await admit_binding(package, "oracle_1", binding, base)

    assert admitted.valid and admitted.reason == "binding_admitted_on_base"
    assert copies == [False]
    # The base digest, then the copy before and after the check.
    assert manifests == [False] * 3
    assert removals == [False]


@pytest.mark.parametrize("entry", ["admit_check_package", "admit_binding"])
async def test_a_run_cancelled_during_a_copy_waits_for_it_and_leaves_no_copy(
    entry: str, tmp_path: Path, base_checkout: Path, package: CheckPackage, monkeypatch
) -> None:
    # The copy runs in a worker thread that cannot be stopped. Were the
    # cancellation re-raised at once, the cleanup would remove the run's root
    # while the copy still runs, and the copy would recreate it (``copy_checkout``
    # creates its destination's parent) and leave a whole checkout behind.
    temp = tmp_path / "tmp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    started, release, copied = threading.Event(), threading.Event(), threading.Event()
    real_copy = admission.copy_checkout

    def blocking_copy(source: Path, destination: Path) -> None:
        started.set()
        assert release.wait(_WAIT)
        real_copy(source, destination)
        copied.set()

    monkeypatch.setattr(admission, "copy_checkout", blocking_copy)
    run: Awaitable[Any]
    if entry == "admit_check_package":
        run, prefix = admit_check_package(package, base_checkout), "ouroboros-check-"
    else:
        oracle_package, binding, base = _oracle_case(tmp_path)
        run, prefix = admit_binding(oracle_package, "oracle_1", binding, base), "ouroboros-binding-"
    task = asyncio.ensure_future(run)
    assert await asyncio.to_thread(started.wait, _WAIT)
    assert len(list(temp.glob(f"{prefix}*"))) == 1

    task.cancel()
    for _ in range(5):
        await asyncio.sleep(0)
    assert not task.done()  # still waiting for the copy: no cleanup has started
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, _WAIT)

    assert copied.is_set()
    assert list(temp.glob(f"{prefix}*")) == []


async def _on_loop_reference(func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """The previous behavior: the very same call, made synchronously on the event loop."""
    return func(*args, **kwargs)


def _comparable(value: Any) -> Any:
    """A receipt's data without its clock readings."""
    if isinstance(value, dict):
        return {key: _comparable(item) for key, item in value.items() if key not in _VOLATILE}
    if isinstance(value, list):
        return [_comparable(item) for item in value]
    return value


def _mutating_package() -> CheckPackage:
    mutate = (
        "open('calc.py', 'w').write('def add(a, b):\\n    return a + b\\n')\n"
        f"print('{SIGNATURE}')\nraise SystemExit(1)\n"
    )
    return build_package(make_seed(), repro_script=mutate)


def _occupy(root: Path) -> Path:
    (root / "probe" / "test_add.py").mkdir(parents=True)
    return root


def _fifo(root: Path) -> Path:
    (root / "probe").mkdir()
    os.mkfifo(root / "probe" / "test_add.py")
    return root


Scenario = Callable[[Path, Path, Path, Path], Awaitable[Any]]


async def _base_admitted(tmp: Path, work: Path, base: Path, fixed: Path) -> Any:
    return await admit_check_package(build_package(make_seed()), base, work_dir=work)


async def _base_mutated(tmp: Path, work: Path, base: Path, fixed: Path) -> Any:
    return await admit_check_package(_mutating_package(), base, work_dir=work)


async def _base_file_mismatch(tmp: Path, work: Path, base: Path, fixed: Path) -> Any:
    ref = BaseFileRef(path="README.md", sha256="0" * 64)
    package = build_package(make_seed(), base_files=(ref,))
    return await admit_check_package(package, base, work_dir=work)


async def _base_unreadable(tmp: Path, work: Path, base: Path, fixed: Path) -> Any:
    os.mkfifo(base / "pipe")
    try:
        await admit_check_package(build_package(make_seed()), base, work_dir=work)
    except PermissionError as exc:
        return type(exc).__name__, str(exc)
    raise AssertionError("an unreadable base raised nothing")


async def _candidate_pass(tmp: Path, work: Path, base: Path, fixed: Path) -> Any:
    return await verify_candidate(build_package(make_seed()), fixed, work_dir=work)


async def _candidate_fail(tmp: Path, work: Path, base: Path, fixed: Path) -> Any:
    return await verify_candidate(build_package(make_seed()), base, work_dir=work)


async def _candidate_layout(tmp: Path, work: Path, base: Path, fixed: Path) -> Any:
    return await verify_candidate(build_package(make_seed()), _occupy(fixed), work_dir=work)


async def _candidate_unreadable(tmp: Path, work: Path, base: Path, fixed: Path) -> Any:
    return await verify_candidate(build_package(make_seed()), _fifo(fixed), work_dir=work)


async def _candidate_missing(tmp: Path, work: Path, base: Path, fixed: Path) -> Any:
    try:
        await verify_candidate(build_package(make_seed()), tmp / "absent", work_dir=work)
    except FileNotFoundError as exc:
        return type(exc).__name__, str(exc)
    raise AssertionError("a missing candidate raised nothing")


async def _binding(tmp: Path, work: Path, base: Path, fixed: Path) -> Any:
    package, binding, oracle_base = _oracle_case(tmp)
    return await admit_binding(package, "oracle_1", binding, oracle_base)


_SCENARIOS: dict[str, Scenario] = {
    "base_admitted": _base_admitted,
    "base_mutated": _base_mutated,
    "base_file_mismatch": _base_file_mismatch,
    "candidate_pass": _candidate_pass,
    "candidate_fail": _candidate_fail,
    "candidate_layout": _candidate_layout,
    "candidate_missing": _candidate_missing,
    "binding": _binding,
}
if sys.platform != "win32":
    _SCENARIOS["candidate_unreadable"] = _candidate_unreadable
    _SCENARIOS["base_unreadable"] = _base_unreadable


@pytest.mark.parametrize("name", sorted(_SCENARIOS))
async def test_receipts_match_the_synchronous_reference(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = _SCENARIOS[name]

    async def outcome() -> Any:
        # Same inputs at the same paths for both runs: only the executor differs.
        root = tmp_path / "case"
        shutil.rmtree(root, ignore_errors=True)
        base = _repo(root / "base", {"calc.py": "def add(a, b):\n    return a - b\n"})
        fixed = _repo(root / "candidate", {"calc.py": "def add(a, b):\n    return a + b\n"})
        for checkout in (base, fixed):
            (checkout / "README.md").write_text("calc\n")
        result = await scenario(root, root / "work", base, fixed)
        if isinstance(result, tuple):
            return result
        return _comparable(result.model_dump(mode="json"))

    with monkeypatch.context() as patch:
        patch.setattr(admission, "_run_off_loop", _on_loop_reference)
        expected = await outcome()
    assert await outcome() == expected
