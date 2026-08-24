"""Self-tests for ``scripts/check-module-size.py``.

Two layers have to hold, and the second is the one that makes the gate real.

Against the source snapshot: growth must fail, a new oversized module must
fail, real shrinkage must force a re-seed (otherwise the ratchet never tightens
and reclaimed headroom silently stays spendable), and a module that reaches the
cap must be retired from the table. A vanished entry must fail loud rather than
silently drop its cap.

Against the base branch: the policy itself is editable in the commit it gates,
so ``TestPolicyTightening`` covers the bypasses that the snapshot layer alone
cannot see -- raising a budget alongside the growth it permits, grandfathering
a new module, re-grandfathering a retired one, and raising ``SOFT_CAP``. An
unreadable baseline must fail closed rather than skip.

The real repository is exercised too, and must stay green.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "check-module-size.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "module-size.yml"


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
    policy_path = repo / "scripts" / "check-module-size.py"
    if not policy_path.exists():
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        policy_path.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(module, "REPO_ROOT", repo)
    monkeypatch.setattr(module, "__file__", str(policy_path))
    monkeypatch.setattr(module, "GRANDFATHERED", grandfathered)


def _baseline_repo(
    root: Path,
    grandfathered: dict[str, int],
    soft_cap: int = 2000,
    reseed_slack: int = 200,
    excluded: frozenset[str] = frozenset({"src/ouroboros/_version.py"}),
    trailer: str = "",
) -> str:
    """Build a git repo whose HEAD carries a given policy, and return its ref.

    The gate reads its baseline by asking git for an earlier copy of itself, so
    a faithful test has to produce a real commit containing a real script body.
    """
    entries = "\n".join(f'    "{k}": {v},' for k, v in grandfathered.items())
    body = (
        'MODULE_SIZE_POLICY_ID = "ouroboros.module-size.v1"\n'
        'SOURCE_ROOT = "src/ouroboros"\n'
        'MODULE_GLOB = "*.py"\n'
        f"SOFT_CAP = {soft_cap}\n"
        f"RESEED_SLACK = {reseed_slack}\n"
        f"GRANDFATHERED: dict[str, int] = {{\n{entries}\n}}\n"
        f"EXCLUDED = frozenset({set(excluded)!r})\n"
        f"{trailer}"
    )
    script = root / "scripts" / "check-module-size.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(body, encoding="utf-8")
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@e",
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(root),
    }
    for command in (
        ["git", "init", "-q", "-b", "base"],
        ["git", "add", "-A"],
        ["git", "commit", "-qm", "baseline policy"],
    ):
        subprocess.run(command, cwd=root, check=True, capture_output=True, env=env)
    return "base"


@pytest.fixture
def module():
    return _load_module()


class TestPolicyTightening:
    """The proposed policy must never loosen the base branch's.

    GRANDFATHERED and SOFT_CAP live in the same commit they gate, so measuring
    source against the proposed table alone proves nothing: a PR could grow a
    module and raise its budget together and still report OK.
    """

    def test_raising_a_budget_fails(self, module, monkeypatch, tmp_path, capsys):
        ref = _baseline_repo(tmp_path, {"src/ouroboros/god.py": 5000})
        _write(tmp_path, "src/ouroboros/god.py", 5500)
        _isolate(module, monkeypatch, tmp_path, {"src/ouroboros/god.py": 5500})

        assert module.main(["--baseline-ref", ref]) == 1
        err = capsys.readouterr().err
        assert "budgets were raised" in err
        assert "5000 -> 5500 (+500)" in err

    def test_adding_an_entry_fails(self, module, monkeypatch, tmp_path, capsys):
        """Covers re-grandfathering too: a retired module is simply absent."""
        ref = _baseline_repo(tmp_path, {"src/ouroboros/god.py": 5000})
        _write(tmp_path, "src/ouroboros/god.py", 5000)
        _write(tmp_path, "src/ouroboros/newcomer.py", 3000)
        _isolate(
            module,
            monkeypatch,
            tmp_path,
            {"src/ouroboros/god.py": 5000, "src/ouroboros/newcomer.py": 3000},
        )

        assert module.main(["--baseline-ref", ref]) == 1
        err = capsys.readouterr().err
        assert "added to GRANDFATHERED" in err
        assert "src/ouroboros/newcomer.py" in err

    def test_raising_the_cap_fails(self, module, monkeypatch, tmp_path, capsys):
        ref = _baseline_repo(tmp_path, {}, soft_cap=2000)
        _write(tmp_path, "src/ouroboros/wide.py", 9000)
        monkeypatch.setattr(module, "SOFT_CAP", 10000)
        _isolate(module, monkeypatch, tmp_path, {})

        assert module.main(["--baseline-ref", ref]) == 1
        assert "SOFT_CAP was raised from 2000 to 10000" in capsys.readouterr().err

    def test_raising_reseed_slack_fails(self, module, monkeypatch, tmp_path, capsys):
        ref = _baseline_repo(tmp_path, {}, reseed_slack=200)
        _write(tmp_path, "src/ouroboros/ok.py", 10)
        monkeypatch.setattr(module, "RESEED_SLACK", 1000)
        _isolate(module, monkeypatch, tmp_path, {})

        assert module.main(["--baseline-ref", ref]) == 1
        assert "RESEED_SLACK was raised from 200 to 1000" in capsys.readouterr().err

    def test_adding_exclusion_fails(self, module, monkeypatch, tmp_path, capsys):
        ref = _baseline_repo(tmp_path, {})
        _write(tmp_path, "src/ouroboros/hidden.py", 10)
        monkeypatch.setattr(
            module,
            "EXCLUDED",
            frozenset({"src/ouroboros/_version.py", "src/ouroboros/hidden.py"}),
        )
        _isolate(module, monkeypatch, tmp_path, {})

        assert module.main(["--baseline-ref", ref]) == 1
        err = capsys.readouterr().err
        assert "added to EXCLUDED" in err
        assert "src/ouroboros/hidden.py" in err

    @pytest.mark.parametrize(
        ("name", "proposed"),
        [("SOURCE_ROOT", "src/ouroboros/mcp"), ("MODULE_GLOB", "tools/*.py")],
    )
    def test_narrowing_measurement_scope_fails(
        self, module, monkeypatch, tmp_path, capsys, name, proposed
    ):
        ref = _baseline_repo(tmp_path, {})
        _write(tmp_path, "src/ouroboros/kept.py", 10)
        _write(tmp_path, "src/ouroboros/mcp/tools/visible.py", 10)
        _isolate(module, monkeypatch, tmp_path, {})
        monkeypatch.setattr(module, name, proposed)

        assert module.main(["--baseline-ref", ref]) == 1
        err = capsys.readouterr().err
        assert "Measurement scope changed" in err
        assert name in err

    def test_renamed_current_script_cannot_hide_predecessor_policy(
        self, module, monkeypatch, tmp_path, capsys
    ):
        ref = _baseline_repo(tmp_path, {"src/ouroboros/god.py": 5000})
        _write(tmp_path, "src/ouroboros/god.py", 5500)
        _isolate(module, monkeypatch, tmp_path, {"src/ouroboros/god.py": 5500})
        canonical = tmp_path / "scripts" / "check-module-size.py"
        renamed = tmp_path / "scripts" / "check-size.py"
        canonical.rename(renamed)
        monkeypatch.setattr(module, "__file__", str(renamed))

        assert module.main(["--baseline-ref", ref]) == 1
        assert "budgets were raised" in capsys.readouterr().err

    def test_duplicate_current_policy_fails_before_merge(
        self, module, monkeypatch, tmp_path, capsys
    ):
        ref = _baseline_repo(tmp_path, {})
        _write(tmp_path, "src/ouroboros/ok.py", 10)
        _isolate(module, monkeypatch, tmp_path, {})
        decoy = tmp_path / "scripts" / "decoy.py"
        decoy.write_text(
            (tmp_path / "scripts" / "check-module-size.py").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        assert module.main(["--baseline-ref", ref]) == 1
        assert "exactly one module-size policy" in capsys.readouterr().err

    def test_unrelated_nonliteral_source_root_is_not_a_policy_candidate(
        self, module, monkeypatch, tmp_path
    ):
        ref = _baseline_repo(tmp_path, {})
        _write(tmp_path, "src/ouroboros/ok.py", 10)
        _isolate(module, monkeypatch, tmp_path, {})
        (tmp_path / "scripts" / "unrelated.py").write_text(
            "from pathlib import Path\nSOURCE_ROOT = Path(__file__).parent\n",
            encoding="utf-8",
        )

        assert module.main(["--baseline-ref", ref]) == 0

    def test_policy_outside_scripts_is_rejected(self, module, monkeypatch, tmp_path, capsys):
        ref = _baseline_repo(tmp_path, {})
        _write(tmp_path, "src/ouroboros/ok.py", 10)
        _isolate(module, monkeypatch, tmp_path, {})
        canonical = tmp_path / "scripts" / "check-module-size.py"
        moved = tmp_path / "tools" / "check-module-size.py"
        moved.parent.mkdir()
        canonical.rename(moved)
        monkeypatch.setattr(module, "__file__", str(moved))

        assert module.main(["--baseline-ref", ref]) == 1
        assert "found none" in capsys.readouterr().err

    def test_lowering_is_allowed(self, module, monkeypatch, tmp_path):
        """Tightening is the whole point; it must not be mistaken for drift."""
        ref = _baseline_repo(tmp_path, {"src/ouroboros/god.py": 5000})
        _write(tmp_path, "src/ouroboros/god.py", 4700)
        _isolate(module, monkeypatch, tmp_path, {"src/ouroboros/god.py": 4700})

        assert module.main(["--baseline-ref", ref]) == 0

    def test_partial_reseed_fails(self, module, monkeypatch, tmp_path, capsys):
        """A decrease must lock in all significant shrinkage, not retain slack."""
        ref = _baseline_repo(tmp_path, {"src/ouroboros/god.py": 5000})
        _write(tmp_path, "src/ouroboros/god.py", 4500)
        _isolate(module, monkeypatch, tmp_path, {"src/ouroboros/god.py": 4700})

        assert module.main(["--baseline-ref", ref]) == 1
        err = capsys.readouterr().err
        assert "Partial re-seeds preserve headroom" in err
        assert '"src/ouroboros/god.py": 4500,' in err
        assert "predecessor 5000, proposed 4700" in err

    def test_exact_reseed_passes(self, module, monkeypatch, tmp_path):
        ref = _baseline_repo(tmp_path, {"src/ouroboros/god.py": 5000})
        _write(tmp_path, "src/ouroboros/god.py", 4500)
        _isolate(module, monkeypatch, tmp_path, {"src/ouroboros/god.py": 4500})

        assert module.main(["--baseline-ref", ref]) == 0

    def test_removing_an_entry_is_allowed(self, module, monkeypatch, tmp_path):
        ref = _baseline_repo(tmp_path, {"src/ouroboros/god.py": 5000})
        _write(tmp_path, "src/ouroboros/god.py", 1500)
        _isolate(module, monkeypatch, tmp_path, {})

        assert module.main(["--baseline-ref", ref]) == 0

    def test_unresolvable_ref_fails_closed(self, module, monkeypatch, tmp_path, capsys):
        """A skipped comparison is the hole this check exists to close."""
        _baseline_repo(tmp_path, {})
        _write(tmp_path, "src/ouroboros/ok.py", 10)
        _isolate(module, monkeypatch, tmp_path, {})

        assert module.main(["--baseline-ref", "refs/heads/never-existed"]) == 1
        assert "does not resolve" in capsys.readouterr().err

    def test_missing_baseline_script_is_the_introducing_commit(
        self, module, monkeypatch, tmp_path, capsys
    ):
        """The gate's own first PR has no predecessor policy to tighten."""
        env = {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@e",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@e",
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(tmp_path),
        }
        (tmp_path / "unrelated.txt").write_text("x", encoding="utf-8")
        for command in (
            ["git", "init", "-q", "-b", "base"],
            ["git", "add", "-A"],
            ["git", "commit", "-qm", "no gate yet"],
        ):
            subprocess.run(command, cwd=tmp_path, check=True, capture_output=True, env=env)
        _write(tmp_path, "src/ouroboros/ok.py", 10)
        _isolate(module, monkeypatch, tmp_path, {})

        assert module.main(["--baseline-ref", "base"]) == 0
        assert "introducing commit" in capsys.readouterr().out

    def test_baseline_show_failure_fails_closed(self, module, monkeypatch, tmp_path, capsys):
        ref = _baseline_repo(tmp_path, {})
        _write(tmp_path, "src/ouroboros/ok.py", 10)
        _isolate(module, monkeypatch, tmp_path, {})
        original_run = module.subprocess.run

        def failing_show(command, *args, **kwargs):  # noqa: ANN001, ANN202
            if command[:2] == ["git", "show"]:
                raise subprocess.CalledProcessError(128, command)
            return original_run(command, *args, **kwargs)

        monkeypatch.setattr(module.subprocess, "run", failing_show)

        assert module.main(["--baseline-ref", ref]) == 1
        assert "could not inspect policy candidate" in capsys.readouterr().err

    def test_baseline_policy_source_is_decoded_as_utf8(self, module, monkeypatch, tmp_path):
        ref = _baseline_repo(tmp_path, {}, trailer="# 한글 정책\n")
        _write(tmp_path, "src/ouroboros/ok.py", 10)
        _isolate(module, monkeypatch, tmp_path, {})
        monkeypatch.setattr(module.subprocess, "_text_encoding", lambda: "ascii")

        assert module.main(["--baseline-ref", ref]) == 0

    def test_no_baseline_ref_skips_the_comparison(self, module, monkeypatch, tmp_path):
        """A bare local run must not require a git repository."""
        _write(tmp_path, "src/ouroboros/ok.py", 10)
        _isolate(module, monkeypatch, tmp_path, {})

        assert module.main([]) == 0


