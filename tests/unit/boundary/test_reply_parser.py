"""The canonical constructor-reply parser: total, and refusals are closed codes."""

from __future__ import annotations

import copy
import json
import random
from typing import Any

import pytest

from ouroboros.boundary.oracle_build import (
    DECLARED_NOT_EXECUTABLE,
    ReplyError,
    ReplyFailure,
    normalize_reply,
    package_from_reply,
    reply_failure_reason,
)
from ouroboros.boundary.package import CheckPackage, package_record_bytes, seal_package

from .clamp_fixtures import _oracle, _seed

SECRET = 6173
GOOD = _oracle(1, "oracle_1", "reproduction", (15, 0, 10), 10)


def _with(path: tuple[Any, ...], value: object) -> dict[str, Any]:
    """``{"oracles": [GOOD]}`` with the field at ``path`` (under the oracle) replaced."""
    oracle = copy.deepcopy(GOOD)
    target: Any = oracle
    for key in path[:-1]:
        target = target[key]
    if value is _DROP:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return {"oracles": [oracle]}


_DROP = object()

MALFORMED: list[tuple[object, ReplyFailure]] = [
    ([], ReplyFailure.REPLY_NOT_OBJECT),
    ({"checks": 1}, ReplyFailure.SECTION_NOT_LIST),
    ({"oracles": {"criterion": 1}}, ReplyFailure.SECTION_NOT_LIST),
    ({"oracles": [SECRET]}, ReplyFailure.ENTRY_NOT_OBJECT),
    (_with(("criterion",), "1"), ReplyFailure.CRITERION_INVALID),
    (_with(("criterion",), True), ReplyFailure.CRITERION_INVALID),
    (_with(("role",), f"r{SECRET}"), ReplyFailure.ROLE_INVALID),
    (_with(("call_kind",), SECRET), ReplyFailure.CALL_KIND_INVALID),
    (_with(("params",), ["value", SECRET]), ReplyFailure.PARAMS_INVALID),
    (_with(("default_binding",), f"m.f{SECRET}"), ReplyFailure.BINDING_INVALID),
    (_with(("default_binding",), {"symbol": f"/tmp/{SECRET}.py"}), ReplyFailure.BINDING_INVALID),
    (_with(("target_named_in_criterion",), _DROP), ReplyFailure.TARGET_NAMED_NOT_BOOLEAN),
    (_with(("target_named_in_criterion",), "yes"), ReplyFailure.TARGET_NAMED_NOT_BOOLEAN),
    (_with(("cases",), SECRET), ReplyFailure.CASE_SHAPE),
    (_with(("cases",), []), ReplyFailure.CASE_SHAPE),
    (_with(("cases", 0), SECRET), ReplyFailure.CASE_SHAPE),
    (_with(("cases", 0, "held_out"), _DROP), ReplyFailure.HELD_OUT_NOT_BOOLEAN),
    (_with(("cases", 0, "held_out"), 1), ReplyFailure.HELD_OUT_NOT_BOOLEAN),
    (_with(("cases", 0, "args"), [SECRET]), ReplyFailure.CASE_SHAPE),
    (_with(("cases", 0, "expect"), SECRET), ReplyFailure.CASE_SHAPE),
    (_with(("cases", 0, "expect"), {"kind": f"k{SECRET}"}), ReplyFailure.ORACLE_INVALID),
    ({"checks": [{"check_id": "s", "role": "reproduction"}]}, ReplyFailure.ARGV_INVALID),
    ({"files": [{"path": SECRET, "content": ""}]}, ReplyFailure.FILE_INVALID),
    ({"uncovered": [{"criterion": f"{SECRET}"}]}, ReplyFailure.UNCOVERED_INVALID),
    ({"uncovered": [{"criterion": SECRET}]}, ReplyFailure.CRITERION_INVALID),
    (_with(("criterion",), 0), ReplyFailure.CRITERION_INVALID),
    (_with(("criterion",), -1), ReplyFailure.CRITERION_INVALID),
    ({"uncovered": [{"criterion": 0}]}, ReplyFailure.UNCOVERED_INVALID),
    # Only a held-out case can verify a pass: an oracle of stated cases alone is refused.
    (
        _with(("cases",), [dict(case, held_out=False) for case in GOOD["cases"]]),
        ReplyFailure.ORACLE_WITHOUT_HELD_OUT_CASE,
    ),
]


@pytest.mark.parametrize(("reply", "code"), MALFORMED)
def test_every_malformed_reply_is_a_closed_code_without_its_values(
    reply: object, code: ReplyFailure
) -> None:
    with pytest.raises(ReplyError) as raised:
        package_from_reply(reply, _seed(), input_digest="1" * 64, generator="t")
    assert raised.value.code is code
    assert str(raised.value) == code.value
    reason = reply_failure_reason(raised.value)
    assert reason == f"constructor_reply_invalid:{code.value}"
    assert str(SECRET) not in reason


def test_normalize_keeps_unknown_keys_and_fills_every_section() -> None:
    oracle = {**GOOD, "reference": {"source": "def f(): ...", "symbol": "f"}}
    normalized = normalize_reply({"oracles": [oracle]})
    assert set(normalized) == {"oracles", "checks", "files", "uncovered"}
    assert normalized["oracles"][0]["reference"] == oracle["reference"]
    assert normalized["checks"] == normalized["files"] == normalized["uncovered"] == []


