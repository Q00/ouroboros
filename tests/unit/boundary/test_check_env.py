"""Environment and interpreter for model-written checks (boundary/check_env.py)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import pytest

from ouroboros.boundary.admission import _run_argv, admit_check_package, verify_candidate
from ouroboros.boundary.check_env import (
    INTERPRETER_CHANGED,
    CheckCommand,
    CheckUnavailable,
    check_command,
    check_scratch,
    pin_interpreter,
    resolve_check_interpreter,
)
from ouroboros.boundary.package import CheckRole

from .calc_fixtures import _package, _seed
from .test_oracle import FIXED, _repo
from .test_oracle import _package as _oracle_package
from .test_oracle import _seed as _oracle_seed

SECRETS = {
    "OPENAI_API_KEY": "sk-test",
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "GH_TOKEN": "ghp_test",
    "GITHUB_TOKEN": "ghs_test",
    "AWS_SECRET_ACCESS_KEY": "aws-test",
    "MY_SERVICE_PASSWORD": "hunter2",
}
# A parent variable no name pattern would call a credential: only an
# environment built from an allowlist keeps it out.
SENTINEL_NAME = "OUROBOROS_TEST_PARENT_ONLY"
SENTINEL_VALUE = "parent-credential-sentinel-7f3a"

# Preservation check: passes only when no credential-like variable is visible.
NO_SECRET_SCRIPT = """import os, sys
leaked = sorted(k for k in os.environ if any(t in k for t in ("KEY", "TOKEN", "SECRET", "PASSWORD")))
print("leaked:", leaked)
sys.exit(1 if leaked else 0)
"""

# HOME and the sandbox's temp directory variables point to the scratch directory.
POSIX_KEYS = {"PATH", "HOME", "TMPDIR", "TMP", "TEMP"}


def _command_env(
    scratch: Path, source: dict[str, str], interpreter: str = sys.executable
) -> dict[str, str]:
    """The environment ``check_command`` gives a check (sandbox off: launcher env is the same)."""
    command = check_command(
        ["python3", "-c", "pass"],
        cwd=scratch,
        writable_root=scratch,
        interpreter=pin_interpreter(interpreter, "test"),
        scratch=scratch,
        source=source,
    )
    assert isinstance(command, CheckCommand)
    return dict(command.env)


def test_the_environment_is_built_from_the_allowlist_not_copied(tmp_path: Path) -> None:
    source = {
        "PATH": "/bin",
        "HOME": "/home/user",
        "LC_ALL": "C",
        "VIRTUAL_ENV": "/other/venv",
        "PYTHONPATH": ".",
        "LC_SECRET_TOKEN": "a name pattern would have kept this",
        SENTINEL_NAME: SENTINEL_VALUE,
        **SECRETS,
    }
    env = _command_env(tmp_path, source)
    expected = POSIX_KEYS | {"LC_ALL"}
    if sys.platform == "win32":
        expected |= {"USERPROFILE", "APPDATA", "LOCALAPPDATA"}
    assert set(env) - {"VIRTUAL_ENV"} == expected
    assert env["LC_ALL"] == "C" and env["PATH"].endswith("/bin")
    # HOME and the temp directory are the scratch directory, never the parent's.
    assert env["HOME"] == str(tmp_path / "home") and (tmp_path / "home").is_dir()
    assert os.path.realpath(env["TMPDIR"]) == os.path.realpath(tmp_path)
    assert SENTINEL_VALUE not in json.dumps(env)
    assert env.get("VIRTUAL_ENV") != "/other/venv"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX venv layout")
def test_a_virtualenv_interpreter_names_its_venv_and_leads_path(tmp_path: Path) -> None:
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    python = venv / "bin" / "python3"
    python.symlink_to(sys.executable)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    env = _command_env(scratch, {"PATH": "/bin"}, str(python))
    assert env["VIRTUAL_ENV"] == str(venv)
    assert env["PATH"] == os.pathsep.join((str(venv / "bin"), "/bin"))
    plain = tmp_path / "plain"
    (plain / "bin").mkdir(parents=True)
    (plain / "bin" / "python3").symlink_to(sys.executable)
    other = _command_env(scratch, {"PATH": "/bin"}, str(plain / "bin" / "python3"))
    assert "VIRTUAL_ENV" not in other and other["PATH"] == "/bin"


def test_the_scratch_directory_is_removed_afterwards() -> None:
    with check_scratch() as scratch:
        env = _command_env(scratch, {"PATH": "/bin"})
        assert Path(env["HOME"]).parent == scratch and scratch.is_dir()
    assert not scratch.exists()


async def test_a_script_check_never_sees_a_parent_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real child process, launched the way every script check is (``_run_argv``)."""
    monkeypatch.setenv(SENTINEL_NAME, SENTINEL_VALUE)
    for key, value in SECRETS.items():
        monkeypatch.setenv(key, value)
    parent_home = os.environ.get("HOME", "")
    argv = ["python3", "-c", "import json, os; print(json.dumps(dict(os.environ)))"]
    # No ``env``: the library default must not hand the child this process's environment.
    completed = await _run_argv(
        argv, tmp_path, 30, interpreter=pin_interpreter(sys.executable, "test")
    )
    assert completed.return_code == 0, completed.stderr
    child = json.loads(completed.stdout)
    assert SENTINEL_NAME not in child
    assert not set(SECRETS) & set(child)
    assert SENTINEL_VALUE not in completed.stdout.decode()
    assert not any(value in completed.stdout.decode() for value in SECRETS.values())
    assert child["HOME"] != parent_home
    assert not Path(child["HOME"]).exists()  # the scratch directory is gone
    if sys.platform != "win32":
        # Only the built names (VIRTUAL_ENV: this interpreter's venv); the OS
        # itself may add LC_CTYPE or macOS's __CF_USER_TEXT_ENCODING at exec.
        assert set(child) - {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"} <= (
            POSIX_KEYS | {"VIRTUAL_ENV"} | set(_copied_names())
        )


