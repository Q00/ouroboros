"""The frozen trusted wrapper owns only contained, exclusive output writes."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from ouroboros.core import filesystem_capability
from ouroboros.harness import wrapper_runner
from ouroboros.harness.wrapper_receipt import _RUNNER_PROGRAM, trusted_wrapper_invocation


def _argv(*paths: str) -> list[str]:
    return [
        sys.executable,
        "-I",
        "-c",
        _RUNNER_PROGRAM,
        "--receipt",
        paths[0],
        "--stdout",
        paths[1],
        "--stderr",
        paths[2],
        "--",
        sys.executable,
        "-c",
        "from pathlib import Path; Path('inner-ran').write_text('yes'); print('output')",
    ]


def test_frozen_wrapper_creates_normal_nested_outputs(tmp_path: Path) -> None:
    (tmp_path / "out" / "nested").mkdir(parents=True)
    # The isolated frozen bootstrap must not import task-controlled modules.
    (tmp_path / "ouroboros.py").write_text("raise AssertionError('task import')")
    paths = tuple(f"out/nested/{name}" for name in ("receipt.json", "stdout.txt", "stderr.txt"))
    argv = _argv(*paths)
    assert trusted_wrapper_invocation(shlex.join(argv), str(tmp_path)) is not None
    result = subprocess.run(argv, cwd=tmp_path, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert all((tmp_path / path).is_file() for path in paths)
    assert all((tmp_path / path).stat().st_mode & 0o777 == 0o600 for path in paths)


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("output_index", [0, 1, 2])
def test_frozen_wrapper_refuses_each_symlinked_output_parent(
    tmp_path: Path, nested: bool, output_index: int
) -> None:
    task = tmp_path / "task"
    external = tmp_path / "external"
    task.mkdir()
    external.mkdir()
    if nested:
        (task / "out").mkdir()
        (task / "out" / "linked").symlink_to(external, target_is_directory=True)
        parent = "out/linked"
    else:
        (task / "out").symlink_to(external, target_is_directory=True)
        parent = "out"
    names = ["receipt.json", "stdout.txt", "stderr.txt"]
    names[output_index] = f"{parent}/{names[output_index]}"
    argv = _argv(*names)
    assert trusted_wrapper_invocation(shlex.join(argv), str(task)) is not None
    result = subprocess.run(argv, cwd=task, capture_output=True, check=False)
    assert result.returncode != 0
    assert list(external.iterdir()) == []
    assert not (task / "inner-ran").exists()
    assert not any((task / name).exists() for name in names)


def test_frozen_wrapper_does_not_replace_existing_output(tmp_path: Path) -> None:
    (tmp_path / "stdout.txt").write_text("original")
    result = subprocess.run(
        _argv("receipt.json", "stdout.txt", "stderr.txt"),
        cwd=tmp_path,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert (tmp_path / "stdout.txt").read_text() == "original"
    assert not (tmp_path / "receipt.json").exists()


def test_wrapper_fails_before_inner_when_capabilities_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        filesystem_capability, "nofollow_directory_capabilities_available", lambda: False
    )

    def unexpected_run(*args, **kwargs):
        pytest.fail("inner command ran without output capabilities")

    monkeypatch.setattr(wrapper_runner.subprocess, "run", unexpected_run)
    with pytest.raises(OSError, match="unavailable"):
        wrapper_runner.main(
            [
                "--receipt",
                "receipt.json",
                "--stdout",
                "stdout.txt",
                "--stderr",
                "stderr.txt",
                "--",
                "unused",
            ]
        )
    assert list(tmp_path.iterdir()) == []


def test_wrapper_cleans_output_if_parent_moves_during_inner(tmp_path: Path, monkeypatch) -> None:
    task = tmp_path / "task"
    external = tmp_path / "external"
    (task / "out").mkdir(parents=True)
    external.mkdir()
    monkeypatch.chdir(task)

    def replace_parent(*args, **kwargs):
        (task / "out").rename(external / "moved")
        (task / "out").symlink_to(external, target_is_directory=True)
        return subprocess.CompletedProcess(args[0], 0, stdout=b"output", stderr=b"")

    monkeypatch.setattr(wrapper_runner.subprocess, "run", replace_parent)
    with pytest.raises(filesystem_capability.HeldPathChanged):
        wrapper_runner.main(
            [
                "--receipt",
                "out/receipt.json",
                "--stdout",
                "out/stdout.txt",
                "--stderr",
                "out/stderr.txt",
                "--",
                "unused",
            ]
        )
    assert sorted(path.relative_to(external).as_posix() for path in external.rglob("*")) == [
        "moved"
    ]


def _pytest_wrapper_argv(paths: tuple[str, str, str], test_path: Path) -> list[str]:
    return [
        sys.executable,
        "-I",
        "-c",
        _RUNNER_PROGRAM,
        "--receipt",
        paths[0],
        "--stdout",
        paths[1],
        "--stderr",
        paths[2],
        "--",
        sys.executable,
        "-m",
        "pytest",
        "-q",
        str(test_path),
    ]


def _make_pytest_contract(tmp_path: Path, *, failing: bool) -> tuple[Path, Path]:
    test_path = tmp_path / "test_contract.py"
    marker = tmp_path / "test-body-ran"
    assertion = "assert False, 'expected inner failure'" if failing else "assert True"
    expected_path = os.environ.get("PATH", "")
    test_path.write_text(
        "import os\n"
        "from pathlib import Path\n\n"
        "def test_contract():\n"
        f"    assert os.environ.get('PATH') == {expected_path!r}\n"
        f"    Path({str(marker)!r}).write_text('ran')\n"
        f"    {assertion}\n",
        encoding="utf-8",
    )
    return test_path, marker


def _run_wrapper(
    *, frozen: bool, tmp_path: Path, monkeypatch, test_path: Path, addopts: str | None
) -> tuple[int, bytes, bytes]:
    paths = ("receipt.json", "stdout.txt", "stderr.txt")
    env = dict(os.environ)
    env.pop("PYTEST_ADDOPTS", None)
    if addopts is not None:
        env["PYTEST_ADDOPTS"] = addopts
    if frozen:
        completed = subprocess.run(
            _pytest_wrapper_argv(paths, test_path),
            cwd=tmp_path,
            env=env,
            capture_output=True,
            check=False,
        )
        return (
            completed.returncode,
            (tmp_path / paths[1]).read_bytes(),
            (tmp_path / paths[2]).read_bytes(),
        )
    monkeypatch.chdir(tmp_path)
    if addopts is not None:
        monkeypatch.setenv("PYTEST_ADDOPTS", addopts)
    else:
        monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    return (
        wrapper_runner.main(
            [
                "--receipt",
                paths[0],
                "--stdout",
                paths[1],
                "--stderr",
                paths[2],
                "--",
                sys.executable,
                "-m",
                "pytest",
                "-q",
                str(test_path),
            ]
        ),
        (tmp_path / paths[1]).read_bytes(),
        (tmp_path / paths[2]).read_bytes(),
    )


@pytest.mark.parametrize("frozen", [False, True], ids=["package", "frozen-runner"])
def test_wrapper_passes_normal_inner_test(frozen: bool, tmp_path: Path, monkeypatch) -> None:
    test_path, marker = _make_pytest_contract(tmp_path, failing=False)
    status, stdout, _ = _run_wrapper(
        frozen=frozen,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        test_path=test_path,
        addopts=None,
    )

    assert status == 0
    assert marker.read_text(encoding="utf-8") == "ran"
    assert b"1 passed" in stdout
    receipt = json.loads((tmp_path / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["test_result"] == "passed"


@pytest.mark.parametrize("frozen", [False, True], ids=["package", "frozen-runner"])
def test_wrapper_runs_inner_tests_when_collect_only_is_inherited(
    frozen: bool, tmp_path: Path, monkeypatch
) -> None:
    test_path, marker = _make_pytest_contract(tmp_path, failing=False)
    status, stdout, _ = _run_wrapper(
        frozen=frozen,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        test_path=test_path,
        addopts="--collect-only",
    )

    assert status == 0
    assert marker.read_text(encoding="utf-8") == "ran"
    assert b"1 passed" in stdout
    receipt = json.loads((tmp_path / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["test_result"] == "passed"
    assert receipt["inner_exit_status"] == 0


@pytest.mark.parametrize("frozen", [False, True], ids=["package", "frozen-runner"])
def test_wrapper_records_real_inner_test_failure(frozen: bool, tmp_path: Path, monkeypatch) -> None:
    test_path, marker = _make_pytest_contract(tmp_path, failing=True)
    status, stdout, _ = _run_wrapper(
        frozen=frozen,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        test_path=test_path,
        addopts=None,
    )

    assert status != 0
    assert marker.read_text(encoding="utf-8") == "ran"
    assert b"1 failed" in stdout
    receipt = json.loads((tmp_path / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["test_result"] == "failed"
    assert receipt["inner_exit_status"] == status
