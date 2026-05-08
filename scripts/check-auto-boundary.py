#!/usr/bin/env python3
"""Enforce the `ooo auto` product boundary at PR time.

Per Q00/ouroboros#725, `ooo auto` has a permanent product boundary:
`goal -> interview -> Seed -> handoff`. Domain-specific operational
workflows (GitHub PR ops, Jira, Slack, Linear, ...) belong in plugins,
not in core auto.

This script greps the `ooo auto` core source files for forbidden domain
keywords and exits non-zero if any are found. It is the mechanical
enforcement layer paired with #734's documentary work.

Run locally:
    python3 scripts/check-auto-boundary.py

CI:
    .github/workflows/auto-boundary.yml runs this on every PR.

Allowlist:
    Lines that genuinely need a forbidden keyword (rare; usually a
    legacy import) can be marked with the trailing comment
    `# domain-keyword-allowed: <reason>` to bypass the check. The
    marker is honored only when it appears inside a real Python
    comment token (the file is tokenized for this), so a stray
    "domain-keyword-allowed:" inside a string literal cannot be used
    to smuggle a forbidden keyword past the guard. Each allowlist
    usage requires reviewer sign-off.

Coverage strategy:
    The scan target is the *union* of (a) every `*.py` under
    `src/ouroboros/auto/`, plus (b) explicit extra files such as
    `src/ouroboros/cli/commands/auto.py`. New files added under the
    auto package are automatically covered. A small set of ANCHOR_FILES
    is checked for existence; if any anchor is missing the guard fails
    loudly so a refactor that renames or removes a load-bearing file
    cannot silently strip enforcement coverage.

Pattern strategy:
    Forbidden patterns are Python regexes scoped with inline
    case-insensitivity flags (``(?i:...)``) so each pattern can mix
    case-insensitive and case-sensitive sub-matches as needed. This
    catches realistic identifier forms (`GitHubClient`,
    `github_client`, `PullRequestHandler`, `pullRequestId`,
    `LinearClient`) without false-positiving on benign code such as
    ``linear_time``, ``linearize``, or "linear pipeline" -- the
    Linear-the-product pattern requires either the URL form, a
    PascalCase composition (lowercase ``linear`` + an explicit
    uppercase letter), or one of an enumerated set of integration
    suffixes.

    Each keyword carries TWO recognition forms so that compound
    identifiers across naming conventions are not silent bypasses:

    1. *Token-start* form: anchored by ``(?<![A-Za-z])`` so the keyword
       starts after a non-letter (start-of-line, whitespace, ``_``,
       digit, punctuation). This catches ``GitHubClient``,
       ``GITHUB_TOKEN``, ``github_client``, ``_github_url``, and the
       SCREAMING_SNAKE form ``FOO_GITHUB_BASE``.

    2. *camelCase-boundary* form: anchored by ``(?<=[a-z])`` (lowercase
       letter on the left) plus a *case-sensitive* uppercase first
       character of the keyword. This catches mixed-case compounds like
       ``openGithubIssue``, ``makePullRequestUrl``, ``sendToSlack``,
       ``openJiraIssue``, and ``notifyLinearAdapter`` -- the exact
       bypass class that earlier iterations of this guard missed -- while
       still rejecting the explicitly-benign embedded-substring forms
       that only contain the keyword in lowercase (``mygithub_thing``,
       ``mypull_request_data``, ``collinearPoints``, ``bilinearForm``,
       ``nonlinearAdapter``, ``nonlinear_adapter``). The discrimination
       is purely the case of the keyword's first letter at the boundary:
       lowercase = embedded substring (benign), uppercase = camelCase
       composition (domain leak).
"""

from __future__ import annotations

import io
from pathlib import Path
import re
import sys
import tokenize

REPO_ROOT = Path(__file__).resolve().parents[1]


# Required-anchor files that constitute the load-bearing core of `ooo
# auto`. The guard fails loud if any anchor is missing -- silent loss
# of enforcement coverage during a refactor would defeat the purpose
# of the guard.
ANCHOR_FILES: tuple[str, ...] = (
    "src/ouroboros/cli/commands/auto.py",
    "src/ouroboros/auto/pipeline.py",
    "src/ouroboros/auto/interview_driver.py",
    "src/ouroboros/auto/state.py",
    "src/ouroboros/auto/adapters.py",
    "src/ouroboros/auto/grading.py",
    "src/ouroboros/auto/seed_repairer.py",
    "src/ouroboros/auto/seed_reviewer.py",
    "src/ouroboros/auto/progress.py",
)


# Auto-discovered scan roots. All `*.py` files under each directory are
# scanned (recursively), so newly added auto-package files are covered
# without an explicit list update.
SCAN_DIRS: tuple[str, ...] = ("src/ouroboros/auto",)