def test_clean_tree_passes(module, monkeypatch, tmp_path, capsys):
    """A table entry matching its module is the steady state.

    Note this asserts only the snapshot layer, since no baseline ref is passed.
    That an arbitrary oversized module passes *once listed* is precisely why
    the table cannot be its own baseline -- see ``TestPolicyTightening``.
    """
    _write(tmp_path, "src/ouroboros/small.py", 100)
    _write(tmp_path, "src/ouroboros/big.py", 3000)
    _isolate(module, monkeypatch, tmp_path, {"src/ouroboros/big.py": 3000})

    assert module.main([]) == 0
    assert "OK" in capsys.readouterr().out


def test_ungrandfathered_module_over_cap_fails(module, monkeypatch, tmp_path, capsys):
    _write(tmp_path, "src/ouroboros/new.py", module.SOFT_CAP + 1)
    _isolate(module, monkeypatch, tmp_path, {})

    assert module.main([]) == 1
    err = capsys.readouterr().err
    assert "not grandfathered" in err
    assert "src/ouroboros/new.py" in err


def test_module_exactly_at_cap_passes(module, monkeypatch, tmp_path):
    """The cap is inclusive; an off-by-one here would reject a legal module."""
    _write(tmp_path, "src/ouroboros/edge.py", module.SOFT_CAP)
    _isolate(module, monkeypatch, tmp_path, {})

    assert module.main([]) == 0


