"""Self-tests for ``scripts/check-auto-boundary.py``.

The guard's value is proportional to its precision: it must catch real
domain-keyword leaks (including realistic Python identifier forms such
as ``GitHubClient`` and ``github_client``) AND must not false-positive
on benign code. It must also fail loud when a load-bearing anchor file
disappears, so a refactor cannot silently strip enforcement coverage.
Both directions are exercised here.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "check-auto-boundary.py"


def _load_module():
    """Load the hyphenated script as a module so we can call ``main()``
    directly with custom REPO_ROOT / configuration."""
    spec = importlib.util.spec_from_file_location("check_auto_boundary", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _make_anchor_layout(repo: Path, anchors: tuple[str, ...]) -> None:
    """Create empty placeholders for every anchor path so the
    fail-loud-on-missing branch doesn't fire in unrelated tests."""
    for rel in anchors:
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text("# placeholder\n")


def _isolate(
    module,
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
    *,
    scan_dirs: tuple[str, ...] = ("src/ouroboros/auto",),
    scan_extra_files: tuple[str, ...] = ("src/ouroboros/cli/commands/auto.py",),
    anchor_files: tuple[str, ...] | None = None,
) -> None:
    """Point the module at a fake repo with controlled scan/anchor sets."""
    if anchor_files is None:
        anchor_files = scan_extra_files
    monkeypatch.setattr(module, "REPO_ROOT", repo)
    monkeypatch.setattr(module, "SCAN_DIRS", scan_dirs)
    monkeypatch.setattr(module, "SCAN_EXTRA_FILES", scan_extra_files)
    monkeypatch.setattr(module, "ANCHOR_FILES", anchor_files)


def test_clean_repo_passes_via_subprocess() -> None:
    """The current `ooo auto` source must pass the guard.

    This is the runtime invariant the guard exists to protect: at any
    point in main, every scanned file is free of forbidden keywords.
    """
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"guard failed on a presumed-clean main:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    assert "OK" in result.stdout


