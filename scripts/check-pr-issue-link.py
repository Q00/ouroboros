#!/usr/bin/env python3
"""Extract the issue numbers a pull request body visibly references.

Per Q00/ouroboros#1777, every non-exempt PR must be traceable to an issue.
A board audit on 2026-07-28 closed 11 issues, 6 of which were already fully
implemented on `main` -- the work had landed but no PR linked back.

This script owns one half of that gate: *which numbers does a reader of the
rendered body actually see?* It deliberately does not decide whether those
numbers exist or whether they are issues rather than pull requests --
`#N` is a shared namespace, and only GitHub can resolve it. The workflow
does that resolution on the numbers reported here, and separately consults
GitHub's own ``closingIssuesReferences`` for sidebar-linked issues.

Why this is not a one-line grep:
    ``.github/PULL_REQUEST_TEMPLATE.md`` mentions ``#1234``, ``#1256``, and
    ``#1258`` inside HTML comments as instructions to the author. A naive
    scan therefore passes an *unedited template*, which defeats the gate for
    exactly the PRs it exists to catch. Non-rendered regions must be removed
    before scanning, so the check sees what a reviewer sees.

Stripped before scanning, in this order:
    - HTML comments ``<!-- ... -->`` (the template's instructions live here)
    - raw-text HTML blocks ``<pre>``/``<code>``/``<script>``/``<style>``,
      content included -- ``<pre>#4242</pre>`` renders as a literal
    - fenced code blocks ``` / ~~~ (sample diffs, logs, shell transcripts)
    - indented code blocks (four spaces or a tab), e.g. a pasted traceback
    - inline code spans ``` `...` `` (e.g. a literal ``#1234`` in prose)
    - autolinks and URLs (``.../pull/1234#issuecomment-...`` fragments)
    - remaining HTML tags, then HTML entities -- ``&#1234;`` is a character
      reference, not an issue reference, and its digits must not survive

A reference is any ``#<digits>`` that survives. Both ``Closes #123`` and
``Part of #123`` qualify: most PRs here are slices of an epic and must not
auto-close their parent, so the gate requires a trail, not a closure.

Run locally:
    python3 scripts/check-pr-issue-link.py --body-file body.md

CI:
    .github/workflows/pr-hygiene.yml runs this on every PR.

Output:
    One candidate issue number per line on stdout, ascending, deduplicated.

Exit codes:
    0 -- at least one candidate reference is visible
    1 -- none
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys

# Order matters. Raw-text HTML blocks and fences are removed with their
# content before inline spans, so a backtick inside a fence cannot split an
# unrelated span. Entities are removed last, after tags, because stripping
# `<...>` first would otherwise leave a bare `&#1234;` looking like prose.
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_RAW_TEXT_HTML = re.compile(
    r"<(pre|code|script|style|kbd|samp)\b[^>]*>.*?</\1\s*>",
    re.DOTALL | re.IGNORECASE,
)
_UNCLOSED_RAW_TEXT_HTML = re.compile(
    r"<(pre|code|script|style|kbd|samp)\b[^>]*>.*\Z",
    re.DOTALL | re.IGNORECASE,
)
_FENCED_BLOCK = re.compile(
    r"^[ \t]*(`{3,}|~{3,}).*?(?:^[ \t]*\1[ \t]*$|\Z)", re.DOTALL | re.MULTILINE
)
# A line indented by four spaces or a tab renders as code in Markdown. This is
# intentionally conservative: it can also swallow a reference buried in a
# deeply indented list, which costs a contributor one body edit. The opposite
# error -- a pasted traceback silently satisfying the gate -- costs the gate.
_INDENTED_CODE = re.compile(r"^(?: {4,}|\t).*$", re.MULTILINE)
_INLINE_CODE = re.compile(r"(`+)(?:.|\n)*?\1")
# Any http(s) token, so `#123` appearing as a URL fragment is not a reference.
_URL = re.compile(r"<?https?://\S+>?")
_HTML_TAG = re.compile(r"<[^>\n]{1,200}>")
# Numeric (`&#1234;`), hex (`&#x4d2;`), and named (`&amp;`) character
# references. The numeric form is why this matters: its digits follow a `#`.
_HTML_ENTITY = re.compile(r"&#?[0-9A-Za-z]{1,32};")

# `#123` not preceded by a word character, `/`, `&`, or another `#`. The `/`
# guard drops leftovers like `owner/repo#12`; `#` drops Markdown headings; `&`
# is belt-and-braces for an entity that somehow escaped _HTML_ENTITY.
_ISSUE_REF = re.compile(r"(?<![0-9A-Za-z_/#&])#(\d+)")


def visible_text(body: str) -> str:
    """Return `body` with every region a reader would not see as prose removed."""
    text = _HTML_COMMENT.sub(" ", body)
    text = _RAW_TEXT_HTML.sub(" ", text)
    text = _UNCLOSED_RAW_TEXT_HTML.sub(" ", text)
    text = _FENCED_BLOCK.sub(" ", text)
    text = _INDENTED_CODE.sub(" ", text)
    text = _INLINE_CODE.sub(" ", text)
    text = _URL.sub(" ", text)
    text = _HTML_TAG.sub(" ", text)
    text = _HTML_ENTITY.sub(" ", text)
    return text


def find_issue_references(body: str) -> list[int]:
    """Return every candidate issue number visible to a reader, ascending.

    "Candidate" is precise: `#N` cannot distinguish an issue from a pull
    request, and this function does not know which numbers exist. Resolving
    that is the workflow's job.
    """
    seen = {int(match.group(1)) for match in _ISSUE_REF.finditer(visible_text(body))}
    return sorted(seen)


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
        sys.stdout.write("".join(f"{number}\n" for number in references))
        return 0

    sys.stderr.write(
        "pr-issue-link: no visible issue reference in the PR body.\n"
        "\n"
        "References inside HTML comments, code (fenced, indented, or inline),\n"
        "raw-text HTML, URLs, or character entities do not count -- the\n"
        "unedited PR template contains several such examples, and accepting\n"
        "them would let any template-created PR pass.\n"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
