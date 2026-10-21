"""Late binding of a frozen oracle to the artifact's entry point, and check tiers.

A frozen oracle (``boundary/oracle.py``) states expected behavior in terms of
declared parameters. It reaches the artifact only through a binding, which is
pure data::

    {criterion_key, symbol, arg_map, call_kind}

- ``symbol``: a dotted import path (``function``: ``module.func`` or
  ``module.Class.staticmethod``; ``method``: ``module.Class.method``) or, for
  ``cli``, a checkout-relative script path or ``-m dotted.module``.
- ``arg_map``: closed grammar (``BINDING_GRAMMAR``). It may only rename or
  permute the oracle's declared parameters: each declared parameter maps to a
  positional index (``int``) or to a keyword name (an identifier; for ``cli``
  a ``--flag``). No literals, expressions, defaults, callables, or routing of
  outputs into inputs. An empty ``arg_map`` passes every parameter by its
  declared name (``cli``: ``--name value``).

Where a binding comes from, and what the base run showed, decides the tier of
a check. Tiers rest on runtime evidence, never on a static reading of files:
the harness resolves a symbol in the target process and reports it resolved
only when the code it found is a file of the checkout under test
(``boundary/harness.py``).

- ``A``: admission ran the oracle on the base through its default binding and
  the target resolved there, or it was missing there and the constructor
  declared that the criterion names it (``target_named_in_criterion``;
  ``oracle.base_run_tier``).
- ``A_prime``: the worker declared a binding (typed evidence ``entry_points``)
  that satisfies the grammar, and the frozen oracle run through it on the
  base behaves as the oracle's role requires (``admit_binding`` in
  ``boundary/admission.py``).
- ``U``: unverified; no binding where one is needed (``no_binding``), or the
  criterion has no executable check.
- ``S``: a model-written script check. It makes no claim about its target: its
  pass is advisory and only its failure counts, so no tier proof is needed.
- ``C``: excluded at admission (per-check admission, ``boundary/per_check.py``).

Nothing here imports or executes artifact code.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

BINDING_GRAMMAR = "ouroboros.binding_grammar.v1"
CHECK_DIR = ".ouroboros_checks"

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_IDENTIFIER = re.compile(rf"^{_IDENT}$")
_DOTTED = re.compile(rf"^{_IDENT}(?:\.{_IDENT})+$")
_MODULE = re.compile(rf"^{_IDENT}(?:\.{_IDENT})*$")
_CLI_FLAG = re.compile(r"^--?[A-Za-z0-9][A-Za-z0-9_-]*$")
_CLI_PATH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$")
_MAX_SYMBOL_CHARS = 200


class CallKind(StrEnum):
    """How the oracle harness invokes the bound target."""

    FUNCTION = "function"
    METHOD = "method"
    CLI = "cli"


class CheckTier(StrEnum):
    """Where the target of a check came from (ASCII values; ``A_prime`` renders as A')."""

    A = "A"
    A_PRIME = "A_prime"
    U = "U"
    S = "S"
    """A model-written script check: advisory pass, authoritative failure, no target claim."""
    C = "C"

    @property
    def label(self) -> str:
        """Display form: ``A``, ``A'``, ``U``, ``S``, ``C``."""
        return "A'" if self is CheckTier.A_PRIME else self.value


class BindingSource(StrEnum):
    """Who supplied the binding a check ran through."""

    DEFAULT = "default"
    DECLARED = "declared"


class Binding(BaseModel):
    """A validated binding: pure data, no code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    criterion_key: str = Field(..., min_length=1)
    symbol: str = Field(..., min_length=1, max_length=_MAX_SYMBOL_CHARS)
    arg_map: dict[str, int | str] = Field(default_factory=dict)
    call_kind: CallKind

    @model_validator(mode="after")
    def _closed_grammar(self) -> Binding:
        # The model owns the grammar, so a binding built directly or loaded
        # from a stored package is held to the same rules as ``parse_binding``.
        _validate_symbol(self.symbol, self.call_kind)
        _validate_arg_map_values(self.arg_map, self.call_kind)
        return self

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form (sorted ``arg_map``)."""
        return {
            "criterion_key": self.criterion_key,
            "symbol": self.symbol,
            "arg_map": {key: self.arg_map[key] for key in sorted(self.arg_map)},
            "call_kind": self.call_kind.value,
        }

    def describe(self) -> str:
        """One-line human form, for repair messages and run output."""
        if not self.arg_map:
            return f"{self.call_kind.value} {self.symbol}"
        mapping = ", ".join(f"{key}->{self.arg_map[key]}" for key in sorted(self.arg_map))
        return f"{self.call_kind.value} {self.symbol} ({mapping})"


class BindingError(ValueError):
    """A raw binding does not satisfy the closed grammar."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_ALLOWED_KEYS = frozenset({"symbol", "arg_map", "call_kind", "criterion", "criterion_key"})


def _validate_symbol(symbol: object, call_kind: CallKind) -> str:
    if not isinstance(symbol, str) or not symbol or len(symbol) > _MAX_SYMBOL_CHARS:
        raise BindingError("symbol_not_a_string")
    if call_kind is CallKind.CLI:
        if symbol.startswith("-m "):
            if not _MODULE.fullmatch(symbol[3:]):
                raise BindingError("cli_module_not_dotted")
            return symbol
        if not _CLI_PATH.fullmatch(symbol):
            raise BindingError("cli_path_invalid")
        parts = PurePosixPath(symbol).parts
        if symbol.startswith("/") or any(part in {"..", "."} for part in parts):
            raise BindingError("cli_path_invalid")
        return symbol
    if not _DOTTED.fullmatch(symbol):
        raise BindingError("symbol_not_dotted_path")
    if call_kind is CallKind.METHOD and symbol.count(".") < 2:
        raise BindingError("method_symbol_needs_module_class_method")
    return symbol


def is_dotted_symbol(value: object) -> bool:
    """Whether ``value`` is a dotted import path the binding grammar accepts (``a.b``)."""
    try:
        _validate_symbol(value, CallKind.FUNCTION)
    except BindingError:
        return False
    return True


def _validate_arg_map_values(arg_map: Mapping[str, object], call_kind: CallKind) -> None:
    """The grammar of ``arg_map`` values: unique positions from 0, or unique names."""
    positions: list[int] = []
    names: list[str] = []
    for value in arg_map.values():
        if isinstance(value, bool):
            raise BindingError("arg_map_value_not_a_position_or_name")
        if isinstance(value, int):
            if value < 0:
                raise BindingError("arg_map_position_out_of_range")
            positions.append(value)
        elif isinstance(value, str):
            pattern = _CLI_FLAG if call_kind is CallKind.CLI else _IDENTIFIER
            if not pattern.fullmatch(value):
                raise BindingError("arg_map_value_not_a_position_or_name")
            names.append(value)
        else:
            raise BindingError("arg_map_value_not_a_position_or_name")
    if len(set(positions)) != len(positions) or len(set(names)) != len(names):
        raise BindingError("arg_map_targets_not_unique")
    if positions and sorted(positions) != list(range(len(positions))):
        raise BindingError("arg_map_positions_not_contiguous")


def _validate_arg_map(
    raw: object, params: Sequence[str], call_kind: CallKind
) -> dict[str, int | str]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise BindingError("arg_map_not_an_object")
    if not raw:
        return {}
    if set(raw) != set(params):
        raise BindingError("arg_map_keys_differ_from_declared_parameters")
    for value in raw.values():
        if isinstance(value, int) and not isinstance(value, bool) and value >= len(params):
            raise BindingError("arg_map_position_out_of_range")
    _validate_arg_map_values(raw, call_kind)
    return {str(key): value for key, value in raw.items() if isinstance(value, int | str)}


def parse_binding(
    raw: object,
    *,
    criterion_key: str,
    params: Sequence[str],
    call_kind: CallKind | None = None,
) -> Binding:
    """Parse a raw binding against the closed grammar; raise ``BindingError``.

    ``call_kind`` (the oracle's) must match the raw value when the raw value
    carries one; a binding cannot switch a function oracle to a CLI.
    """
    if not isinstance(raw, Mapping):
        raise BindingError("binding_not_an_object")
    extra = set(raw) - _ALLOWED_KEYS
    if extra:
        raise BindingError("binding_has_unknown_keys")
    raw_kind = raw.get("call_kind", call_kind.value if call_kind else None)
    try:
        kind = CallKind(str(raw_kind))
    except ValueError as exc:
        raise BindingError("call_kind_invalid") from exc
    if call_kind is not None and kind is not call_kind:
        raise BindingError("call_kind_differs_from_oracle")
    symbol = _validate_symbol(raw.get("symbol"), kind)
    arg_map = _validate_arg_map(raw.get("arg_map"), params, kind)
    return Binding(criterion_key=criterion_key, symbol=symbol, arg_map=arg_map, call_kind=kind)


# --------------------------------------------------------------------------
# Declared bindings (worker typed evidence ``entry_points``)


class BindingValidation(BaseModel):
    """Outcome of validating one declared binding (grammar, then the base run)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    criterion_key: str
    valid: bool
    reason: str
    binding: Binding | None = None
    indeterminate: bool = False


def declared_entry_points(evidence: Any) -> list[Any]:
    """The raw ``entry_points`` list from a typed evidence record or mapping."""
    getter = getattr(evidence, "get", None)
    if getter is None:
        return []
    raw = getter("entry_points")
    if isinstance(raw, Mapping):
        return [raw]
    if isinstance(raw, list):
        return list(raw)
    return []


def parse_declared_binding(
    raw: object,
    *,
    criterion_key: str,
    params: Sequence[str],
    call_kind: CallKind,
) -> BindingValidation:
    """The grammar check of a worker-declared binding (``binding_invalid:<reason>`` otherwise).

    Nothing about the target is decided here. Whether the binding reaches the
    artifact, and reaches it inside the checkout, is shown by running the
    frozen oracle through it: once on the base (``admission.admit_binding``,
    which must match the oracle's role) and then on the candidate.
    """
    try:
        binding = parse_binding(
            raw, criterion_key=criterion_key, params=params, call_kind=call_kind
        )
    except BindingError as exc:
        return BindingValidation(
            criterion_key=criterion_key, valid=False, reason=f"binding_invalid:{exc.reason}"
        )
    return BindingValidation(
        criterion_key=criterion_key, valid=True, reason="binding_grammar_ok", binding=binding
    )


@dataclass(frozen=True, slots=True)
class TierAssignment:
    """The tier of one oracle check and the binding it runs through (if any)."""

    criterion_key: str
    check_id: str | None
    tier: CheckTier
    binding: Binding | None
    binding_source: BindingSource | None
    status_hint: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "criterion_key": self.criterion_key,
            "check_id": self.check_id,
            "tier": self.tier.value,
            "binding_source": self.binding_source.value if self.binding_source else None,
            "binding": self.binding.to_dict() if self.binding else None,
            "status_hint": self.status_hint,
            "reason": self.reason,
        }


def assign_tier(
    *,
    criterion_key: str,
    check_id: str | None,
    default_binding: Binding | None,
    default_tier_a: bool,
    declared: BindingValidation | None,
) -> TierAssignment:
    """Tier rule for one oracle check.

    - admission gave the check tier ``A`` (``default_tier_a``, from the base
      run: ``oracle.base_run_tier``): ``A`` through the default binding;
    - otherwise a declared binding that validated (grammar and base run):
      ``A_prime`` through it;
    - a declared binding that did not validate: ``U`` with ``status_hint``
      ``indeterminate`` and its ``binding_invalid:*`` (or
      ``binding_admission_timeout``) reason;
    - no binding where one is needed: ``U`` with ``status_hint``
      ``unverified`` and reason ``no_binding``.
    """
    if default_binding is not None and default_tier_a:
        return TierAssignment(
            criterion_key,
            check_id,
            CheckTier.A,
            default_binding,
            BindingSource.DEFAULT,
            "run",
            "default_binding_resolves",
        )
    if declared is not None and declared.valid and declared.binding is not None:
        return TierAssignment(
            criterion_key,
            check_id,
            CheckTier.A_PRIME,
            declared.binding,
            BindingSource.DECLARED,
            "run",
            declared.reason,
        )
    if declared is not None:
        return TierAssignment(
            criterion_key,
            check_id,
            CheckTier.U,
            declared.binding,
            BindingSource.DECLARED,
            "indeterminate",
            declared.reason,
        )
    return TierAssignment(
        criterion_key, check_id, CheckTier.U, None, None, "unverified", "no_binding"
    )


ENTRY_POINTS_FIELD = "entry_points"


def entry_points_request(interface: Mapping[str, Any] | None = None) -> str:
    """The worker instruction for the optional ``entry_points`` evidence field.

    ``interface`` (``OracleSpec.interface()``: call kind and input names, no
    cases) lets the worker map the check's inputs onto its own parameters.
    """
    inputs = ""
    if interface and interface.get("params"):
        names = ", ".join(str(name) for name in interface["params"])
        inputs = (
            f" The check calls it as a {interface.get('call_kind', 'function')} with the inputs "
            f"({names}); map each input to your parameter."
        )
    return (
        "\nThe result of this criterion is checked through its entry point. Also include "
        '"entry_points" in the evidence JSON: a list with one object {"symbol": dotted import '
        "path of the function or Class.method that implements this criterion (for a command: "
        'a script path relative to the working directory, or "-m package.module"), '
        '"call_kind": "function", "method" or "cli", "arg_map": {input: 0-based position or '
        "parameter name (a command: --flag)}}. Omit arg_map when your parameters carry the "
        f"input names in order.{inputs} arg_map may only rename or reorder inputs: no values, "
        "defaults or code. Never point it at a test or a file under .ouroboros_checks.\n"
    )


def tier_summary(tiers: Iterable[CheckTier]) -> dict[str, int]:
    """Counts per tier, every tier present (``A``, ``A_prime``, ``U``, ``S``, ``C``)."""
    counts = {tier.value: 0 for tier in CheckTier}
    for tier in tiers:
        counts[tier.value] += 1
    return counts


__all__ = [
    "BINDING_GRAMMAR",
    "Binding",
    "BindingError",
    "BindingSource",
    "BindingValidation",
    "CallKind",
    "CheckTier",
    "TierAssignment",
    "assign_tier",
    "ENTRY_POINTS_FIELD",
    "declared_entry_points",
    "entry_points_request",
    "parse_binding",
    "parse_declared_binding",
    "tier_summary",
]
