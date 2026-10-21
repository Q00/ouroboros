"""Build frozen oracle specs and oracle packages from plain data.

Used by the product constructor (``boundary/constructor.py``), which maps its
model reply onto a package with ``package_from_reply``, and available to
callers that assemble packages themselves (the study harness). Every function
here validates; a problem raises ``CheckPackageError`` so the caller records a
construction failure. Nothing here calls a model or runs a check.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import ValidationError

from ouroboros.boundary.binding import (
    BINDING_GRAMMAR,
    CHECK_DIR,
    BindingError,
    CallKind,
    is_dotted_symbol,
    parse_binding,
)
from ouroboros.boundary.call_grammar import (
    CALL_INVALID,
    GrammarError,
    check_case_files,
    check_reads,
    check_template,
    has_built_value,
    is_receiver,
)
from ouroboros.boundary.harness import symbol_refs
from ouroboros.boundary.oracle import (
    OracleCase,
    OracleSpec,
    SetupCall,
    case_id_for,
    is_oracle_file,
)
from ouroboros.boundary.package import (
    ORACLE_PACKAGE_SCHEMA,
    AssertionLink,
    CheckPackage,
    CheckPackageError,
    CheckRole,
    CheckSpec,
    PackageFile,
    UncoveredObligation,
    mint_check_ids,
    mint_package_ids,
    oracle_check,
    oracle_files,
    script_assertion_id,
    seed_criterion_keys,
    seed_digest,
)
from ouroboros.core.seed import Seed

CHECK_INTERPRETERS = frozenset({"python3", "python"})
_MIN_SIGNATURE_CHARS = 12


def build_oracle_spec(
    seed: Seed,
    *,
    criterion_index: int,
    check_id: str,
    call_kind: str,
    params: Sequence[str],
    default_binding: Mapping[str, Any],
    cases: Sequence[Mapping[str, Any]],
    target_named_in_criterion: bool = False,
    setup: Sequence[Mapping[str, Any]] = (),
    inputs: Mapping[str, Any] | None = None,
    receiver: Any = None,
    project: Sequence[Mapping[str, Any]] = (),
) -> OracleSpec:
    """Validate one criterion's oracle data and freeze it.

    Every case must declare ``held_out`` as a JSON boolean: ``false`` for a
    case the specification states, ``true`` for one it withheld. The
    declaration is frozen as given. Case ids are the product's (``c1``,
    ``c2``, ... in the given order); a ``case_id`` in ``cases`` is ignored,
    so no identifier the caller chose is frozen. ``target_named_in_criterion``
    is frozen too; the tier itself comes from the admission base run
    (``OracleSpec.base_run_tier``). ``setup`` is the oracle's declared setup
    calls (``{"symbol", "args", "kwargs"}``), frozen in order. ``inputs``,
    ``receiver`` and ``project`` are the oracle's built call
    (``boundary/call_grammar.py``), frozen as given.
    """
    keys = seed_criterion_keys(seed)
    if not 0 <= criterion_index < len(keys):
        raise CheckPackageError(f"{check_id}: criterion index out of range")
    key = keys[criterion_index]
    if not isinstance(target_named_in_criterion, bool):
        raise CheckPackageError(f"{check_id}: target_named_in_criterion must be true or false")
    for case in cases:
        if not isinstance(case, Mapping) or not isinstance(case.get("held_out"), bool):
            raise CheckPackageError(f"{check_id}: every case must declare held_out true or false")
    try:
        kind = CallKind(str(call_kind))
        binding = parse_binding(
            {k: v for k, v in default_binding.items() if k not in {"criterion", "criterion_key"}},
            criterion_key=key,
            params=tuple(inputs) if inputs is not None else tuple(params),
            call_kind=kind,
        )
        parsed = tuple(
            OracleCase.model_validate({**dict(case), "case_id": case_id_for(position)})
            for position, case in enumerate(cases, start=1)
        )
        calls = tuple(SetupCall.model_validate(dict(call)) for call in setup)
    except BindingError as exc:
        raise CheckPackageError(f"{check_id}: default binding: {exc.reason}") from exc
    except (ValidationError, ValueError, TypeError) as exc:
        raise CheckPackageError(f"{check_id}: oracle cases do not match the schema: {exc}") from exc
    try:
        return OracleSpec(
            criterion_key=key,
            check_id=check_id,
            call_kind=kind,
            params=tuple(params),
            default_binding=binding,
            cases=parsed,
            target_named_in_criterion=target_named_in_criterion,
            setup=calls,
            inputs=None if inputs is None else dict(inputs),
            receiver=receiver,
            project=tuple(dict(read) for read in project),
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
    try:
        oracles, script_checks = mint_package_ids(keys, oracles, script_checks)
    except (KeyError, ValidationError, ValueError) as exc:
        raise CheckPackageError("package parts do not match the Seed's criteria") from exc
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


class ReplyFailure(StrEnum):
    """Why a constructor reply was refused: a closed, value-free code.

    These codes are the only trace of a refused reply that reaches a
    package, a record, an event, a rendered line or a later prompt. The
    reply's own text (and any exception message built from it) never does:
    it may carry held-out inputs and expected values.
    """

    REPLY_NOT_JSON = "reply_not_json"
    REPLY_NOT_OBJECT = "reply_not_object"
    SECTION_NOT_LIST = "section_not_list"
    ENTRY_NOT_OBJECT = "entry_not_object"
    CRITERION_INVALID = "criterion_invalid"
    ROLE_INVALID = "role_invalid"
    CALL_KIND_INVALID = "call_kind_invalid"
    PARAMS_INVALID = "params_invalid"
    BINDING_INVALID = "binding_invalid"
    CASE_SHAPE = "case_shape"
    HELD_OUT_NOT_BOOLEAN = "held_out_not_boolean"
    ORACLE_WITHOUT_HELD_OUT_CASE = "oracle_without_held_out_case"
    """Every oracle declares at least one held-out case (only those can verify a pass)."""
    TARGET_NAMED_NOT_BOOLEAN = "target_named_not_boolean"
    SYMBOL_REF_INVALID = "symbol_ref_invalid"
    """A ``{"$symbol": ...}`` input does not name a dotted import path."""
    SETUP_INVALID = "setup_invalid"
    """An oracle's ``setup`` is not a list of ``{"symbol", "args", "kwargs"}`` calls."""
    CALL_INVALID = "call_invalid"
    """An oracle's ``inputs`` or ``receiver`` breaks the built-call grammar
    (``boundary/call_grammar.py``), or case data holds a ``$call`` or ``$param``."""
    PROJECT_INVALID = "project_invalid"
    """An oracle's ``project`` is not a list of reads."""
    CASE_FILES_INVALID = "case_files_invalid"
    """A case's ``files`` is not a map of plain relative paths to text."""
    ORACLE_INVALID = "oracle_invalid"
    FILE_INVALID = "file_invalid"
    ARGV_INVALID = "argv_invalid"
    SIGNATURE_INVALID = "failure_signature_invalid"
    ASSERTIONS_INVALID = "assertions_invalid"
    UNCOVERED_INVALID = "uncovered_invalid"
    PACKAGE_INVALID = "package_invalid"
    REPLY_MALFORMED = "reply_malformed"
    """A shape no specific rule names; the parser never lets an exception through."""


