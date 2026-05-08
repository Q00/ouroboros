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

    # Map each declared FORBIDDEN_PATTERNS entry to a single-pattern
    # sample. The samples are chosen so they match exactly one regex,
    # surviving the first-match-wins ordering of ``_scan_file``.
    samples_by_index = [
        "host = 'github.com'",  # github
        "if 'pull_request' in payload: ...",  # pull_request (snake)
        "handler = PullRequestHandler()",  # pullrequest (compressed)
        "uri = '/pulls/42'",  # /pulls?/
        "issue = 'JIRA-1'",  # jira
        "channel = '#xchan'  # slack",  # slack (only in trailing real-Python comment)
        "client = LinearClient()",  # linear (PascalCase composition)
    ]

    assert len(samples_by_index) == len(module.FORBIDDEN_PATTERNS), (
        "samples_by_index must align 1:1 with FORBIDDEN_PATTERNS; "
        f"got {len(samples_by_index)} samples for "
        f"{len(module.FORBIDDEN_PATTERNS)} patterns"
    )

    import re as _re

    for i, (pattern, sample) in enumerate(
        zip(module.FORBIDDEN_PATTERNS, samples_by_index, strict=True)
    ):
        safe = _re.sub(r"[^a-zA-Z0-9]", "_", pattern)[:40].strip("_") or f"p{i}"
        fake_repo = tmp_path / f"case-{i}-{safe}"
        watched_dir = fake_repo / "src" / "ouroboros" / "cli" / "commands"
        watched_dir.mkdir(parents=True)
        (watched_dir / "auto.py").write_text(sample + "\n")
        _isolate(module, monkeypatch, fake_repo)
        rc = module.main()
        assert rc == 1, f"pattern {pattern!r} not caught for sample {sample!r}"


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
        # camelCase-compound forms where the keyword sits in the middle
        # of an identifier preceded by a lowercase letter (the bypass
        # class flagged by bot review on commit c2b6943).
        ("issue = openGithubIssue()", "openGithubIssue (Github mid-camelCase)"),
        ("url = makePullRequestUrl()", "makePullRequestUrl (PullRequest mid-camelCase)"),
        ("sendToSlack(msg)", "sendToSlack (Slack mid-camelCase)"),
        ("issue = openJiraIssue()", "openJiraIssue (Jira mid-camelCase)"),
        ("hook = notifyLinearAdapter()", "notifyLinearAdapter (Linear mid-camelCase)"),
        ("client = maybeLinearClient()", "maybeLinearClient (Linear mid-camelCase)"),
        ("notifier = registerSlackNotifier()", "registerSlackNotifier (Slack mid-camelCase)"),
        ("payload = buildGithubPayload()", "buildGithubPayload (Github mid-camelCase)"),
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


def test_symlink_directory_resolving_outside_repo_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlinked directory that resolves OUTSIDE the repo must not
    pull files into the scan.

    The boundary the guard cares about is "does this file still resolve
    inside the repo?", not "is this path a symlink?". A symlink farm
    such as ``src/ouroboros/auto/external -> /home/user/vendor/sdk``
    would otherwise make ``Path.rglob`` descend into the link target
    and produce false-positive failures on vendored code we
    deliberately did not police.
    """
    module = _load_module()
    fake_repo = tmp_path / "repo"
    auto_dir = fake_repo / "src" / "ouroboros" / "auto"
    auto_dir.mkdir(parents=True)
    (auto_dir / "clean.py").write_text("# clean\n")
    cli_dir = fake_repo / "src" / "ouroboros" / "cli" / "commands"
    cli_dir.mkdir(parents=True)
    (cli_dir / "auto.py").write_text("# clean\n")

    # External vendored tree containing a forbidden token. This is OUTSIDE
    # the scan boundary by intent.
    external = tmp_path / "external_vendor"
    external.mkdir()
    (external / "github_helper.py").write_text("class GitHubAdapter:\n    pass\n")

    # Build the symlink farm: ``auto/external -> external_vendor``.
    (auto_dir / "external").symlink_to(external, target_is_directory=True)

    _isolate(module, monkeypatch, fake_repo)
    rc = module.main()
    assert rc == 0, "scan should skip files whose resolved path is outside REPO_ROOT"


def test_symlinked_python_file_resolving_outside_repo_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlinked ``*.py`` file whose target lives outside the repo is
    skipped — same out-of-repo boundary as the directory-symlink case
    above, applied to a single-file symlink such as
    ``auto/legacy.py -> /usr/local/vendor/sdk.py``."""
    module = _load_module()
    fake_repo = tmp_path / "repo"
    auto_dir = fake_repo / "src" / "ouroboros" / "auto"
    auto_dir.mkdir(parents=True)
    (auto_dir / "clean.py").write_text("# clean\n")
    cli_dir = fake_repo / "src" / "ouroboros" / "cli" / "commands"
    cli_dir.mkdir(parents=True)
    (cli_dir / "auto.py").write_text("# clean\n")

    external = tmp_path / "vendor.py"
    external.write_text("class GitHubVendor:\n    pass\n")
    (auto_dir / "legacy.py").symlink_to(external)

    _isolate(module, monkeypatch, fake_repo)
    rc = module.main()
    assert rc == 0, "single-file symlink resolving outside REPO_ROOT must not be scanned"


