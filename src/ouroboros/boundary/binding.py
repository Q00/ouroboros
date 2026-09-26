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

Where a binding comes from decides the tier of a check:

- ``A``: the constructor's default binding resolves, because its symbol exists
  at the base or is named in the criterion text.
- ``A_prime``: the worker declared a binding (typed evidence ``entry_points``)
  and it validated: grammar, the symbol exists in the artifact, it is new or
  changed by the artifact or exists at the base, it does not live under the
  check directory, and the frozen oracle run through it on the base behaves as
  the oracle's role requires (``admit_binding`` in ``boundary/admission.py``).
- ``U``: unverified; no binding where one is needed (``no_binding``), or the
  criterion has no executable check.
- ``C``: rejected at admission (``boundary/admission_rules.py``).

Nothing here imports or executes artifact code: symbol resolution is a static
walk over the checkout's Python sources.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
import hashlib
from pathlib import Path, PurePosixPath
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

BINDING_GRAMMAR = "ouroboros.binding_grammar.v1"
CHECK_DIR = ".ouroboros_checks"

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_IDENTIFIER = re.compile(rf"^{_IDENT}$")
_DOTTED = re.compile(rf"^{_IDENT}(?:\.{_IDENT})+$")
_MODULE = re.compile(rf"^{_IDENT}(?:\.{_IDENT})*$")
_CLI_FLAG = re.compile(r"^--?[A-Za-z0-9][A-Za-z0-9_-]*$")
_CLI_PATH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$")
_MAX_SYMBOL_CHARS = 200
_MAX_REEXPORT_HOPS = 3
_SOURCE_ROOTS = (".", "src")
_SKIPPED_DIRS = frozenset({".git", "__pycache__", ".venv", "venv", "node_modules", CHECK_DIR})


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
    C = "C"

    @property
    def label(self) -> str:
        """Display form: ``A``, ``A'``, ``U``, ``C``."""
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


def _validate_arg_map(
    raw: object, params: Sequence[str], call_kind: CallKind
) -> dict[str, int | str]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise BindingError("arg_map_not_an_object")
    if not raw:
        return {}
    keys = set(raw)
    if keys != set(params):
        raise BindingError("arg_map_keys_differ_from_declared_parameters")
    positions: list[int] = []
    names: list[str] = []
    result: dict[str, int | str] = {}
    for key, value in raw.items():
        if isinstance(value, bool):
            raise BindingError("arg_map_value_not_a_position_or_name")
        if isinstance(value, int):
            if not 0 <= value < len(params):
                raise BindingError("arg_map_position_out_of_range")
            positions.append(value)
        elif isinstance(value, str):
            pattern = _CLI_FLAG if call_kind is CallKind.CLI else _IDENTIFIER
            if not pattern.fullmatch(value):
                raise BindingError("arg_map_value_not_a_position_or_name")
            names.append(value)
        else:
            raise BindingError("arg_map_value_not_a_position_or_name")
        result[str(key)] = value
    if len(set(positions)) != len(positions) or len(set(names)) != len(names):
        raise BindingError("arg_map_targets_not_unique")
    if positions and sorted(positions) != list(range(len(positions))):
        raise BindingError("arg_map_positions_not_contiguous")
    return result


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
# Static symbol resolution (no import, no execution)


@dataclass(frozen=True, slots=True)
class SymbolLocation:
    """Where a symbol is defined in one checkout."""

    path: str
    qualname: str
    source_sha256: str


def _module_files(root: Path, module: str) -> list[tuple[str, Path]]:
    parts = module.split(".")
    found: list[tuple[str, Path]] = []
    for source_root in _SOURCE_ROOTS:
        base = root if source_root == "." else root / source_root
        for candidate in (
            base.joinpath(*parts).with_suffix(".py"),
            base.joinpath(*parts, "__init__.py"),
        ):
            if candidate.is_file() and not candidate.is_symlink():
                relative = candidate.relative_to(root).as_posix()
                if not _under_skipped(relative):
                    found.append((relative, candidate))
    return found


def _under_skipped(relative: str) -> bool:
    return any(part in _SKIPPED_DIRS for part in PurePosixPath(relative).parts[:-1])


def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
        return None


def _segment_digest(path: Path, node: ast.AST) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        text = ""
    segment = ast.get_source_segment(text, node) or ast.dump(node)
    return hashlib.sha256(segment.encode("utf-8")).hexdigest()


def _binds_name(node: ast.stmt, name: str) -> bool:
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return node.name == name
    if isinstance(node, ast.Assign):
        return any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
    if isinstance(node, ast.AnnAssign):
        return isinstance(node.target, ast.Name) and node.target.id == name
    return False


