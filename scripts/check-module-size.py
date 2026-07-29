#!/usr/bin/env python3
"""Ratchet the size of `src/ouroboros` modules so god-modules can only shrink.

Why this exists: per Q00/ouroboros#1797, `OrchestratorRunner` reached 11,100
lines across 180 methods inside an 11,980-line module, and `parallel_executor.py`
reached 13,111. A 2026-07-29 measurement of `src/ouroboros` found 480 Python
files with a median of 292 lines -- but the 25 files above 2,000 lines hold
34.4% of all source. The distribution is not gradual; it is a small set of
gravity wells that absorb every new concern because no other home is obvious.

Nothing stops that today. #1769 added ~324 lines and nine private methods to
`runner.py` while under review, and no gate noticed. This script is the
mechanical floor under the #1797 extraction work: it does not split anything,
it only guarantees the numbers cannot go back up between extraction PRs.

The rule has two halves:

1. Any module NOT in GRANDFATHERED must stay at or under SOFT_CAP lines.
   The cap sits in a natural gap in the distribution -- no file in the
   repository is currently between 1,800 and 2,000 lines -- so it cannot bite
   an ordinary module by accident.

2. A module IN GRANDFATHERED must stay at or under its recorded budget, and
   must RE-SEED once it drops more than RESEED_SLACK below it. The re-seed
   half is what makes this a ratchet rather than a static cap: without it, a
   PR that removes 500 lines leaves the old headroom available for the next
   contributor to spend. The failure message prints the exact replacement
   line, so satisfying it is a one-line edit in the same PR.

Physical lines are counted, including blanks and comments. That is gameable in
principle by writing longer lines, but `line-length = 100` in pyproject.toml
already bounds that, and physical length is what actually costs a reviewer.

Run locally:
    python3 scripts/check-module-size.py

CI:
    .github/workflows/module-size.yml runs this on every PR.

Retiring an entry:
    When a grandfathered module falls to SOFT_CAP or below, delete its line
    from GRANDFATHERED entirely. It is then held by the universal cap and can
    never be grandfathered again. That deletion is the unit of progress on
    #1797.
"""

from __future__ import annotations

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_ROOT = REPO_ROOT / "src" / "ouroboros"

# Universal ceiling for any module not listed below.
SOFT_CAP = 2000

# How far a grandfathered module may drop below its budget before the budget
# must be re-seeded. Small enough that real shrinkage gets locked in; large
# enough that routine edits do not churn this table.
RESEED_SLACK = 200

# Modules that already exceeded SOFT_CAP when this gate was introduced
# (2026-07-29, main @ ffc94e8a7). Each may shrink, never grow. Delete an entry
# once it reaches SOFT_CAP; do not add new ones.
GRANDFATHERED: dict[str, int] = {
    "src/ouroboros/orchestrator/parallel_executor.py": 13111,
    "src/ouroboros/orchestrator/runner.py": 11980,
    "src/ouroboros/auto/pipeline.py": 5204,
    "src/ouroboros/mcp/tools/authoring_handlers.py": 3800,
    "src/ouroboros/cli/commands/setup.py": 3522,
    "src/ouroboros/orchestrator/execution_authority.py": 3449,
    "src/ouroboros/orchestrator/codex_cli_runtime.py": 3259,
    "src/ouroboros/mcp/tools/subagent.py": 3163,
    "src/ouroboros/cli/commands/plugin.py": 3053,
    "src/ouroboros/mcp/job_manager.py": 2950,
    "src/ouroboros/mcp/tools/execution_handlers.py": 2880,
    "src/ouroboros/persistence/event_store.py": 2699,
    "src/ouroboros/auto/interview_driver.py": 2496,
    "src/ouroboros/mcp/tools/auto_handler.py": 2479,
    "src/ouroboros/config/loader.py": 2400,
    "src/ouroboros/plugin/firewall.py": 2389,
    "src/ouroboros/evaluation/detector.py": 2362,
    "src/ouroboros/mcp/server/adapter.py": 2336,
    "src/ouroboros/orchestrator/mcp_tools.py": 2296,
    "src/ouroboros/auto/answerer.py": 2287,
    "src/ouroboros/mcp/tools/evaluation_handlers.py": 2268,
    "src/ouroboros/orchestrator/capabilities/__init__.py": 2232,
    "src/ouroboros/mcp/tools/job_handlers.py": 2208,
    "src/ouroboros/orchestrator/adapter.py": 2125,
    "src/ouroboros/evolution/loop.py": 2088,
}

