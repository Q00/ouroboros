"""The frozen trusted wrapper owns only contained, exclusive output writes."""

import json
import os
from pathlib import Path
import shlex
import shutil
import signal
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


def test_frozen_wrapper_invocation_is_stable_across_hash_seeds(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    producer = (
        "import json,sys; "
        "sys.path.insert(0,sys.argv[1]); "
        "from ouroboros.harness.wrapper_receipt import _RUNNER_PROGRAM; "
        "print(json.dumps([sys.executable,'-I','-c',_RUNNER_PROGRAM,"
        "'--receipt','receipt.json','--stdout','stdout.txt','--stderr','stderr.txt',"
        "'--',sys.executable,'-c',\"from pathlib import Path; "
        "Path('inner-ran').write_text('yes')\"]))"
    )
    producer_env = {**os.environ, "PYTHONHASHSEED": "1"}
    produced = subprocess.run(
        [sys.executable, "-c", producer, str(root)],
        capture_output=True,
        check=True,
        env=producer_env,
        text=True,
    )
    argv = json.loads(produced.stdout)
    consumer = (
        "import json,shlex,sys; "
        "sys.path.insert(0,sys.argv[1]); "
        "from ouroboros.harness.wrapper_receipt import trusted_wrapper_invocation; "
        "assert trusted_wrapper_invocation(shlex.join(json.loads(sys.argv[2])),sys.argv[3])"
    )
    consumer_env = {**os.environ, "PYTHONHASHSEED": "2"}
    subprocess.run(
        [sys.executable, "-c", consumer, str(root), json.dumps(argv), str(tmp_path)],
        capture_output=True,
        check=True,
        env=consumer_env,
        text=True,
    )

    result = subprocess.run(argv, cwd=tmp_path, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "inner-ran").read_text() == "yes"
    assert json.loads((tmp_path / "receipt.json").read_text())["test_result"] == "passed"


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


@pytest.mark.parametrize("frozen", [False, True], ids=["package", "frozen-runner"])
@pytest.mark.parametrize(
    "config_name,config",
    [
        ("pytest.ini", "[pytest]\naddopts = --co\n"),
        ("pyproject.toml", '[tool.pytest.ini_options]\naddopts = ["--collect-only"]\n'),
        ("tox.ini", "[pytest]\naddopts = --collect-only\n"),
        ("setup.cfg", "[tool:pytest]\naddopts = --collect-only\n"),
    ],
)
def test_wrapper_rejects_collection_from_config(
    frozen: bool, config_name: str, config: str, tmp_path: Path, monkeypatch
) -> None:
    test_path, marker = _make_pytest_contract(tmp_path, failing=False)
    (tmp_path / config_name).write_text(config)
    status, _, _ = _run_wrapper(
        frozen=frozen,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        test_path=test_path,
        addopts=None,
    )
    assert status != 0
    assert not marker.exists()
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert receipt["inner_exit_status"] == 0
    assert receipt["outer_exit_status"] == status
    assert receipt["test_result"] == "failed"


@pytest.mark.parametrize(
    "mode",
    [
        "--collect-only",
        "--collectonly",
        "--setup-plan",
        "--setupplan",
        "--setup-only",
        "--setuponly",
        "--cache-show",
    ],
)
def test_frozen_wrapper_rejects_nonexecuting_pytest_modes(mode: str, tmp_path: Path) -> None:
    test_path, marker = _make_pytest_contract(tmp_path, failing=False)
    argv = _pytest_wrapper_argv(
        ("receipt.json", "stdout.txt", "stderr.txt"), test_path.relative_to(tmp_path)
    )
    argv.insert(-1, mode)
    result = subprocess.run(argv, cwd=tmp_path, capture_output=True, check=False)
    assert result.returncode != 0
    assert not marker.exists()
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert receipt["inner_exit_status"] == 0
    assert receipt["test_result"] == "failed"


@pytest.mark.parametrize("module_option", ["-m", "-pm"])
@pytest.mark.parametrize("python_option", [None, "-I", "-IB"])
def test_frozen_wrapper_rejects_coverage_collection_only(
    tmp_path: Path, module_option: str, python_option: str | None
) -> None:
    test_path, marker = _make_pytest_contract(tmp_path, failing=False)
    argv = _pytest_wrapper_argv(("receipt.json", "stdout.txt", "stderr.txt"), test_path)
    index = argv.index("pytest")
    argv[index:index] = ["coverage", "run", module_option]
    if python_option:
        argv.insert(argv.index("--") + 2, python_option)
    argv.insert(-1, "--collect-only")
    result = subprocess.run(argv, cwd=tmp_path, capture_output=True, check=False)
    assert result.returncode != 0
    assert not marker.exists()
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert receipt["inner_exit_status"] == 0
    assert receipt["test_result"] == "failed"


def test_frozen_wrapper_accepts_executed_coverage_pytest(tmp_path: Path) -> None:
    test_path, marker = _make_pytest_contract(tmp_path, failing=False)
    argv = _pytest_wrapper_argv(("receipt.json", "stdout.txt", "stderr.txt"), test_path)
    index = argv.index("pytest")
    argv[index:index] = ["coverage", "run", "-m"]
    result = subprocess.run(argv, cwd=tmp_path, capture_output=True, check=False)
    assert result.returncode == 0
    assert marker.exists()
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert receipt["test_result"] == "passed"


def test_frozen_wrapper_keeps_coverage_script_arguments(tmp_path: Path) -> None:
    script = tmp_path / "script.py"
    marker = tmp_path / "script-ran"
    script.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    argv = _pytest_wrapper_argv(("receipt.json", "stdout.txt", "stderr.txt"), script)
    index = argv.index("pytest")
    argv[index:] = ["coverage", "run", str(script), "-m", "pytest", "--collect-only"]
    result = subprocess.run(argv, cwd=tmp_path, capture_output=True, check=False)
    assert result.returncode == 0
    assert marker.exists()
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert receipt["test_result"] == "passed"


def test_frozen_wrapper_rejects_coverage_pytest_script_version(tmp_path: Path) -> None:
    test_path, marker = _make_pytest_contract(tmp_path, failing=False)
    argv = _pytest_wrapper_argv(("receipt.json", "stdout.txt", "stderr.txt"), test_path)
    index = argv.index("pytest")
    argv[index:] = ["coverage", "run", str(Path(sys.executable).with_name("pytest")), "--version"]
    result = subprocess.run(argv, cwd=tmp_path, capture_output=True, check=False)
    assert result.returncode != 0
    assert not marker.exists()
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert receipt["inner_exit_status"] == 0
    assert receipt["test_result"] == "failed"


@pytest.mark.parametrize(
    "coverage_name", ["coverage3", f"coverage-{sys.version_info.major}.{sys.version_info.minor}"]
)
def test_frozen_wrapper_rejects_versioned_coverage_collection(
    tmp_path: Path, coverage_name: str
) -> None:
    coverage_path = Path(sys.executable).with_name(coverage_name)
    if not coverage_path.is_file():
        pytest.skip(f"{coverage_name} entrypoint is not installed")
    test_path, marker = _make_pytest_contract(tmp_path, failing=False)
    argv = _pytest_wrapper_argv(("receipt.json", "stdout.txt", "stderr.txt"), test_path)
    index = argv.index("--")
    argv[index + 1 :] = [
        str(coverage_path),
        "run",
        "-m",
        "pytest",
        "--collect-only",
        str(test_path),
    ]
    result = subprocess.run(argv, cwd=tmp_path, capture_output=True, check=False)
    assert result.returncode != 0
    assert not marker.exists()
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert receipt["inner_exit_status"] == 0
    assert receipt["test_result"] == "failed"


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (["coverage", "run", "-m", "pytest", "--collect-only"], True),
        (["coverage", "run", "-pm", "pytest", "--collect-only"], True),
        (["coverage3", "run", "-m", "pytest", "--collect-only"], True),
        (["coverage-3.13", "run", "-m", "pytest", "--collect-only"], True),
        # Existing generic launcher fallback remains conservative for custom names.
        (["coverage-report", "run", "-m", "pytest", "--collect-only"], True),
        (["coverage", "run", "/venv/bin/pytest", "--version"], True),
        (["python", "-m", "coverage", "run", "-m", "pytest", "--collect-only"], True),
        (["python3.13", "-m", "coverage", "run", "--module", "pytest", "--co"], True),
        (["python", "-I", "-m", "coverage", "run", "-m", "pytest", "--co"], True),
        (["python", "-IB", "-m", "coverage", "run", "-m", "pytest", "--co"], True),
        (["python", "-W", "ignore", "-m", "coverage", "run", "-m", "pytest", "--co"], True),
        (["coverage", "run", "--context", "abc", "-m", "pytest", "--co"], True),
        (["python", "-m", "coverage", "run", "-m", "pytest", "test_file.py"], False),
        (["python", "-m", "other", "-m", "pytest", "--collect-only"], False),
        (["coverage", "run", "script.py", "-m", "pytest", "--collect-only"], False),
        (["python", "script.py", "-m", "pytest", "--collect-only"], False),
        (["env", "python", "-m", "pytest", "--collect-only"], True),
        (["py.exe", "-3", "-m", "pytest", "--collect-only"], True),
        (["pythonw.exe", "-m", "pytest", "--collect-only"], True),
        (["custom-python", "-m", "pytest", "--collect-only"], True),
        (["echo", "-m", "coverage", "run", "-m", "pytest", "--collect-only"], False),
    ],
)
def test_nonexecuting_coverage_pytest_command(command: list[str], expected: bool) -> None:
    assert wrapper_runner._nonexecuting_pytest_mode(command, check_config=False) is expected


