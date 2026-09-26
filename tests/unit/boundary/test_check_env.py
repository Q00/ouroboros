"""Environment and interpreter for model-written checks (boundary/check_env.py)."""

from __future__ import annotations

import os
from pathlib import Path
import sys

import pytest

from ouroboros.boundary import admit_check_package
from ouroboros.boundary.check_env import (
    resolve_check_interpreter,
    scrubbed_check_environment,
)
from ouroboros.boundary.package import CheckRole
from ouroboros.boundary.run_wiring import CheckPackageSettings, prepare_check_package
from ouroboros.persistence.event_store import EventStore

from .test_run_wiring import FakeConstructor, _ok, _package, _seed

SECRETS = {
    "OPENAI_API_KEY": "sk-test",
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "GH_TOKEN": "ghp_test",
    "GITHUB_TOKEN": "ghs_test",
    "AWS_SECRET_ACCESS_KEY": "aws-test",
    "MY_SERVICE_PASSWORD": "hunter2",
}

# Preservation check: passes only when no credential-like variable is visible.
NO_SECRET_SCRIPT = """import os, sys
leaked = sorted(k for k in os.environ if any(t in k for t in ("KEY", "TOKEN", "SECRET", "PASSWORD")))
print("leaked:", leaked)
sys.exit(1 if leaked else 0)
"""


def test_scrub_keeps_the_allowlist_and_drops_credentials() -> None:
    env = scrubbed_check_environment(
        {"PATH": "/bin", "HOME": "/h", "LC_ALL": "C", "VIRTUAL_ENV": "/v", **SECRETS}
    )
    assert env == {"PATH": "/bin", "HOME": "/h", "LC_ALL": "C", "VIRTUAL_ENV": "/v"}


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


async def test_product_admission_hides_credentials_from_checks(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in SECRETS.items():
        monkeypatch.setenv(key, value)
    seed = _seed("add(2, 3) returns 5")
    package = _preservation_package(seed)

    # The library default keeps the caller's environment (a harness may need it).
    unscrubbed = await admit_check_package(package, repo)
    assert unscrubbed.verdict.value == "rejected"

    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    try:
        state = await prepare_check_package(
            seed,
            event_store=store,
            constructor=FakeConstructor(_ok(package)),
            execution_id="exec_env",
            base_checkout=repo,
            worker_workspace=repo,
            runtime_label="codex",
            settings=CheckPackageSettings(enabled=True, max_construction_attempts=1),
            store_dir=tmp_path / "store",
        )
    finally:
        await store.close()
    assert state.admitted, state.failure_reason
    assert state.admission is not None
    assert state.admission.interpreter_source == "python3_fallback"
    summary = state.admission.event_summary()
    assert summary["interpreter_source"] == "python3_fallback"
    assert "interpreter" not in summary  # the absolute path stays in the stored receipt


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
        env=scrubbed_check_environment({"PATH": os.environ.get("PATH", "")}),
        interpreter=chosen.path,
        interpreter_source=chosen.source,
    )
    assert result.verdict.value == "admitted", result.reasons
    assert marker.read_text().strip() == "used"
    assert result.interpreter_source == "project_venv"
