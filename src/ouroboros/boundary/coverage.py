"""Criterion coverage before the worker starts: the replacement call.

One replacement call (product policy only), a step of
``run_wiring.prepare_check_package`` before any worker starts. After per-check
admission (``boundary/per_check.py``), every criterion without an admitted
check is a replacement target (``replacement_targets``), whatever reason the
constructor gave for leaving it uncovered, with a plain-text reason
(``why_excluded``) that names no case value. The replacement checks are merged
with the admitted checks of the earlier version (``merge_replacement``) into a
new package that goes through the same reference check, seal and admission.
Reasons are descriptive only; none of them changes which criteria are targets.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ouroboros.boundary.oracle import is_oracle_file
from ouroboros.boundary.oracle_build import DECLARED_NOT_EXECUTABLE, assemble_package
from ouroboros.boundary.package import (
    CheckPackage,
    CheckRole,
    CheckSpec,
    PackageFile,
    canonical_json_bytes,
    sha256_bytes,
)
from ouroboros.boundary.per_check import (
    HELD_OUT_NOT_DISCRIMINATING,
    INDETERMINATE_ON_BASE,
    NO_ADMITTED_REPRODUCTION_CHECK,
    PRESERVATION_FAILS_ON_BASE,
    REPRO_PASSES_ON_BASE,
    criteria_without_admitted_check,
)

if TYPE_CHECKING:
    from ouroboros.boundary.oracle import OracleSpec
    from ouroboros.core.seed import Seed

REPLACEMENT_CONFLICT = "replacement_conflict"
_REASON_CHARS = 160

_WHY: dict[str, str] = {
    REPRO_PASSES_ON_BASE: (
        "its reproduction check passes on the base code, so it does not reproduce the bug"
    ),
    PRESERVATION_FAILS_ON_BASE: (
        "its preservation check fails on the base code, so it does not describe behavior "
        "the base code already has"
    ),
    HELD_OUT_NOT_DISCRIMINATING: (
        "every held-out case of its reproduction check already passes on the base code, "
        "so the check cannot tell a fix from no fix; write at least one held-out case "
        "that the base code fails"
    ),
    INDETERMINATE_ON_BASE: (
        "its check decided nothing on the base code (it failed before reaching its own "
        "assertion, timed out, could not start, or flooded its output); write a check "
        "that runs to its own assertion on the base code"
    ),
    NO_ADMITTED_REPRODUCTION_CHECK: (
        "its reproduction check was excluded on the base code (it passed there, showed "
        "nothing there, or could not tell a fix from no fix); only a preservation check "
        "remains, which cannot show the fix"
    ),
    "construction_timeout": "the earlier construction ran out of time before its check",
    "reference_unavailable": "the reference implementation written for its oracle did not run",
    "reference_contradicts_stated_case": (
        "the reference implementation written for its oracle did not reproduce a case "
        "declared as stated in the Seed"
    ),
    "oracle_inconsistent": (
        "every case of its oracle disagreed with the reference implementation written for it"
    ),
    "constructor_omitted": "the earlier reply wrote no check and no reason for it",
    DECLARED_NOT_EXECUTABLE: "the earlier reply listed it as not checkable by running code",
}


def why_excluded(reason: str) -> str:
    """Plain-text reason a criterion has no admitted check (never a case value)."""
    known = _WHY.get(reason)
    if known is not None:
        return known
    text = " ".join(reason.split())[:_REASON_CHARS]
    return f"the earlier reply left it without a check ({text})"


def _links(check: CheckSpec) -> set[str]:
    return {link.criterion_key for link in check.assertions}


def _parts(
    package: CheckPackage, keep: Any
) -> tuple[list[tuple[OracleSpec, CheckRole]], list[CheckSpec], list[PackageFile]]:
    """Oracles, script checks and script files of ``package`` for which ``keep(check)`` holds."""
    roles = {check.check_id: check.role for check in package.checks}
    oracles = [
        (spec, roles[spec.check_id])
        for spec in package.oracles
        if keep(next(c for c in package.checks if c.check_id == spec.check_id))
    ]
    oracle_ids = {spec.check_id for spec in package.oracles}
    scripts = [
        check for check in package.checks if check.check_id not in oracle_ids and keep(check)
    ]
    paths = {arg for check in scripts for arg in check.argv[1:]}
    files = [item for item in package.files if not is_oracle_file(item.path) and item.path in paths]
    return oracles, scripts, files


def replacement_targets(package: CheckPackage, excluded: Mapping[str, str]) -> dict[str, str]:
    """Every criterion of an admitted package without an admitted check, with the reason.

    ``excluded`` is the admission's ``excluded_checks`` (check id to its
    recorded exclusion reason). Criteria the
    constructor left uncovered are targets too, with its reason, whatever
    that reason says.
    """
    targets = {item.criterion_key: item.reason for item in package.uncovered}
    targets.update(criteria_without_admitted_check(package, excluded))
    return {key: targets[key] for key in package.criterion_keys if key in targets}


def merge_replacement(
    package: CheckPackage,
    excluded: Mapping[str, str],
    replacement: CheckPackage,
    targets: Mapping[str, str],
    seed: Seed,
) -> tuple[CheckPackage, dict[str, str]]:
    """The admitted checks of ``package`` plus ``replacement``'s checks for ``targets``.

    Returns the merged package and, per target, why it is still uncovered
    (a target the replacement did not link, or whose script's file path
    collides with a kept check's file: ``replacement_conflict``). Excluded
    checks are not carried over. Check ids never collide: ``assemble_package``
    re-mints every id from the merged structure.
    """
    excluded_ids = set(excluded)
    kept_oracles, kept_scripts, kept_files = _parts(
        package, lambda check: check.check_id not in excluded_ids
    )
    taken_paths = {item.path for item in kept_files}
    target_keys = set(targets)
    new_oracles, new_scripts, new_files = _parts(
        replacement, lambda check: bool(_links(check)) and _links(check) <= target_keys
    )
    still: dict[str, str] = {}
    oracles = [*kept_oracles, *new_oracles]
    scripts = list(kept_scripts)
    files = list(kept_files)
    by_path = {item.path: item for item in new_files}
    for check in new_scripts:
        paths = set(check.argv[1:])
        if paths & taken_paths:
            for key in _links(check):
                still[key] = REPLACEMENT_CONFLICT
            continue
        scripts.append(check)
        files.extend(by_path[path] for path in sorted(paths) if path in by_path)
    linked = {spec.criterion_key for spec, _role in oracles} | {
        key for check in scripts for key in _links(check)
    }
    replacement_uncovered = {item.criterion_key: item.reason for item in replacement.uncovered}
    for key in targets:
        if key not in linked and key not in still:
            still[key] = replacement_uncovered.get(key, "constructor_omitted")
    uncovered = {
        **{item.criterion_key: item.reason for item in package.uncovered},
        **{key: reason for key, reason in targets.items() if key not in linked},
        **{key: reason for key, reason in still.items() if key not in linked},
    }
    merged = assemble_package(
        seed,
        input_digest=sha256_bytes(
            canonical_json_bytes(
                {"kept": package.input_digest, "replacement": replacement.input_digest}
            )
        ),
        generator=replacement.generator,
        oracles=oracles,
        script_checks=scripts,
        script_files=_unique(files),
        uncovered={key: reason for key, reason in uncovered.items() if key not in linked},
        generated_at=replacement.generated_at,
    )
    return merged, {key: reason for key, reason in still.items() if key not in linked}


def _unique(files: list[PackageFile]) -> list[PackageFile]:
    seen: set[str] = set()
    result = []
    for item in files:
        if item.path not in seen:
            seen.add(item.path)
            result.append(item)
    return result


__all__ = [
    "REPLACEMENT_CONFLICT",
    "merge_replacement",
    "replacement_targets",
    "why_excluded",
]
