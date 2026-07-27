"""Tests for the PR issue-link gate.

The load-bearing case is `test_unedited_pull_request_template_is_rejected`:
`.github/PULL_REQUEST_TEMPLATE.md` mentions #1234, #1256, and #1258 inside
HTML comments, so a naive scan passes an unedited template and the gate
becomes a no-op for exactly the PRs it exists to catch.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "check-pr-issue-link.py"
TEMPLATE = REPO_ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"


def run(body: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--body", body],
        capture_output=True,
        text=True,
    )


def test_unedited_pull_request_template_is_rejected() -> None:
    """The repository's own template must not satisfy the gate."""
    result = run(TEMPLATE.read_text(encoding="utf-8"))

    assert result.returncode == 1, (
        "The unedited PR template passed the issue-link gate. Its HTML comments "
        "contain #N examples; if those count as references, every "
        "template-created PR passes without linking anything."
    )
    assert "no visible issue reference" in result.stderr


def test_template_actually_contains_issue_numbers() -> None:
    """Guard the guard: if the template stops mentioning #N, the test above passes vacuously."""
    import re

    assert re.search(r"#\d+", TEMPLATE.read_text(encoding="utf-8")), (
        "The PR template no longer contains any #N, so the regression test "
        "above would pass for the wrong reason. Point it at a fixture instead."
    )


def test_closing_reference_is_accepted() -> None:
    result = run("## Summary\n\nDoes the thing.\n\nCloses #1777.\n")
    assert result.returncode == 0
    assert "#1777" in result.stdout


def test_plain_reference_is_accepted() -> None:
    """Epic slices reference without closing; the gate wants a trail, not a closure."""
    result = run("## Summary\n\nOne slice.\n\nPart of #1465.\n")
    assert result.returncode == 0
    assert "#1465" in result.stdout


def test_empty_body_is_rejected() -> None:
    assert run("").returncode == 1


def test_body_without_any_reference_is_rejected() -> None:
    result = run("## Summary\n\nRefactors the parser. No issue for this.\n")
    assert result.returncode == 1


def test_reference_only_inside_html_comment_is_rejected() -> None:
    result = run("## Summary\n\nDoes the thing.\n\n<!-- e.g. Fixes #1234 -->\n")
    assert result.returncode == 1


def test_reference_only_inside_fenced_code_is_rejected() -> None:
    body = "## Summary\n\nSee the log:\n\n```\nerror at #4242\n```\n"
    assert run(body).returncode == 1


def test_reference_only_inside_tilde_fence_is_rejected() -> None:
    body = "## Summary\n\n~~~text\nrefs #4242\n~~~\n"
    assert run(body).returncode == 1


def test_reference_only_inside_inline_code_is_rejected() -> None:
    result = run("## Summary\n\nThe literal token `#1234` is parsed verbatim.\n")
    assert result.returncode == 1


def test_url_fragment_is_not_a_reference() -> None:
    body = (
        "## Summary\n\nSee https://github.com/Q00/ouroboros/pull/1700#issuecomment-1 for context.\n"
    )
    assert run(body).returncode == 1


def test_markdown_heading_is_not_a_reference() -> None:
    assert run("# Title\n\n## Summary\n\n### 3. Detail\n").returncode == 1


def test_cross_repository_reference_is_not_counted() -> None:
    """`owner/repo#12` points elsewhere; this gate is about local traceability."""
    assert run("## Summary\n\nMirrors astral-sh/ruff#12345.\n").returncode == 1


def test_reference_outside_a_fence_still_counts() -> None:
    body = "## Summary\n\nCloses #1777.\n\n```\nunrelated #4242\n```\n"
    result = run(body)
    assert result.returncode == 0
    assert "#1777" in result.stdout
    assert "#4242" not in result.stdout


def test_body_file_input_matches_literal_input(tmp_path: Path) -> None:
    body_file = tmp_path / "body.md"
    body_file.write_text("Refs #1681.\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--body-file", str(body_file)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "#1681" in result.stdout