def _find_in_body(body: Sequence[ast.stmt], name: str) -> ast.stmt | None:
    found: ast.stmt | None = None
    for node in body:
        if _binds_name(node, name):
            found = node  # the last binding wins, as at runtime
    return found


def _reexport(module: str, body: Sequence[ast.stmt], name: str, is_package: bool) -> str | None:
    """``from X import name`` (possibly aliased) at module level: the source module."""
    for node in body:
        if not isinstance(node, ast.ImportFrom):
            continue
        for alias in node.names:
            if (alias.asname or alias.name) != name:
                continue
            if node.level == 0:
                return f"{node.module}.{alias.name}" if node.module else None
            package = module.split(".")
            if not is_package:
                package = package[:-1]
            if node.level > 1:
                package = package[: len(package) - (node.level - 1)]
            target = [*package, *(node.module.split(".") if node.module else [])]
            return ".".join([*target, alias.name]) if target else None
    return None


def locate_symbol(root: Path, symbol: str, call_kind: CallKind) -> SymbolLocation | None:
    """Find where ``symbol`` is defined under ``root`` without importing anything."""
    root = root.resolve()
    if call_kind is CallKind.CLI:
        if symbol.startswith("-m "):
            module = symbol[3:]
            for relative, path in _module_files(root, module):
                return SymbolLocation(relative, module, _file_digest(path))
            for relative, path in _module_files(root, f"{module}.__main__"):
                return SymbolLocation(relative, module, _file_digest(path))
            return None
        path = root / symbol
        if path.is_file() and not path.is_symlink() and not _under_skipped(symbol):
            return SymbolLocation(symbol, symbol, _file_digest(path))
        return None
    return _locate_python(root, symbol, call_kind, hops=0)


def _file_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _locate_python(
    root: Path, symbol: str, call_kind: CallKind, *, hops: int
) -> SymbolLocation | None:
    parts = symbol.split(".")
    for split in range(len(parts) - 1, 0, -1):
        module, chain = ".".join(parts[:split]), parts[split:]
        if call_kind is CallKind.METHOD and len(chain) != 2:
            continue
        if call_kind is CallKind.FUNCTION and len(chain) > 2:
            continue
        for relative, path in _module_files(root, module):
            tree = _parse(path)
            if tree is None:
                continue
            node = _find_in_body(tree.body, chain[0])
            if node is None:
                if hops < _MAX_REEXPORT_HOPS:
                    target = _reexport(module, tree.body, chain[0], path.name == "__init__.py")
                    if target:
                        found = _locate_python(
                            root, ".".join([target, *chain[1:]]), call_kind, hops=hops + 1
                        )
                        if found is not None:
                            return found
                continue
            for name in chain[1:]:
                if not isinstance(node, ast.ClassDef):
                    node = None
                    break
                node = _find_in_body(node.body, name)
                if node is None:
                    break
            if node is None:
                continue
            if call_kind is CallKind.METHOD and not isinstance(
                node, ast.FunctionDef | ast.AsyncFunctionDef
            ):
                continue
            return SymbolLocation(relative, ".".join(chain), _segment_digest(path, node))
    return None


_WORD = re.compile(rf"{_IDENT}")


def symbol_named_in_text(symbol: str, text: str) -> bool:
    """Whether the symbol's final name appears as a whole word in ``text``."""
    if symbol.startswith("-m "):
        name = symbol[3:].rsplit(".", 1)[-1]
    elif "/" in symbol or symbol.endswith(".py"):
        name = PurePosixPath(symbol).name
        return name in text
    else:
        name = symbol.rsplit(".", 1)[-1]
    return name in set(_WORD.findall(text))


def default_binding_resolves(binding: Binding, base: Path, criterion_text: str) -> bool:
    """Tier A rule: the default symbol exists at the base or the criterion names it."""
    return locate_symbol(base, binding.symbol, binding.call_kind) is not None or (
        symbol_named_in_text(binding.symbol, criterion_text)
    )


# --------------------------------------------------------------------------
# Declared bindings (worker typed evidence ``entry_points``)


