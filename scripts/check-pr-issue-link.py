#!/usr/bin/env python3
"""Decide whether a pull request body carries a visible issue reference.

Per Q00/ouroboros#1777, every non-exempt PR must be traceable to an issue.
A board audit on 2026-07-28 closed 11 issues, 6 of which were already fully
implemented on `main` -- the work had landed but no PR linked back.

This script owns the *body* half of that gate. The other half is GitHub's
own ``closingIssuesReferences``, which the workflow queries first and which
also covers issues linked through the Development sidebar.

Why this is not a one-line grep:
    ``.github/PULL_REQUEST_TEMPLATE.md`` mentions ``#1234``, ``#1256``, and
    ``#1258`` inside HTML comments as instructions to the author. A naive
    scan therefore passes an *unedited template*, which defeats the gate for
    exactly the PRs it exists to catch. Non-rendered regions must be removed
    before scanning, so the check sees what a reviewer sees.

Stripped before scanning:
    - HTML comments ``<!-- ... -->`` (the template's instructions live here)
    - fenced code blocks ``` / ~~~ (sample diffs, logs, shell transcripts)
    - inline code spans ``` `...` `` (e.g. a literal ``#1234`` in prose)
    - autolinks and URLs (``.../pull/1234#issuecomment-...`` fragments)

A reference is any ``#<digits>`` that survives. Both ``Closes #123`` and
``Part of #123`` qualify: most PRs here are slices of an epic and must not
auto-close their parent, so the gate requires a trail, not a closure.

Run locally:
    python3 scripts/check-pr-issue-link.py --body-file body.md

CI:
    .github/workflows/pr-hygiene.yml runs this on every PR.

Exit codes:
    0 -- a visible issue reference was found
    1 -- no visible issue reference
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys

# Order matters: fenced blocks are removed before inline spans so that a
# stray backtick inside a fence cannot split an unrelated span.
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_FENCED_BLOCK = re.compile(
    r"^[ \t]*(`{3,}|~{3,}).*?(?:^[ \t]*\1[ \t]*$|\Z)", re.DOTALL | re.MULTILINE
)
_INLINE_CODE = re.compile(r"(`+)(?:.|\n)*?\1")
# Any http(s) token, so `#123` appearing as a URL fragment is not a reference.
_URL = re.compile(r"<?https?://\S+>?")

# `#123` not preceded by a word character, `/`, or another `#`. The `/` guard
# drops leftovers like `owner/repo#12`; the `#` guard drops Markdown headings.
_ISSUE_REF = re.compile(r"(?<![0-9A-Za-z_/#])#(\d+)")


def visible_text(body: str) -> str:
    """Return `body` with every non-rendered region removed."""
    text = _HTML_COMMENT.sub(" ", body)
    text = _FENCED_BLOCK.sub(" ", text)
    text = _INLINE_CODE.sub(" ", text)
    text = _URL.sub(" ", text)
    return text


def find_issue_references(body: str) -> list[int]:
    """Return every issue number visible to a reader of `body`, in order."""
    return [int(match.group(1)) for match in _ISSUE_REF.finditer(visible_text(body))]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--body-file", type=Path, help="file holding the PR body")
    source.add_argument("--body", help="PR body as a literal string")
    args = parser.parse_args()

    if args.body_file is not None:
        body = args.body_file.read_text(encoding="utf-8")
    else:
        body = args.body

    references = find_issue_references(body)
    if references:
        rendered = ", ".join(f"#{number}" for number in references)
        sys.stdout.write(f"pr-issue-link: found {rendered}\n")
        return 0

    sys.stderr.write(
        "pr-issue-link: no visible issue reference in the PR body.\n"
        "\n"
        "References inside HTML comments, fenced code blocks, inline code, or\n"
        "URLs do not count -- the unedited PR template contains several such\n"
        "examples, and accepting them would let any template-created PR pass.\n"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