def test_in_repo_symlink_clean_target_still_scanned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An in-repo symlink (target still resolves inside the repo) must
    remain in the scan set.

    Pins the contract boundary the bot review on PR #797 flagged: the
    earlier ``is_symlink()``-based filter turned every in-repo symlink
    into an unscanned blind spot, even though Python imports the
    symlinked file normally and a contributor could quietly stash
    domain-specific code behind such a link. The fix anchors the
    exclusion on "resolved target escapes REPO_ROOT", not on path
    type.
    """
    module = _load_module()
    fake_repo = tmp_path / "repo"
    auto_dir = fake_repo / "src" / "ouroboros" / "auto"
    auto_dir.mkdir(parents=True)
    cli_dir = fake_repo / "src" / "ouroboros" / "cli" / "commands"
    cli_dir.mkdir(parents=True)
    (cli_dir / "auto.py").write_text("# clean\n")

    # In-repo destination — the scan target file lives elsewhere in the
    # repo, but is reachable through the auto/ symlink.
    shared = fake_repo / "src" / "ouroboros" / "shared"
    shared.mkdir(parents=True)
    (shared / "legacy.py").write_text("# clean\n")
    (auto_dir / "legacy.py").symlink_to(shared / "legacy.py")

    _isolate(module, monkeypatch, fake_repo)
    rc = module.main()
    assert rc == 0, "in-repo symlink to clean file must not regress the scan"


def test_scan_extra_file_symlink_to_outside_repo_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for PR #797 review: the repo-boundary check must apply
    to ``SCAN_EXTRA_FILES`` too, not only to ``SCAN_DIRS``.

    Reproduces the exact failure the bot flagged: ``auto.py`` (a
    SCAN_EXTRA_FILES entry) is a symlink to a file containing
    ``GitHubVendor`` outside the repo. The earlier patch only applied
    the resolve-and-check rule inside the SCAN_DIRS loop, so the extra-
    file slot still resolved and scanned the external file
    unconditionally and ``main()`` returned 1.
    """
    module = _load_module()
    fake_repo = tmp_path / "repo"
    auto_dir = fake_repo / "src" / "ouroboros" / "auto"
    auto_dir.mkdir(parents=True)
    (auto_dir / "clean.py").write_text("# clean\n")
    cli_dir = fake_repo / "src" / "ouroboros" / "cli" / "commands"
    cli_dir.mkdir(parents=True)

    # External file outside the repo, carrying a forbidden token.
    external = tmp_path / "external_auto.py"
    external.write_text("class GitHubVendor:\n    pass\n")
    # The SCAN_EXTRA_FILES entry is a symlink to that external file.
    (cli_dir / "auto.py").symlink_to(external)

    _isolate(module, monkeypatch, fake_repo)
    rc = module.main()
    assert rc == 0, "SCAN_EXTRA_FILES entry resolving outside REPO_ROOT must not be scanned"