def test_grandfathered_growth_fails(module, monkeypatch, tmp_path, capsys):
    _write(tmp_path, "src/ouroboros/god.py", 5001)
    _isolate(module, monkeypatch, tmp_path, {"src/ouroboros/god.py": 5000})

    assert module.main([]) == 1
    err = capsys.readouterr().err
    assert "grew" in err
    assert "budget 5000 (+1)" in err


def test_grandfathered_at_budget_passes(module, monkeypatch, tmp_path):
    _write(tmp_path, "src/ouroboros/god.py", 5000)
    _isolate(module, monkeypatch, tmp_path, {"src/ouroboros/god.py": 5000})

    assert module.main([]) == 0


def test_shrinkage_within_slack_passes(module, monkeypatch, tmp_path):
    """Routine edits must not churn the table."""
    _write(tmp_path, "src/ouroboros/god.py", 5000 - module.RESEED_SLACK)
    _isolate(module, monkeypatch, tmp_path, {"src/ouroboros/god.py": 5000})

    assert module.main([]) == 0


def test_real_shrinkage_demands_reseed(module, monkeypatch, tmp_path, capsys):
    """Without this the ratchet is only a static cap: reclaimed headroom would
    stay available for the next contributor to spend."""
    shrunk = 5000 - module.RESEED_SLACK - 1
    _write(tmp_path, "src/ouroboros/god.py", shrunk)
    _isolate(module, monkeypatch, tmp_path, {"src/ouroboros/god.py": 5000})

    assert module.main([]) == 1
    err = capsys.readouterr().err
    assert "shrank" in err
    # The message must be paste-ready, or nobody will re-seed.
    assert f'"src/ouroboros/god.py": {shrunk},' in err


