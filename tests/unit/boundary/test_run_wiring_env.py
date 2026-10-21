"""Product admission through run wiring runs checks in the allowlisted environment."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

from ouroboros.boundary.admission import admit_check_package
from ouroboros.boundary.run_wiring import CheckPackageSettings, prepare_check_package
from ouroboros.persistence.event_store import EventStore

from .calc_fixtures import _seed
from .fake_constructors import FakeConstructor, _ok
from .test_check_env import SECRETS, _preservation_package


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    return root


async def _prepare(seed, package, repo: Path, tmp_path: Path):
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    try:
        return await prepare_check_package(
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


async def test_product_admission_hides_credentials_from_checks(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Credential hiding only; independent of which interpreter is detected."""
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    for key, value in SECRETS.items():
        monkeypatch.setenv(key, value)
    seed = _seed("add(2, 3) returns 5")
    package = _preservation_package(seed)

    # The library default builds the same environment: no caller can hand a
    # check the parent's variables.
    library = await admit_check_package(package, repo)
    assert library.verdict.value == "admitted", library.reasons

    state = await _prepare(seed, package, repo, tmp_path)
    assert state.admitted, state.failure_reason


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX venv layout")
@pytest.mark.parametrize("active", [True, False], ids=["active_venv", "python3_fallback"])
async def test_product_admission_records_the_detected_interpreter(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, active: bool
) -> None:
    """Interpreter selection follows VIRTUAL_ENV (the detector's only env input)."""
    for key, value in SECRETS.items():
        monkeypatch.setenv(key, value)
    if active:
        venv = tmp_path / "active-env"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python3").symlink_to(sys.executable)
        monkeypatch.setenv("VIRTUAL_ENV", str(venv))
    else:
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    seed = _seed("add(2, 3) returns 5")
    state = await _prepare(seed, _preservation_package(seed), repo, tmp_path)

    expected = "active_venv" if active else "python3_fallback"
    # Admitted means the credential check passed under either interpreter.
    assert state.admitted, state.failure_reason
    assert state.admission is not None
    assert state.admission.interpreter_source == expected
    if active:
        assert state.admission.interpreter == str(venv / "bin" / "python3")
    summary = state.admission.event_summary()
    assert summary["interpreter_source"] == expected
    assert "interpreter" not in summary  # the absolute path stays in the stored receipt