def _run_python_flag_wrapper(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
    with subprocess.Popen(
        argv,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name != "nt",
    ) as process:
        try:
            stdout, stderr = process.communicate(input=b"", timeout=15)
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.communicate()
            raise
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


@pytest.mark.parametrize("python_option", ["-R", "-d", "-x", "-i", "-V", "-Ri"])
def test_frozen_wrapper_rejects_collection_with_python_flags(
    tmp_path: Path, python_option: str
) -> None:
    test_path, marker = _make_pytest_contract(tmp_path, failing=False)
    argv = _pytest_wrapper_argv(("receipt.json", "stdout.txt", "stderr.txt"), test_path)
    argv.insert(argv.index("--") + 2, python_option)
    argv.insert(-1, "--collect-only")
    result = _run_python_flag_wrapper(argv, tmp_path)
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert receipt["inner_exit_status"] == 0, result.stderr
    assert not marker.exists()
    assert result.returncode == receipt["outer_exit_status"] == 1
    assert receipt["test_result"] == "failed"


@pytest.mark.skipif(os.name == "nt", reason="controlled POSIX launcher, not native py.exe")
@pytest.mark.parametrize("selector", ["-V:3.12", "-3.12-64"])
def test_frozen_wrapper_rejects_collection_with_windows_python_selectors(
    tmp_path: Path, selector: str
) -> None:
    test_path, marker = _make_pytest_contract(tmp_path, failing=False)
    launcher = tmp_path / "py.exe"
    launcher.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        f"assert sys.argv[1] == {selector!r}\n"
        f"os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[2:]])\n"
    )
    launcher.chmod(0o700)
    argv = _pytest_wrapper_argv(("receipt.json", "stdout.txt", "stderr.txt"), test_path)
    index = argv.index("--") + 1
    argv[index] = str(launcher)
    argv.insert(index + 1, selector)
    argv.insert(-1, "--collect-only")
    result = _run_python_flag_wrapper(argv, tmp_path)
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert receipt["inner_exit_status"] == 0, result.stderr
    assert not marker.exists()
    assert result.returncode == receipt["outer_exit_status"] == 1
    assert receipt["test_result"] == "failed"


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (["--future-python-flag", "-m", "pytest", "--co"], True),
        (["-R", "-m", "coverage", "run", "-m", "pytest", "--co"], True),
        (["-R", "-m", "pytest", "test_file.py"], False),
        (["-R", "script.py", "-m", "pytest", "--co"], False),
        (["-R", "-c", "pass", "-m", "pytest", "--co"], False),
        (["-R", "-cpass", "-m", "pytest", "--co"], False),
        (["-Rcpass", "-m", "pytest", "--co"], False),
        (["-Bicpass", "-m", "pytest", "--co"], False),
        (["-R", "--", "-m", "pytest", "--co"], False),
        (["-R", "-", "-m", "pytest", "--co"], False),
        (["-W", "-m", "pytest", "--co"], False),
        (["-X", "-m", "pytest", "--co"], False),
        (["--check-hash-based-pycs", "-m", "pytest", "--co"], False),
        (["-m", "other", "-m", "pytest", "--co"], False),
        (["-R", "-m", "other", "-m", "pytest", "--co"], False),
        (["-R", "-m"], False),
        (["-W"], False),
        (["-X"], False),
        (["--check-hash-based-pycs"], False),
    ],
)
def test_python_option_boundaries(arguments: list[str], expected: bool) -> None:
    assert (
        wrapper_runner._nonexecuting_pytest_mode(["python", *arguments], check_config=False)
        is expected
    )


