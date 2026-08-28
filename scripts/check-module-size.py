#!/usr/bin/env python3
"""Ratchet the size of `src/ouroboros` modules so god-modules can only shrink.

Why this exists: per Q00/ouroboros#1797, `OrchestratorRunner` reached 11,100
lines across 180 methods inside an 11,980-line module, and `parallel_executor.py`
reached 13,158. A 2026-08-01 measurement of `src/ouroboros` found 486 Python
files with a median of 297 lines -- but the 26 files above 2,000 lines hold
35.3% of all source. The distribution is not gradual; it is a small set of
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

3. The policy itself may only tighten. GRANDFATHERED, SOFT_CAP, RESEED_SLACK,
   EXCLUDED, SOURCE_ROOT, and MODULE_GLOB are editable in the same commit they
   gate, so measuring source against the *proposed* policy proves nothing on
   its own: a PR could grow a module and raise its budget together, add a new
   oversized module and grandfather it, or simply narrow the measured tree.
   The proposed policy is therefore compared with the base branch's: no key or
   exclusion may be added, no budget may increase, neither numeric limit may
   rise, and measurement scope must remain identical. Removals and decreases
   are the only permitted directions for debt and numeric limits.

Physical lines are counted, including blanks and comments. That is gameable by
writing longer lines. `line-length = 100` in pyproject.toml makes that awkward,
but it is not a hard bound -- `E501` is in the ruff ignore list, and the
formatter cannot split a long string literal or URL. The count is a proxy for
what costs a reviewer, not a tamper-proof metric; the base-branch comparison in
rule 3 is what actually makes the gate hard to walk around.

Run locally:
    python3 scripts/check-module-size.py                    # current state only
    python3 scripts/check-module-size.py --baseline-ref origin/main

CI:
    .github/workflows/module-size.yml runs this on every PR with
    the immutable PR base SHA, and on main pushes with the pre-push SHA.
    checkout uses fetch-depth: 0 so either predecessor is available.

Retiring an entry:
    When a grandfathered module falls to SOFT_CAP or below, delete its line
    from GRANDFATHERED entirely. It is then held by the universal cap and, by
    rule 3, can never be grandfathered again. That deletion is the unit of
    progress on #1797.
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent

# Unique discovery sentinel. Generic names such as SOURCE_ROOT or
# GRANDFATHERED may legitimately appear in unrelated repository utilities.
MODULE_SIZE_POLICY_ID = "ouroboros.module-size.v1"

# Measurement scope is policy, not implementation detail. Both literals are
# compared with the predecessor before current source is measured, so a PR
# cannot make the gate green by narrowing the root or file selection.
SOURCE_ROOT = "src/ouroboros"
MODULE_GLOB = "*.py"

# Universal ceiling for any module not listed below.
SOFT_CAP = 2000

# How far a grandfathered module may drop below its budget before the budget
# must be re-seeded. Small enough that real shrinkage gets locked in; large
# enough that routine edits do not churn this table.
RESEED_SLACK = 200

# Modules that already exceeded SOFT_CAP when this gate was introduced
# (2026-08-01, main @ db9a19d05). Each may shrink, never grow. Delete an entry
# once it reaches SOFT_CAP; do not add new ones.
GRANDFATHERED: dict[str, int] = {
    "src/ouroboros/orchestrator/parallel_executor.py": 13158,
    "src/ouroboros/orchestrator/runner.py": 12144,
    "src/ouroboros/auto/pipeline.py": 3306,
    "src/ouroboros/cli/commands/setup.py": 5225,
    "src/ouroboros/orchestrator/codex_cli_runtime.py": 4140,
    "src/ouroboros/mcp/tools/authoring_handlers.py": 3722,  # was 3780
    "src/ouroboros/orchestrator/execution_authority.py": 3449,
    "src/ouroboros/mcp/tools/subagent.py": 2639,  # was 3163
    "src/ouroboros/persistence/event_store.py": 3102,
    "src/ouroboros/cli/commands/plugin.py": 3053,
    "src/ouroboros/mcp/job_manager.py": 2950,
    "src/ouroboros/mcp/tools/execution_handlers.py": 2869,
    "src/ouroboros/bigbang/seed_generator.py": 2637,
    "src/ouroboros/mcp/server/adapter.py": 2612,
    "src/ouroboros/auto/interview_driver.py": 2496,
    "src/ouroboros/mcp/tools/auto_handler.py": 2488,
    "src/ouroboros/config/loader.py": 2432,
    "src/ouroboros/plugin/firewall.py": 2389,
    "src/ouroboros/evaluation/detector.py": 2362,
    "src/ouroboros/orchestrator/mcp_tools.py": 2296,
    "src/ouroboros/auto/answerer.py": 2287,
    "src/ouroboros/mcp/tools/evaluation_handlers.py": 2268,
    "src/ouroboros/orchestrator/capabilities/__init__.py": 2232,
    "src/ouroboros/mcp/tools/job_handlers.py": 2208,
    "src/ouroboros/orchestrator/adapter.py": 2125,
    "src/ouroboros/evolution/loop.py": 2099,
}

# Generated files carry no review cost and are not authored by hand.
EXCLUDED = frozenset({"src/ouroboros/_version.py"})


def _line_count(path: Path) -> int:
    """Return physical lines, counting a final unterminated line."""
    return len(path.read_text(encoding="utf-8").splitlines())


def _relative(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _measure() -> dict[str, int]:
    source_root = REPO_ROOT / SOURCE_ROOT
    return {
        rel: _line_count(path)
        for path in sorted(source_root.rglob(MODULE_GLOB))
        if (rel := _relative(path)) not in EXCLUDED
    }


class BaselineUnavailable(Exception):
    """Raised when a requested baseline ref cannot be read."""


Policy = tuple[dict[str, int], int, int, frozenset[str], str, str]
_POLICY_SENTINEL = "ouroboros.module-size.v1"
_POLICY_NAMES = frozenset(
    {
        "MODULE_SIZE_POLICY_ID",
        "GRANDFATHERED",
        "SOFT_CAP",
        "RESEED_SLACK",
        "EXCLUDED",
        "SOURCE_ROOT",
        "MODULE_GLOB",
    }
)


def _policy_literals(source: str) -> Policy | None:
    """Extract every enforcement-semantic literal from this script.

    Parsed with ``ast`` rather than imported: the baseline comes from another
    commit, and a gate must never execute the code it is judging.
    """
    module = ast.parse(source)
    assignments: dict[str, ast.expr] = {}
    for node in module.body:
        targets = (
            [node.target]
            if isinstance(node, ast.AnnAssign)
            else node.targets
            if isinstance(node, ast.Assign)
            else []
        )
        for target in targets:
            if not isinstance(target, ast.Name) or target.id not in _POLICY_NAMES:
                continue
            if node.value is None:
                continue
            if target.id in assignments:
                raise BaselineUnavailable(f"policy assigns {target.id} more than once")
            assignments[target.id] = node.value

    sentinel_node = assignments.get("MODULE_SIZE_POLICY_ID")
    if sentinel_node is None:
        return None
    try:
        sentinel = ast.literal_eval(sentinel_node)
    except (TypeError, ValueError) as exc:
        raise BaselineUnavailable("MODULE_SIZE_POLICY_ID is not a literal") from exc
    if sentinel != _POLICY_SENTINEL:
        raise BaselineUnavailable(f"unknown module-size policy id {sentinel!r}")

    found: dict[str, object] = {}
    for name, value in assignments.items():
        if name == "MODULE_SIZE_POLICY_ID":
            continue
        try:
            if (
                name == "EXCLUDED"
                and isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "frozenset"
                and len(value.args) == 1
                and not value.keywords
            ):
                found[name] = frozenset(ast.literal_eval(value.args[0]))
            else:
                found[name] = ast.literal_eval(value)
        except (TypeError, ValueError) as exc:
            raise BaselineUnavailable(f"{name} is not a literal in the policy copy") from exc
    table = found.get("GRANDFATHERED")
    cap = found.get("SOFT_CAP")
    slack = found.get("RESEED_SLACK")
    excluded = found.get("EXCLUDED")
    source_root = found.get("SOURCE_ROOT")
    module_glob = found.get("MODULE_GLOB")
    if (
        not isinstance(table, dict)
        or not isinstance(cap, int)
        or not isinstance(slack, int)
        or not isinstance(excluded, frozenset)
        or not all(isinstance(item, str) for item in excluded)
        or not isinstance(source_root, str)
        or not source_root
        or not isinstance(module_glob, str)
        or not module_glob
    ):
        raise BaselineUnavailable(
            "baseline copy does not define GRANDFATHERED, SOFT_CAP, "
            "RESEED_SLACK, EXCLUDED, SOURCE_ROOT, and MODULE_GLOB as literals"
        )
    return (
        {str(k): int(v) for k, v in table.items()},
        cap,
        slack,
        frozenset(excluded),
        source_root,
        module_glob,
    )


def _baseline_policy(ref: str) -> Policy | None:
    """Find the unique policy recorded at ``ref`` independent of its current path.

    A ref that does not resolve is an error: a silently skipped comparison is
    exactly the hole this check exists to close. A ref that resolves but has no
    script declaring the policy sentinel is the gate's own introducing PR, which has
    no predecessor policy to tighten. Scanning the predecessor's scripts tree
    instead of using the current ``__file__`` path prevents a rename plus
    workflow edit from masquerading as a new gate.
    """
    try:
        subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        raise BaselineUnavailable(
            f"baseline ref {ref!r} does not resolve; ensure the checkout uses "
            "fetch-depth: 0 and that the ref has been fetched"
        ) from exc
    try:
        tree = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", "-z", ref, "--", "scripts"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        raise BaselineUnavailable(f"could not inspect scripts at {ref!r}: {exc}") from exc

    candidates: list[tuple[str, Policy]] = []
    for raw_path in tree.stdout.split(b"\0"):
        if not raw_path or not raw_path.endswith(b".py"):
            continue
        try:
            path = raw_path.decode("utf-8")
        except UnicodeError as exc:
            raise BaselineUnavailable("baseline scripts path is not UTF-8") from exc
        try:
            completed = subprocess.run(
                ["git", "show", f"{ref}:{path}"],
                cwd=REPO_ROOT,
                check=True,
                capture_output=True,
                encoding="utf-8",
            )
            policy = _policy_literals(completed.stdout)
        except (UnicodeError, subprocess.CalledProcessError, OSError, SyntaxError) as exc:
            raise BaselineUnavailable(f"could not inspect policy candidate {path!r}") from exc
        if policy is not None:
            candidates.append((path, policy))

    if not candidates:
        return None
    if len(candidates) != 1:
        paths = ", ".join(path for path, _ in candidates)
        raise BaselineUnavailable(f"baseline contains multiple module-size policies: {paths}")
    return candidates[0][1]


def _validate_current_policy_location() -> None:
    """Require one current policy, in the script that is actually executing."""
    scripts_root = REPO_ROOT / "scripts"
    try:
        paths = sorted(scripts_root.rglob("*.py"))
    except OSError as exc:
        raise BaselineUnavailable("could not enumerate current scripts tree") from exc

    candidates: list[Path] = []
    for path in paths:
        try:
            policy = _policy_literals(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, SyntaxError) as exc:
            raise BaselineUnavailable(
                f"could not inspect current policy candidate {_relative(path)!r}"
            ) from exc
        if policy is not None:
            candidates.append(path.resolve())

    if len(candidates) != 1:
        rendered = ", ".join(_relative(path) for path in candidates) or "none"
        raise BaselineUnavailable(
            f"current scripts tree must contain exactly one module-size policy; found {rendered}"
        )
    active = Path(__file__).resolve()
    if candidates[0] != active:
        raise BaselineUnavailable(
            "the unique current module-size policy is not the script being executed"
        )


def _policy_regressions(
    baseline: Policy,
) -> tuple[
    list[str],
    list[tuple[str, int, int]],
    tuple[int, int] | None,
    tuple[int, int] | None,
    list[str],
    list[tuple[str, str, str]],
]:
    """Compare the proposed policy against the baseline's."""
    (
        baseline_table,
        baseline_cap,
        baseline_slack,
        baseline_excluded,
        baseline_source_root,
        baseline_module_glob,
    ) = baseline
    added = sorted(set(GRANDFATHERED) - set(baseline_table))
    raised = sorted(
        (key, GRANDFATHERED[key], baseline_table[key])
        for key in set(GRANDFATHERED) & set(baseline_table)
        if GRANDFATHERED[key] > baseline_table[key]
    )
    cap_raised = (SOFT_CAP, baseline_cap) if baseline_cap < SOFT_CAP else None
    slack_raised = (RESEED_SLACK, baseline_slack) if baseline_slack < RESEED_SLACK else None
    exclusions_added = sorted(EXCLUDED - baseline_excluded)
    scope_changes = [
        (name, previous, proposed)
        for name, previous, proposed in (
            ("SOURCE_ROOT", baseline_source_root, SOURCE_ROOT),
            ("MODULE_GLOB", baseline_module_glob, MODULE_GLOB),
        )
        if proposed != previous
    ]
    return added, raised, cap_raised, slack_raised, exclusions_added, scope_changes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline-ref",
        default=None,
        help=(
            "Git ref holding the policy this run must not loosen (CI passes "
            "origin/main). Omitted: only the current source snapshot is checked."
        ),
    )
    args = parser.parse_args(argv)

    try:
        _validate_current_policy_location()
    except BaselineUnavailable as exc:
        sys.stderr.write(f"module-size: FAILED -- {exc}.\n")
        return 1

    source_root = REPO_ROOT / SOURCE_ROOT
    if not source_root.is_dir():
        sys.stderr.write(
            f"module-size: FAILED -- source root {SOURCE_ROOT} does not exist.\n"
            "This gate cannot verify anything; fix the checkout or update SOURCE_ROOT.\n"
        )
        return 1

    added: list[str] = []
    raised: list[tuple[str, int, int]] = []
    cap_raised: tuple[int, int] | None = None
    slack_raised: tuple[int, int] | None = None
    exclusions_added: list[str] = []
    scope_changes: list[tuple[str, str, str]] = []
    baseline_policy: Policy | None = None
    if args.baseline_ref is not None:
        try:
            baseline = _baseline_policy(args.baseline_ref)
        except BaselineUnavailable as exc:
            sys.stderr.write(
                f"module-size: FAILED -- {exc}.\nRefusing to pass without the "
                "baseline policy: an unchecked table can grandfather anything.\n"
            )
            return 1
        if baseline is None:
            print(
                f"module-size: no policy at {args.baseline_ref} -- this is the "
                "gate's introducing commit; skipping the tightening check."
            )
        else:
            baseline_policy = baseline
            (
                added,
                raised,
                cap_raised,
                slack_raised,
                exclusions_added,
                scope_changes,
            ) = _policy_regressions(baseline)

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
    inexact_reseeds: list[tuple[str, int, int, int]] = []
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

    if baseline_policy is not None:
        baseline_table = baseline_policy[0]
        for rel, previous_budget in sorted(baseline_table.items()):
            count = sizes.get(rel)
            proposed_budget = GRANDFATHERED.get(rel)
            if (
                count is None
                or proposed_budget is None
                or count <= SOFT_CAP
                or previous_budget - count <= RESEED_SLACK
                or proposed_budget == count
            ):
                continue
            inexact_reseeds.append((rel, count, previous_budget, proposed_budget))

        # The predecessor comparison is stronger than the snapshot check: it
        # also catches a partial decrease that leaves up to RESEED_SLACK lines
        # available to regrow. Avoid printing two remediations for the same path.
        inexact_paths = {rel for rel, *_ in inexact_reseeds}
        needs_reseed = [item for item in needs_reseed if item[0] not in inexact_paths]

    if (
        not (
            vanished
            or over_cap
            or over_budget
            or needs_reseed
            or inexact_reseeds
            or retired
            or added
            or raised
            or exclusions_added
            or scope_changes
        )
        and cap_raised is None
        and slack_raised is None
    ):
        print(
            f"module-size: OK ({len(sizes)} modules, cap {SOFT_CAP}, "
            f"{len(GRANDFATHERED)} grandfathered)"
        )
        return 0

    write = sys.stderr.write
    write("module-size: FAILED\n\n")

    if cap_raised is not None:
        proposed, previous = cap_raised
        write(
            f"SOFT_CAP was raised from {previous} to {proposed}. The cap is the "
            "policy this\ngate enforces; raising it in the commit it governs "
            "would let any module\nthrough. It may only be lowered.\n\n"
        )

    if slack_raised is not None:
        proposed, previous = slack_raised
        write(
            f"RESEED_SLACK was raised from {previous} to {proposed}. Raising the "
            "slack\nreopens reclaimed headroom, so it may only be lowered.\n\n"
        )

    if exclusions_added:
        write(
            "Paths were added to EXCLUDED. Exclusions remove modules from all size\n"
            "enforcement and may only be removed, never added:\n"
        )
        for rel in exclusions_added:
            write(f"  {rel}\n")
        write("\n")

    if scope_changes:
        write(
            "Measurement scope changed. SOURCE_ROOT and MODULE_GLOB define which\n"
            "modules the gate can see and must match the predecessor exactly:\n"
        )
        for name, previous, proposed in scope_changes:
            write(f"  {name}: {previous!r} -> {proposed!r}\n")
        write("\n")

    if added:
        write(
            "Modules were added to GRANDFATHERED. The set is closed (frozen "
            "2026-08-01):\nentries may only be removed. Split the module, or "
            "keep it under the cap --\nadding it here would grandfather new "
            "debt, and would also let a module that\nalready earned its way "
            "out come back in:\n"
        )
        for rel in added:
            write(f"  {rel}: {GRANDFATHERED[rel]}\n")
        write("\n")

    if raised:
        write(
            "Grandfathered budgets were raised. A budget may only decrease -- "
            "raising it\nin the same commit that grows the module is exactly "
            "the bypass this gate\nexists to prevent:\n"
        )
        for rel, proposed, previous in raised:
            write(f"  {rel}: {previous} -> {proposed} (+{proposed - previous})\n")
        write("\n")

    if vanished:
        write(
            "Grandfathered modules no longer exist. GRANDFATHERED is a closed set, so\n"
            "their budget cannot be transferred to a renamed or moved path. Split or\n"
            f"reduce every replacement module to {SOFT_CAP} lines or fewer, then remove\n"
            "the stale entry from scripts/check-module-size.py in the same PR:\n"
        )
        for rel in vanished:
            write(f"  gone: {rel}\n")
        write("\n")

    if over_cap:
        write(
            f"Modules over the {SOFT_CAP}-line cap that are not grandfathered.\n"
            "Split the module instead of adding an entry -- GRANDFATHERED is a closed\n"
            "set frozen at 2026-08-01 and must only ever shrink (see #1797):\n"
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

    if inexact_reseeds:
        write(
            f"Grandfathered modules shrank by more than {RESEED_SLACK} lines relative to\n"
            "the predecessor, but their replacement budgets do not exactly match the\n"
            "measured sizes. Partial re-seeds preserve headroom that a later PR can spend;\n"
            "use these exact GRANDFATHERED entries instead:\n"
        )
        for rel, count, previous_budget, proposed_budget in inexact_reseeds:
            write(
                f'    "{rel}": {count},   # predecessor {previous_budget}, '
                f"proposed {proposed_budget}\n"
            )
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