class ReplyError(CheckPackageError):
    """A refused constructor reply; its message is its closed code only."""

    def __init__(self, code: ReplyFailure) -> None:
        super().__init__(code.value)
        self.code = code


DECLARED_NOT_EXECUTABLE = "declared_not_executable"
"""Uncovered reason of a criterion the constructor listed under ``uncovered``.

The constructor's own wording is not kept: it is model text.
"""
REPLY_INVALID_PREFIX = "constructor_reply_invalid"
_SECTIONS = ("oracles", "checks", "files", "uncovered")


def reply_failure_reason(exc: CheckPackageError) -> str:
    """The construction failure reason of a refused reply (``constructor_reply_invalid:<code>``)."""
    code = exc.code if isinstance(exc, ReplyError) else ReplyFailure.PACKAGE_INVALID
    return f"{REPLY_INVALID_PREFIX}:{code.value}"


def _require(condition: bool, code: ReplyFailure) -> None:
    if not condition:
        raise ReplyError(code)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_criterion(value: object) -> bool:
    """A criterion number: a positive integer (the Seed's range is checked with the Seed)."""
    return _is_int(value) and value >= 1  # type: ignore[operator]


def _optional_str(value: object) -> bool:
    return value is None or isinstance(value, str)


def _str_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _entries(reply: Mapping[str, Any], section: str) -> list[dict[str, Any]]:
    raw = reply.get(section)
    if raw is None:
        return []
    _require(isinstance(raw, list), ReplyFailure.SECTION_NOT_LIST)
    _require(all(isinstance(item, Mapping) for item in raw), ReplyFailure.ENTRY_NOT_OBJECT)
    return [dict(item) for item in raw]


def _refs_valid(value: object) -> bool:
    try:
        return all(is_dotted_symbol(name) for name in symbol_refs(value))
    except ValueError:
        return False