# Extra individual files to include in the scan that live outside the
# SCAN_DIRS roots.
SCAN_EXTRA_FILES: tuple[str, ...] = ("src/ouroboros/cli/commands/auto.py",)


# Forbidden domain keywords expressed as Python regexes. Patterns are
# intentionally precise: a guard whose patterns false-positive on
# benign code (e.g. ``linear_time``, ``linearize``, "linear pipeline")
# turns into a CI-noise generator and gets allowlisted into
# uselessness. ``(?i:...)`` is used for case-insensitive sub-matches;
# bracket character classes such as ``[A-Z]`` outside that scope stay
# case-sensitive, which is the exact discrimination needed for
# PascalCase-composition forms.
#
# Every keyword has TWO recognition arms (separated by ``|``):
#
#   * Token-start arm ``(?<![A-Za-z])(?i:<kw>)``: the keyword sits at
#     the start of a token (preceded by start-of-line, whitespace,
#     ``_``, digit, ``/``, ``"``, etc.). Case-insensitive, so it
#     catches ``GitHubClient``, ``GITHUB_TOKEN``, ``github_client``,
#     ``_github_url``, and SCREAMING_SNAKE forms like
#     ``FOO_GITHUB_BASE``. We deliberately do *not* use ``\b`` here,
#     because ``_`` is a word character and ``\b`` would lose the
#     underscore-adjacent forms (``notify_slack_user``,
#     ``FOO_GITHUB_BASE``, ``linear_client``).
#
#   * camelCase-boundary arm ``(?<=[a-z])<KW first letter capitalized>(?i:<kw rest>)``:
#     the keyword sits at a camelCase boundary (lowercase letter on
#     the left, *uppercase* keyword first letter, case-insensitive
#     remainder). This catches ``openGithubIssue``,
#     ``makePullRequestUrl``, ``sendToSlack``, ``openJiraIssue``,
#     ``notifyLinearAdapter`` -- the bypass class earlier iterations
#     missed. The case-sensitive uppercase first letter is what
#     preserves the explicitly-benign embedded-lowercase forms
#     (``mygithub_thing``, ``mypull_request_data``,
#     ``collinearPoints``, ``bilinearForm``, ``nonlinearAdapter``,
#     ``nonlinear_adapter``): in those, the keyword's first letter is
#     lowercase, so the camelCase arm does not match.
FORBIDDEN_PATTERNS: tuple[str, ...] = (
    r"(?<![A-Za-z])(?i:github)|(?<=[a-z])G(?i:ithub)",
    r"(?<![A-Za-z])(?i:pull_request)",
    # Compressed camelCase / no-underscore form ``PullRequestHandler`` /
    # ``pullRequestId`` / ``makePullRequestUrl`` that the snake-case
    # pattern above misses (the underscore prevents the substrings from
    # sharing).
    r"(?<![A-Za-z])(?i:pullrequest)|(?<=[a-z])P(?i:ullrequest)",
    r"(?i:/pulls?/)",  # `/` is already a non-word boundary
    r"(?<![A-Za-z])(?i:jira)|(?<=[a-z])J(?i:ira)",
    r"(?<![A-Za-z])(?i:slack)|(?<=[a-z])S(?i:lack)",
    # Linear (the issue tracker / SaaS) -- tightened to:
    #   * the URL form ``linear.app`` / ``linear.com``, or
    #   * a PascalCase composition (`LinearClient`, `LinearAdapter`,
    #     `LinearAuth`, ...): "(?i:linear)" is case-insensitive but the
    #     trailing ``[A-Z]`` is case-sensitive, so this distinguishes
    #     ``LinearClient`` from benign ``linearize`` / ``linear_time``,
    #   * an explicit integration suffix in snake_case
    #     (`linear_client`, `linear_webhook`, ...),
    #   * a camelCase composition where ``Linear`` is preceded by a
    #     lowercase letter (``notifyLinearAdapter``,
    #     ``maybeLinearClient``).
    # Plain "linear pipeline" / "linear scan" / "linear_time" do NOT
    # match. Each token-start arm carries ``(?<![A-Za-z])`` and each
    # camelCase arm carries ``(?<=[a-z])L`` (case-sensitive) so that
    # embedded forms like ``collinearPoints``, ``bilinearForm``,
    # ``nonlinearAdapter``, and ``nonlinear_adapter`` also do not match.
    (
        r"(?<![A-Za-z])(?i:linear\.(?:app|com))"
        r"|(?<![A-Za-z])(?i:linear)[A-Z]"
        r"|(?<![A-Za-z])(?i:linear_(?:client|adapter|api|webhook|sdk|service|integration|notifier|hook|bot|messenger|action))"
        r"|(?<=[a-z])L(?i:inear)[A-Z]"
        r"|(?<=[a-z])L(?i:inear_(?:client|adapter|api|webhook|sdk|service|integration|notifier|hook|bot|messenger|action))"
    ),
)