def _persisted(package: CheckPackage) -> str:
    """The record and the summary, with the random package id and Seed digest blinded.

    Both are hex strings that vary from run to run (the id is random, the
    fixture Seed carries its creation time), so a numeric secret can occur in
    them by chance; every other byte is searched.
    """
    text = package_record_bytes(package).decode("utf-8") + json.dumps(package.manifest_summary())
    return text.replace(package.package_id, "<package_id>").replace(
        package.seed_digest, "<seed_digest>"
    )


def test_a_declared_uncovered_reason_is_recorded_as_a_code_not_as_its_text() -> None:
    reply = {"oracles": [GOOD], "uncovered": [{"criterion": 2, "reason": f"secret {SECRET}"}]}
    package = seal_package(package_from_reply(reply, _seed(), input_digest="1" * 64, generator="t"))
    reasons = {item.reason for item in package.uncovered}
    assert DECLARED_NOT_EXECUTABLE in reasons
    assert str(SECRET) not in _persisted(package)


def test_identifiers_the_constructor_chose_never_reach_the_package() -> None:
    # A check id or case id could spell a held-out value; the product mints
    # its own (oracle_<n>, c<position>, script_<n>_<k>, <check>.a<m>).
    oracle = copy.deepcopy(GOOD)
    oracle["check_id"] = f"expected_{SECRET}"
    for case in oracle["cases"]:
        case["case_id"] = f"expected_{SECRET}"
    script = {
        "check_id": f"s{SECRET}",
        "role": "preservation",
        "argv": ["python3", ".ouroboros_checks/probe.py"],
        "target_named_in_criterion": False,
        "assertions": [{"criterion": 2, "assertion_id": f"a{SECRET}", "locator": f"{SECRET}"}],
    }
    reply = {
        "oracles": [oracle],
        "checks": [script],
        "files": [{"path": ".ouroboros_checks/probe.py", "content": "print('ok')\n"}],
    }
    package = seal_package(package_from_reply(reply, _seed(), input_digest="1" * 64, generator="t"))
    assert [check.check_id for check in package.checks] == ["oracle_1", "script_2_1"]
    assert [case.case_id for case in package.oracles[0].cases] == [
        f"c{n}" for n in range(1, len(GOOD["cases"]) + 1)
    ]
    assert [link.assertion_id for link in package.checks[1].assertions] == ["script_2_1.a1"]
    assert package.checks[1].assertions[0].locator is None
    assert str(SECRET) not in _persisted(package)


def test_minting_is_stable_when_a_reply_is_normalized_twice() -> None:
    once = normalize_reply({"oracles": [GOOD, copy.deepcopy(GOOD)]})
    assert [entry["check_id"] for entry in once["oracles"]] == ["oracle_1", "oracle_1_2"]
    assert normalize_reply(once) == once


def _random_json(rng: random.Random, depth: int = 0) -> Any:
    """A random JSON value: every type the reply parser can receive."""
    kinds = ["null", "bool", "int", "float", "str"] + (["list", "object"] if depth < 4 else [])
    kind = rng.choice(kinds)
    if kind == "null":
        return None
    if kind == "bool":
        return rng.random() < 0.5
    if kind == "int":
        return rng.choice([0, -1, 1, 2, 3, SECRET, 2**70, -(2**70)])
    if kind == "float":
        return rng.choice([0.0, -1.5, 1e308, 3.25])
    if kind == "str":
        return rng.choice(["", "x", f"s{SECRET}", "oracle_1", "../x", "/abs", "c1", "returns"])
    if kind == "list":
        return [_random_json(rng, depth + 1) for _ in range(rng.randint(0, 3))]
    keys = [
        "oracles", "checks", "files", "uncovered", "criterion", "role", "call_kind", "params",
        "default_binding", "symbol", "arg_map", "cases", "args", "expect", "kind", "value",
        "held_out", "target_named_in_criterion", "assertions", "argv", "path", "content",
    ]  # fmt: skip
    return {rng.choice(keys): _random_json(rng, depth + 1) for _ in range(rng.randint(0, 4))}


def _mutated(rng: random.Random, value: Any) -> Any:
    """``value`` (a valid reply) with one nested field replaced by random JSON."""
    mutated = copy.deepcopy(value)
    target: Any = mutated
    while True:
        if isinstance(target, dict) and target:
            key = rng.choice(sorted(target))
        elif isinstance(target, list) and target:
            key = rng.randrange(len(target))
        else:
            return mutated
        if rng.random() < 0.4 or not isinstance(target[key], dict | list):
            target[key] = _random_json(rng)
            return mutated
        target = target[key]


@pytest.mark.parametrize("seed_value", range(40))
def test_the_parser_is_total_over_arbitrary_json(seed_value: int) -> None:
    # Whatever JSON the model returns, the only exception is a closed ReplyError.
    rng = random.Random(seed_value)
    valid = {"oracles": [GOOD], "uncovered": [{"criterion": 2}]}
    for reply in [_random_json(rng) for _ in range(25)] + [_mutated(rng, valid) for _ in range(75)]:
        for parse in (
            normalize_reply,
            lambda r: package_from_reply(r, _seed(), input_digest="1" * 64, generator="t"),
        ):
            try:
                parse(reply)
            except ReplyError as exc:
                assert str(exc) == exc.code.value