def test_reaching_the_cap_demands_retirement(module, monkeypatch, tmp_path, capsys):
    """At or below the cap the entry is dead weight and must be deleted, not
    re-seeded -- otherwise a retired module could be grandfathered again."""
    _write(tmp_path, "src/ouroboros/god.py", module.SOFT_CAP)
    _isolate(module, monkeypatch, tmp_path, {"src/ouroboros/god.py": 5000})

    assert module.main([]) == 1
    err = capsys.readouterr().err
    assert "Delete these entries" in err
    assert "src/ouroboros/god.py" in err


def test_vanished_entry_fails_loud(module, monkeypatch, tmp_path, capsys):
    """A renamed module must not silently lose its cap."""
    _write(tmp_path, "src/ouroboros/kept.py", 10)
    _isolate(module, monkeypatch, tmp_path, {"src/ouroboros/moved.py": 5000})

    assert module.main([]) == 1
    err = capsys.readouterr().err
    assert "no longer exist" in err
    assert "budget cannot be transferred" in err
    assert "gone: src/ouroboros/moved.py" in err


def test_missing_source_root_fails(module, monkeypatch, tmp_path, capsys):
    """An empty or wrong checkout must not report a green check."""
    _isolate(module, monkeypatch, tmp_path, {})

    assert module.main([]) == 1
    assert "does not exist" in capsys.readouterr().err


def test_excluded_generated_module_is_ignored(module, monkeypatch, tmp_path):
    _write(tmp_path, "src/ouroboros/_version.py", module.SOFT_CAP + 500)
    _isolate(module, monkeypatch, tmp_path, {})

    assert module.main([]) == 0


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


def test_workflow_uses_event_specific_predecessor() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert (
        "BASELINE_REF: ${{ github.event_name == 'pull_request' && "
        "github.event.pull_request.base.sha || github.event.before }}"
    ) in workflow
    assert '--baseline-ref "${BASELINE_REF}"' in workflow
    assert "--baseline-ref origin/main" not in workflow
