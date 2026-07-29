"""Self-tests for ``scripts/check-module-size.py``.

The gate is only worth its CI minute if all four transitions behave: growth
must fail, a new oversized module must fail, real shrinkage must force a
re-seed (otherwise the ratchet never tightens and reclaimed headroom silently
stays spendable), and a module that reaches the cap must be retired from the
grandfather table. A vanished entry must fail loud rather than silently drop
its cap. All of those are exercised here, plus the real repository, which must
stay green.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "check-module-size.py"


def _load_module():
    """Load the hyphenated script as a module so ``main()`` can be called with
    a fabricated source tree."""
    spec = importlib.util.spec_from_file_location("check_module_size", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _write(root: Path, rel: str, lines: int) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"# line {n}\n" for n in range(lines)), encoding="utf-8")


def _isolate(
    module,
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
    grandfathered: dict[str, int],
) -> None:
    """Point the module at a fabricated repository with a controlled table."""
    monkeypatch.setattr(module, "REPO_ROOT", repo)
    monkeypatch.setattr(module, "SOURCE_ROOT", repo / "src" / "ouroboros")
    monkeypatch.setattr(module, "GRANDFATHERED", grandfathered)


@pytest.fixture
def module():
    return _load_module()


def test_clean_tree_passes(module, monkeypatch, tmp_path, capsys):
    _write(tmp_path, "src/ouroboros/small.py", 100)
    _write(tmp_path, "src/ouroboros/big.py", 3000)
    _isolate(module, monkeypatch, tmp_path, {"src/ouroboros/big.py": 3000})

    assert module.main() == 0
    assert "OK" in capsys.readouterr().out


def test_ungrandfathered_module_over_cap_fails(module, monkeypatch, tmp_path, capsys):
    _write(tmp_path, "src/ouroboros/new.py", module.SOFT_CAP + 1)
    _isolate(module, monkeypatch, tmp_path, {})

    assert module.main() == 1
    err = capsys.readouterr().err
    assert "not grandfathered" in err
    assert "src/ouroboros/new.py" in err


def test_module_exactly_at_cap_passes(module, monkeypatch, tmp_path):
    """The cap is inclusive; an off-by-one here would reject a legal module."""
    _write(tmp_path, "src/ouroboros/edge.py", module.SOFT_CAP)
    _isolate(module, monkeypatch, tmp_path, {})

    assert module.main() == 0


def test_grandfathered_growth_fails(module, monkeypatch, tmp_path, capsys):
    _write(tmp_path, "src/ouroboros/god.py", 5001)
    _isolate(module, monkeypatch, tmp_path, {"src/ouroboros/god.py": 5000})

    assert module.main() == 1
    err = capsys.readouterr().err
    assert "grew" in err
    assert "budget 5000 (+1)" in err


def test_grandfathered_at_budget_passes(module, monkeypatch, tmp_path):
    _write(tmp_path, "src/ouroboros/god.py", 5000)
    _isolate(module, monkeypatch, tmp_path, {"src/ouroboros/god.py": 5000})

    assert module.main() == 0


def test_shrinkage_within_slack_passes(module, monkeypatch, tmp_path):
    """Routine edits must not churn the table."""
    _write(tmp_path, "src/ouroboros/god.py", 5000 - module.RESEED_SLACK)
    _isolate(module, monkeypatch, tmp_path, {"src/ouroboros/god.py": 5000})

    assert module.main() == 0


def test_real_shrinkage_demands_reseed(module, monkeypatch, tmp_path, capsys):
    """Without this the ratchet is only a static cap: reclaimed headroom would
    stay available for the next contributor to spend."""
    shrunk = 5000 - module.RESEED_SLACK - 1
    _write(tmp_path, "src/ouroboros/god.py", shrunk)
    _isolate(module, monkeypatch, tmp_path, {"src/ouroboros/god.py": 5000})

    assert module.main() == 1
    err = capsys.readouterr().err
    assert "shrank" in err
    # The message must be paste-ready, or nobody will re-seed.
    assert f'"src/ouroboros/god.py": {shrunk},' in err


def test_reaching_the_cap_demands_retirement(module, monkeypatch, tmp_path, capsys):
    """At or below the cap the entry is dead weight and must be deleted, not
    re-seeded -- otherwise a retired module could be grandfathered again."""
    _write(tmp_path, "src/ouroboros/god.py", module.SOFT_CAP)
    _isolate(module, monkeypatch, tmp_path, {"src/ouroboros/god.py": 5000})

    assert module.main() == 1
    err = capsys.readouterr().err
    assert "Delete these entries" in err
    assert "src/ouroboros/god.py" in err


def test_vanished_entry_fails_loud(module, monkeypatch, tmp_path, capsys):
    """A renamed module must not silently lose its cap."""
    _write(tmp_path, "src/ouroboros/kept.py", 10)
    _isolate(module, monkeypatch, tmp_path, {"src/ouroboros/moved.py": 5000})

    assert module.main() == 1
    err = capsys.readouterr().err
    assert "no longer exist" in err
    assert "gone: src/ouroboros/moved.py" in err


def test_missing_source_root_fails(module, monkeypatch, tmp_path, capsys):
    """An empty or wrong checkout must not report a green check."""
    _isolate(module, monkeypatch, tmp_path, {})

    assert module.main() == 1
    assert "does not exist" in capsys.readouterr().err


def test_excluded_generated_module_is_ignored(module, monkeypatch, tmp_path):
    _write(tmp_path, "src/ouroboros/_version.py", module.SOFT_CAP + 500)
    _isolate(module, monkeypatch, tmp_path, {})

    assert module.main() == 0


def test_final_line_without_newline_counts(module, monkeypatch, tmp_path):
    """``splitlines`` semantics: a file whose last line lacks a terminator
    still spends that line, unlike ``wc -l``."""
    path = tmp_path / "src" / "ouroboros" / "tail.py"
    path.parent.mkdir(parents=True)
    path.write_text("a\nb\nc", encoding="utf-8")
    _isolate(module, monkeypatch, tmp_path, {})

    assert module._line_count(path) == 3


def test_real_repository_is_green():
    """The seeded budgets must match main, or the gate lands red on merge."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert "module-size: OK" in result.stdout
