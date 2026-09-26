"""Build frozen oracle specs and oracle packages from plain data.

Used by the product constructor (``boundary/constructor.py``) and available
to callers that assemble packages themselves (the study harness). Every
function here validates; a problem raises ``CheckPackageError`` so the caller
records a construction failure.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ouroboros.boundary.binding import (
    BINDING_GRAMMAR,
    BindingError,
    CallKind,
    default_binding_resolves,
    parse_binding,
    symbol_named_in_text,
)
from ouroboros.boundary.oracle import (
    OracleCase,
    OracleSpec,
    mark_held_out,
    worker_visible_seed_text,
)
from ouroboros.boundary.package import (
    ORACLE_PACKAGE_SCHEMA,
    CheckPackage,
    CheckPackageError,
    CheckRole,
    CheckSpec,
    PackageFile,
    UncoveredObligation,
    oracle_check,
    oracle_files,
    seed_criterion_keys,
    seed_digest,
)
from ouroboros.core.seed import AcceptanceCriterionSpec, Seed


def criterion_text(seed: Seed, index: int) -> str:
    """The description of the criterion at 0-based ``index``."""
    criterion = seed.acceptance_criteria[index]
    if isinstance(criterion, AcceptanceCriterionSpec):
        return criterion.description
    return str(criterion).strip()


def build_oracle_spec(
    seed: Seed,
    *,
    criterion_index: int,
    check_id: str,
    call_kind: str,
    params: Sequence[str],
    default_binding: Mapping[str, Any],
    cases: Sequence[Mapping[str, Any]],
    base_checkout: Path | None = None,
) -> OracleSpec:
    """Validate one criterion's oracle data and freeze it.

    ``held_out`` is set by the product rule (a case whose literals do not all
    appear in the Seed text the worker sees), whatever ``cases`` says.
    ``default_resolves`` is the tier-A rule: the default symbol exists in
    ``base_checkout`` or the criterion text names it.
    """
    keys = seed_criterion_keys(seed)
    if not 0 <= criterion_index < len(keys):
        raise CheckPackageError(f"{check_id}: criterion index out of range")
    key = keys[criterion_index]
    try:
        kind = CallKind(str(call_kind))
        binding = parse_binding(
            {k: v for k, v in default_binding.items() if k not in {"criterion", "criterion_key"}},
            criterion_key=key,
            params=tuple(params),
            call_kind=kind,
        )
        parsed = tuple(OracleCase.model_validate(dict(case)) for case in cases)
    except BindingError as exc:
        raise CheckPackageError(f"{check_id}: default binding: {exc.reason}") from exc
    except (ValidationError, ValueError, TypeError) as exc:
        raise CheckPackageError(f"{check_id}: oracle cases do not match the schema: {exc}") from exc
    text = criterion_text(seed, criterion_index)
    resolves = (
        default_binding_resolves(binding, base_checkout, text)
        if base_checkout is not None
        else symbol_named_in_text(binding.symbol, text)
    )
    try:
        return OracleSpec(
            criterion_key=key,
            check_id=check_id,
            call_kind=kind,
            params=tuple(params),
            default_binding=binding,
            default_resolves=resolves,
            cases=mark_held_out(parsed, worker_visible_seed_text(seed)),
        )
    except (ValidationError, ValueError) as exc:
        raise CheckPackageError(f"{check_id}: oracle does not match the schema: {exc}") from exc


def assemble_package(
    seed: Seed,
    *,
    input_digest: str,
    generator: str | None,
    oracles: Sequence[tuple[OracleSpec, CheckRole]] = (),
    script_checks: Sequence[CheckSpec] = (),
    script_files: Sequence[PackageFile] = (),
    uncovered: Mapping[str, str] | None = None,
    generated_at: datetime | None = None,
) -> CheckPackage:
    """One package from oracle checks, model-written script checks, and uncovered criteria.

    Criteria neither checked nor listed are added as uncovered with reason
    ``constructor_omitted``. Without oracles the package is a plain v1
    package, byte for byte what the earlier constructor produced.
    """
    keys = seed_criterion_keys(seed)
    checks = [oracle_check(spec, role) for spec, role in oracles] + list(script_checks)
    linked = {link.criterion_key for check in checks for link in check.assertions}
    gaps: dict[str, str] = {
        key: reason for key, reason in (uncovered or {}).items() if key not in linked
    }
    for key in keys:
        if key not in linked and key not in gaps:
            gaps[key] = "constructor_omitted"
    specs = tuple(spec for spec, _role in oracles)
    try:
        return CheckPackage(
            schema_version=ORACLE_PACKAGE_SCHEMA if specs else "ouroboros.check_package.v1",
            seed_digest=seed_digest(seed),
            criterion_keys=keys,
            input_digest=input_digest,
            generated_at=generated_at or datetime.now(UTC),
            generator=generator,
            checks=tuple(checks),
            files=(*oracle_files(specs), *script_files),
            uncovered=tuple(
                UncoveredObligation(criterion_key=key, reason=reason)
                for key, reason in gaps.items()
            ),
            oracles=specs,
            binding_grammar=BINDING_GRAMMAR if specs else None,
        )
    except (ValidationError, ValueError) as exc:
        if isinstance(exc, CheckPackageError):
            raise
        raise CheckPackageError(f"package does not match the schema: {exc}") from exc


__all__ = ["assemble_package", "build_oracle_spec", "criterion_text"]
