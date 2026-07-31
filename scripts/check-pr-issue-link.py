#!/usr/bin/env python3
"""Extract the issues a rendered pull request body links to.

Per Q00/ouroboros#1777, every non-exempt PR must be traceable to an issue.
A board audit on 2026-07-28 closed 11 issues, 6 of which were already fully
implemented on `main` -- the work had landed but no PR linked back.

The question this answers is "what does a reader of the rendered body see?",
so the input is GitHub's own rendering of the body (``POST /markdown`` with
``mode=gfm`` and a repository ``context``), not its Markdown source. The
workflow renders; this script reads the anchors out.

Why not scan the source text:
    Four review rounds on the original regex approach each found another
    construct that looks like ``#N`` but renders as something else: HTML
    comments closed and unclosed, fenced and indented and blockquoted code,
    inline code, URL fragments, character entities, raw-text HTML blocks,
    link reference definitions, and inline link destinations. Markdown
    context is not a regular language, and GitHub's renderer is the only
    authority on what GitHub shows.

Delegating also settles two questions the scanner could not answer at all:

    * `#N` is a shared namespace. GitHub renders an issue as
      ``/{owner}/{repo}/issues/N`` and a pull request as ``.../pull/N``, so
      "See PR #1735" no longer counts as an issue trail.
    * GitHub autolinks only numbers that exist. ``#0`` and ``#999999`` render
      as plain text, so a typo cannot satisfy the gate.

Run locally:
    gh api -X POST /markdown --input - <<< '{"text": "...", "mode": "gfm",
        "context": "Q00/ouroboros"}' > rendered.html
    python3 scripts/check-pr-issue-link.py --rendered-file rendered.html \
        --owner Q00 --repo ouroboros

CI:
    .github/workflows/pr-hygiene.yml runs this on every PR.

Output:
    One linked issue number per line on stdout, ascending, deduplicated.

Exit codes:
    0 -- the rendered body links to at least one issue in this repository
    1 -- it does not
"""

from __future__ import annotations

import argparse
from html.parser import HTMLParser
from pathlib import Path
import re
import sys
import unicodedata

# Elements that render something by themselves. A `<span></span>` does not.
_SELF_RENDERING_TAGS = frozenset({"img", "picture", "svg", "video"})

# A character is visible when it occupies space on screen. Rather than deny a
# finite list -- which missed U+2061 FUNCTION APPLICATION, among others --
# classify by Unicode general category: control (Cc), format (Cf), surrogate
# (Cs), private use (Co), unassigned (Cn), and the separators (Zs/Zl/Zp) all
# render nothing. Everything else, including emoji (So) and CJK (Lo), does.
_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zs", "Zl", "Zp"})


def _has_visible_glyph(text: str) -> bool:
    """Return whether `text` renders anything a reader can see."""
    return any(unicodedata.category(ch) not in _INVISIBLE_CATEGORIES for ch in text)


class _IssueLinkCollector(HTMLParser):
    """Collect issue numbers from ``<a href>`` targets in rendered HTML.

    An anchor counts only when it carries visible text. GitHub renders
    ``[](.../issues/1777)`` as an empty anchor, which links nothing a reader
    can see or click, so it is not a trail.
    """

    def __init__(self, owner: str, repo: str) -> None:
        super().__init__(convert_charrefs=True)
        # Anchored and repo-scoped on purpose: a link into another repository
        # is not local traceability, and `/pull/N` is not an issue. Owner and
        # repository are compared case-insensitively and a trailing slash is
        # tolerated, because GitHub treats those URLs as the same page.
        # The digit count is bounded. An author-supplied destination can hold
        # arbitrarily many digits, and `int()` raises `ValueError` past
        # CPython's integer-string limit -- a crash in a gate is a red check
        # for a valid PR. No repository has a nine-digit issue number.
        self._pattern = re.compile(
            rf"^(?:https?://(?:www\.)?github\.com)?"
            rf"/{re.escape(owner)}/{re.escape(repo)}/issues/(\d{{1,9}})/?(?:[#?].*)?$",
            re.IGNORECASE,
        )
        self.issue_numbers: set[int] = set()
        self._open_issue_numbers: list[int | None] = []
        self._text_seen: list[bool] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            # Only an image renders on its own. An empty wrapper such as
            # `<span></span>` shows nothing, and anything with real content --
            # `<code>#1777</code>` -- is caught by `handle_data` instead.
            if self._text_seen and tag in _SELF_RENDERING_TAGS:
                self._text_seen[-1] = True
            return
        number: int | None = None
        for name, value in attrs:
            if name == "href" and value:
                match = self._pattern.match(value.strip())
                if match:
                    number = int(match.group(1))
        self._open_issue_numbers.append(number)
        self._text_seen.append(False)

    def handle_data(self, data: str) -> None:
        if self._text_seen and _has_visible_glyph(data):
            self._text_seen[-1] = True

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or not self._open_issue_numbers:
            return
        number = self._open_issue_numbers.pop()
        visible = self._text_seen.pop()
        if number is not None and visible:
            self.issue_numbers.add(number)


def find_linked_issues(rendered_html: str, *, owner: str, repo: str) -> list[int]:
    """Return the repository issue numbers the rendered body links to, ascending.

    These are *candidates*. GitHub existence- and type-checks the shorthand it
    autolinks itself, but an author-supplied destination is rendered verbatim:
    ``[x](/owner/repo/issues/0)`` produces an anchor for an issue that does not
    exist. The workflow resolves each number against the repository before
    accepting it.
    """
    collector = _IssueLinkCollector(owner, repo)
    collector.feed(rendered_html)
    collector.close()
    return sorted(collector.issue_numbers)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--rendered-file", type=Path, help="file holding the rendered HTML")
    source.add_argument("--rendered", help="rendered HTML as a literal string")
    parser.add_argument("--owner", required=True, help="repository owner")
    parser.add_argument("--repo", required=True, help="repository name")
    args = parser.parse_args()

    if args.rendered_file is not None:
        rendered = args.rendered_file.read_text(encoding="utf-8")
    else:
        rendered = args.rendered

    issues = find_linked_issues(rendered, owner=args.owner, repo=args.repo)
    if issues:
        sys.stdout.write("".join(f"{number}\n" for number in issues))
        return 0

    sys.stderr.write(
        "pr-issue-link: the rendered PR body links to no issue in this repository.\n"
        "\n"
        "GitHub autolinks `#N` only where it renders as prose and only when N\n"
        "exists, so a number inside a comment, a code block, a link target, or\n"
        "a reference definition does not count -- and neither does a pull\n"
        "request number, which renders as /pull/N rather than /issues/N.\n"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
