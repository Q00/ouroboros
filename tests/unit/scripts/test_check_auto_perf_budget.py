"""Self-tests for ``scripts/check-auto-perf-budget.py``.

This guard enforces RFC #1256 §I5: every PR touching
``src/ouroboros/auto/`` must include a filled R-run comparison section
in the PR body. Two design properties make the test surface
load-bearing:

1. **Applicability detection** is what decides whether the gate runs
   at all. Mis-classifying an auto/-touching PR as "not applicable"
   is a silent bypass.
2. **Fail-closed on indeterminate inputs** is the bot-review blocker:
   if ``git diff`` cannot enumerate changed files, the script must
   refuse to return 0. The earlier behavior (silent empty list →
   "not applicable" → exit 0) was the exact bypass class flagged.

Both properties are pinned here alongside the section-presence
parser so a future refactor that loosens either contract trips
these tests.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "check-auto-perf-budget.py"


def _load_module():
    """Load the hyphenated script as a module so we can call ``main()``
    and the inner helpers directly without spawning a subprocess."""
    spec = importlib.util.spec_from_file_location("check_auto_perf_budget", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# --- _section_present ---------------------------------------------------


def test_section_present_returns_true_for_filled_table() -> None:
    """A real PR body with all three required metric rows filled
    passes the presence check."""
    module = _load_module()
    body = (
        "## Summary\nstuff\n"
        "## R-run comparison\n\n"
        "| Metric | Baseline | This PR | Ratio |\n"
        "|---|---|---|---|\n"
        "| Rounds completed in 600 s | 100 | 95 | 0.95 |\n"
        "| Per-round wall-clock (s/round) | 6.0 | 6.3 | 1.05 |\n"
        "| Terminal reason | ready | ready | n/a |\n"
    )
    assert module._section_present(body) is True


def test_section_present_returns_false_when_header_missing() -> None:
    module = _load_module()
    assert module._section_present("## Summary\nno table here") is False


def test_section_present_returns_false_for_blank_template_table() -> None:
    """Empty cells (the default template) MUST not pass — that was the
    explicit author-skipped-the-requirement signal we're guarding."""
    module = _load_module()
    body = (
        "## R-run comparison\n\n"
        "| Metric | Baseline | This PR | Ratio |\n"
        "|---|---|---|---|\n"
        "| Rounds completed in 600 s |  |  |  |\n"
        "| Per-round wall-clock (s/round) |  |  |  |\n"
        "| Terminal reason |  |  |  |\n"
    )
    assert module._section_present(body) is False


def test_section_present_returns_false_when_metric_row_missing() -> None:
    """Header present but a required metric row absent → not compliant.

    Authors deleting a row to dodge filling it must not pass the gate.
    """
    module = _load_module()
    body = (
        "## R-run comparison\n\n"
        "| Metric | Baseline | This PR | Ratio |\n"
        "|---|---|---|---|\n"
        "| Rounds completed in 600 s | 100 | 95 | 0.95 |\n"
        # "Per-round wall-clock" row removed
        "| Terminal reason | ready | ready | n/a |\n"
    )
    assert module._section_present(body) is False


def test_section_present_returns_false_for_partially_filled_rows() -> None:
    """Bot-review blocker (commit 074e24bd → req_1779886636_125): a row
    where only ONE of the three comparison cells is non-empty (e.g.
    ``Baseline=TBD`` with PR and Ratio blank) MUST be rejected.

    The prior ``any(cells)`` check accepted this shape, which let an
    applicable PR pass the gate with a placeholder in one column and
    no actual side-by-side measurement — the exact §I5 process
    bypass the gate exists to prevent.
    """
    module = _load_module()
    body = (
        "## R-run comparison\n\n"
        "| Metric | Baseline | This PR | Ratio |\n"
        "|---|---|---|---|\n"
        "| Rounds completed in 600 s | TBD |  |  |\n"
        "| Per-round wall-clock (s/round) | TBD |  |  |\n"
        "| Terminal reason | TBD |  |  |\n"
    )
    assert module._section_present(body) is False


