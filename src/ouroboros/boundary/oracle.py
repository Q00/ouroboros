"""Frozen oracle: per-criterion expected behavior plus a product-owned harness.

The constructor contributes data only: for each criterion it can check, the
declared parameters, a default binding, and cases (inputs and the expected
outcome). The product turns that data into package files:

- ``.ouroboros_checks/oracle/oracle.json``: every oracle (cases, declared
  parameters, default binding, failure signature, binding grammar version);
- ``.ouroboros_checks/oracle/harness.py``: ``ORACLE_HARNESS_SOURCE``, fixed
  product code.

Both files are covered by the package hash, so the failure signature, the
cases, and the grammar are frozen before any worker starts. Neither file is
written anywhere while a check runs: the controller (``boundary/oracle_run.py``)
passes the harness source to each target process as an argument and keeps
the oracle data in its own memory.

Isolation: the target role of the harness runs in the project interpreter,
one process per case, and receives the binding and one call's inputs, never
an expected value; it returns what it observed as JSON framed by a
per-process random nonce. The comparison runs inside the controller process
itself, with the comparison functions of the harness module
(``boundary/harness.py``), imported with ``oracle_run`` before any target
runs, against frozen expectations held in memory. A workspace interpreter,
``sitecustomize``, import-time monkeypatch, or an edit of the standard library
on disk can therefore change what the target returns, not the comparison. It
never derives an expectation from the artifact.

Library targets: a case input may name an importable object instead of a
JSON value (``{"$symbol": "package.module.Name"}``, ``harness.SYMBOL_REF``),
and an oracle may declare ``setup``: calls of checkout callables, with JSON
arguments, that the harness makes in the target process before it resolves
the target (a library that must be configured first). Neither applies to a
CLI oracle, whose inputs are command-line text.

Built calls (``boundary/call_grammar.py``): a case's ``args`` are the JSON
data of the declared ``params``, which the reference takes by name. An oracle
may declare how the target's call is built from them: ``inputs`` (the call's
parameters, each a template over the params; ``$call`` chains build objects
such as a fitted model), ``receiver`` (a method oracle's instance, built the
same way, instead of the class called with ``init``), and ``project`` (reads
applied to each returned value before it is compared, for a result that is
not JSON). A case may expect ``no_raise`` (the call returns anything); such
a case can fail a candidate but never verifies a pass, because a target
that returns anything passes it (``boundary/acceptance.py``). A CLI case
may carry ``files``, which the controller writes into a fresh directory
that becomes the command's working directory. All of it is data the
constructor writes; the harness that interprets it is the product's, and
all of it is frozen in ``oracle.json`` with the cases, each new field only
when declared, so an oracle without one freezes the same data as before.

Held-out cases: the constructor declares, per case, whether the case is one
the specification states (``held_out: false``) or one it withheld
(``held_out: true``); the product records that declaration as it is and never
infers it from text. Held-out cases count toward the criterion's verdict.

Identifiers are the product's: an oracle's check id is ``oracle_<n>`` (the
criterion's number; ``oracle_<n>_<k>`` for its ``k``-th oracle) and its cases
are ``c1``, ``c2``, ... in the order the constructor gave them
(``oracle_check_id``, ``case_id_for``). The models refuse any other form, so an
identifier the constructor chose (which could spell a held-out value) never
reaches a record, a receipt or the journal, where held-out cases appear by id.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from importlib import resources
import json
import re
import sys
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ouroboros.boundary.binding import (
    BINDING_GRAMMAR,
    Binding,
    CallKind,
    CheckTier,
    is_dotted_symbol,
)
from ouroboros.boundary.call_grammar import (
    GrammarError,
    check_case_files,
    check_reads,
    check_template,
    has_built_value,
    is_receiver,
)
from ouroboros.boundary.harness import bind_params, symbol_refs

ORACLE_SCHEMA = "ouroboros.oracle.v1"
ORACLE_DIR = ".ouroboros_checks/oracle"
ORACLE_DATA_PATH = f"{ORACLE_DIR}/oracle.json"
ORACLE_HARNESS_PATH = f"{ORACLE_DIR}/harness.py"
BINDINGS_FILE = "bindings.json"
ORACLE_RESULT_PREFIX = "OUROBOROS_ORACLE_RESULT "
# The product harness (``boundary/harness.py``), as the text every oracle
# package freezes and every target process runs.
ORACLE_HARNESS_SOURCE = (
    resources.files("ouroboros.boundary").joinpath("harness.py").read_text(encoding="utf-8")
)
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ORACLE_CHECK_PREFIX = "oracle_"
_CASE_PREFIX = "c"


def oracle_check_id(criterion_number: int, ordinal: int = 1) -> str:
    """The product's check id of the ``ordinal``-th oracle of criterion ``criterion_number``."""
    if criterion_number < 1 or ordinal < 1:
        raise ValueError("criterion numbers and ordinals start at 1")
    suffix = "" if ordinal == 1 else f"_{ordinal}"
    return f"{_ORACLE_CHECK_PREFIX}{criterion_number}{suffix}"