class BindingValidation(BaseModel):
    """Outcome of validating one declared binding (static part and base run)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    criterion_key: str
    valid: bool
    reason: str
    binding: Binding | None = None
    location: str | None = None
    exists_at_base: bool | None = None
    changed_by_artifact: bool | None = None
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


def _is_under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def validate_declared_binding_static(
    raw: object,
    *,
    criterion_key: str,
    params: Sequence[str],
    call_kind: CallKind,
    artifact: Path,
    base: Path,
    base_manifest: Mapping[str, str] | None = None,
    check_dir: str = CHECK_DIR,
) -> BindingValidation:
    """Grammar plus the static rules for a worker-declared binding.

    Valid when: the grammar holds; the symbol exists in the artifact; its
    defining file is not under ``check_dir``; and it exists at the base or the
    artifact introduced or changed it (new or changed defining file, or a
    changed definition). The base run through the binding is separate
    (``admission.admit_binding``).
    """

    def invalid(reason: str, **extra: Any) -> BindingValidation:
        return BindingValidation(
            criterion_key=criterion_key, valid=False, reason=f"binding_invalid:{reason}", **extra
        )

    try:
        binding = parse_binding(
            raw, criterion_key=criterion_key, params=params, call_kind=call_kind
        )
    except BindingError as exc:
        return invalid(exc.reason)
    if call_kind is CallKind.CLI and _is_under(binding.symbol, check_dir):
        return invalid("binding_in_check_dir", binding=binding)
    location = locate_symbol(artifact, binding.symbol, binding.call_kind)
    if location is None:
        return invalid("symbol_not_found", binding=binding)
    if _is_under(location.path, check_dir):
        return invalid("binding_in_check_dir", binding=binding, location=location.path)
    at_base = locate_symbol(base, binding.symbol, binding.call_kind)
    if base_manifest is not None:
        artifact_digest = _file_digest(artifact / location.path)
        file_changed = base_manifest.get(location.path) != artifact_digest
    else:
        file_changed = at_base is None or at_base.path != location.path
    changed = file_changed or (
        at_base is not None and at_base.source_sha256 != location.source_sha256
    )
    if at_base is None and not changed:
        return invalid("symbol_not_introduced_by_artifact", binding=binding, location=location.path)
    return BindingValidation(
        criterion_key=criterion_key,
        valid=True,
        reason="binding_static_ok",
        binding=binding,
        location=location.path,
        exists_at_base=at_base is not None,
        changed_by_artifact=changed,
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
    default_resolves: bool,
    declared: BindingValidation | None,
) -> TierAssignment:
    """Tier rule for one oracle check.

    - the default binding resolves: ``A`` through the default binding;
    - otherwise a declared binding that validated (static rules and base run):
      ``A_prime`` through it;
    - a declared binding that did not validate: ``U`` with ``status_hint``
      ``indeterminate`` and its ``binding_invalid:*`` (or
      ``binding_admission_timeout``) reason;
    - no binding where one is needed: ``U`` with ``status_hint``
      ``unverified`` and reason ``no_binding``.
    """
    if default_binding is not None and default_resolves:
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


def _workspace_top_level(root: Path, name: str) -> bool:
    for source_root in _SOURCE_ROOTS:
        base = root if source_root == "." else root / source_root
        if (base / f"{name}.py").is_file() or (base / name).is_dir():
            return True
    return False


def script_check_tier(source: str, *, base: Path, criterion_text: str) -> tuple[CheckTier, str]:
    """Tier of a model-written script check: its hard-coded imports are its binding.

    ``A`` when every symbol it imports from outside the standard library
    exists at the base or is named in the criterion text; otherwise ``U``
    (``no_binding``: the script names a target the criterion does not fix, so
    a correct artifact that chose another name would fail it).
    """
    import sys

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return CheckTier.A, "script_unparsed"
    stdlib = sys.stdlib_module_names
    root = base.resolve()
    for node in ast.walk(tree):
        symbols: list[str] = []
        if isinstance(node, ast.Import):
            symbols = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            symbols = [f"{node.module}.{alias.name}" for alias in node.names if alias.name != "*"]
        for symbol in symbols:
            top = symbol.split(".", 1)[0]
            if top in stdlib or top == "__future__":
                continue
            if symbol_named_in_text(symbol, criterion_text):
                continue
            if "." not in symbol:
                if _workspace_top_level(root, symbol):
                    continue
                return CheckTier.U, f"no_binding:{symbol}"
            if _module_files(root, symbol) or locate_symbol(root, symbol, CallKind.FUNCTION):
                continue
            return CheckTier.U, f"no_binding:{symbol}"
    return CheckTier.A, "script_imports_resolve"


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
    """Counts per tier, every tier present (``A``, ``A_prime``, ``U``, ``C``)."""
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
    "SymbolLocation",
    "TierAssignment",
    "assign_tier",
    "ENTRY_POINTS_FIELD",
    "declared_entry_points",
    "entry_points_request",
    "default_binding_resolves",
    "locate_symbol",
    "parse_binding",
    "script_check_tier",
    "symbol_named_in_text",
    "tier_summary",
    "validate_declared_binding_static",
]