def _copied_names() -> tuple[str, ...]:
    from ouroboros.boundary.check_env import CHECK_ENV_COPIED

    return CHECK_ENV_COPIED


async def test_an_oracle_target_process_never_sees_a_parent_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The implementation under an oracle check runs in the same built environment."""
    monkeypatch.setenv(SENTINEL_NAME, SENTINEL_VALUE)
    for key, value in SECRETS.items():
        monkeypatch.setenv(key, value)
    report = tmp_path / "target-env.json"
    probe = f"import json, os\nopen({str(report)!r}, 'w').write(json.dumps(dict(os.environ)))\n"
    base = _repo(tmp_path / "base", {"mathutils.py": FIXED})
    candidate = _repo(tmp_path / "cand", {"mathutils.py": probe + FIXED})
    package = _oracle_package(_oracle_seed(), base)
    result = await verify_candidate(package, candidate)
    assert result.verdict.value == "pass"
    seen = json.loads(report.read_text())
    assert SENTINEL_NAME not in seen and not set(SECRETS) & set(seen)
    assert SENTINEL_VALUE not in report.read_text()
    assert seen["HOME"] != os.environ.get("HOME")


def _preservation_package(seed):
    package = _package(seed, "keep_env", NO_SECRET_SCRIPT)
    check = package.checks[0]
    return package.model_copy(
        update={
            "checks": (
                check.model_copy(
                    update={"role": CheckRole.PRESERVATION, "failure_signature": None}
                ),
            )
        }
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    return root


def _fake_venv(root: Path) -> Path:
    bin_dir = root / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    python = bin_dir / "python3"
    python.symlink_to(sys.executable)
    return python


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX venv layout")
def test_interpreter_prefers_the_project_venv(tmp_path: Path) -> None:
    checkout = tmp_path / "project"
    python = _fake_venv(checkout)
    chosen = resolve_check_interpreter(checkout, environ={})
    assert (chosen.path, chosen.source) == (str(python), "project_venv")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX venv layout")
def test_linked_worktree_uses_the_main_tree_venv(tmp_path: Path) -> None:
    main = tmp_path / "main"
    python = _fake_venv(main)
    (main / ".git" / "worktrees" / "task").mkdir(parents=True)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {main / '.git' / 'worktrees' / 'task'}\n")
    chosen = resolve_check_interpreter(worktree, environ={})
    assert (chosen.path, chosen.source) == (str(python), "project_venv")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX venv layout")
def test_active_virtualenv_then_python3_fallback(tmp_path: Path) -> None:
    active = tmp_path / "active-env"
    (active / "bin").mkdir(parents=True)
    (active / "bin" / "python3").symlink_to(sys.executable)
    chosen = resolve_check_interpreter(tmp_path / "plain", environ={"VIRTUAL_ENV": str(active)})
    assert chosen.source == "active_venv"
    fallback = resolve_check_interpreter(tmp_path / "plain", environ={})
    assert fallback.source == "python3_fallback"
    assert os.path.basename(fallback.path).startswith("python3")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX venv layout")
async def test_admission_runs_checks_with_the_resolved_interpreter(
    repo: Path, tmp_path: Path
) -> None:
    marker = tmp_path / "used-interpreter"
    wrapper = repo / ".venv" / "bin" / "python3"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text(f'#!/bin/sh\necho used > "{marker}"\nexec "{sys.executable}" "$@"\n')
    wrapper.chmod(0o755)
    seed = _seed("add(2, 3) returns 5")
    package = _preservation_package(seed)
    chosen = resolve_check_interpreter(repo, environ={})
    result = await admit_check_package(
        package,
        repo,
        env={"PATH": os.environ.get("PATH", "")},
        interpreter=chosen,
    )
    assert result.verdict.value == "admitted", result.reasons
    assert marker.read_text().strip() == "used"
    assert result.interpreter_source == "project_venv"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX venv layout")
async def test_a_pin_from_a_relative_checkout_runs_from_the_copied_checkout(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The checkout is named relative to the working directory; every check
    # runs in its own copy, so a relative interpreter path would not resolve.
    _fake_venv(repo)
    monkeypatch.chdir(tmp_path)
    chosen = resolve_check_interpreter(Path(repo.name), environ={})
    assert chosen.source == "project_venv"
    assert Path(chosen.path).is_absolute()
    assert chosen.problem() is None
    result = await admit_check_package(
        _preservation_package(_seed("add(2, 3) returns 5")),
        Path(repo.name),
        env={"PATH": os.environ.get("PATH", "")},
        interpreter=chosen,
    )
    assert result.verdict.value == "admitted", result.reasons
    assert result.interpreter == str(repo / ".venv" / "bin" / "python3")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_the_entry_point_refuses_a_replaced_interpreter(tmp_path: Path) -> None:
    python = _fake_venv(tmp_path / "project")
    pinned = resolve_check_interpreter(tmp_path / "project", environ={})
    with check_scratch(tmp_path) as scratch:
        command = check_command(
            ["python3", "-c", "pass"],
            cwd=tmp_path,
            writable_root=tmp_path,
            interpreter=pinned,
            scratch=scratch,
        )
        assert isinstance(command, CheckCommand)
        assert command.argv[0] == str(python)
        # The worker swaps the venv's interpreter for something else.
        python.unlink()
        python.write_text("#!/bin/sh\nexit 0\n")
        python.chmod(0o755)
        refused = check_command(
            ["python3", "-c", "pass"],
            cwd=tmp_path,
            writable_root=tmp_path,
            interpreter=pinned,
            scratch=scratch,
        )
    assert refused == CheckUnavailable(INTERPRETER_CHANGED, str(python))


async def test_a_refused_script_check_is_indeterminate_and_never_runs(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    missing = pin_interpreter(str(tmp_path / "no-such-python"), "test")
    argv = ["python3", "-c", f"open({str(marker)!r}, 'w').write('x')"]
    completed = await _run_argv(argv, tmp_path, 30, interpreter=missing)
    assert completed.unavailable == "interpreter_unavailable"
    assert completed.return_code is None and not marker.exists()


# --------------------------------------------------------------------------
# Confinement by the shared execution sandbox (runtime/exec_sandbox.py)


def _require_real_sandbox() -> None:
    from ouroboros.runtime.exec_sandbox import sandbox_unavailable_reason

    reason = sandbox_unavailable_reason(deny_network=True)
    if reason is not None:
        pytest.skip(f"execution sandbox unavailable on this host: {reason}")


WRITE_OUTSIDE_SCRIPT = """import sys
target = sys.argv[1]
try:
    with open(target, "w") as handle:
        handle.write("escaped")
