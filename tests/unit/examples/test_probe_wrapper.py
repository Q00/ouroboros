"""The probe wrapper's value encoding, called in this process (no subprocess)."""

from __future__ import annotations

import json
import math

import pytest

from ouroboros.examples import probe_wrapper as wrapper
from ouroboros.examples.record import OutcomeRecord, ProbeStatus


def test_plain_json_values_are_kept() -> None:
    value = {"a": [1, 2.5, "x", None, True], "b": {"c": False}}
    assert wrapper.encode(value) == value


def test_a_tuple_is_a_list_and_a_set_is_sorted_by_json_text() -> None:
    assert wrapper.encode((1, (2, 3))) == [1, [2, 3]]
    assert wrapper.encode({3, 1, 2}) == [1, 2, 3]
    assert wrapper.encode(frozenset({"b", "a"})) == ["a", "b"]
    mixed = {1, "1", None}
    assert wrapper.encode(mixed) == sorted(wrapper.encode(list(mixed)), key=wrapper._json_text)


def test_set_items_are_ordered_by_json_text_not_by_value() -> None:
    assert wrapper.encode({5, 100, 10, 1}) == [1, 10, 100, 5]
    assert wrapper.encode({"b", ("a",)}) == ["b", ["a"]]


def test_bool_stays_bool_and_int_subclass_becomes_int() -> None:
    class Flag(int):
        pass

    assert wrapper.encode(True) is True
    encoded = wrapper.encode(Flag(3))
    assert encoded == 3 and type(encoded) is int


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_a_non_finite_float_is_tagged(value: float) -> None:
    encoded = wrapper.encode(value)
    assert encoded == {wrapper.OBJECT_KEY: ["builtins.float", repr(value)]}


def test_an_int_too_long_is_tagged_by_its_bit_length() -> None:
    big = 1 << (wrapper.MAX_INT_BITS + 5)
    assert wrapper.encode(big) == {wrapper.OBJECT_KEY: ["builtins.int", f"{big.bit_length()} bits"]}
    assert wrapper.encode(1 << 100) == 1 << 100


def test_a_dict_with_a_non_string_key_is_tagged() -> None:
    encoded = wrapper.encode({1: "a"})
    assert encoded == {wrapper.OBJECT_KEY: ["builtins.dict", "{1: 'a'}"]}


def test_another_object_is_its_qualified_type_and_repr() -> None:
    class Point:
        def __repr__(self) -> str:
            return "Point(1, 2)"

    encoded = wrapper.encode([Point()])
    assert encoded == [
        {
            wrapper.OBJECT_KEY: [
                f"{__name__}.test_another_object_is_its_qualified_type_and_repr.<locals>.Point",
                "Point(1, 2)",
            ]
        }
    ]


def test_a_repr_that_raises_gives_a_fixed_marker() -> None:
    class Broken:
        def __repr__(self) -> str:
            raise RuntimeError("no")

    tag = wrapper.encode(Broken())[wrapper.OBJECT_KEY]
    assert tag[1].startswith("<unrenderable ") and tag[1].endswith(".Broken>")


def test_a_long_repr_is_cut() -> None:
    class Long:
        def __repr__(self) -> str:
            return "x" * 1000

    assert len(wrapper.encode(Long())[wrapper.OBJECT_KEY][1]) == wrapper.MAX_TEXT


def test_nesting_past_the_depth_limit_is_tagged() -> None:
    nested: list = []
    current = nested
    for _ in range(wrapper.MAX_DEPTH + 5):
        inner: list = []
        current.append(inner)
        current = inner
    text = json.dumps(wrapper.encode(nested))
    assert wrapper.OBJECT_KEY in text


def test_a_self_referencing_list_terminates() -> None:
    loop: list = []
    loop.append(loop)
    assert wrapper.OBJECT_KEY in json.dumps(wrapper.encode(loop))


def test_an_oversized_value_is_replaced_by_its_digest() -> None:
    value = ["y" * 1000] * (wrapper.MAX_VALUE_CHARS // 1000 + 1)
    tag = wrapper.encode(value)[wrapper.OBJECT_KEY]
    assert tag[0] == "builtins.list"
    assert tag[1].startswith("sha256:") and len(tag[1]) == len("sha256:") + 64
    assert wrapper.encode(list(value)) == wrapper.encode(value)


def test_every_encoded_value_builds_an_outcome_record() -> None:
    class Odd:
        pass

    for value in (None, 1, 1.5, "s", [1, {2}], {"k": (1,)}, Odd(), math.nan, {1: 2}):
        record = OutcomeRecord(
            "p", "base", 0, ProbeStatus.RETURNED, {"value": wrapper.encode(value)}
        )
        assert record.digest()


def test_the_exception_type_is_always_module_qualified() -> None:
    assert wrapper._type_name(ValueError("x")) == "builtins.ValueError"
    assert wrapper._type_name(json.JSONDecodeError("m", "d", 0)) == "json.decoder.JSONDecodeError"


def test_a_message_that_raises_falls_back_to_repr_or_a_marker() -> None:
    class Odd(Exception):
        def __str__(self) -> str:
            raise RuntimeError("no")

    assert wrapper._message(Odd("a")) == "Odd('a')"
    assert len(wrapper._message(ValueError("z" * 5000))) == wrapper.MAX_MESSAGE
