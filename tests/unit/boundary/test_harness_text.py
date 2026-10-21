"""The oracle harness records an observation whatever the observed object's text does.

A raised exception is recorded by its type and as text. Rendering the text
runs the object's own ``__str__`` or ``__repr__``, which is project code: a
Django ``ValidationError`` evaluates its lazily translated message, which
needs settings the harness never configures, so ``str()`` raises. The
target process used to die there (exit 1, no result frame), and a correct
fix failed every case that expects that exception. The type still decides a
``raises`` case; the text is diagnostic and never decides one.
"""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pytest

from ouroboros.boundary import harness
from ouroboros.boundary.admission import admit_check_package, verify_candidate
from ouroboros.boundary.oracle_run import valid_entry
from ouroboros.boundary.receipts import CandidateVerdict, PackageVerdict

from .test_oracle import _oracle_result, _package, _repo, _seed


class Unconfigured(Exception):
    pass


class LazyMessage:
    """A message that needs configuration to become text (Django's lazy ``gettext``)."""

    def __str__(self) -> str:
        raise Unconfigured("settings are not configured")


class StrFails(Exception):
    def __str__(self) -> str:
        raise Unconfigured("settings are not configured")


class BothFail(Exception):
    """Django's ``ValidationError`` shape: ``__repr__`` goes through ``__str__``."""

    def __str__(self) -> str:
        return "; ".join(str(item) for item in self.args)

    def __repr__(self) -> str:
        return f"BothFail({self})"


class Unprintable:
    def __repr__(self) -> str:
        raise Unconfigured("no repr")

    def __str__(self) -> str:
        raise Unconfigured("no str")


def _raise(exc: BaseException) -> Any:
    def target() -> Any:
        raise exc

    return target


def _call() -> dict[str, Any]:
    return {"case_id": "c1", "args": [], "kwargs": {}}


def _raises(name: str) -> dict[str, Any]:
    return {"case_id": "c1", "expect": {"kind": "raises", "exception": name}}


def test_an_exception_whose_str_raises_is_recorded_and_its_raise_case_passes() -> None:
    entry = harness._run(_raise(StrFails("x")), "function", _call())
    assert entry["outcome"] == "raised"
    assert entry["exception"][:2] == ["StrFails", "Exception"]
    # ``repr`` still renders: BaseException's repr shows the args.
    assert entry["repr"] == "StrFails: StrFails('x')"
    assert valid_entry(entry, "c1")
    assert harness._judge_python(_raises("StrFails"), entry, "f()") == (True, "")
    assert harness._judge_python(_raises("Exception"), entry, "f()") == (True, "")


def test_an_exception_neither_str_nor_repr_renders_is_recorded_by_a_fixed_marker() -> None:
    entry = harness._run(_raise(BothFail(LazyMessage(), "invalid", None)), "function", _call())
    assert entry["outcome"] == "raised"
    assert entry["exception"][:2] == ["BothFail", "Exception"]
    # The type, what rendering raised, and the builtin-scalar args; never a
    # project object's text.
    assert entry["repr"] == (
        "BothFail: <unrenderable BothFail: Unconfigured while rendering;"
        " args <LazyMessage>, 'invalid', None>"
    )
    assert valid_entry(entry, "c1")
    assert harness._judge_python(_raises("BothFail"), entry, "f()") == (True, "")


def test_an_unrenderable_message_never_passes_a_case_that_does_not_expect_that_raise() -> None:
    # No rule reads the text: a raise is judged by type, so the marker can
    # only appear in a failure's detail.
    entry = harness._run(_raise(BothFail(LazyMessage())), "function", _call())
    expectations = [
        {"kind": "raises", "exception": "KeyError"},
        {"kind": "returns", "value": "settings are not configured"},
        {"kind": "no_raise"},
    ]
    for expect in expectations:
        passed, detail = harness._judge_python({"case_id": "c1", "expect": expect}, entry, "f()")
        assert not passed
        assert harness.UNRENDERABLE + "BothFail" in detail


def test_a_returned_value_whose_repr_raises_is_recorded() -> None:
    entry = harness._run(lambda: Unprintable(), "function", _call())
    assert entry["outcome"] == "returned"
    assert entry["encodable"] is False
    assert entry["repr"] == "<unrenderable Unprintable: Unconfigured while rendering>"
    assert valid_entry(entry, "c1")
    case = {"case_id": "c1", "expect": {"kind": "no_raise"}}
    assert harness._judge_python(case, entry, "f()") == (True, "")


def test_the_text_fallback_is_bounded_and_never_raises() -> None:
    class BadArgs(Exception):
        @property
        def args(self) -> Any:  # type: ignore[override]
            raise Unconfigured("no args")

        def __str__(self) -> str:
            raise Unconfigured("no str")

        def __repr__(self) -> str:
            raise SystemExit(3)

    assert harness._text(BadArgs()) == "<unrenderable BadArgs: Unconfigured while rendering>"
    entry = harness._run(_raise(StrFails(*range(500))), "function", _call())
    assert len(entry["repr"]) == harness.MAX_REPR


# --------------------------------------------------------------- end to end

# A stand-in for django.core.validators: the base accepts every value; the
# fix raises a ValidationError whose message cannot become text without
# configuration.
VALIDATOR = (
    "class ImproperlyConfigured(Exception):\n"
    "    pass\n"
    "\n"
    "\n"
    "class Lazy:\n"
    "    def __str__(self):\n"
    "        raise ImproperlyConfigured('Requested setting USE_I18N, but settings are not "
    "configured.')\n"
    "\n"
    "\n"
    "class ValidationError(Exception):\n"
    "    def __init__(self, message, code=None, params=None):\n"
    "        super().__init__(message, code, params)\n"
    "        self.message = message\n"
    "\n"
    "    def __str__(self):\n"
    "        return repr([str(self.message)])\n"
    "\n"
    "    def __repr__(self):\n"
    "        return 'ValidationError(%s)' % self\n"
    "\n"
    "\n"
    "def validate_url(value):\n"
    "{BODY}"
)
BASE = VALIDATOR.replace("{BODY}", "    return None\n")
GOLD = VALIDATOR.replace(
    "{BODY}",
    "    if '\\n' in value or ' ' in value:\n"
    "        raise ValidationError(Lazy(), code='invalid')\n"
    "    return None\n",
)
CASES = [
    {
        "case_id": "c1",
        "held_out": False,
        "args": {"value": "http://example.com/\n"},
        "expect": {"kind": "raises", "exception": "ValidationError"},
    },
    {
        "case_id": "c2",
        "held_out": True,
        "args": {"value": "http://exa mple.com/"},
        "expect": {"kind": "raises", "exception": "ValidationError"},
    },
]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX no-follow proof")
async def test_a_fix_raising_an_unrenderable_exception_passes_its_raise_cases(
    tmp_path: Path,
) -> None:
    base = _repo(tmp_path / "base", {"validators.py": BASE})
    package = _package(
        _seed("validate_url rejects a URL with a newline or a space"),
        base,
        symbol="validators.validate_url",
        cases=CASES,
        params=("value",),
    )
    admission = await admit_check_package(package, base)
    assert admission.verdict is PackageVerdict.ADMITTED

    gold = _repo(tmp_path / "gold", {"validators.py": GOLD})
    result = await verify_candidate(package, gold)
    assert result.verdict is CandidateVerdict.PASS
    assert [c["passed"] for c in _oracle_result(result.checks[0])["cases"]] == [True, True]