_COMPILED_PATTERNS: tuple[re.Pattern[str], ...] = tuple(re.compile(p) for p in FORBIDDEN_PATTERNS)


# Marker comment that allowlists a single line.
ALLOWLIST_MARKER = "domain-keyword-allowed:"


def _resolve_scan_targets() -> tuple[list[Path], list[str]]:
    """Return ``(scan_targets, missing_anchors)``.

    ``scan_targets`` is the union of every existing ``*.py`` file under
    SCAN_DIRS plus any SCAN_EXTRA_FILES that exist. ``missing_anchors``
    is the list of ANCHOR_FILES that do not exist on disk; a non-empty
    list means the guard must fail loud.
    """
    targets: list[Path] = []
    seen: set[Path] = set()

    for d in SCAN_DIRS:
        root = REPO_ROOT / d
        if root.is_dir():
            for p in sorted(root.rglob("*.py")):
                rp = p.resolve()
                if rp not in seen:
                    seen.add(rp)
                    targets.append(p)

    for rel in SCAN_EXTRA_FILES:
        p = REPO_ROOT / rel
        if p.is_file():
            rp = p.resolve()
            if rp not in seen:
                seen.add(rp)
                targets.append(p)

    missing = [rel for rel in ANCHOR_FILES if not (REPO_ROOT / rel).is_file()]
    return targets, missing


def _allowlisted_lines(text: str) -> set[int]:
    """Return the set of line numbers carrying a *real* Python comment
    that contains the allowlist marker.

    A naive substring check (``ALLOWLIST_MARKER in line``) would also
    bypass a forbidden keyword when the marker appears inside a string
    literal -- e.g. ``MSG = "domain-keyword-allowed: github"`` -- which
    silently undermines the contract documented in CONTRIBUTING.md and
    the script docstring. Tokenize the file and only honor the marker
    when it appears in a ``COMMENT`` token.

    On tokenize failure (malformed Python, partial file, etc.), return
    an empty set: the safer default for a boundary guard is to flag
    rather than over-allowlist.
    """
    allowed: set[int] = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT and ALLOWLIST_MARKER in tok.string:
                allowed.add(tok.start[0])
    except (tokenize.TokenizeError, IndentationError, SyntaxError):
        return set()
    return allowed


def _scan_file(path: Path) -> list[tuple[int, str, str]]:
    """Return offending ``(line_no, line, matched_pattern)`` tuples for ``path``.

    Lines carrying the allowlist marker *as a real comment* are skipped.
    Lines inside string literals or docstrings are still checked -- a
    stray keyword in a docstring of a watched file would catch, which
    is the desired behavior; and a forbidden keyword on a line whose
    only marker is inside a string literal is *not* allowlisted.
    """
    findings: list[tuple[int, str, str]] = []
    if not path.is_file():
        return findings
    text = path.read_text(encoding="utf-8")
    allowed_lines = _allowlisted_lines(text)
    for lineno, line in enumerate(text.splitlines(), start=1):
        if lineno in allowed_lines:
            continue
        for pattern, compiled in zip(FORBIDDEN_PATTERNS, _COMPILED_PATTERNS, strict=True):
            if compiled.search(line):
                findings.append((lineno, line.rstrip(), pattern))
                break
    return findings


def main() -> int:
    targets, missing = _resolve_scan_targets()

    if missing:
        sys.stderr.write(
            "ooo-auto-boundary: FAILED -- required anchor files are missing.\n"
            "These files define the `ooo auto` product surface; if you\n"
            "renamed/moved/deleted them, update ANCHOR_FILES in\n"
            "scripts/check-auto-boundary.py in the same PR so enforcement\n"
            "coverage is preserved.\n\n"
        )
        for rel in missing:
            sys.stderr.write(f"  missing anchor: {rel}\n")
        return 1

    all_findings: list[tuple[Path, int, str, str]] = []
    for path in targets:
        for lineno, line, pattern in _scan_file(path):
            all_findings.append((path, lineno, line, pattern))

    if not all_findings:
        print(f"ooo-auto-boundary: OK ({len(targets)} files scanned, 0 findings)")
        return 0

    sys.stderr.write(
        "ooo-auto-boundary: FAILED -- domain keywords leaked into core auto.\n"
        "Per Q00/ouroboros#725, these belong in a UserLevel plugin, not in `ooo auto`.\n\n"
    )
    for path, lineno, line, pattern in all_findings:
        try:
            rel = path.relative_to(REPO_ROOT)
        except ValueError:
            rel = path
        sys.stderr.write(f"  {rel}:{lineno}: matched {pattern!r}\n    {line}\n")
    sys.stderr.write(
        "\n"
        "If a forbidden keyword is genuinely necessary on a line (rare), append\n"
        f"  # {ALLOWLIST_MARKER} <reason>\n"
        "and add a brief PR-description rationale.\n"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
