"""Tests for the PR issue-link gate.

The gate reads GitHub's *rendering* of the PR body, not its Markdown source.
Four review rounds on the earlier regex scanner each surfaced another
construct that looks like `#N` but renders as something else -- HTML comments
(closed and unclosed), fenced/indented/blockquoted code, inline code, URL
fragments, character entities, raw-text HTML blocks, link reference
definitions, and inline link destinations. Markdown context is not a regular
language, so the renderer is the authority.

The fixtures below are the HTML GitHub actually returned for each Markdown
input, captured via `POST /markdown` with `mode=gfm` and `context=Q00/ouroboros`.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "check-pr-issue-link.py"

OWNER = "Q00"
REPO = "ouroboros"

ISSUE_ANCHOR = '<a class="issue-link" href="https://github.com/Q00/ouroboros/issues/1777">#1777</a>'
PULL_ANCHOR = '<a class="issue-link" href="https://github.com/Q00/ouroboros/pull/1735">#1735</a>'


def run(rendered: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--rendered", rendered, "--owner", OWNER, "--repo", REPO],
        capture_output=True,
        text=True,
    )


def issues(rendered: str) -> list[int]:
    return [int(line) for line in run(rendered).stdout.split()]


# ── accepted ─────────────────────────────────────────────────────────


def test_issue_anchor_is_reported() -> None:
    assert issues(f"<p>Closes {ISSUE_ANCHOR}.</p>") == [1777]


def test_relative_href_is_accepted() -> None:
    """GitHub emits site-relative hrefs in some rendering modes."""
    assert issues('<p><a href="/Q00/ouroboros/issues/1777">#1777</a></p>') == [1777]


def test_multiple_anchors_are_sorted_and_deduplicated() -> None:
    rendered = (
        '<p><a href="/Q00/ouroboros/issues/1777">#1777</a> '
        '<a href="/Q00/ouroboros/issues/1465">#1465</a> '
        '<a href="/Q00/ouroboros/issues/1777">again</a></p>'
    )
    assert issues(rendered) == [1465, 1777]


def test_fragment_or_query_suffix_still_resolves() -> None:
    rendered = '<p><a href="/Q00/ouroboros/issues/1777#issuecomment-42">c</a></p>'
    assert issues(rendered) == [1777]


# ── rejected ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("rendered", "reason"),
    [
        pytest.param("<p>No references here.</p>", "no anchor", id="no-anchor"),
        pytest.param("", "empty", id="empty"),
        pytest.param(f"<p>See PR {PULL_ANCHOR}.</p>", "pull request", id="pull-request"),
        pytest.param(
            '<p><a href="https://github.com/astral-sh/ruff/issues/12345">x</a></p>',
            "other repository",
            id="cross-repository",
        ),
        pytest.param(
            '<p><a href="https://example.com/Q00/ouroboros/issues/1777">x</a></p>',
            "other host",
            id="foreign-host",
        ),
        pytest.param(
            '<p><a href="/Q00/ouroboros-mirror/issues/1777">x</a></p>',
            "similar repository name",
            id="prefix-lookalike-repo",
        ),
        pytest.param(
            "<p>The literal text #1777 was not autolinked.</p>",
            "plain text",
            id="unlinked-text",
        ),
    ],
)
def test_non_issue_links_are_rejected(rendered: str, reason: str) -> None:
    result = run(rendered)
    assert result.returncode == 1, f"should have been rejected: {reason}"
    assert "links to no issue" in result.stderr


def test_code_block_containing_an_issue_url_is_not_a_link() -> None:
    """Rendered code is text, not an anchor -- the renderer already decided."""
    rendered = "<pre><code>https://github.com/Q00/ouroboros/issues/4242\n</code></pre>"
    assert run(rendered).returncode == 1


# ── the constructs four review rounds found ──────────────────────────


@pytest.mark.parametrize(
    ("markdown_source", "rendered"),
    [
        pytest.param("Closes #1777.", f"<p>Closes {ISSUE_ANCHOR}.</p>", id="prose"),
        pytest.param(
            "See [details](#1777).",
            '<p>See <a href="#1777">details</a>.</p>',
            id="link-destination",
        ),
        pytest.param("<!-- Refs #1777 -->", "", id="html-comment"),
        pytest.param("<!-- Refs #1777", "", id="unclosed-html-comment"),
        pytest.param(
            "```\nlog #1777\n```",
            "<pre><code>log #1777\n</code></pre>",
            id="fenced-code",
        ),
        pytest.param(
            "    trace #1777",
            "<pre><code>trace #1777\n</code></pre>",
            id="indented-code",
        ),
        pytest.param(
            ">     trace #1777",
            "<blockquote>\n<pre><code>trace #1777\n</code></pre>\n</blockquote>",
            id="blockquoted-code",
        ),
        pytest.param("`#1777`", "<p><code>#1777</code></p>", id="inline-code"),
        pytest.param("[hidden]: #1777", "", id="link-reference-definition"),
        pytest.param("An entity &#1777; here.", "<p>An entity ۱ here.</p>", id="entity"),
        pytest.param("<pre>#1777</pre>", "<pre>#1777</pre>", id="raw-text-html"),
    ],
)
def test_rendered_context_decides(markdown_source: str, rendered: str) -> None:
    """Only prose autolinks. Everything else renders without an anchor."""
    expected = [1777] if markdown_source.startswith("Closes") else []
    assert issues(rendered) == expected, f"for Markdown source: {markdown_source!r}"


# ── I/O contract ─────────────────────────────────────────────────────


def test_rendered_file_input_matches_literal_input(tmp_path: Path) -> None:
    rendered_file = tmp_path / "rendered.html"
    rendered_file.write_text(f"<p>Refs {ISSUE_ANCHOR}</p>", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--rendered-file",
            str(rendered_file),
            "--owner",
            OWNER,
            "--repo",
            REPO,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout.split() == ["1777"]


def test_numbers_are_emitted_one_per_line_for_the_shell_loop() -> None:
    rendered = '<a href="/Q00/ouroboros/issues/12">a</a><a href="/Q00/ouroboros/issues/7">b</a>'
    assert run(rendered).stdout == "7\n12\n"


def test_malformed_html_does_not_crash() -> None:
    """`html.parser` is lenient by design; the gate must not fail on noise."""
    assert run("<p><a href=<<>> unclosed").returncode == 1