def _check_case(case: object) -> None:
    if not isinstance(case, Mapping):
        raise ReplyError(ReplyFailure.CASE_SHAPE)
    _require(isinstance(case.get("held_out"), bool), ReplyFailure.HELD_OUT_NOT_BOOLEAN)
    _require(
        isinstance(case.get("args", {}), Mapping)
        and isinstance(case.get("expect"), Mapping)
        and (case.get("init") is None or isinstance(case.get("init"), Mapping))
        and _optional_str(case.get("stdin")),
        ReplyFailure.CASE_SHAPE,
    )
    _require(
        _refs_valid([dict(case.get("args") or {}), dict(case.get("init") or {})]),
        ReplyFailure.SYMBOL_REF_INVALID,
    )
    _require(
        not has_built_value([dict(case.get("args") or {}), dict(case.get("init") or {})]),
        ReplyFailure.CALL_INVALID,
    )
    if case.get("files") is not None:
        try:
            check_case_files(case.get("files"))
        except GrammarError as exc:
            raise ReplyError(ReplyFailure.CASE_FILES_INVALID) from exc


def _check_built_call(entry: Mapping[str, Any]) -> None:
    """The oracle's ``inputs``, ``receiver`` and ``project``, when given, fit the grammar."""
    params = entry.get("params", [])
    inputs, receiver = entry.get("inputs"), entry.get("receiver")
    try:
        if inputs is not None:
            if not isinstance(inputs, Mapping):
                raise GrammarError(CALL_INVALID)
            for template in inputs.values():
                check_template(template, params)
        if receiver is not None:
            if not is_receiver(receiver):
                raise GrammarError(CALL_INVALID)
            check_template(receiver, params)
        if entry.get("project") is not None:
            check_reads(entry.get("project"), params)
    except GrammarError as exc:
        raise ReplyError(ReplyFailure(exc.code)) from exc


def _check_setup(setup: object) -> None:
    if not isinstance(setup, list):
        raise ReplyError(ReplyFailure.SETUP_INVALID)
    for call in setup:
        _require(
            isinstance(call, Mapping)
            and set(call) <= {"symbol", "args", "kwargs"}
            and is_dotted_symbol(call.get("symbol"))
            and isinstance(call.get("args", []), list)
            and isinstance(call.get("kwargs", {}), Mapping),
            ReplyFailure.SETUP_INVALID,
        )
        _require(
            _refs_valid([call.get("args", []), dict(call.get("kwargs", {}))]),
            ReplyFailure.SYMBOL_REF_INVALID,
        )


def _check_oracle(entry: Mapping[str, Any]) -> None:
    _require(_is_criterion(entry.get("criterion")), ReplyFailure.CRITERION_INVALID)
    _require(entry.get("role") in {role.value for role in CheckRole}, ReplyFailure.ROLE_INVALID)
    _require(
        entry.get("call_kind", CallKind.FUNCTION.value) in {kind.value for kind in CallKind},
        ReplyFailure.CALL_KIND_INVALID,
    )
    _require(_str_list(entry.get("params", [])), ReplyFailure.PARAMS_INVALID)
    _require(isinstance(entry.get("default_binding"), Mapping), ReplyFailure.BINDING_INVALID)
    _require(
        isinstance(entry.get("target_named_in_criterion"), bool),
        ReplyFailure.TARGET_NAMED_NOT_BOOLEAN,
    )
    _check_setup(entry.get("setup", []))
    _check_built_call(entry)
    cases = entry.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ReplyError(ReplyFailure.CASE_SHAPE)
    for case in cases:
        _check_case(case)
    _require(
        any(case.get("held_out") is True for case in cases),
        ReplyFailure.ORACLE_WITHOUT_HELD_OUT_CASE,
    )


def _check_script(entry: Mapping[str, Any]) -> None:
    _require(entry.get("role") in {role.value for role in CheckRole}, ReplyFailure.ROLE_INVALID)
    _require(_str_list(entry.get("argv")), ReplyFailure.ARGV_INVALID)
    _require(_optional_str(entry.get("cwd")), ReplyFailure.ARGV_INVALID)
    _require(_optional_str(entry.get("failure_signature")), ReplyFailure.SIGNATURE_INVALID)
    links = entry.get("assertions")
    if not isinstance(links, list) or not links:
        raise ReplyError(ReplyFailure.ASSERTIONS_INVALID)
    for link in links:
        _require(
            isinstance(link, Mapping) and _is_criterion(link.get("criterion")),
            ReplyFailure.ASSERTIONS_INVALID,
        )