def test_scan_extra_file_in_repo_symlink_is_still_scanned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Conjugate of the above: an in-repo symlink at the
    SCAN_EXTRA_FILES slot must remain scanned. ``auto.py`` →
    ``shared/auto.py`` (same repo) carrying a forbidden token must be
    caught — the boundary is REPO_ROOT, not symlink-or-not."""
    module = _load_module()
    fake_repo = tmp_path / "repo"
    auto_dir = fake_repo / "src" / "ouroboros" / "auto"
    auto_dir.mkdir(parents=True)
    (auto_dir / "clean.py").write_text("# clean\n")
    cli_dir = fake_repo / "src" / "ouroboros" / "cli" / "commands"
    cli_dir.mkdir(parents=True)

    shared = fake_repo / "src" / "ouroboros" / "shared"
    shared.mkdir(parents=True)
    (shared / "auto.py").write_text("class GitHubAdapter:\n    pass\n")
    # In-repo symlink at the SCAN_EXTRA_FILES slot.
    (cli_dir / "auto.py").symlink_to(shared / "auto.py")

    _isolate(module, monkeypatch, fake_repo)
    rc = module.main()
    assert rc == 1, (
        "in-repo symlink at SCAN_EXTRA_FILES carrying a forbidden token must still be caught"
    )


def test_in_repo_symlink_dirty_target_is_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An in-repo symlink whose target carries a forbidden keyword MUST
    be caught — the resolved path is in-repo, so it is part of the
    boundary contract.

    This is the exact regression class the bot review on PR #797
    described: ``auto/legacy.py -> ../shared/has_github.py`` would have
    been silently skipped under the prior is-symlink filter, allowing a
    contributor to launder domain code behind a symlink.
    """
    module = _load_module()
    fake_repo = tmp_path / "repo"
    auto_dir = fake_repo / "src" / "ouroboros" / "auto"
    auto_dir.mkdir(parents=True)
    cli_dir = fake_repo / "src" / "ouroboros" / "cli" / "commands"
    cli_dir.mkdir(parents=True)
    (cli_dir / "auto.py").write_text("# clean\n")

    shared = fake_repo / "src" / "ouroboros" / "shared"
    shared.mkdir(parents=True)
    (shared / "leaked.py").write_text("class GitHubAdapter:\n    pass\n")
    # In-repo symlink that lives under SCAN_DIRS — Python would import
    # this file normally, so the guard MUST police it.
    (auto_dir / "leaked.py").symlink_to(shared / "leaked.py")

    _isolate(module, monkeypatch, fake_repo)
    rc = module.main()
    assert rc == 1, (
        "an in-repo symlink whose target contains a forbidden token must be "
        "caught — the boundary is REPO_ROOT, not symlink-or-not"
    )


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


def test_allowlist_marker_inside_string_literal_does_not_bypass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The allowlist marker is only honored inside a real comment.

    A forbidden keyword on a line whose only marker is embedded in a
    string literal -- e.g. ``MSG = "domain-keyword-allowed: github"``
    -- must still be flagged. Substring-only marker detection (the
    pre-fix behavior) was a real bypass that defeated the guard.
    """
    module = _load_module()
    fake_repo = tmp_path / "repo"
    watched_dir = fake_repo / "src" / "ouroboros" / "cli" / "commands"
    watched_dir.mkdir(parents=True)
    (watched_dir / "auto.py").write_text('MSG = "domain-keyword-allowed: docs github"\n')

    _isolate(module, monkeypatch, fake_repo)
    rc = module.main()
    assert rc == 1


def test_allowlist_marker_in_real_comment_still_bypasses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The allowlist marker still works when it appears in a genuine
    Python comment (trailing ``#`` form) -- regression guard for the
    string-literal fix above."""
    module = _load_module()
    fake_repo = tmp_path / "repo"
    watched_dir = fake_repo / "src" / "ouroboros" / "cli" / "commands"
    watched_dir.mkdir(parents=True)
    (watched_dir / "auto.py").write_text(
        "import legacy_github  # domain-keyword-allowed: legacy plumbing\n"
    )

    _isolate(module, monkeypatch, fake_repo)
    rc = module.main()
    assert rc == 0