def test_section_present_returns_false_when_only_ratio_missing() -> None:
    """Two filled, one blank still fails. The contract is ALL three
    comparison cells populated — the Ratio column is what makes the
    measurement comparable, so it cannot be left empty even when
    Baseline and This PR are both filled."""
    module = _load_module()
    body = (
        "## R-run comparison\n\n"
        "| Metric | Baseline | This PR | Ratio |\n"
        "|---|---|---|---|\n"
        "| Rounds completed in 600 s | 100 | 95 |  |\n"
        "| Per-round wall-clock (s/round) | 6.0 | 6.3 |  |\n"
        "| Terminal reason | ready | ready |  |\n"
    )
    assert module._section_present(body) is False


def test_section_present_accepts_explicit_na_in_every_cell() -> None:
    """Authors who genuinely have no comparison (e.g. substrate-only
    PRs) must mark every cell explicitly. Filling all three with
    ``N/A`` / ``n/a`` is auditable and accepted; one filled cell with
    two blanks is not."""
    module = _load_module()
    body = (
        "## R-run comparison\n\n"
        "| Metric | Baseline | This PR | Ratio |\n"
        "|---|---|---|---|\n"
        "| Rounds completed in 600 s | N/A | N/A | n/a |\n"
        "| Per-round wall-clock (s/round) | N/A | N/A | n/a |\n"
        "| Terminal reason | N/A | N/A | n/a |\n"
    )
    assert module._section_present(body) is True


def test_section_present_returns_false_when_one_row_partial_others_filled() -> None:
    """Even a single partially-filled row inside an otherwise complete
    table fails the gate — the contract is per-row, not aggregate."""
    module = _load_module()
    body = (
        "## R-run comparison\n\n"
        "| Metric | Baseline | This PR | Ratio |\n"
        "|---|---|---|---|\n"
        "| Rounds completed in 600 s | 100 | 95 | 0.95 |\n"
        "| Per-round wall-clock (s/round) | 6.0 |  |  |\n"  # partial
        "| Terminal reason | ready | ready | n/a |\n"
    )
    assert module._section_present(body) is False


# --- _changed_paths_from_event fail-closed contract ---------------------


def test_changed_paths_raises_when_git_diff_fails() -> None:
    """Bot-review blocker #2: a git-diff failure MUST raise the
    ChangedPathsUnavailable sentinel so the caller can fail closed.
    The prior behavior (silent empty list) silently disabled the gate
    on every checkout/ref-layout failure."""
    module = _load_module()

    fake_event = {"pull_request": {"base": {"ref": "main"}}}
    with patch.object(
        module.subprocess,
        "run",
        side_effect=subprocess.CalledProcessError(128, ["git", "diff"]),
    ):
        with pytest.raises(module.ChangedPathsUnavailable):
            module._changed_paths_from_event(fake_event)


def test_changed_paths_raises_when_git_binary_missing() -> None:
    """The OSError path (``git`` not installed, broken PATH) is also
    indeterminate and must raise rather than return ``[]``."""
    module = _load_module()
    fake_event = {"pull_request": {"base": {"ref": "main"}}}
    with patch.object(module.subprocess, "run", side_effect=OSError("no git binary")):
        with pytest.raises(module.ChangedPathsUnavailable):
            module._changed_paths_from_event(fake_event)


def test_changed_paths_returns_split_lines_on_success() -> None:
    """Happy path: the helper returns one entry per changed file with
    blank lines stripped."""
    module = _load_module()
    fake_event = {"pull_request": {"base": {"ref": "main"}}}
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout="src/ouroboros/auto/pipeline.py\nREADME.md\n\n",
        stderr="",
    )
    with patch.object(module.subprocess, "run", return_value=completed):
        result = module._changed_paths_from_event(fake_event)
    assert result == ["src/ouroboros/auto/pipeline.py", "README.md"]


