"""Tests for the PR issue-link gate.

The load-bearing case is `test_unedited_pull_request_template_is_rejected`:
`.github/PULL_REQUEST_TEMPLATE.md` mentions #1234, #1256, and #1258 inside
HTML comments, so a naive scan passes an unedited template and the gate
becomes a no-op for exactly the PRs it exists to catch.

The script reports *candidates* only. Whether a number exists, and whether it
is an issue rather than a pull request, is resolved by the workflow against
the GitHub API -- `#N` is a shared namespace and no amount of text parsing can
settle it.
"""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "check-pr-issue-link.py"
TEMPLATE = REPO_ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"


def run(body: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--body", body],
        capture_output=True,
        text=True,
    )


def references(body: str) -> list[int]:
    """Candidate numbers the script reports, as integers."""
    result = run(body)
    return [int(line) for line in result.stdout.split()]


# ── the regression this gate exists for ──────────────────────────────


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
    assert re.search(r"#\d+", TEMPLATE.read_text(encoding="utf-8")), (
        "The PR template no longer contains any #N, so the regression test "
        "above would pass for the wrong reason. Point it at a fixture instead."
    )


# ── accepted ─────────────────────────────────────────────────────────


def test_closing_reference_is_accepted() -> None:
    assert references("## Summary\n\nDoes the thing.\n\nCloses #1777.\n") == [1777]


def test_plain_reference_is_accepted() -> None:
    """Epic slices reference without closing; the gate wants a trail, not a closure."""
    assert references("## Summary\n\nOne slice.\n\nPart of #1465.\n") == [1465]


def test_multiple_references_are_reported_sorted_and_deduplicated() -> None:
    body = "Refs #1777, #1465, and #1777 again.\n"
    assert references(body) == [1465, 1777]


def test_reference_outside_a_fence_still_counts() -> None:
    body = "## Summary\n\nCloses #1777.\n\n```\nunrelated #4242\n```\n"
    assert references(body) == [1777]


# ── rejected: nothing there ──────────────────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("", id="empty"),
        pytest.param("## Summary\n\nRefactors the parser. No issue for this.\n", id="no-number"),
        pytest.param("# Title\n\n## Summary\n\n### 3. Detail\n", id="markdown-headings"),
        pytest.param("## Summary\n\nMirrors astral-sh/ruff#12345.\n", id="cross-repository"),
    ],
)
def test_bodies_without_a_reference_are_rejected(body: str) -> None:
    assert run(body).returncode == 1


# ── rejected: present, but not visible as prose ──────────────────────


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("## Summary\n\n<!-- e.g. Fixes #1234 -->\n", id="html-comment"),
        pytest.param("## Summary\n\n```\nerror at #4242\n```\n", id="fenced-backtick"),
        pytest.param("## Summary\n\n~~~text\nrefs #4242\n~~~\n", id="fenced-tilde"),
        pytest.param("## Summary\n\nThe token `#1234` is parsed verbatim.\n", id="inline-code"),
        pytest.param(
            "## Summary\n\nSee https://github.com/Q00/ouroboros/pull/1700#issuecomment-1 here.\n",
            id="url-fragment",
        ),
        pytest.param("## Summary\n\n<pre>error at #4242</pre>\n", id="pre-block"),
        pytest.param("## Summary\n\n<code>#4242</code>\n", id="code-block"),
        pytest.param("## Summary\n\n<samp>#4242</samp>\n", id="samp-block"),
        pytest.param("## Summary\n\n<pre>\nlog #4242\n", id="unclosed-pre-block"),
        pytest.param("## Summary\n\n    traceback at #4242\n", id="indented-code-spaces"),
        pytest.param("## Summary\n\n\tlog line #4242\n", id="indented-code-tab"),
        pytest.param("## Summary\n\nAn off-by-one: &#1234; in the output.\n", id="entity-decimal"),
        pytest.param("## Summary\n\nThe byte &#x4d2; appears here.\n", id="entity-hex"),
        pytest.param("## Summary\n\n<!-- Refs #4242\n", id="unclosed-html-comment"),
        pytest.param("## Summary\n\ntext\n\n[hidden]: #4242\n", id="link-reference-definition"),
        pytest.param("## Summary\n\n>     trace #4242\n", id="blockquoted-indented-code"),
        pytest.param("## Summary\n\n> ```\n> log #4242\n> ```\n", id="blockquoted-fence"),
    ],
)
def test_non_rendered_regions_do_not_count(body: str) -> None:
    """A reader of the rendered body sees none of these as an issue link."""
    assert run(body).returncode == 1


def test_a_real_reference_survives_alongside_every_masked_form() -> None:
    """The stripping must not be so eager that it swallows the genuine link."""
    body = (
        "## Summary\n\nCloses #1777.\n\n"
        "<!-- Fixes #1234 -->\n"
        "<pre>#4242</pre>\n"
        "Entity &#5555; here.\n"
        "    indented #6666\n"
        "```\nfenced #7777\n```\n"
        "Inline `#8888` token.\n"
        "https://example.com/x/9999#frag\n"
    )
    assert references(body) == [1777]


# ── I/O contract ─────────────────────────────────────────────────────


def test_body_file_input_matches_literal_input(tmp_path: Path) -> None:
    body_file = tmp_path / "body.md"
    body_file.write_text("Refs #1681.\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--body-file", str(body_file)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout.split() == ["1681"]


def test_candidates_are_emitted_one_per_line_for_the_shell_loop() -> None:
    """The workflow iterates stdout with `for number in ${candidates}`."""
    result = run("Refs #12, #7.\n")
    assert result.stdout == "7\n12\n"


def test_blockquoted_prose_still_counts() -> None:
    """Stripping `>` must remove the marker, not the visible sentence behind it."""
    assert references("## Summary\n\n> Closes #1777 per review.\n") == [1777]


def test_arrow_operators_are_not_blockquote_markers() -> None:
    """`_BLOCKQUOTE_PREFIX` is line-anchored, so prose arrows are untouched."""
    assert references("The flow is A -> B, and this Closes #1777.\n") == [1777]


def test_reference_survives_an_unclosed_comment_later_in_the_body() -> None:
    assert references("Closes #1777.\n\n<!-- Refs #4242\n") == [1777]
