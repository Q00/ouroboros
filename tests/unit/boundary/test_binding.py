"""Bindings: closed grammar, static validation, and tier assignment."""

from __future__ import annotations

from pathlib import Path

import pytest

from ouroboros.boundary.binding import (
    Binding,
    BindingError,
    BindingValidation,
    CallKind,
    CheckTier,
    assign_tier,
    default_binding_resolves,
    entry_points_request,
    locate_symbol,
    parse_binding,
    script_check_tier,
    symbol_named_in_text,
    tier_summary,
    validate_declared_binding_static,
)
from ouroboros.boundary.tree import tree_manifest

PARAMS = ("value", "low", "high")


def _parse(raw: object, kind: CallKind = CallKind.FUNCTION) -> Binding:
    return parse_binding(raw, criterion_key="k1", params=PARAMS, call_kind=kind)


def test_grammar_accepts_renames_and_permutations() -> None:
    binding = _parse(
        {"symbol": "mathutils.clamp", "arg_map": {"value": 0, "low": "lo", "high": "hi"}}
    )
    assert binding.arg_map == {"value": 0, "low": "lo", "high": "hi"}
    assert _parse({"symbol": "pkg.mod.fn"}).arg_map == {}
    permuted = _parse({"symbol": "m.f", "arg_map": {"value": 2, "low": 0, "high": 1}})
    assert permuted.describe() == "function m.f (high->1, low->0, value->2)"


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        # A literal injected as an argument (the value never reaches the target).
        ({"symbol": "m.f", "arg_map": {"value": 0, "low": 1, "high": 10}}, "position_out_of_range"),
        ({"symbol": "m.f", "arg_map": {"value": 0, "low": 1, "high": "10"}}, "not_a_position"),
        ({"symbol": "m.f", "arg_map": {"value": 0, "low": 1, "high": {"literal": 10}}}, "not_a"),
        ({"symbol": "m.f", "arg_map": {"value": 0, "low": 1, "high": True}}, "not_a_position"),
        ({"symbol": "m.f", "arg_map": {"value": 0, "low": 1, "high": 2.0}}, "not_a_position"),
        # A lambda, in the map or as the symbol.
        ({"symbol": "m.f", "arg_map": {"value": "lambda v: 10", "low": 1, "high": 2}}, "not_a"),
        ({"symbol": "lambda v, lo, hi: min(hi, v)", "arg_map": {}}, "symbol_not_dotted_path"),
        # A nested call, in the map or as the symbol.
        ({"symbol": "m.f", "arg_map": {"value": "min(high, value)", "low": 1, "high": 2}}, "not_a"),
        ({"symbol": "m.wrap(m.f)"}, "symbol_not_dotted_path"),
        # Defaults, routing, and anything else outside the grammar.
        ({"symbol": "m.f", "arg_map": {"value": 0, "low": 1}}, "keys_differ"),
        ({"symbol": "m.f", "arg_map": {"value": 0, "low": 0, "high": 1}}, "not_unique"),
        ({"symbol": "m.f", "arg_map": {"value": 0, "low": 2, "high": "high"}}, "not_contiguous"),
        ({"symbol": "m.f", "defaults": {"high": 10}}, "unknown_keys"),
        ({"symbol": "m.f", "call_kind": "cli"}, "call_kind_differs"),
        ({"symbol": "f"}, "symbol_not_dotted_path"),
        ("m.f", "binding_not_an_object"),
    ],
)
def test_grammar_rejects_everything_but_rename_or_permute(raw: object, reason: str) -> None:
    with pytest.raises(BindingError) as caught:
        _parse(raw)
    assert reason in caught.value.reason


def test_cli_grammar() -> None:
    binding = _parse(
        {
            "symbol": "tools/clamp.py",
            "call_kind": "cli",
            "arg_map": {"value": 0, "low": "--low", "high": "--high"},
        },
        CallKind.CLI,
    )
    assert binding.call_kind is CallKind.CLI
    assert _parse({"symbol": "-m pkg.cli", "call_kind": "cli"}, CallKind.CLI).symbol == "-m pkg.cli"
    for bad in ("../x.py", "/abs/x.py", "x.py; rm -rf /", "-m pkg; ls"):
        with pytest.raises(BindingError):
            _parse({"symbol": bad, "call_kind": "cli"}, CallKind.CLI)
    with pytest.raises(BindingError):
        _parse(
            {
                "symbol": "x.py",
                "call_kind": "cli",
                "arg_map": {"value": 0, "low": 1, "high": "high"},
            },
            CallKind.CLI,
        )


