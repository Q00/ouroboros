"""Bindings: closed grammar, declared-binding grammar, and tier assignment."""

from __future__ import annotations

import json

from pydantic import ValidationError
import pytest

from ouroboros.boundary.binding import (
    Binding,
    BindingError,
    BindingValidation,
    CallKind,
    CheckTier,
    assign_tier,
    entry_points_request,
    parse_binding,
    parse_declared_binding,
    tier_summary,
)
from ouroboros.boundary.oracle import ORACLE_DATA_PATH
from ouroboros.boundary.oracle_build import assemble_package, build_oracle_spec
from ouroboros.boundary.package import CheckPackage, CheckRole, sha256_bytes

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


def _declared(raw: object, kind: CallKind = CallKind.FUNCTION) -> BindingValidation:
    return parse_declared_binding(raw, criterion_key="k1", params=PARAMS, call_kind=kind)


def test_a_declared_binding_is_checked_for_its_grammar_only() -> None:
    # Whether it reaches the artifact, inside the checkout, is shown by running
    # the oracle through it (admission.admit_binding, then the candidate run).
    ok = _declared({"symbol": "interp.mix", "arg_map": {"value": 0, "low": 1, "high": 2}})
    assert ok.valid and ok.reason == "binding_grammar_ok" and ok.binding is not None
    bad = _declared({"symbol": "interp.mix", "arg_map": {"value": "1+1", "low": 1, "high": 2}})
    assert not bad.valid and bad.reason.startswith("binding_invalid:arg_map")


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        ({"symbol": ".ouroboros_checks.fake.mix"}, CallKind.FUNCTION),
        ({"symbol": ".ouroboros_checks/fake.py", "call_kind": "cli"}, CallKind.CLI),
        ({"symbol": "/tmp/outside.py", "call_kind": "cli"}, CallKind.CLI),
        ({"symbol": "../outside.py", "call_kind": "cli"}, CallKind.CLI),
    ],
)
def test_a_declared_binding_outside_the_grammar_is_refused(raw: object, kind: CallKind) -> None:
    result = _declared(raw, kind)
    assert not result.valid and result.reason.startswith("binding_invalid:")


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
        default_tier_a=True,
        declared=valid,
    )
    assert (a.tier, a.binding, a.status_hint) == (CheckTier.A, default, "run")
    a_prime = assign_tier(
        criterion_key="k1",
        check_id="o1",
        default_binding=default,
        default_tier_a=False,
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
        default_tier_a=False,
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
        default_tier_a=False,
        declared=timeout,
    )
    assert (late.status_hint, late.reason) == ("indeterminate", "binding_admission_timeout")
    none = assign_tier(
        criterion_key="k1",
        check_id="o1",
        default_binding=default,
        default_tier_a=False,
        declared=None,
    )
    assert (none.tier, none.status_hint, none.reason) == (CheckTier.U, "unverified", "no_binding")
    assert CheckTier.A_PRIME.label == "A'" and CheckTier.A_PRIME.value == "A_prime"
    assert tier_summary([CheckTier.A, CheckTier.A, CheckTier.U, CheckTier.S]) == {
        "A": 2,
        "A_prime": 0,
        "U": 1,
        "S": 1,
        "C": 0,
    }


def test_entry_points_request_names_the_inputs_only() -> None:
    text = entry_points_request({"call_kind": "function", "params": ["a", "b", "t"]})
    assert '"entry_points"' in text and "(a, b, t)" in text
    assert "expect" not in text and "held" not in text
    assert "(a, b, t)" not in entry_points_request(None)


def test_the_binding_model_owns_the_closed_grammar() -> None:
    # Direct construction is held to the grammar parse_binding enforces.
    for symbol in ("/tmp/foreign.py", "../outside.py", "./x.py"):
        with pytest.raises(ValidationError):
            Binding(criterion_key="k1", symbol=symbol, call_kind=CallKind.CLI)
    with pytest.raises(ValidationError):
        Binding(criterion_key="k1", symbol="not a path", call_kind=CallKind.FUNCTION)
    with pytest.raises(ValidationError):
        Binding(criterion_key="k1", symbol="m.f", arg_map={"a": 1}, call_kind=CallKind.FUNCTION)
    assert Binding(criterion_key="k1", symbol="tool.py", call_kind=CallKind.CLI).symbol == (
        "tool.py"
    )


def test_a_stored_package_cannot_smuggle_an_absolute_cli_target(seed) -> None:
    # The bot's probe: build a valid CLI oracle package, rewrite the stored
    # binding to an absolute path, and load it canonically.
    spec = build_oracle_spec(
        seed,
        criterion_index=0,
        check_id="oracle_1",
        call_kind="cli",
        params=("a", "b"),
        default_binding={"symbol": "tool.py"},
        cases=[
            {
                "case_id": "c1",
                "held_out": True,
                "args": {"a": 1, "b": 2},
                "expect": {"kind": "cli", "exit_code": 0},
            }
        ],
    )
    package = assemble_package(
        seed,
        input_digest="1" * 64,
        generator="test",
        oracles=((spec, CheckRole.REPRODUCTION),),
    )
    data = json.loads(package.to_json_bytes())
    assert CheckPackage.model_validate(data).sha256 == package.sha256
    data["oracles"][0]["default_binding"]["symbol"] = "/tmp/foreign.py"
    # Keep the frozen oracle data file consistent with the edit, so only the
    # binding grammar can refuse the package.
    for item in data["files"]:
        if item["path"] == ORACLE_DATA_PATH:
            oracle_data = json.loads(item["content"])
            oracle_data["oracles"][0]["default_binding"]["symbol"] = "/tmp/foreign.py"
            item["content"] = (
                json.dumps(oracle_data, sort_keys=True, indent=1, ensure_ascii=False) + "\n"
            )
            item["sha256"] = sha256_bytes(item["content"].encode("utf-8"))
    with pytest.raises(ValidationError, match="cli_path_invalid"):
        CheckPackage.model_validate(data)
    with pytest.raises(ValidationError, match="cli_path_invalid"):
        CheckPackage.model_validate_json(json.dumps(data))