def normalize_reply(reply: object) -> dict[str, list[dict[str, Any]]]:
    """The canonical, shape-checked form of a constructor reply, or ``ReplyError``.

    Every reply (single, per criterion, replacement) goes through this before
    it is restricted to criteria, merged, parsed into a package, or used for a
    prompt. It checks every container and scalar type the parser relies on
    and the required declarations: each case's ``held_out`` and each
    oracle's ``target_named_in_criterion`` are JSON
    booleans. Unknown keys are kept (an oracle's ``reference`` is read by
    ``reference_check``). The result has exactly the four sections, each a
    list of objects.

    Total: every domain is checked before identifiers are minted, and any
    other exception from a shape no rule names is ``REPLY_MALFORMED``. The
    only exception this function raises is ``ReplyError``.
    """
    try:
        return _normalize(reply)
    except ReplyError:
        raise
    except Exception as exc:  # noqa: BLE001 - total: model output never escapes as another error
        raise ReplyError(ReplyFailure.REPLY_MALFORMED) from exc


def _normalize(reply: object) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(reply, Mapping):
        raise ReplyError(ReplyFailure.REPLY_NOT_OBJECT)
    normalized = {section: _entries(reply, section) for section in _SECTIONS}
    for entry in normalized["oracles"]:
        _check_oracle(entry)
    for entry in normalized["checks"]:
        _check_script(entry)
    _mint_identifiers(normalized)
    for entry in normalized["files"]:
        _require(
            isinstance(entry.get("path"), str) and isinstance(entry.get("content"), str),
            ReplyFailure.FILE_INVALID,
        )
    for entry in normalized["uncovered"]:
        _require(
            _is_criterion(entry.get("criterion")) and _optional_str(entry.get("reason")),
            ReplyFailure.UNCOVERED_INVALID,
        )
    return normalized


def _mint_identifiers(normalized: dict[str, list[dict[str, Any]]]) -> None:
    """Replace every identifier the constructor chose with the product's.

    Oracles become ``oracle_<n>`` (``oracle_<n>_<k>`` for a criterion's
    ``k``-th oracle) and their cases ``c1``, ``c2``, ...; script checks become
    ``script_<n>_<k>`` after the first criterion they link, with assertion
    ids ``<check_id>.a<m>``, and their free-text locators are dropped. An
    identifier is persisted next to redacted held-out cases, so one the model
    chose could spell a held-out value; the product's are positional and
    carry nothing. Minting depends only on criterion numbers and order, so a
    reply normalized twice keeps its ids.
    """
    oracle_ids, script_ids = mint_check_ids(
        [entry["criterion"] for entry in normalized["oracles"]],
        [entry["assertions"][0]["criterion"] for entry in normalized["checks"]],
    )
    for entry, check_id in zip(normalized["oracles"], oracle_ids, strict=True):
        entry["check_id"] = check_id
        entry["cases"] = [
            {**case, "case_id": case_id_for(position)}
            for position, case in enumerate(entry["cases"], start=1)
        ]
    for entry, check_id in zip(normalized["checks"], script_ids, strict=True):
        entry["check_id"] = check_id
        entry["assertions"] = [
            {
                "criterion": link["criterion"],
                "assertion_id": script_assertion_id(check_id, position),
            }
            for position, link in enumerate(entry["assertions"], start=1)
        ]


def _criterion_key(keys: Sequence[str], raw: object) -> str:
    if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= len(keys):
        raise ReplyError(ReplyFailure.CRITERION_INVALID)
    return keys[raw - 1]


def _check_file_path(raw: str) -> str:
    _require(raw.startswith(f"{CHECK_DIR}/") and not is_oracle_file(raw), ReplyFailure.FILE_INVALID)
    return raw


def _oracle_from_entry(
    seed: Seed, keys: Sequence[str], raw: Mapping[str, Any]
) -> tuple[OracleSpec, CheckRole]:
    number = raw["criterion"]
    _criterion_key(keys, number)
    try:
        spec = build_oracle_spec(
            seed,
            criterion_index=number - 1,
            check_id=raw["check_id"],
            call_kind=raw.get("call_kind") or CallKind.FUNCTION.value,
            params=tuple(raw.get("params") or ()),
            default_binding=dict(raw["default_binding"]),
            cases=list(raw["cases"]),
            target_named_in_criterion=raw["target_named_in_criterion"],
            setup=list(raw.get("setup") or ()),
            inputs=raw.get("inputs"),
            receiver=raw.get("receiver"),
            project=list(raw.get("project") or ()),
        )
    except CheckPackageError as exc:
        raise ReplyError(
            ReplyFailure.BINDING_INVALID
            if isinstance(exc.__cause__, BindingError)
            else ReplyFailure.ORACLE_INVALID
        ) from exc
    return spec, CheckRole(raw["role"])