except OSError:
    print("denied")
    sys.exit(0)
print("wrote outside the copy")
sys.exit(1)
"""


async def test_a_confined_script_check_cannot_write_outside_its_copy(
    tmp_path: Path, real_check_isolation: None
) -> None:
    _require_real_sandbox()
    copy = tmp_path / "copy"
    copy.mkdir()
    (copy / "probe.py").write_text(WRITE_OUTSIDE_SCRIPT)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside.resolve() / "escaped.txt"
    completed = await _run_argv(
        ["python3", "probe.py", str(target)],
        copy,
        60,
        interpreter=pin_interpreter(sys.executable, "test"),
        scratch_parent=tmp_path,
        writable_root=copy,
    )
    assert completed.unavailable is None
    assert completed.return_code == 0, completed.stdout + completed.stderr
    assert b"denied" in completed.stdout
    assert not target.exists()
    # Inside the copy the check may still write.
    inside = await _run_argv(
        ["python3", "-c", "open('made.txt', 'w').write('ok')"],
        copy,
        60,
        interpreter=pin_interpreter(sys.executable, "test"),
        scratch_parent=tmp_path,
        writable_root=copy,
    )
    assert inside.return_code == 0, inside.stderr
    assert (copy / "made.txt").read_text() == "ok"


async def test_confined_oracle_targets_keep_the_frame_protocol(
    tmp_path: Path, real_check_isolation: None
) -> None:
    """An oracle check decides through the sandbox's launcher exactly as unconfined."""
    _require_real_sandbox()
    base = _repo(tmp_path / "base", {"mathutils.py": FIXED})
    candidate = _repo(tmp_path / "cand", {"mathutils.py": FIXED})
    package = _oracle_package(_oracle_seed(), base)
    result = await verify_candidate(package, candidate)
    assert result.verdict.value == "pass", [c.reason for c in result.checks]