def case_id_for(position: int) -> str:
    """The product's id of the case at 1-based ``position`` in its oracle."""
    if position < 1:
        raise ValueError("case positions start at 1")
    return f"{_CASE_PREFIX}{position}"


def _positive_int(text: str) -> int | None:
    if not text.isdecimal() or text.startswith("0"):
        return None
    return int(text)


def is_oracle_check_id(value: str) -> bool:
    """Whether ``value`` is an id ``oracle_check_id`` produces."""
    if not value.startswith(_ORACLE_CHECK_PREFIX):
        return False
    number, _sep, ordinal = value[len(_ORACLE_CHECK_PREFIX) :].partition("_")
    parsed = _positive_int(number)
    if parsed is None:
        return False
    if not _sep:
        return True
    rank = _positive_int(ordinal)
    return rank is not None and rank >= 2


def case_position(value: str) -> int | None:
    """The position ``case_id_for`` encoded in ``value``, or ``None`` for any other form."""
    if not value.startswith(_CASE_PREFIX):
        return None
    return _positive_int(value[len(_CASE_PREFIX) :])


def failure_signature_for(check_id: str) -> str:
    """The frozen failure signature of an oracle check."""
    return f"OUROBOROS_CHECK_FAILED:{check_id}"


def _json_value(value: Any) -> Any:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"value is not plain JSON: {exc}") from exc
    return value