def test_offending_file_is_caught(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A synthetic file containing a forbidden keyword must be caught."""
    module = _load_module()
    fake_repo = tmp_path / "repo"
    watched_dir = fake_repo / "src" / "ouroboros" / "cli" / "commands"
    watched_dir.mkdir(parents=True)
    offending = watched_dir / "auto.py"
    offending.write_text(
        "def handle(url: str) -> None:\n    if 'github.com' in url:\n        do_pr_things(url)\n"
    )

    _isolate(module, monkeypatch, fake_repo)
    rc = module.main()
    assert rc == 1


def test_allowlist_marker_bypasses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A line carrying the allowlist marker is not flagged."""
    module = _load_module()
    fake_repo = tmp_path / "repo"
    watched_dir = fake_repo / "src" / "ouroboros" / "cli" / "commands"
    watched_dir.mkdir(parents=True)
    offending = watched_dir / "auto.py"
    offending.write_text(
        "# Routing reuses an unrelated GitHub adapter import. "
        "# domain-keyword-allowed: legacy plumbing\n"
        "x = 1\n"
    )

    _isolate(module, monkeypatch, fake_repo)
    rc = module.main()
    assert rc == 0


def test_missing_anchor_file_fails_loud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If a load-bearing anchor file is missing (e.g. removed in a
    refactor without updating ANCHOR_FILES), the guard MUST fail loud.

    This is the bot-review-flagged silent-failure mode: a hand-maintained
    file list combined with "missing == clean" turns refactors into
    accidental coverage strippers.
    """
    module = _load_module()
    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()

    _isolate(
        module,
        monkeypatch,
        fake_repo,
        scan_dirs=(),  # nothing to discover
        scan_extra_files=(),
        anchor_files=("src/ouroboros/cli/commands/auto.py",),  # does not exist
    )
    rc = module.main()
    assert rc == 1


def test_each_forbidden_pattern_independently_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """For each forbidden pattern, a synthetic offender is caught.

    Meta-test that the pattern list is wired into the scan loop, so
    additions to FORBIDDEN_PATTERNS take effect without wiring code.
    Each sample is chosen to match exactly one pattern so the test
    survives the first-match-wins ordering of ``_scan_file``.
    """
    module = _load_module()

    samples = {
        "github": "host = 'github.com'",
        "pull_request": "if 'pull_request' in payload: ...",
        "pullrequest": "handler = PullRequestHandler()",
        "/pulls/": "uri = '/pulls/42'",
        "/pull/": "uri = '/pull/42'",
        "jira": "issue = 'JIRA-1'",
        "slack": "channel = '#xchan'  # slack",
        "linear": "client = LinearClient()",
    }
    import re as _re

    for i, pattern in enumerate(module.FORBIDDEN_PATTERNS):
        assert pattern in samples, f"add a sample for {pattern!r}"
        safe = _re.sub(r"[^a-zA-Z0-9]", "_", pattern).strip("_")
        fake_repo = tmp_path / f"case-{i}-{safe}"
        watched_dir = fake_repo / "src" / "ouroboros" / "cli" / "commands"
        watched_dir.mkdir(parents=True)
        (watched_dir / "auto.py").write_text(samples[pattern] + "\n")
        _isolate(module, monkeypatch, fake_repo)
        rc = module.main()
        assert rc == 1, f"pattern {pattern!r} not caught for sample"


@pytest.mark.parametrize(
    "snippet,reason",
    [
        ("client = GitHubClient()", "PascalCase identifier"),
        ("from .ghub import github_client", "snake_case identifier"),
        ("from foo import GitHubAdapter", "PascalCase import"),
        ("issue = JiraIssue(id=1)", "Jira PascalCase"),
        ("def notify_slack_user(): pass", "slack snake_case"),
        ("notifier = SlackNotifier()", "Slack PascalCase"),
        ("FOO_GITHUB_BASE = 'x'", "SCREAMING_SNAKE_CASE"),
        # Compressed camelCase / no-underscore forms that the original
        # ``pull_request`` / ``linear.app`` substrings missed.
        ("handler = PullRequestHandler()", "PullRequest PascalCase"),
        ("event_id = pullRequestId", "pullRequest camelCase"),
        ("class PRHandler(PullRequestBase): ...", "PullRequest base class"),
        ("client = LinearClient()", "Linear PascalCase"),
        ("adapter = LinearAdapter()", "Linear adapter PascalCase"),
        ("from foo import linear_client", "linear snake_case"),
    ],
)
def test_identifier_forms_are_caught(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    snippet: str,
    reason: str,
) -> None:
    """Realistic Python identifier forms (camelCase, snake_case,
    PascalCase, SCREAMING_SNAKE_CASE, import-from) must be caught.

    The original word-boundary regex (``\\bgithub\\b`` etc.) silently
    skipped these. The bot review flagged this as a guard bypass; the
    relaxed substring matching closes it.
    """
    module = _load_module()
    fake_repo = tmp_path / "repo"
    watched_dir = fake_repo / "src" / "ouroboros" / "cli" / "commands"
    watched_dir.mkdir(parents=True)
    (watched_dir / "auto.py").write_text(snippet + "\n")
    _isolate(module, monkeypatch, fake_repo)
    rc = module.main()
    assert rc == 1, f"{reason}: {snippet!r} should have been flagged"


def test_auto_discovery_picks_up_new_files_in_auto_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A brand-new file dropped under ``src/ouroboros/auto/`` is scanned
    automatically -- no manual list update required.

    This addresses the bot review design note that a hand-maintained
    file list weakens the enforcement contract: a contributor adding a
    new domain-tainted module would otherwise be invisible to the
    guard until someone remembered to extend the list.
    """
    module = _load_module()
    fake_repo = tmp_path / "repo"
    auto_dir = fake_repo / "src" / "ouroboros" / "auto"
    auto_dir.mkdir(parents=True)
    # Anchor placeholder so the missing-anchor branch doesn't fire.
    (fake_repo / "src" / "ouroboros" / "cli" / "commands").mkdir(parents=True)
    (fake_repo / "src" / "ouroboros" / "cli" / "commands" / "auto.py").write_text("# clean\n")
    # New file with a forbidden keyword embedded in an identifier.
    (auto_dir / "new_module.py").write_text("class GitHubAdapter:\n    pass\n")

    _isolate(module, monkeypatch, fake_repo)
    rc = module.main()
    assert rc == 1, "auto-discovery missed a new file under src/ouroboros/auto/"


def test_clean_auto_dir_with_anchors_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean auto/ dir containing several files plus all anchors
    passes the guard."""
    module = _load_module()
    fake_repo = tmp_path / "repo"
    auto_dir = fake_repo / "src" / "ouroboros" / "auto"
    auto_dir.mkdir(parents=True)
    (auto_dir / "pipeline.py").write_text("def run() -> None:\n    pass\n")
    (auto_dir / "extra_module.py").write_text("VALUE = 1\n")
    cli_dir = fake_repo / "src" / "ouroboros" / "cli" / "commands"
    cli_dir.mkdir(parents=True)
    (cli_dir / "auto.py").write_text("# entrypoint\n")

    _isolate(
        module,
        monkeypatch,
        fake_repo,
        anchor_files=(
            "src/ouroboros/cli/commands/auto.py",
            "src/ouroboros/auto/pipeline.py",
        ),
    )
    rc = module.main()
    assert rc == 0


def test_keyword_in_docstring_of_watched_file_is_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Docstrings and comments are part of the watched surface: a
    docstring example referencing a domain workflow should be caught
    so that contributors are nudged to put it in a plugin doc instead."""
    module = _load_module()
    fake_repo = tmp_path / "repo"
    watched_dir = fake_repo / "src" / "ouroboros" / "cli" / "commands"
    watched_dir.mkdir(parents=True)
    (watched_dir / "auto.py").write_text(
        '"""Helpers.\n\n    Example: ``ooo auto --target slack-bot``.\n"""\n'
    )
    _isolate(module, monkeypatch, fake_repo)
    rc = module.main()
    assert rc == 1


def test_anchor_file_present_but_outside_scan_dir_still_anchored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An anchor file living outside the SCAN_DIRS roots is still
    enforced as must-exist (it represents a load-bearing surface even
    if discovery wouldn't have picked it up)."""
    module = _load_module()
    fake_repo = tmp_path / "repo"
    auto_dir = fake_repo / "src" / "ouroboros" / "auto"
    auto_dir.mkdir(parents=True)
    (auto_dir / "pipeline.py").write_text("# clean\n")
    # cli/commands/auto.py is intentionally NOT created
    _isolate(
        module,
        monkeypatch,
        fake_repo,
        anchor_files=(
            "src/ouroboros/cli/commands/auto.py",
            "src/ouroboros/auto/pipeline.py",
        ),
    )
    rc = module.main()
    assert rc == 1


def test_scan_extra_files_are_scanned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A SCAN_EXTRA_FILES entry that lives outside SCAN_DIRS is still
    scanned for forbidden keywords (regression guard for the union
    discovery logic)."""
    module = _load_module()
    fake_repo = tmp_path / "repo"
    cli_dir = fake_repo / "src" / "ouroboros" / "cli" / "commands"
    cli_dir.mkdir(parents=True)
    (cli_dir / "auto.py").write_text("import GitHubClient  # noqa\n")
    # Empty auto package so SCAN_DIRS contributes nothing
    auto_dir = fake_repo / "src" / "ouroboros" / "auto"
    auto_dir.mkdir(parents=True)
    _isolate(module, monkeypatch, fake_repo)
    rc = module.main()
    assert rc == 1