# Generated files carry no review cost and are not authored by hand.
EXCLUDED = frozenset({"src/ouroboros/_version.py"})


def _line_count(path: Path) -> int:
    """Return physical lines, counting a final unterminated line."""
    return len(path.read_text(encoding="utf-8").splitlines())


def _relative(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _measure() -> dict[str, int]:
    return {
        rel: _line_count(path)
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        if (rel := _relative(path)) not in EXCLUDED
    }


def main() -> int:
    if not SOURCE_ROOT.is_dir():
        sys.stderr.write(
            f"module-size: FAILED -- source root {_relative(SOURCE_ROOT)} does not exist.\n"
            "This gate cannot verify anything; fix the checkout or update SOURCE_ROOT.\n"
        )
        return 1

    sizes = _measure()

    # A grandfathered path that vanished means the entry was renamed, moved, or
    # deleted without updating this table. Staying silent would drop the cap on
    # whatever the module became -- the exact failure mode this gate exists to
    # prevent -- so it is a hard error, mirroring ANCHOR_FILES in
    # scripts/check-auto-boundary.py.
    vanished = sorted(set(GRANDFATHERED) - set(sizes))
    over_cap: list[tuple[str, int]] = []
    over_budget: list[tuple[str, int, int]] = []
    needs_reseed: list[tuple[str, int, int]] = []
    retired: list[tuple[str, int]] = []

    for rel, count in sorted(sizes.items()):
        budget = GRANDFATHERED.get(rel)
        if budget is None:
            if count > SOFT_CAP:
                over_cap.append((rel, count))
            continue
        if count > budget:
            over_budget.append((rel, count, budget))
        elif count <= SOFT_CAP:
            retired.append((rel, count))
        elif budget - count > RESEED_SLACK:
            needs_reseed.append((rel, count, budget))

    if not (vanished or over_cap or over_budget or needs_reseed or retired):
        print(
            f"module-size: OK ({len(sizes)} modules, cap {SOFT_CAP}, "
            f"{len(GRANDFATHERED)} grandfathered)"
        )
        return 0

    write = sys.stderr.write
    write("module-size: FAILED\n\n")

    if vanished:
        write(
            "Grandfathered modules no longer exist. If you renamed, moved, or split\n"
            "them, update GRANDFATHERED in scripts/check-module-size.py in the same PR\n"
            "so the cap follows the code:\n"
        )
        for rel in vanished:
            write(f"  gone: {rel}\n")
        write("\n")

    if over_cap:
        write(
            f"Modules over the {SOFT_CAP}-line cap that are not grandfathered.\n"
            "Split the module instead of adding an entry -- GRANDFATHERED is a closed\n"
            "set frozen at 2026-07-29 and must only ever shrink (see #1797):\n"
        )
        for rel, count in over_cap:
            write(f"  {rel}: {count} lines (+{count - SOFT_CAP} over cap)\n")
        write("\n")

    if over_budget:
        write(
            "Grandfathered modules grew. These are the modules #1797 exists to shrink;\n"
            "put the new code in a new module instead of extending them:\n"
        )
        for rel, count, budget in over_budget:
            write(f"  {rel}: {count} lines, budget {budget} (+{count - budget})\n")
        write("\n")

    if needs_reseed:
        write(
            f"Grandfathered modules shrank by more than {RESEED_SLACK} lines. Lock the gain in\n"
            "by replacing these lines in GRANDFATHERED (scripts/check-module-size.py),\n"
            "otherwise the reclaimed headroom stays available to spend:\n"
        )
        for rel, count, budget in needs_reseed:
            write(f'    "{rel}": {count},   # was {budget}\n')
        write("\n")

    if retired:
        write(
            f"Grandfathered modules reached the {SOFT_CAP}-line cap. Delete these entries\n"
            "from GRANDFATHERED entirely -- the universal cap holds them from now on:\n"
        )
        for rel, count in retired:
            write(f"  {rel}: {count} lines\n")
        write("\n")

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
