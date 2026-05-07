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
    `# domain-keyword-allowed: <reason>` to bypass the check. Each
    allowlist usage requires reviewer sign-off.

Coverage strategy:
    The scan target is the *union* of (a) every `*.py` under
    `src/ouroboros/auto/`, plus (b) explicit extra files such as
    `src/ouroboros/cli/commands/auto.py`. New files added under the
    auto package are automatically covered. A small set of ANCHOR_FILES
    is checked for existence; if any anchor is missing the guard fails
    loudly so a refactor that renames or removes a load-bearing file
    cannot silently strip enforcement coverage.

Pattern strategy:
    Forbidden patterns are matched as case-insensitive *substrings*,
    not word-boundaried regex. This catches realistic identifier forms
    (`GitHubClient`, `github_client`, `JiraIssue`, `SlackNotifier`)
    that a word-boundary regex would miss. Patterns are deliberately
    chosen with low false-positive risk on the actual `ooo auto`
    surface (verified against current main).
"""

from __future__ import annotations

from pathlib import Path
import sys

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


# Forbidden domain keywords (case-insensitive substrings). Word
# boundaries are deliberately NOT used: a contributor can otherwise
# bypass the guard by writing `GitHubClient` or `github_client` and
# having the regex skip identifier-embedded forms (the gap flagged by
# the bot review of the original guard).
FORBIDDEN_PATTERNS: tuple[str, ...] = (
    "github",
    # Both forms are needed: lowercased lines preserve underscores, so
    # `pull_request` (snake_case) and `pullrequest` (camelCase compressed
    # form, e.g. PullRequestHandler/pullRequestId) are distinct
    # substrings.
    "pull_request",
    "pullrequest",
    "/pulls/",
    "/pull/",
    "jira",
    "slack",
    # `linear` (rather than `linear.app`) is required to catch identifier
    # forms such as `LinearClient` / `LinearAdapter`. The substring also
    # subsumes the original URL match `linear.app`. Verified zero false
    # positives on the actual scan target (current main).
    "linear",
)


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


def _scan_file(path: Path) -> list[tuple[int, str, str]]:
    """Return offending ``(line_no, line, matched_pattern)`` tuples for ``path``.

    Lines carrying the allowlist marker are skipped. Lines inside
    string literals or comments are still checked -- a stray keyword
    in a docstring of a watched file would catch, which is the desired
    behavior.
    """
    findings: list[tuple[int, str, str]] = []
    if not path.is_file():
        return findings
    text = path.read_text(encoding="utf-8")
    for lineno, line in enumerate(text.splitlines(), start=1):
        if ALLOWLIST_MARKER in line:
            continue
        lowered = line.lower()
        for pattern in FORBIDDEN_PATTERNS:
            if pattern in lowered:
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
