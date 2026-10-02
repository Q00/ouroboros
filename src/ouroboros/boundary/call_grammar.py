"""The closed grammar of built oracle inputs, projections and case files (validation only).

An oracle's cases state JSON data: the declared parameters, which the
constructor's reference implementation takes by name. A library call often
needs more than JSON: an object built by a chain of calls (a fitted model,
a figure's sub-part), a read of a non-JSON result (an attribute, a method's
value), or a file a command reads. The constructor writes each of these as
data in the forms below; the product's harness (``boundary/harness.py``)
interprets them in the target process, and nothing here runs anything.

Template (a value the target receives in place of JSON):

- a JSON value;
- ``{"$symbol": "package.module.Name"}``: the object that path imports;
- ``{"$param": "name"}``: the case's value of the declared parameter
  ``name`` (the controller substitutes it before the call is sent, so a
  target never sees a ``$param``);
- ``{"$call": "package.module.factory", "args": [...], "kwargs": {...},
  "then": [read, ...]}``: the callable that path imports, called with the
  templates in ``args`` and ``kwargs``, then each read applied in order to
  the value so far.

Read (one step on an object):

- ``{"attr": "name"}``: the attribute;
- ``{"method": "name", "args": [...], "kwargs": {...}, "keep": bool}``: the
  method's return value, or with ``keep`` true the same object (a call made
  for its effect);
- ``{"item": key}``: ``value[key]`` for an integer or string key;
- ``{"each": [read, ...]}``: the list of those reads applied to every item.

Names are identifiers and never dunder names. Where each form may appear is
the oracle's rule (``boundary/oracle.py``): templates in an oracle's
``inputs`` and ``receiver`` and in the arguments of a read; reads in a
``$call``'s ``then`` and in an oracle's ``project``; case ``args`` and
``init`` stay JSON data with ``$symbol`` only, because they are what the
reference receives.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
import re
from typing import Any

from ouroboros.boundary.binding import is_dotted_symbol
from ouroboros.boundary.harness import (
    CALL_KEYS,
    CALL_REF,
    call_ref,
    param_ref,
    symbol_ref,
)

MAX_DEPTH = 50
MAX_READS = 32
MAX_CASE_FILES = 32
MAX_CASE_FILE_BYTES = 256 * 1024
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FILE_PART = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


class GrammarError(ValueError):
    """A value does not satisfy the closed grammar; ``code`` names the form."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


CALL_INVALID = "call_invalid"
PROJECT_INVALID = "project_invalid"
CASE_FILES_INVALID = "case_files_invalid"


def _name(value: object) -> bool:
    return (
        isinstance(value, str) and bool(_IDENTIFIER.fullmatch(value)) and not value.startswith("__")
    )


def check_template(value: Any, params: Collection[str], *, depth: int = 0) -> None:
    """``value`` is a template whose ``$param`` references name ``params``."""
    if depth > MAX_DEPTH:
        raise GrammarError(CALL_INVALID)
    if isinstance(value, dict):
        if CALL_REF in value:
            _check_call(value, params, depth)
            return
        name = param_ref(value)
        if name is not None:
            if name not in params:
                raise GrammarError(CALL_INVALID)
            return
        ref = symbol_ref(value)
        if ref is not None:
            if not is_dotted_symbol(ref):
                raise GrammarError(CALL_INVALID)
            return
        if not all(isinstance(key, str) for key in value):
            raise GrammarError(CALL_INVALID)
        for item in value.values():
            check_template(item, params, depth=depth + 1)
        return
    if isinstance(value, list):
        for item in value:
            check_template(item, params, depth=depth + 1)
        return
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float) and value == value and abs(value) != float("inf"):
        return
    raise GrammarError(CALL_INVALID)


def _check_arguments(step: Mapping[str, Any], params: Collection[str], depth: int) -> None:
    args, kwargs = step.get("args", []), step.get("kwargs", {})
    if not isinstance(args, list) or not isinstance(kwargs, dict):
        raise GrammarError(CALL_INVALID)
    if not all(isinstance(key, str) and _IDENTIFIER.fullmatch(key) for key in kwargs):
        raise GrammarError(CALL_INVALID)
    check_template(args, params, depth=depth + 1)
    for item in kwargs.values():
        check_template(item, params, depth=depth + 1)