# --- main() applicability + exit-code matrix ----------------------------


def _write_event(tmp_path: Path, *, body: str) -> Path:
    event = {"pull_request": {"body": body, "base": {"ref": "main"}}}
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event), encoding="utf-8")
    return event_path


def test_main_returns_zero_for_pr_not_touching_auto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PR with only non-auto changes is not applicable → exit 0."""
    module = _load_module()
    event_path = _write_event(tmp_path, body="## Summary\ndocs only")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setattr(module.sys, "argv", ["check-auto-perf-budget.py"])

    with patch.object(module, "_changed_paths_from_event", return_value=["README.md"]):
        assert module.main() == 0


def test_main_returns_zero_for_auto_pr_with_filled_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An applicable PR with a filled R-run section passes."""
    module = _load_module()
    body = (
        "## Summary\n"
        "## R-run comparison\n\n"
        "| Metric | Baseline | This PR | Ratio |\n"
        "|---|---|---|---|\n"
        "| Rounds completed in 600 s | 100 | 95 | 0.95 |\n"
        "| Per-round wall-clock (s/round) | 6.0 | 6.3 | 1.05 |\n"
        "| Terminal reason | ready | ready | n/a |\n"
    )
    event_path = _write_event(tmp_path, body=body)
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setattr(module.sys, "argv", ["check-auto-perf-budget.py"])

    with patch.object(
        module, "_changed_paths_from_event", return_value=["src/ouroboros/auto/pipeline.py"]
    ):
        assert module.main() == 0


def test_main_returns_one_for_auto_pr_without_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headline contract: an applicable PR missing the section
    fails the gate with exit 1."""
    module = _load_module()
    event_path = _write_event(tmp_path, body="## Summary\nno R-run table")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setattr(module.sys, "argv", ["check-auto-perf-budget.py"])

    with patch.object(
        module, "_changed_paths_from_event", return_value=["src/ouroboros/auto/pipeline.py"]
    ):
        assert module.main() == 1


def test_main_fails_closed_when_path_discovery_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bot-review blocker pin: an indeterminate changed-paths result
    must exit 2, NOT 0. This is the regression class the prior
    fail-open behavior shipped — ``git diff`` failing silently
    disabled the gate."""
    module = _load_module()
    event_path = _write_event(tmp_path, body="anything")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setattr(module.sys, "argv", ["check-auto-perf-budget.py"])

    with patch.object(
        module,
        "_changed_paths_from_event",
        side_effect=module.ChangedPathsUnavailable("git not reachable"),
    ):
        assert module.main() == 2


def test_main_returns_zero_when_no_event_and_no_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local-invocation guard: no GITHUB_EVENT_PATH and no --body is
    not an error — there's nothing to evaluate, so the script
    no-ops with exit 0 and a stderr note."""
    module = _load_module()
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    monkeypatch.setattr(module.sys, "argv", ["check-auto-perf-budget.py"])
    assert module.main() == 0


def test_main_returns_two_on_malformed_event_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt event payload is an indeterminate input → exit 2."""
    module = _load_module()
    event_path = tmp_path / "event.json"
    event_path.write_text("{ not json", encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setattr(module.sys, "argv", ["check-auto-perf-budget.py"])
    assert module.main() == 2


# --- --body local dry-run mode ------------------------------------------


def test_main_body_mode_fails_closed_on_git_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The --body local dry-run path also fails closed on git error.

    Otherwise developers using the script locally would see "OK" even
    when their checkout cannot enumerate changed files, masking the
    same indeterminate-input bypass class as the CI path.
    """
    module = _load_module()
    body_file = tmp_path / "body.md"
    body_file.write_text("## Summary\nlocal", encoding="utf-8")
    monkeypatch.setattr(module.sys, "argv", ["check-auto-perf-budget.py", "--body", str(body_file)])

    with patch.object(
        module.subprocess,
        "run",
        side_effect=subprocess.CalledProcessError(1, ["git", "diff"]),
    ):
        assert module.main() == 2