class OracleExpectation(BaseModel):
    """The frozen expected outcome of one case."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["returns", "raises", "no_raise", "cli"]
    value: Any = None
    approx: float | None = Field(default=None, ge=0)
    exception: str | None = None
    exit_code: int | None = None
    stdout: str | None = None
    stdout_contains: str | None = None

    @field_validator("value")
    @classmethod
    def _plain(cls, value: Any) -> Any:
        return _json_value(value)

    @model_validator(mode="after")
    def _shape(self) -> OracleExpectation:
        if self.kind == "raises":
            if not self.exception or not _IDENTIFIER.fullmatch(self.exception):
                raise ValueError("raises expects an exception class name")
        elif self.exception is not None:
            raise ValueError("exception is only valid for kind 'raises'")
        if self.kind == "no_raise" and (self.value is not None or self.approx is not None):
            raise ValueError("no_raise expects no value")
        cli_fields = (self.exit_code, self.stdout, self.stdout_contains)
        if self.kind == "cli":
            if all(item is None for item in cli_fields):
                raise ValueError("cli expects exit_code, stdout or stdout_contains")
        elif any(item is not None for item in cli_fields):
            raise ValueError("exit_code/stdout are only valid for kind 'cli'")
        return self


class OracleCase(BaseModel):
    """One call of the target: inputs by declared parameter, and the expectation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    args: dict[str, Any] = Field(default_factory=dict)
    init: dict[str, Any] | None = None
    stdin: str | None = None
    expect: OracleExpectation
    held_out: bool = False
    files: dict[str, str] | None = None
    """A CLI case's files (relative path to text), written into its working directory."""

    @field_validator("case_id")
    @classmethod
    def _case_id(cls, value: str) -> str:
        if case_position(value) is None:
            raise ValueError("a case id is the product's c<position> (case_id_for)")
        return value

    @field_validator("args", "init")
    @classmethod
    def _plain_args(cls, value: Any) -> Any:
        if value is None:
            return None
        if has_built_value(_json_value(value)):
            raise ValueError("case data holds no $call or $param; the oracle's inputs build values")
        return value

    @field_validator("files")
    @classmethod
    def _files(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is not None:
            check_case_files(value)
        return value

    @property
    def verifies(self) -> bool:
        """Whether a pass of this case can verify a criterion (every kind but ``no_raise``)."""
        return self.expect.kind != "no_raise"


def imported_before_checkout(module: str) -> bool:
    """Whether Python imports the top-level ``module`` before any checkout code.

    A built-in, frozen or already imported module comes before every search
    path, so a standard library name is never the checkout's, whatever the
    checkout holds. Read from this interpreter's module lists: a name that
    only the project interpreter has is not known here.
    """
    return module in sys.stdlib_module_names or module in sys.builtin_module_names


def target_module(binding: Binding) -> str | None:
    """The top-level module ``binding`` imports; ``None`` for a CLI script path."""
    if binding.call_kind is CallKind.CLI:
        if not binding.symbol.startswith("-m "):
            return None
        return binding.symbol[3:].split(".")[0]
    return binding.symbol.split(".")[0]


def _require_symbol_refs(value: Any) -> None:
    """Every symbol reference inside ``value`` names a dotted import path."""
    if not all(is_dotted_symbol(name) for name in symbol_refs(value)):
        raise ValueError("a symbol reference names a dotted import path")


class SetupCall(BaseModel):
    """One declared setup call: a checkout callable and its JSON arguments."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    args: list[Any] = Field(default_factory=list)
    kwargs: dict[str, Any] = Field(default_factory=dict)

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, value: str) -> str:
        if not is_dotted_symbol(value):
            raise ValueError("a setup call names a dotted import path")
        return value

    @field_validator("args", "kwargs")
    @classmethod
    def _plain_args(cls, value: Any) -> Any:
        _require_symbol_refs(_json_value(value))
        return value


class OracleSpec(BaseModel):
    """The frozen oracle of one criterion, executed by one check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    criterion_key: str = Field(..., min_length=1)
    check_id: str = Field(..., min_length=1)
    call_kind: CallKind
    params: tuple[str, ...] = ()
    default_binding: Binding
    cases: tuple[OracleCase, ...] = Field(..., min_length=1)
    target_named_in_criterion: bool = False
    """The constructor declared that the criterion names the default symbol."""
    setup: tuple[SetupCall, ...] = ()
    """Calls the harness makes, in order, before it resolves the target."""
    inputs: dict[str, Any] | None = None
    """The target call's parameters, each a template over ``params`` (``call_grammar``)."""
    receiver: Any = None
    """A method oracle's instance: a ``$call`` or ``$symbol`` template over ``params``."""
    project: tuple[dict[str, Any], ...] = ()
    """Reads applied to each returned value of a ``returns`` case before comparison."""

    @model_validator(mode="after")
    def _consistent(self) -> OracleSpec:
        if len(set(self.params)) != len(self.params) or not all(
            _IDENTIFIER.fullmatch(name) for name in self.params
        ):
            raise ValueError(f"{self.check_id}: params must be unique identifiers")
        if not is_oracle_check_id(self.check_id):
            raise ValueError("an oracle's check id is the product's oracle_<n> (oracle_check_id)")
        if not any(case.held_out for case in self.cases):
            # A pass verifies a criterion only through a held-out case (the
            # candidate controls what its target process reports, see
            # boundary/acceptance.py), so an oracle without one decides nothing.
            raise ValueError(f"{self.check_id}: an oracle needs at least one held-out case")
        self._check_built_call()
        positions = [case_position(case.case_id) or 0 for case in self.cases]
        if positions != sorted(set(positions)):
            # Increasing and unique; gaps remain where the reference check
            # dropped a case.
            raise ValueError(f"{self.check_id}: case ids must increase")
        for case in self.cases:
            if set(case.args) != set(self.params):
                raise ValueError(
                    f"{self.check_id}: case {case.case_id} must give exactly the declared params"
                )
            if (case.expect.kind == "cli") != (self.call_kind is CallKind.CLI):
                raise ValueError(f"{self.check_id}: case {case.case_id} expectation kind")
            if case.init is not None and (
                self.call_kind is not CallKind.METHOD or self.receiver is not None
            ):
                raise ValueError(f"{self.check_id}: init is only valid for method oracles")
            if case.files is not None and self.call_kind is not CallKind.CLI:
                raise ValueError(f"{self.check_id}: files are only valid for CLI oracles")
            refs = symbol_refs(case.args) + symbol_refs(case.init)
            if refs and self.call_kind is CallKind.CLI:
                raise ValueError(f"{self.check_id}: a CLI case takes no symbol reference")
            _require_symbol_refs([case.args, case.init])
        if self.setup and self.call_kind is CallKind.CLI:
            raise ValueError(
                f"{self.check_id}: setup is only valid for function and method oracles"
            )
        binding = self.default_binding
        if binding.criterion_key != self.criterion_key or binding.call_kind is not self.call_kind:
            raise ValueError(f"{self.check_id}: default binding does not match the oracle")
        if binding.arg_map and set(binding.arg_map) != set(self.call_params):
            raise ValueError(f"{self.check_id}: default binding arg_map keys")
        return self

    def _check_built_call(self) -> None:
        """``inputs``, ``receiver`` and ``project`` satisfy the grammar and fit the call kind."""
        built = self.inputs is not None or self.receiver is not None or bool(self.project)
        if built and self.call_kind is CallKind.CLI:
            raise ValueError(f"{self.check_id}: inputs, receiver and project are not for CLI")
        if self.receiver is not None and self.call_kind is not CallKind.METHOD:
            raise ValueError(f"{self.check_id}: a receiver is only valid for method oracles")
        try:
            if self.inputs is not None:
                if not all(_IDENTIFIER.fullmatch(name) for name in self.inputs):
                    raise ValueError(f"{self.check_id}: inputs are named by identifiers")
                for template in self.inputs.values():
                    check_template(template, self.params)
            if self.receiver is not None:
                if not is_receiver(self.receiver):
                    raise ValueError(f"{self.check_id}: a receiver is a $call or $symbol")
                check_template(self.receiver, self.params)
            if self.project:
                check_reads(list(self.project), self.params)
        except GrammarError as exc:
            raise ValueError(f"{self.check_id}: {exc.code}") from exc

    @property
    def call_params(self) -> tuple[str, ...]:
        """The names the target is called with: ``inputs`` when declared, else ``params``.

        A binding's ``arg_map`` maps these, and a worker declaring its own
        entry point sees these (``interface``).
        """
        return tuple(self.inputs) if self.inputs is not None else self.params

    def call_frame(self, case: OracleCase) -> dict[str, Any]:
        """What the target process receives for ``case`` besides the split arguments.

        The receiver and the projection, each ``$param`` bound to the case's
        value; never an expected value.
        """
        frame: dict[str, Any] = {}
        if self.receiver is not None:
            frame["receiver"] = bind_params(self.receiver, case.args)
        if self.project and case.expect.kind == "returns":
            frame["project"] = bind_params(list(self.project), case.args)
        return frame

    @property
    def failure_signature(self) -> str:
        return failure_signature_for(self.check_id)

    @property
    def held_out_count(self) -> int:
        return sum(1 for case in self.cases if case.held_out)

    def base_run_tier(self, resolve: str | None) -> CheckTier:
        """The admitted tier of this oracle's check, from what its base run resolved.

        ``A`` when the target process resolved the default binding inside
        the base checkout (``resolve`` ``ok``), or found it missing there while
        the constructor declared that the criterion names it (the feature
        the worker is asked to add) and the worker can add it to the
        checkout; otherwise ``U`` (a worker-declared binding may still make
        it ``A_prime``). A missing target that can never be checkout code (a
        standard library module, ``imported_before_checkout``) is ``U``: no
        candidate could pass it, and an admitted check nobody can pass would
        reject every candidate. The resolution is the harness's own, in a
        process, so a file that merely looks like the target (a workspace
        module shadowing the standard library, a package linked in from
        outside the checkout) does not count.
        """
        if resolve == "ok":
            return CheckTier.A
        module = target_module(self.default_binding)
        addable = module is None or not imported_before_checkout(module)
        if resolve == "missing" and self.target_named_in_criterion and addable:
            return CheckTier.A
        return CheckTier.U

    def interface(self) -> dict[str, Any]:
        """What a worker may learn: call kind and the call's parameter names, no cases."""
        return {"call_kind": self.call_kind.value, "params": list(self.call_params)}


# --------------------------------------------------------------------------
# Package files


def oracle_data(oracles: Sequence[OracleSpec]) -> dict[str, Any]:
    """The frozen data the harness reads (``oracle.json``)."""
    return {
        "schema_version": ORACLE_SCHEMA,
        "binding_grammar": BINDING_GRAMMAR,
        "oracles": [_oracle_entry(spec) for spec in oracles],
    }


def _oracle_entry(spec: OracleSpec) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "criterion_key": spec.criterion_key,
        "check_id": spec.check_id,
        "call_kind": spec.call_kind.value,
        "params": list(spec.params),
        "failure_signature": spec.failure_signature,
        "default_binding": spec.default_binding.to_dict(),
        "cases": [_case_entry(case) for case in spec.cases],
    }
    # Each only when declared: an oracle without one freezes the same bytes as before.
    if spec.setup:
        entry["setup"] = [call.model_dump(mode="json") for call in spec.setup]
    if spec.inputs is not None:
        entry["inputs"] = spec.inputs
    if spec.receiver is not None:
        entry["receiver"] = spec.receiver
    if spec.project:
        entry["project"] = list(spec.project)
    return entry