@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize("help_option", ["--help", "--he", "--h", "-h", "-mh", "-ph"])
@pytest.mark.parametrize("trailing", [[], ["-m", "pytest", "--collect-only"]])
def test_frozen_wrapper_refuses_coverage_help_receipt(
    tmp_path: Path, direct: bool, help_option: str, trailing: list[str]
) -> None:
    test_path, marker = _make_pytest_contract(tmp_path, failing=False)
    argv = _pytest_wrapper_argv(("receipt.json", "stdout.txt", "stderr.txt"), test_path)
    coverage = Path(sys.executable).with_name("coverage.exe" if os.name == "nt" else "coverage")
    command = [str(coverage)] if direct else [sys.executable, "-m", "coverage"]
    argv[argv.index("--") + 1 :] = [*command, "run", help_option, *trailing]
    result = subprocess.run(argv, cwd=tmp_path, capture_output=True, check=False)
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert receipt["inner_exit_status"] == 0
    assert receipt["test_result"] == "failed"
    assert result.returncode == receipt["outer_exit_status"] == 1
    assert not marker.exists()


@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize("mode", ["collect", "help", "execute"])
@pytest.mark.parametrize("option", ["append", "module", "context", "data", "rcfile"])
def test_frozen_wrapper_coverage_abbreviations_preserve_execution_evidence(
    tmp_path: Path, direct: bool, mode: str, option: str
) -> None:
    test_path, marker = _make_pytest_contract(tmp_path, failing=False)
    config = tmp_path / "coverage.ini"
    config.write_text("[run]\n")
    prefixes = {
        "append": ["--app", "-m", "pytest"],
        "module": ["--mod", "pytest"],
        "context": ["--contex=contract", "-m", "pytest"],
        "data": ["--data-f", str(tmp_path / ".coverage"), "-m", "pytest"],
        "rcfile": ["--rcf", str(config), "-m", "pytest"],
    }
    coverage = Path(sys.executable).with_name("coverage.exe" if os.name == "nt" else "coverage")
    command = [str(coverage)] if direct else [sys.executable, "-m", "coverage"]
    options = {"collect": ["--co"], "help": ["--help"], "execute": []}[mode]
    argv = _pytest_wrapper_argv(("receipt.json", "stdout.txt", "stderr.txt"), test_path)
    argv[argv.index("--") + 1 :] = [*command, "run", *prefixes[option], *options, str(test_path)]
    result = subprocess.run(argv, cwd=tmp_path, capture_output=True, check=False)
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert receipt["inner_exit_status"] == 0
    assert receipt["test_result"] == ("passed" if mode == "execute" else "failed")
    assert result.returncode == receipt["outer_exit_status"] == (0 if mode == "execute" else 1)
    assert marker.exists() is (mode == "execute")


