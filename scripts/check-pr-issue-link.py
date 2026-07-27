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


class _IssueLinkCollector(HTMLParser):
    """Collect issue numbers from ``<a href>`` targets in rendered HTML."""

    def __init__(self, owner: str, repo: str) -> None:
        super().__init__(convert_charrefs=True)
        # Anchored and repo-scoped on purpose: a link into another repository
        # is not local traceability, and `/pull/N` is not an issue.
        self._pattern = re.compile(
            rf"^(?:https?://github\.com)?/{re.escape(owner)}/{re.escape(repo)}/issues/(\d+)(?:[#?].*)?$"
        )
        self.issue_numbers: set[int] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        for name, value in attrs:
            if name == "href" and value:
                match = self._pattern.match(value)
                if match:
                    self.issue_numbers.add(int(match.group(1)))


def find_linked_issues(rendered_html: str, *, owner: str, repo: str) -> list[int]:
    """Return the repository issues the rendered body links to, ascending."""
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