def _case_entry(case: OracleCase) -> dict[str, Any]:
    entry = case.model_dump(mode="json")
    if entry.get("files") is None:
        entry.pop("files", None)
    return entry


def oracle_data_text(oracles: Sequence[OracleSpec]) -> str:
    return json.dumps(oracle_data(oracles), sort_keys=True, indent=1, ensure_ascii=False) + "\n"


def is_oracle_file(path: str) -> bool:
    """Oracle files are never materialized in a checkout copy (or anywhere else)."""
    return path == ORACLE_DIR or path.startswith(ORACLE_DIR + "/")


# --------------------------------------------------------------------------
# Results: closed models of what the harness comparison decided


class CaseResult(BaseModel):
    """One case's verdict. ``detail`` renders the call and what was observed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    held_out: bool
    passed: bool
    detail: str = ""

    @field_validator("case_id")
    @classmethod
    def _case_id(cls, value: str) -> str:
        if case_position(value) is None:
            raise ValueError("a case id is the product's c<position> (case_id_for)")
        return value


class OracleResult(BaseModel):
    """What one oracle check decided (``harness.compare``); closed, no free-form fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_id: str
    criterion_key: str
    binding_source: Literal["default", "declared"]
    symbol: str
    call_kind: CallKind
    resolve: str
    cases: tuple[CaseResult, ...]

    @field_validator("check_id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not is_oracle_check_id(value):
            raise ValueError("an oracle's check id is the product's oracle_<n> (oracle_check_id)")
        return value

    @property
    def held_out_passed(self) -> bool:
        """At least one held-out case was run and passed."""
        return any(case.held_out and case.passed for case in self.cases)


# --------------------------------------------------------------------------
# Result views


def journal_safe_oracle_result(result: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Per-case pass/fail and held-out flags only; no inputs or observed values."""
    if not result:
        return None
    return {
        "binding_source": result.get("binding_source"),
        "resolve": result.get("resolve"),
        "cases": [
            {
                "case_id": case.get("case_id"),
                "held_out": bool(case.get("held_out")),
                "passed": bool(case.get("passed")),
            }
            for case in result.get("cases") or ()
        ],
    }


def redact_held_out(result: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """``result`` with held-out cases reduced to case id, flags, and pass/fail.

    Stored receipts and printed output use this form: a held-out case's input,
    expected value, and observation stay in memory only.
    """
    if result is None:
        return None
    cases = [
        (
            {
                "case_id": case.get("case_id"),
                "held_out": True,
                "passed": bool(case.get("passed")),
            }
            if case.get("held_out")
            else dict(case)
        )
        for case in result.get("cases") or ()
    ]
    return {**result, "cases": cases}


def failed_heldout_only(result: OracleResult | None) -> bool:
    """Diagnostic: every failing case of the result is a held-out case."""
    if result is None:
        return False
    failing = [case for case in result.cases if not case.passed]
    return bool(failing) and all(case.held_out for case in failing)


__all__ = [
    "BINDINGS_FILE",
    "ORACLE_DATA_PATH",
    "ORACLE_DIR",
    "ORACLE_HARNESS_PATH",
    "ORACLE_HARNESS_SOURCE",
    "ORACLE_RESULT_PREFIX",
    "ORACLE_SCHEMA",
    "CaseResult",
    "OracleCase",
    "OracleResult",
    "OracleExpectation",
    "OracleSpec",
    "SetupCall",
    "case_id_for",
    "case_position",
    "failed_heldout_only",
    "is_oracle_check_id",
    "failure_signature_for",
    "is_oracle_file",
    "journal_safe_oracle_result",
    "oracle_check_id",
    "oracle_data",
    "oracle_data_text",
    "redact_held_out",
]