async def test_an_unavailable_sandbox_makes_every_check_indeterminate_and_runs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, real_check_isolation: None
) -> None:
    from ouroboros.boundary import check_env
    from ouroboros.runtime.exec_sandbox import SandboxUnavailable, SandboxUnavailableReason

    calls: list[tuple[str, ...]] = []

    def refuse(argv, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(tuple(argv))
        return SandboxUnavailable(SandboxUnavailableReason.SANDBOX_UNAVAILABLE)

    monkeypatch.setattr(check_env, "confine", refuse)
    marker = tmp_path / "ran"
    script = f"open({str(marker)!r}, 'w').write('x')\n" + NO_SECRET_SCRIPT
    seed = _seed("add(2, 3) returns 5")
    repo = _repo(tmp_path / "repo", {"calc.py": "def add(a, b):\n    return a + b\n"})
    scripted = _preservation_package_with(seed, script)
    admission = await admit_check_package(scripted, repo)
    assert [(c.status.value, c.reason) for c in admission.checks] == [
        ("indeterminate", "sandbox_unavailable")
    ]
    assert not marker.exists()
    oracle_base = _repo(tmp_path / "obase", {"mathutils.py": FIXED})
    oracle = await verify_candidate(_oracle_package(_oracle_seed(), oracle_base), oracle_base)
    assert oracle.checks and all(
        (c.status.value, c.reason) == ("indeterminate", "sandbox_unavailable")
        for c in oracle.checks
    )
    assert calls  # every process was offered to the sandbox, none ran


def _preservation_package_with(seed, script):  # type: ignore[no-untyped-def]
    package = _package(seed, "keep_env", script)
    check = package.checks[0]
    return package.model_copy(
        update={
            "checks": (
                check.model_copy(
                    update={"role": CheckRole.PRESERVATION, "failure_signature": None}
                ),
            )
        }
    )


async def test_a_confined_script_check_never_sees_a_parent_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, real_check_isolation: None
) -> None:
    """Under the real sandbox the command runs with the check environment, not the launcher's."""
    _require_real_sandbox()
    monkeypatch.setenv(SENTINEL_NAME, SENTINEL_VALUE)
    for key, value in SECRETS.items():
        monkeypatch.setenv(key, value)
    argv = ["python3", "-c", "import json, os; print(json.dumps(dict(os.environ)))"]
    completed = await _run_argv(
        argv, tmp_path, 60, interpreter=pin_interpreter(sys.executable, "test")
    )
    assert completed.return_code == 0, completed.stderr
    child = json.loads(completed.stdout)
    assert SENTINEL_NAME not in child and not set(SECRETS) & set(child)
    assert SENTINEL_VALUE not in completed.stdout.decode()
    assert child["HOME"] != os.environ.get("HOME", "")