def _script_from_entry(
    keys: Sequence[str],
    raw: Mapping[str, Any],
    file_paths: set[str],
    signatures: set[str],
) -> CheckSpec:
    check_id = raw["check_id"]
    role = CheckRole(raw["role"])
    signature = raw.get("failure_signature") or None
    if role is CheckRole.REPRODUCTION:
        if (
            not isinstance(signature, str)
            or len(signature) < _MIN_SIGNATURE_CHARS
            or signature in signatures
        ):
            raise ReplyError(ReplyFailure.SIGNATURE_INVALID)
        signatures.add(signature)
    else:
        signature = None
    argv = raw["argv"]
    # The only accepted shape is the one the prompt prescribes: a packaged
    # script run by the Python interpreter from the root.
    _require(
        len(argv) == 2
        and argv[0] in CHECK_INTERPRETERS
        and argv[1] in file_paths
        and (raw.get("cwd") or ".") == ".",
        ReplyFailure.ARGV_INVALID,
    )
    assertions = tuple(
        AssertionLink(
            assertion_id=link["assertion_id"],
            criterion_key=_criterion_key(keys, link["criterion"]),
        )
        for link in raw["assertions"]
    )
    return CheckSpec(
        check_id=check_id,
        role=role,
        argv=tuple(argv),
        cwd=".",
        assertions=assertions,
        failure_signature=signature,
    )


def package_from_reply(
    reply: object,
    seed: Seed,
    *,
    input_digest: str,
    generator: str,
    generated_at: datetime | None = None,
    product_uncovered: Mapping[str, str] | None = None,
) -> CheckPackage:
    """Map the constructor's JSON reply onto a ``CheckPackage`` for ``seed``.

    The reply first goes through ``normalize_reply``. ``oracles`` become
    frozen oracle checks run by the product harness (``boundary/oracle.py``);
    ``checks`` are model-written scripts. A criterion the reply lists under
    ``uncovered`` gets the reason ``declared_not_executable``; criteria the
    reply neither links nor lists are added as uncovered with reason
    ``constructor_omitted``. ``product_uncovered`` maps a criterion key to a
    reason the product itself assigned (a timeout, a refused per-criterion
    reply); it applies to criteria the reply does not link. Every oracle
    declares ``target_named_in_criterion``; tiers come from the admission base
    run, not from the reply. Every refusal is a ``ReplyError`` whose
    message is a closed ``ReplyFailure`` code, never text from the reply.
    """
    keys = seed_criterion_keys(seed)
    normalized = normalize_reply(reply)
    stage = ReplyFailure.ORACLE_INVALID
    try:
        oracles = [_oracle_from_entry(seed, keys, raw) for raw in normalized["oracles"]]
        stage = ReplyFailure.FILE_INVALID
        files = tuple(
            PackageFile.from_content(_check_file_path(item["path"]), item["content"])
            for item in normalized["files"]
        )
        stage = ReplyFailure.ASSERTIONS_INVALID
        file_paths = {item.path for item in files}
        signatures: set[str] = set()
        checks = [
            _script_from_entry(keys, raw, file_paths, signatures) for raw in normalized["checks"]
        ]
        stage = ReplyFailure.UNCOVERED_INVALID
        linked = {link.criterion_key for check in checks for link in check.assertions}
        linked |= {spec.criterion_key for spec, _role in oracles}
        uncovered = {
            key: DECLARED_NOT_EXECUTABLE
            for key in (_criterion_key(keys, raw["criterion"]) for raw in normalized["uncovered"])
            if key not in linked
        }
        uncovered.update(
            {key: reason for key, reason in (product_uncovered or {}).items() if key not in linked}
        )
        stage = ReplyFailure.PACKAGE_INVALID
        return assemble_package(
            seed,
            input_digest=input_digest,
            generator=generator,
            oracles=oracles,
            script_checks=checks,
            script_files=files,
            uncovered=uncovered,
            generated_at=generated_at,
        )
    except ReplyError:
        raise
    except Exception as exc:  # noqa: BLE001 - total: model output never escapes as another error
        raise ReplyError(stage) from exc


__all__ = [
    "CHECK_INTERPRETERS",
    "DECLARED_NOT_EXECUTABLE",
    "ReplyError",
    "ReplyFailure",
    "assemble_package",
    "build_oracle_spec",
    "normalize_reply",
    "package_from_reply",
    "reply_failure_reason",
]
