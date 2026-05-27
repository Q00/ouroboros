#!/usr/bin/env python3
"""Enforce RFC #1256 §I5 — PRs touching ``src/ouroboros/auto/`` must
include an R-run comparison section in the PR body.

The check is intentionally minimal: it does not parse the table, run any
benchmarks, or compare against historical baselines. It only verifies the
**presence** of the structured section so reviewers see the per-round
wall-clock data inline. Numerical regression detection lives in #1258
follow-up tooling.

The check runs in two contexts:

1. **Pull request CI** — receives the PR body via ``GITHUB_EVENT_PATH`` and
   verifies the R-run section + filled-in table when changed paths include
   ``src/ouroboros/auto/``.
2. **Local dry-run** — invoked manually as
   ``python3 scripts/check-auto-perf-budget.py --body <file>``; used when
   developing the workflow itself.

Exit codes:
    0 — compliant or not applicable
    1 — applicable but the PR body is missing the R-run section
    2 — unexpected error reading inputs OR changed-path discovery failed
        (the gate fails closed: if we cannot enumerate changed files, we
        cannot prove the PR is non-applicable, so we refuse to silently
        return 0 — this is the bot-review blocker that flagged the
        prior fail-open behavior)
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

AUTO_PATH_PREFIX = "src/ouroboros/auto/"
SECTION_HEADER = "## R-run comparison"
REQUIRED_METRIC_TOKENS = (
    "Rounds completed",
    "Per-round wall-clock",
    "Terminal reason",
    # Bot review on commit c860fa6b (req_1779887321_129) flagged that
    # the parser must enforce every metric row the template asks for.
    # ``EventStore event count`` was in the template at
    # ``.github/PULL_REQUEST_TEMPLATE.md`` but missing here, so an
    # applicable auto PR could leave it blank and still pass. Adding
    # the token closes that bypass; the template and parser are now
    # one list.
    "EventStore event count",
)


class ChangedPathsUnavailable(RuntimeError):
    """Raised when changed-path discovery cannot produce a definitive
    list. The caller must treat this as fail-closed (exit 2) rather
    than as "no auto/ files changed" — the latter is a silent bypass."""


def _changed_paths_from_event(event: dict) -> list[str]:
    """Return the list of changed paths for the PR described by ``event``.

    Uses ``git diff --name-only`` against the merge base; this is more
    accurate than walking the GitHub API and works for any fork that
    mirrors the upstream main branch.

    Fails closed: if the diff cannot be enumerated, raises
    :class:`ChangedPathsUnavailable` so the caller surfaces the
    discovery failure as a CI error (exit 2) instead of silently
    treating the PR as non-applicable. The earlier fail-open behavior
    was flagged as a guard bypass — a checkout/ref layout failure
    would have disabled the budget requirement without anyone noticing.
    """

    pr = event.get("pull_request") or {}
    base = (pr.get("base") or {}).get("ref") or "main"

    try:
        out = subprocess.run(
            ["git", "diff", "--name-only", f"origin/{base}...HEAD"],
            capture_output=True,
            check=True,
            text=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        raise ChangedPathsUnavailable(f"git diff against origin/{base} failed: {exc}") from exc

    return [line for line in out.stdout.splitlines() if line.strip()]


REQUIRED_COMPARISON_COLUMNS = 3  # Baseline | This PR | Ratio


def _section_present(body: str) -> bool:
    if SECTION_HEADER not in body:
        return False
    # Every required metric row must have ALL three comparison-bearing
    # cells (Baseline, This PR, Ratio) populated. Accepting a row where
    # only one cell is non-empty — e.g. `Baseline=TBD` with the PR and
    # Ratio columns blank — would let an applicable PR pass without
    # supplying any actual side-by-side measurement, which is the exact
    # process bypass §I5 exists to close (bot review on commit
    # 074e24bd flagged this as the remaining blocker after the prior
    # `any() → all()` patch went only halfway).
    #
    # Authors who genuinely want to mark a row as "not applicable" (e.g.
    # substrate-only PRs) must fill every cell — `N/A | N/A | n/a` is
    # accepted; one filled cell with two blanks is not.
    section = body.split(SECTION_HEADER, 1)[1]
    for token in REQUIRED_METRIC_TOKENS:
        if token not in section:
            return False
        line = next((row for row in section.splitlines() if token in row), "")
        # Markdown table row: `| Metric ... | Baseline | This PR | Ratio |`.
        # The leading and trailing pipes produce empty boundary cells;
        # the metric name sits at index 1; the three comparison cells
        # follow at index 2..4. We require every comparison cell to be
        # non-empty.
        cells = [c.strip() for c in line.split("|")][2:-1]
        if len(cells) < REQUIRED_COMPARISON_COLUMNS:
            return False
        if not all(cells[:REQUIRED_COMPARISON_COLUMNS]):
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--body",
        type=Path,
        help="Optional path to a PR body file (local dry-run).",
    )
    args = parser.parse_args()

    # Determine changed paths + PR body.
    body = ""
    changed: list[str] = []

    if args.body is not None:
        body = args.body.read_text(encoding="utf-8")
        # Local dry-run: read changed paths from git working tree vs
        # main. A dev-mode failure is informative, not load-bearing —
        # we still surface it to stderr so the operator sees why the
        # decision was made, but we exit 2 (fail closed) to match the
        # CI contract: indeterminate inputs never produce a silent
        # pass.
        try:
            out = subprocess.run(
                ["git", "diff", "--name-only", "origin/main...HEAD"],
                capture_output=True,
                check=True,
                text=True,
            )
            changed = [line for line in out.stdout.splitlines() if line.strip()]
        except (subprocess.CalledProcessError, OSError) as exc:
            print(
                "check-auto-perf-budget: local --body dry-run could not "
                f"enumerate changed paths via git: {exc}. "
                "Fix the local checkout (e.g., `git fetch origin main`) "
                "or run in CI where origin/main is always available.",
                file=sys.stderr,
            )
            return 2
    else:
        event_path = os.environ.get("GITHUB_EVENT_PATH")
        if not event_path or not Path(event_path).is_file():
            print(
                "check-auto-perf-budget: no GITHUB_EVENT_PATH and no --body; nothing to check.",
                file=sys.stderr,
            )
            return 0
        try:
            event = json.loads(Path(event_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"check-auto-perf-budget: failed to parse event: {exc}", file=sys.stderr)
            return 2
        pr = event.get("pull_request") or {}
        body = pr.get("body") or ""
        try:
            changed = _changed_paths_from_event(event)
        except ChangedPathsUnavailable as exc:
            print(
                "check-auto-perf-budget: could not enumerate changed "
                f"paths — {exc}. Refusing to silently treat the PR as "
                f"non-applicable; ensure the workflow checks out with "
                "`fetch-depth: 0` and that origin/main is reachable.",
                file=sys.stderr,
            )
            return 2

    applies = any(path.startswith(AUTO_PATH_PREFIX) for path in changed)
    if not applies:
        print(
            "check-auto-perf-budget: PR does not touch "
            f"`{AUTO_PATH_PREFIX}` — section not required."
        )
        return 0

    if _section_present(body):
        print(
            "check-auto-perf-budget: PR touches "
            f"`{AUTO_PATH_PREFIX}` and includes a filled R-run comparison "
            "section — OK."
        )
        return 0

    print(
        "check-auto-perf-budget: PR touches "
        f"`{AUTO_PATH_PREFIX}` but the R-run comparison section in the PR "
        "body is missing or has no filled metrics.\n"
        "Per RFC #1256 §I5, every PR to `src/ouroboros/auto/` must include "
        "a per-round wall-clock comparison against the latest canonical "
        "baseline. See `.github/PULL_REQUEST_TEMPLATE.md` for the table "
        "format, or capture a fresh R-run with:\n"
        "    OUROBOROS_RUN_CANONICAL=1 uv run pytest tests/canonical/ "
        "-k cli-todo -v",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