@pytest.mark.skipif(os.name == "nt", reason="POSIX env and controlled launcher executable")
@pytest.mark.parametrize("launcher", ["env", "uv", "controlled_uvx"])
@pytest.mark.parametrize("mode", ["help", "collect", "execute"])
def test_frozen_wrapper_nested_launcher_execution_evidence(
    tmp_path: Path, launcher: str, mode: str
) -> None:
    test_path, marker = _make_pytest_contract(tmp_path, failing=False)
    coverage = str(Path(sys.executable).with_name("coverage"))
    if launcher == "env":
        prefix = ["env", coverage]
    elif launcher == "uv":
        uv = shutil.which("uv")
        if uv is None:
            pytest.skip("real uv is unavailable; parent also runs a separate offline uv proof")
        prefix = [
            uv,
            "run",
            "--offline",
            "--no-config",
            "--no-project",
            "--no-env-file",
            "--python",
            sys.executable,
            coverage,
        ]
        original = repr(os.environ["PATH"])
        activated = repr(str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
        test_path.write_text(test_path.read_text().replace(original, activated, 1))
    else:
        shim = tmp_path / "uvx"
        shim.write_text(
            f"#!{sys.executable}\nimport os, sys\n"
            "assert sys.argv[1:4] == ['--from', 'coverage', 'coverage']\n"
            f"os.execv({coverage!r}, [{coverage!r}, *sys.argv[4:]])\n"
        )
        shim.chmod(0o700)
        prefix = [str(shim), "--from", "coverage", "coverage"]
    before_target = ["-h"] if mode == "help" else []
    pytest_options = ["--co"] if mode == "collect" else []
    argv = _pytest_wrapper_argv(("receipt.json", "stdout.txt", "stderr.txt"), test_path)
    argv[argv.index("--") + 1 :] = [
        *prefix,
        "run",
        *before_target,
        "-m",
        "pytest",
        *pytest_options,
        str(test_path),
    ]
    result = subprocess.run(argv, cwd=tmp_path, capture_output=True, check=False)
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert receipt["inner_exit_status"] == 0
    assert receipt["test_result"] == ("passed" if mode == "execute" else "failed")
    assert result.returncode == receipt["outer_exit_status"] == (0 if mode == "execute" else 1)
    assert marker.exists() is (mode == "execute")


@pytest.mark.skipif(os.name == "nt", reason="POSIX env execution")
@pytest.mark.parametrize("change", ["addopts", "cwd", "inert"])
def test_frozen_wrapper_withholds_changed_launcher_context(tmp_path: Path, change: str) -> None:
    target = tmp_path / "sub" if change == "cwd" else tmp_path
    target.mkdir(exist_ok=True)
    test_path, marker = _make_pytest_contract(target, failing=False)
    if change == "cwd":
        (target / "pytest.ini").write_text("[pytest]\naddopts = --collect-only\n")
        prefix = ["env", "-C", str(target)]
    elif change == "addopts":
        prefix = ["env", "PYTEST_ADDOPTS=--collect-only"]
    else:
        prefix = ["env", "PROBE=1"]
    argv = _pytest_wrapper_argv(("receipt.json", "stdout.txt", "stderr.txt"), test_path)
    argv[argv.index("--") + 1 :] = [*prefix, sys.executable, "-m", "pytest", test_path.name]
    result = subprocess.run(argv, cwd=tmp_path, capture_output=True, check=False)
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert receipt["inner_exit_status"] == 0
    assert receipt["test_result"] == "failed"
    assert result.returncode == receipt["outer_exit_status"] == 1
    assert marker.exists() is (change == "inert")


@pytest.mark.parametrize("collect_only", [True, False])
def test_frozen_wrapper_withholds_coverage_config_target(
    tmp_path: Path, collect_only: bool
) -> None:
    test_path, marker = _make_pytest_contract(tmp_path, failing=False)
    options = "--collect-only " if collect_only else ""
    (tmp_path / ".coveragerc").write_text(
        f"[run]\ncommand_line = -m pytest {options}{test_path.name}\n"
    )
    argv = _pytest_wrapper_argv(("receipt.json", "stdout.txt", "stderr.txt"), test_path)
    argv[argv.index("--") + 1 :] = [sys.executable, "-m", "coverage", "run"]
    result = subprocess.run(argv, cwd=tmp_path, capture_output=True, check=False)
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert receipt["inner_exit_status"] == 0
    assert receipt["test_result"] == "failed"
    assert result.returncode == receipt["outer_exit_status"] == 1
    assert marker.exists() is not collect_only


@pytest.mark.parametrize("option", ["-h", "-V", "--version", "--help-env", "-hc", "-qhc"])
def test_frozen_wrapper_withholds_interpreter_reporting_mode(tmp_path: Path, option: str) -> None:
    test_path, marker = _make_pytest_contract(tmp_path, failing=False)
    argv = _pytest_wrapper_argv(("receipt.json", "stdout.txt", "stderr.txt"), test_path)
    argv[argv.index("--") + 1 :] = [sys.executable, option, "-m", "pytest", str(test_path)]
    result = subprocess.run(argv, cwd=tmp_path, capture_output=True, check=False)
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert receipt["inner_exit_status"] == 0
    assert receipt["test_result"] == "failed"
    assert result.returncode == receipt["outer_exit_status"] == 1
    assert not marker.exists()


def test_frozen_wrapper_withholds_interactive_failure_receipt(tmp_path: Path) -> None:
    test_path, marker = _make_pytest_contract(tmp_path, failing=True)
    argv = _pytest_wrapper_argv(("receipt.json", "stdout.txt", "stderr.txt"), test_path)
    argv[argv.index("--") + 1 :] = [sys.executable, "-i", "-m", "pytest", str(test_path)]
    result = subprocess.run(
        argv, cwd=tmp_path, stdin=subprocess.DEVNULL, capture_output=True, check=False
    )
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert receipt["inner_exit_status"] == 0
    assert marker.exists()
    assert receipt["test_result"] == "failed"
    assert result.returncode == receipt["outer_exit_status"] == 1


@pytest.mark.parametrize(
    "prefix", [["-bW", "ignore", "-m", "pytest"], ["-bm", "pytest"], ["-mpytest"]]
)
@pytest.mark.parametrize("collect_only", [True, False])
def test_frozen_wrapper_interpreter_clusters_preserve_test_execution(
    tmp_path: Path, prefix: list[str], collect_only: bool
) -> None:
    test_path, marker = _make_pytest_contract(tmp_path, failing=False)
    argv = _pytest_wrapper_argv(("receipt.json", "stdout.txt", "stderr.txt"), test_path)
    options = ["--co"] if collect_only else []
    argv[argv.index("--") + 1 :] = [sys.executable, *prefix, *options, str(test_path)]
    result = subprocess.run(argv, cwd=tmp_path, capture_output=True, check=False)
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert receipt["inner_exit_status"] == 0
    assert receipt["test_result"] == ("failed" if collect_only else "passed")
    assert result.returncode == receipt["outer_exit_status"] == (1 if collect_only else 0)
    assert marker.exists() is not collect_only


def test_frozen_wrapper_rejects_collection_from_nodeid_config(tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    test_path, marker = _make_pytest_contract(sub, failing=False)
    (sub / "pytest.ini").write_text("[pytest]\naddopts = --collect-only\n")
    argv = _pytest_wrapper_argv(("receipt.json", "stdout.txt", "stderr.txt"), test_path)
    argv[-1] += "::test_contract"
    result = subprocess.run(argv, cwd=tmp_path, capture_output=True, check=False)
    assert result.returncode != 0
    assert not marker.exists()
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert receipt["inner_exit_status"] == 0
    assert receipt["test_result"] == "failed"


@pytest.mark.parametrize(
    "command",
    [
        ["uv", "run", "pytest", "-o", "addopts=--collect-only", "test_inner.py"],
        ["uvx", "pytest", "--override-ini=addopts=--co", "test_inner.py"],
        ["uv.exe", "run", "pytest.exe", "--collect-only", "test_inner.py"],
        ["UVX.EXE", "py.test.EXE", "--setup-plan", "test_inner.py"],
    ],
)
def test_wrapper_recognizes_pytest_launcher_nonexecuting_mode(command: list[str]) -> None:
    assert wrapper_runner._nonexecuting_pytest_mode(command)


def test_wrapper_withholds_authority_for_pytest_argument_file(tmp_path: Path) -> None:
    test_path, marker = _make_pytest_contract(tmp_path, failing=False)
    arguments = tmp_path / "arguments.txt"
    arguments.write_text(f"--collect-only\n{test_path}\n")
    argv = _pytest_wrapper_argv(("receipt.json", "stdout.txt", "stderr.txt"), test_path)
    argv[-1] = f"@{arguments}"
    result = subprocess.run(argv, cwd=tmp_path, capture_output=True, check=False)
    assert result.returncode != 0
    assert not marker.exists()
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert receipt["inner_exit_status"] == 0
    assert receipt["test_result"] == "failed"


@pytest.mark.parametrize(
    "options",
    [
        ["-o", "addopts=--collect-only"],
        ["-o", "addopts=--collectonly"],
        ["-o", "addopts=--setupplan"],
        ["-o", "addopts=--setuponly"],
        ["-oaddopts=--collect-only"],
        ["-o=addopts=--collect-only"],
        ["--override-ini", "addopts=--collect-only"],
        ["--override-ini=addopts=--collect-only"],
        ["-qo=addopts=--collect-only"],
        ["-qqh"],
    ],
)
def test_frozen_wrapper_rejects_nonexecuting_option_spellings(
    options: list[str], tmp_path: Path
) -> None:
    test_path, marker = _make_pytest_contract(tmp_path, failing=False)
    argv = _pytest_wrapper_argv(("receipt.json", "stdout.txt", "stderr.txt"), test_path)
    argv[-1:-1] = options
    result = subprocess.run(argv, cwd=tmp_path, capture_output=True, check=False)
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert result.returncode == receipt["outer_exit_status"] == 1
    assert receipt["inner_exit_status"] == 0
    assert receipt["test_result"] == "failed"
    assert not marker.exists()


@pytest.mark.parametrize(
    "options",
    [
        ["-c", "hidden/custom.ini"],
        ["-chidden/custom.ini"],
        ["-c=hidden/custom.ini"],
        ["--config-file", "hidden/custom.ini"],
        ["--config-file=hidden/custom.ini"],
        ["-qchidden/custom.ini"],
    ],
)
def test_frozen_wrapper_rejects_nonexecuting_config_spellings(
    options: list[str], tmp_path: Path
) -> None:
    test_path, marker = _make_pytest_contract(tmp_path, failing=False)
    (tmp_path / "hidden").mkdir()
    (tmp_path / "hidden/custom.ini").write_text("[pytest]\naddopts = --collect-only\n")
    argv = _pytest_wrapper_argv(("receipt.json", "stdout.txt", "stderr.txt"), test_path)
    argv[-1:-1] = options
    result = subprocess.run(argv, cwd=tmp_path, capture_output=True, check=False)
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert result.returncode == receipt["outer_exit_status"] == 1
    assert receipt["inner_exit_status"] == 0
    assert receipt["test_result"] == "failed"
    assert not marker.exists()


@pytest.mark.parametrize(
    "options",
    [
        ["-o=addopts=-q"],
        ["-qoaddopts=-q"],
        ["-k", "not -h"],
        ["-qknot --co"],
    ],
)
def test_frozen_wrapper_preserves_executing_option_values(
    options: list[str], tmp_path: Path
) -> None:
    test_path, marker = _make_pytest_contract(tmp_path, failing=False)
    argv = _pytest_wrapper_argv(("receipt.json", "stdout.txt", "stderr.txt"), test_path)
    argv[-1:-1] = options
    result = subprocess.run(argv, cwd=tmp_path, capture_output=True, check=False)
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert result.returncode == receipt["inner_exit_status"] == receipt["outer_exit_status"] == 0
    assert receipt["test_result"] == "passed"
    assert marker.read_text() == "ran"


def test_frozen_wrapper_rejects_pytest_exe_collection(tmp_path: Path) -> None:
    launcher = Path(sys.executable).parent / ("pytest.exe" if os.name == "nt" else "pytest")
    alias = tmp_path / "pytest.exe"
    shutil.copy2(launcher, alias)
    test_path, marker = _make_pytest_contract(tmp_path, failing=False)
    argv = _pytest_wrapper_argv(("receipt.json", "stdout.txt", "stderr.txt"), test_path)
    argv[-5:] = [str(alias), "--collect-only", "-q", str(test_path)]
    result = subprocess.run(argv, cwd=tmp_path, capture_output=True, check=False)
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert receipt["inner_exit_status"] == 0
    assert not marker.exists()
    assert result.returncode != 0
    assert receipt["test_result"] == "failed"