def _check_call(node: Mapping[str, Any], params: Collection[str], depth: int) -> None:
    if not set(node) <= CALL_KEYS or not is_dotted_symbol(node.get(CALL_REF)):
        raise GrammarError(CALL_INVALID)
    _check_arguments(node, params, depth)
    check_reads(node.get("then", []), params, depth=depth + 1, code=CALL_INVALID)


def check_reads(
    reads: Any, params: Collection[str], *, depth: int = 0, code: str = PROJECT_INVALID
) -> None:
    """``reads`` is a list of reads (see the module docstring); ``GrammarError(code)`` otherwise."""
    if depth > MAX_DEPTH or not isinstance(reads, list) or len(reads) > MAX_READS:
        raise GrammarError(code)
    for step in reads:
        if not isinstance(step, dict):
            raise GrammarError(code)
        keys = set(step)
        if keys == {"attr"} and _name(step["attr"]):
            continue
        if keys == {"item"} and isinstance(step["item"], (int, str)):
            if isinstance(step["item"], bool):
                raise GrammarError(code)
            continue
        if keys == {"each"} and isinstance(step["each"], list) and step["each"]:
            check_reads(step["each"], params, depth=depth + 1, code=code)
            continue
        if (
            "method" in keys
            and keys <= {"method", "args", "kwargs", "keep"}
            and _name(step["method"])
            and isinstance(step.get("keep", False), bool)
        ):
            try:
                _check_arguments(step, params, depth)
            except GrammarError:
                raise GrammarError(code) from None
            continue
        raise GrammarError(code)


def has_built_value(value: Any, *, depth: int = 0) -> bool:
    """Whether a ``$call`` or ``$param`` appears anywhere in ``value``.

    Case data (``args``, ``init``) never holds one: it is what the reference
    receives, and a target receives a built value only through the oracle's
    ``inputs`` or ``receiver``.
    """
    if depth > MAX_DEPTH:
        return True
    if isinstance(value, dict):
        if CALL_REF in value or param_ref(value) is not None:
            return True
        return any(has_built_value(item, depth=depth + 1) for item in value.values())
    if isinstance(value, list):
        return any(has_built_value(item, depth=depth + 1) for item in value)
    return False


def is_receiver(value: Any) -> bool:
    """A receiver template builds or imports an object: a ``$call`` or ``$symbol``."""
    return call_ref(value) is not None or symbol_ref(value) is not None


def check_case_files(files: Any) -> None:
    """``files`` maps relative POSIX paths to text; ``GrammarError`` otherwise.

    Each path part is a plain name (no ``.``, ``..``, empty part, leading
    dot or separator other than ``/``), so the controller can create every
    file inside a fresh case directory without following a link.
    """
    if not isinstance(files, dict) or not files or len(files) > MAX_CASE_FILES:
        raise GrammarError(CASE_FILES_INVALID)
    total = 0
    for path, text in files.items():
        if not isinstance(path, str) or not isinstance(text, str) or len(path) > 200:
            raise GrammarError(CASE_FILES_INVALID)
        if not all(_FILE_PART.fullmatch(part) for part in path.split("/")):
            raise GrammarError(CASE_FILES_INVALID)
        total += len(text.encode("utf-8"))
    if total > MAX_CASE_FILE_BYTES:
        raise GrammarError(CASE_FILES_INVALID)
    parts = [tuple(path.split("/")) for path in files]
    if any(other[: len(item)] == item for item in parts for other in parts if other != item):
        # One path is a directory of another.
        raise GrammarError(CASE_FILES_INVALID)


__all__ = [
    "CALL_INVALID",
    "CASE_FILES_INVALID",
    "GrammarError",
    "MAX_CASE_FILES",
    "PROJECT_INVALID",
    "check_case_files",
    "check_reads",
    "check_template",
    "has_built_value",
    "is_receiver",
]