def _repo(root: Path, files: dict[str, str]) -> Path:
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return root


def test_locate_symbol_statically(tmp_path: Path) -> None:
    root = _repo(
        tmp_path / "r",
        {
            "mathutils.py": "def clamp(v, lo, hi):\n    return v\n\nclass Box:\n    def area(self):\n        return 1\n",
            "src/geo/__init__.py": "from .shapes import circle\n",
            "src/geo/shapes.py": "def circle(r):\n    return 3 * r * r\n",
            "tools/run.py": "print(1)\n",
        },
    )
    assert locate_symbol(root, "mathutils.clamp", CallKind.FUNCTION).path == "mathutils.py"
    assert locate_symbol(root, "mathutils.Box.area", CallKind.METHOD).qualname == "Box.area"
    reexported = locate_symbol(root, "geo.circle", CallKind.FUNCTION)
    assert reexported is not None and reexported.path == "src/geo/shapes.py"
    assert locate_symbol(root, "tools/run.py", CallKind.CLI) is not None
    assert locate_symbol(root, "mathutils.lerp", CallKind.FUNCTION) is None
    assert locate_symbol(root, "mathutils.clamp", CallKind.METHOD) is None
    assert locate_symbol(root, "nope/run.py", CallKind.CLI) is None


def test_symbol_named_in_text() -> None:
    assert symbol_named_in_text("mathutils.lerp", "add lerp(a, b, t) to mathutils")
    assert not symbol_named_in_text("mathutils.lerp", "add linear interpolation")
    assert not symbol_named_in_text("m.clamp", "clamped values")


def test_default_binding_resolves_at_base_or_by_name(tmp_path: Path) -> None:
    base = _repo(tmp_path / "base", {"mathutils.py": "def clamp(v, lo, hi):\n    return v\n"})
    existing = _parse({"symbol": "mathutils.clamp"})
    named = _parse({"symbol": "mathutils.lerp"})
    open_name = _parse({"symbol": "mathutils.interpolate"})
    assert default_binding_resolves(existing, base, "fix the bound")
    assert default_binding_resolves(named, base, "add lerp(a, b, t)")
    assert not default_binding_resolves(open_name, base, "add linear interpolation")


def _validate(raw: object, artifact: Path, base: Path, **kwargs: object) -> BindingValidation:
    return validate_declared_binding_static(
        raw,
        criterion_key="k1",
        params=PARAMS,
        call_kind=CallKind.FUNCTION,
        artifact=artifact,
        base=base,
        base_manifest=tree_manifest(base),
        **kwargs,
    )


@pytest.fixture
def pair(tmp_path: Path) -> tuple[Path, Path]:
    base = _repo(
        tmp_path / "base",
        {
            "mathutils.py": "def clamp(v, lo, hi):\n    return v\n",
            "other.py": "def keep(x):\n    return x\n",
        },
    )
    artifact = _repo(
        tmp_path / "artifact",
        {
            "mathutils.py": "def clamp(v, lo, hi):\n    return max(lo, min(hi, v))\n",
            "other.py": "def keep(x):\n    return x\n",
            "interp.py": "def mix(a, b, t):\n    return a + (b - a) * t\n",
            ".ouroboros_checks/fake.py": "def mix(a, b, t):\n    return 0\n",
        },
    )
    return base, artifact


def test_declared_binding_introduced_by_the_diff_is_valid(pair: tuple[Path, Path]) -> None:
    base, artifact = pair
    result = _validate(
        {"symbol": "interp.mix", "arg_map": {"value": 0, "low": 1, "high": 2}}, artifact, base
    )
    assert result.valid and result.changed_by_artifact and result.exists_at_base is False
    assert result.location == "interp.py"


def test_declared_binding_modified_by_the_diff_is_valid(pair: tuple[Path, Path]) -> None:
    base, artifact = pair
    result = _validate({"symbol": "mathutils.clamp"}, artifact, base)
    assert result.valid and result.exists_at_base and result.changed_by_artifact


def test_declared_binding_existing_at_base_is_statically_valid(pair: tuple[Path, Path]) -> None:
    base, artifact = pair
    # Unchanged base code passes the static rule; the base run
    # (admission.admit_binding) is what refuses it when it already passes.
    result = _validate({"symbol": "other.keep"}, artifact, base)
    assert result.valid and result.exists_at_base and not result.changed_by_artifact