def test_allowlist_marker_split_string_and_comment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A line where the marker is in a string AND a separate (non-marker)
    real comment exists must NOT bypass. Only a marker in a real comment
    counts."""
    module = _load_module()
    fake_repo = tmp_path / "repo"
    watched_dir = fake_repo / "src" / "ouroboros" / "cli" / "commands"
    watched_dir.mkdir(parents=True)
    (watched_dir / "auto.py").write_text(
        'MSG = "domain-keyword-allowed: github"  # actual comment without marker\n'
    )

    _isolate(module, monkeypatch, fake_repo)
    rc = module.main()
    assert rc == 1


@pytest.mark.parametrize(
    "snippet,reason",
    [
        ("def linear_time_search(): pass", "linear_time identifier"),
        ("# linear pipeline scan", "'linear pipeline' docstring fragment"),
        ("def linearize(matrix): pass", "linearize identifier"),
        ("STEP_LINEAR = 1", "SCREAMING_SNAKE LINEAR_ ... only"),
        ("complexity = 'linear'", "string literal 'linear'"),
        ("# perform a linear search across the timeline", "linear search comment"),
        # Embedded-substring guards: `linear` inside a larger word must
        # not trigger the linear-the-SaaS pattern.
        ("pts = collinearPoints()", "collinearPoints (linear inside)"),
        ("form = bilinearForm()", "bilinearForm (linear inside)"),
        ("ad = nonlinearAdapter()", "nonlinearAdapter (linear inside)"),
        ("from x import nonlinear_adapter", "nonlinear_adapter (snake embed)"),
        # Word-boundary guards for the other keywords too: embedded
        # substrings should NOT trigger.
        ("var = mygithub_thing", "mygithub_thing (embedded github)"),
        ("data = myjira_data", "myjira_data (embedded jira)"),
        ("data = myslack_data", "myslack_data (embedded slack)"),
        ("data = mypull_request_data", "mypull_request_data (embedded pull_request)"),
        ("var = mypullrequest_handler", "mypullrequest (embedded pullrequest)"),
        # Generic English camelCase / PascalCase compounds that begin
        # with ``linear`` but are not Linear-the-product integrations
        # must NOT trigger. The follow-up bot review on commit 293d87c7
        # flagged that the previous ``(?i:linear)[A-Z]`` arm rejected
        # this whole class as if it referenced Linear-the-SaaS.
        ("dt = linearTime", "linearTime (generic camelCase)"),
        ("class LinearTime: ...", "LinearTime (generic PascalCase)"),
        ("op = linearTransform()", "linearTransform (generic camelCase)"),
        ("class LinearTransform: ...", "LinearTransform (generic PascalCase)"),
        ("op = LinearOperator()", "LinearOperator (generic PascalCase)"),
        ("class LinearRegressor: ...", "LinearRegressor (generic PascalCase)"),
        ("y = linearScan(arr)", "linearScan (generic camelCase)"),
        ("var = LinearProgrammingSolver()", "LinearProgrammingSolver (generic compound)"),
    ],
)
def test_linear_word_is_not_a_false_positive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    snippet: str,
    reason: str,
) -> None:
    """The bare English word ``linear`` must NOT trigger the guard.

    Bot review on iteration 3 flagged that a naive substring ``linear``
    rejected legitimate identifiers (``linear_time``, ``linearize``)
    and docstrings (``"linear pipeline"``). The tightened pattern only
    matches Linear-the-SaaS forms (URL, PascalCase, integration suffix).
    """
    module = _load_module()
    fake_repo = tmp_path / "repo"
    watched_dir = fake_repo / "src" / "ouroboros" / "cli" / "commands"
    watched_dir.mkdir(parents=True)
    (watched_dir / "auto.py").write_text(snippet + "\n")
    _isolate(module, monkeypatch, fake_repo)
    rc = module.main()
    assert rc == 0, f"{reason}: {snippet!r} should NOT have been flagged"


@pytest.mark.parametrize(
    "snippet,reason",
    [
        ("client = LinearClient()", "PascalCase composition"),
        ("adapter = LinearAdapter()", "PascalCase composition (Adapter)"),
        ("auth = LinearAuth()", "PascalCase composition (Auth)"),
        ("url = 'https://linear.app/team/x'", "linear.app URL"),
        ("url = 'https://linear.com/...'", "linear.com URL (case-insensitive)"),
        ("url = 'https://Linear.App/...'", "Linear.App URL (case-insensitive)"),
        ("from x import linear_client", "snake_case integration suffix (client)"),
        ("from x import linear_webhook", "snake_case integration suffix (webhook)"),
        ("hook = linear_notifier()", "snake_case integration suffix (notifier)"),
    ],
)
def test_linear_saas_forms_are_caught(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    snippet: str,
    reason: str,
) -> None:
    """Linear-the-SaaS identifier and URL forms must be caught.

    Counterpart to ``test_linear_word_is_not_a_false_positive``: ensures
    the tightened regex still catches what we want, not just rejects
    what we don't.
    """
    module = _load_module()
    fake_repo = tmp_path / "repo"
    watched_dir = fake_repo / "src" / "ouroboros" / "cli" / "commands"
    watched_dir.mkdir(parents=True)
    (watched_dir / "auto.py").write_text(snippet + "\n")
    _isolate(module, monkeypatch, fake_repo)
    rc = module.main()
    assert rc == 1, f"{reason}: {snippet!r} should have been flagged"


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