def test_declared_binding_under_the_check_dir_is_refused(pair: tuple[Path, Path]) -> None:
    base, artifact = pair
    result = _validate({"symbol": ".ouroboros_checks.fake.mix"}, artifact, base)
    assert not result.valid and result.reason.startswith("binding_invalid:")
    cli = validate_declared_binding_static(
        {"symbol": ".ouroboros_checks/fake.py", "call_kind": "cli"},
        criterion_key="k1",
        params=PARAMS,
        call_kind=CallKind.CLI,
        artifact=artifact,
        base=base,
    )
    assert not cli.valid


def test_wrong_symbol_is_refused(pair: tuple[Path, Path]) -> None:
    base, artifact = pair
    result = _validate({"symbol": "interp.blend"}, artifact, base)
    assert not result.valid and result.reason == "binding_invalid:symbol_not_found"
    bad_grammar = _validate(
        {"symbol": "interp.mix", "arg_map": {"value": "1+1", "low": 1, "high": 2}}, artifact, base
    )
    assert bad_grammar.reason.startswith("binding_invalid:arg_map")


def test_tier_assignment_matrix() -> None:
    default = _parse({"symbol": "m.f"})
    declared = _parse({"symbol": "m.g"})
    valid = BindingValidation(
        criterion_key="k1", valid=True, reason="binding_admitted_on_base", binding=declared
    )
    invalid = BindingValidation(
        criterion_key="k1",
        valid=False,
        reason="binding_invalid:binding_passes_on_base",
        binding=declared,
    )
    timeout = BindingValidation(
        criterion_key="k1", valid=False, indeterminate=True, reason="binding_admission_timeout"
    )

    a = assign_tier(
        criterion_key="k1",
        check_id="o1",
        default_binding=default,
        default_resolves=True,
        declared=valid,
    )
    assert (a.tier, a.binding, a.status_hint) == (CheckTier.A, default, "run")
    a_prime = assign_tier(
        criterion_key="k1",
        check_id="o1",
        default_binding=default,
        default_resolves=False,
        declared=valid,
    )
    assert (a_prime.tier, a_prime.binding, a_prime.binding_source.value) == (
        CheckTier.A_PRIME,
        declared,
        "declared",
    )
    bad = assign_tier(
        criterion_key="k1",
        check_id="o1",
        default_binding=default,
        default_resolves=False,
        declared=invalid,
    )
    assert (bad.tier, bad.status_hint, bad.reason) == (
        CheckTier.U,
        "indeterminate",
        "binding_invalid:binding_passes_on_base",
    )
    late = assign_tier(
        criterion_key="k1",
        check_id="o1",
        default_binding=default,
        default_resolves=False,
        declared=timeout,
    )
    assert (late.status_hint, late.reason) == ("indeterminate", "binding_admission_timeout")
    none = assign_tier(
        criterion_key="k1",
        check_id="o1",
        default_binding=default,
        default_resolves=False,
        declared=None,
    )
    assert (none.tier, none.status_hint, none.reason) == (CheckTier.U, "unverified", "no_binding")
    assert CheckTier.A_PRIME.label == "A'" and CheckTier.A_PRIME.value == "A_prime"
    assert tier_summary([CheckTier.A, CheckTier.A, CheckTier.U]) == {
        "A": 2,
        "A_prime": 0,
        "U": 1,
        "C": 0,
    }


def test_script_check_tier_follows_its_imports(tmp_path: Path) -> None:
    base = _repo(tmp_path / "base", {"mathutils.py": "def clamp(v, lo, hi):\n    return v\n"})
    existing = "import sys\nfrom mathutils import clamp\n"
    named = "from mathutils import lerp\n"
    unnamed = "from mathutils import interpolate\n"
    assert script_check_tier(existing, base=base, criterion_text="fix clamp")[0] is CheckTier.A
    assert script_check_tier(named, base=base, criterion_text="add lerp(a, b, t)")[0] is CheckTier.A
    tier, reason = script_check_tier(unnamed, base=base, criterion_text="add linear interpolation")
    assert tier is CheckTier.U and reason == "no_binding:mathutils.interpolate"


def test_entry_points_request_names_the_inputs_only() -> None:
    text = entry_points_request({"call_kind": "function", "params": ["a", "b", "t"]})
    assert '"entry_points"' in text and "(a, b, t)" in text
    assert "expect" not in text and "held" not in text
    assert "(a, b, t)" not in entry_points_request(None)
