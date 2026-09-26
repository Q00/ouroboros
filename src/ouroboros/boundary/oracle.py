"""Frozen oracle: per-criterion expected behavior plus a product-owned harness.

The constructor contributes data only: for each criterion it can check, the
declared parameters, a default binding, and cases (inputs and the expected
outcome). The product turns that data into package files:

- ``.ouroboros_checks/oracle/oracle.json``: every oracle (cases, declared
  parameters, default binding, failure signature, binding grammar version);
- ``.ouroboros_checks/oracle/harness.py``: ``ORACLE_HARNESS_SOURCE``, fixed
  product code.

Both files are covered by the package hash, so the failure signature, the
cases, and the grammar are frozen before any worker starts. The harness reads
the cases and the binding (the default one, or a late binding written to
``bindings.json`` next to it at verification time), calls the target through
the binding, and compares what it observed with the frozen expectation. It
never derives an expectation from the artifact.

Isolation (see ``boundary/admission.py``): the oracle files are materialized
in a controller directory outside the checkout copy, made read-only, and
digested before and after the run. The target is called in a child process
that receives only the call inputs, never the expected values; the parent
harness, which never imports workspace code, does the comparison. A workspace
module that monkeypatches at import time therefore cannot reach the
comparison, and an edit to the check files is a protected-byte mutation.

Held-out cases: a case whose scalar literals (arguments and expected value)
do not all appear in the Seed text a worker sees (goal, constraints, criterion
descriptions) is marked ``held_out`` by the product, whatever the constructor
said. Held-out cases count toward the criterion's verdict.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import json
import math
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ouroboros.boundary.binding import BINDING_GRAMMAR, Binding, CallKind

ORACLE_SCHEMA = "ouroboros.oracle.v1"
ORACLE_DIR = ".ouroboros_checks/oracle"
ORACLE_DATA_PATH = f"{ORACLE_DIR}/oracle.json"
ORACLE_HARNESS_PATH = f"{ORACLE_DIR}/harness.py"
BINDINGS_FILE = "bindings.json"
ORACLE_RESULT_PREFIX = "OUROBOROS_ORACLE_RESULT "
_CASE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


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

    kind: Literal["returns", "raises", "cli"]
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

    @field_validator("case_id")
    @classmethod
    def _case_id(cls, value: str) -> str:
        if not _CASE_ID.fullmatch(value):
            raise ValueError(f"invalid case_id: {value!r}")
        return value

    @field_validator("args", "init")
    @classmethod
    def _plain_args(cls, value: Any) -> Any:
        return None if value is None else _json_value(value)


class OracleSpec(BaseModel):
    """The frozen oracle of one criterion, executed by one check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    criterion_key: str = Field(..., min_length=1)
    check_id: str = Field(..., min_length=1)
    call_kind: CallKind
    params: tuple[str, ...] = ()
    default_binding: Binding
    default_resolves: bool
    cases: tuple[OracleCase, ...] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> OracleSpec:
        if len(set(self.params)) != len(self.params) or not all(
            _IDENTIFIER.fullmatch(name) for name in self.params
        ):
            raise ValueError(f"{self.check_id}: params must be unique identifiers")
        ids = [case.case_id for case in self.cases]
        if len(set(ids)) != len(ids):
            raise ValueError(f"{self.check_id}: case ids must be unique")
        for case in self.cases:
            if set(case.args) != set(self.params):
                raise ValueError(
                    f"{self.check_id}: case {case.case_id} must give exactly the declared params"
                )
            if (case.expect.kind == "cli") != (self.call_kind is CallKind.CLI):
                raise ValueError(f"{self.check_id}: case {case.case_id} expectation kind")
            if case.init is not None and self.call_kind is not CallKind.METHOD:
                raise ValueError(f"{self.check_id}: init is only valid for method oracles")
        binding = self.default_binding
        if binding.criterion_key != self.criterion_key or binding.call_kind is not self.call_kind:
            raise ValueError(f"{self.check_id}: default binding does not match the oracle")
        if binding.arg_map and set(binding.arg_map) != set(self.params):
            raise ValueError(f"{self.check_id}: default binding arg_map keys")
        return self

    @property
    def failure_signature(self) -> str:
        return failure_signature_for(self.check_id)

    @property
    def held_out_count(self) -> int:
        return sum(1 for case in self.cases if case.held_out)

    def interface(self) -> dict[str, Any]:
        """What a worker may learn: call kind and parameter names, no cases."""
        return {"call_kind": self.call_kind.value, "params": list(self.params)}


# --------------------------------------------------------------------------
# Held-out marking against the Seed text a worker sees


def worker_visible_seed_text(seed: Any) -> str:
    """Goal, constraints, and criterion descriptions (what the worker is shown)."""
    from ouroboros.core.seed import AcceptanceCriterionSpec

    parts = [str(seed.goal)]
    parts.extend(str(item) for item in seed.constraints)
    for criterion in seed.acceptance_criteria:
        if isinstance(criterion, AcceptanceCriterionSpec):
            parts.append(criterion.description)
        else:
            parts.append(str(criterion))
    return "\n".join(parts)


def _scalars(value: Any) -> Iterable[Any]:
    if isinstance(value, Mapping):
        for item in value.values():
            yield from _scalars(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _scalars(item)
    elif isinstance(value, bool) or value is None:
        return
    else:
        yield value


def _canonical_number(value: float | int) -> str:
    number = float(value)
    if math.isfinite(number) and number == int(number):
        return str(int(number))
    return repr(number)


def case_in_text(case: OracleCase, text: str) -> bool:
    """Whether every scalar literal of the case appears in ``text``.

    Numbers match as numeric tokens (``10`` equals ``10.0``); strings match as
    substrings. Booleans and null are too generic to identify a case and are
    ignored; a case with no other scalar counts as present.
    """
    numbers = {_canonical_number(float(token)) for token in _NUMBER.findall(text)}
    literals = [
        *_scalars(case.args),
        *_scalars(case.init or {}),
        *_scalars(case.expect.value),
        *(item for item in (case.expect.stdout, case.expect.stdout_contains) if item),
        *(item for item in (case.stdin,) if item),
    ]
    if not literals:
        return True
    for literal in literals:
        if isinstance(literal, int | float):
            if not math.isfinite(float(literal)) or _canonical_number(literal) not in numbers:
                return False
        elif str(literal) not in text:
            return False
    return True


def mark_held_out(cases: Sequence[OracleCase], seed_text: str) -> tuple[OracleCase, ...]:
    """Return the cases with ``held_out`` set by the product rule."""
    return tuple(
        case.model_copy(update={"held_out": not case_in_text(case, seed_text)}) for case in cases
    )


# --------------------------------------------------------------------------
# Package files


def oracle_data(oracles: Sequence[OracleSpec]) -> dict[str, Any]:
    """The frozen data the harness reads (``oracle.json``)."""
    return {
        "schema_version": ORACLE_SCHEMA,
        "binding_grammar": BINDING_GRAMMAR,
        "oracles": [
            {
                "criterion_key": spec.criterion_key,
                "check_id": spec.check_id,
                "call_kind": spec.call_kind.value,
                "params": list(spec.params),
                "failure_signature": spec.failure_signature,
                "default_binding": spec.default_binding.to_dict(),
                "cases": [case.model_dump(mode="json") for case in spec.cases],
            }
            for spec in oracles
        ],
    }


def oracle_data_text(oracles: Sequence[OracleSpec]) -> str:
    return json.dumps(oracle_data(oracles), sort_keys=True, indent=1, ensure_ascii=False) + "\n"


def bindings_text(bindings: Mapping[str, Binding]) -> str:
    """``bindings.json``: check id to the late binding it must run through."""
    return (
        json.dumps(
            {check_id: binding.to_dict() for check_id, binding in sorted(bindings.items())},
            sort_keys=True,
            indent=1,
        )
        + "\n"
    )


def is_oracle_file(path: str) -> bool:
    """Oracle files live in the controller directory, never in the checkout copy."""
    return path == ORACLE_DIR or path.startswith(ORACLE_DIR + "/")


# --------------------------------------------------------------------------
# Result parsing


def parse_oracle_result(stdout: str) -> dict[str, Any] | None:
    """The harness's structured result: the last ``OUROBOROS_ORACLE_RESULT`` line."""
    for line in reversed(stdout.splitlines()):
        if line.startswith(ORACLE_RESULT_PREFIX):
            try:
                value = json.loads(line[len(ORACLE_RESULT_PREFIX) :])
            except json.JSONDecodeError:
                return None
            return value if isinstance(value, dict) else None
    return None


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


def failed_heldout_only(result: Mapping[str, Any] | None) -> bool:
    """Diagnostic: every failing case of the result is a held-out case."""
    if not result:
        return False
    failing = [case for case in result.get("cases") or () if not case.get("passed")]
    return bool(failing) and all(case.get("held_out") for case in failing)


def repair_lines(result: Mapping[str, Any] | None, *, limit: int = 5) -> list[str]:
    """Counterexamples a worker may see: visible cases in full, held-out as a count."""
    if not result:
        return []
    lines: list[str] = []
    hidden = 0
    for case in result.get("cases") or ():
        if case.get("passed"):
            continue
        if case.get("held_out"):
            hidden += 1
            continue
        if len(lines) < limit:
            lines.append(f"- {case.get('detail') or case.get('case_id')}")
    if hidden:
        lines.append(
            f"- {hidden} held-out case(s) also failed (inputs withheld; they test the "
            "criterion's general rule, not only the examples in its text)"
        )
    return lines


# --------------------------------------------------------------------------
# The harness (product code, frozen into every oracle package)

ORACLE_HARNESS_SOURCE = r'''"""Ouroboros oracle harness (product code; the constructor writes data only).

Usage: python harness.py <check_id>   (cwd: the checkout under test)

Reads oracle.json (and bindings.json, when present) from this directory, calls
the bound target in a child process that receives only the inputs, and
compares each observation with the frozen expectation here, in a process that
never imports workspace code.
"""
import json
import os
import secrets
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RESULT_PREFIX = "OUROBOROS_ORACLE_RESULT "
CHILD_TIMEOUT = 100
MAX_REPR = 300

CHILD = r"""
import importlib, json, os, sys

class _Missing(Exception):
    pass

def _plain(value, depth=0):
    if depth > 50:
        raise TypeError("too deep")
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise TypeError("non-finite float")
        return value
    if isinstance(value, (list, tuple)):
        return [_plain(item, depth + 1) for item in value]
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("non-string key")
        return {key: _plain(item, depth + 1) for key, item in value.items()}
    raise TypeError(type(value).__name__)

def _resolve(symbol, kind):
    parts = symbol.split(".")
    for split in range(len(parts) - 1, 0, -1):
        module_name = ".".join(parts[:split])
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            missing = exc.name or ""
            if missing == module_name or module_name.startswith(missing + "."):
                continue
            raise
        target = module
        for name in parts[split:]:
            if not hasattr(target, name):
                raise _Missing(symbol + ": " + name + " not found")
            target = getattr(target, name)
        if kind == "method":
            if len(parts) - split != 2:
                raise _Missing(symbol + ": not module.Class.method")
            owner = module
            for name in parts[split:-1]:
                owner = getattr(owner, name)
            return (owner, parts[-1])
        if not callable(target):
            raise _Missing(symbol + ": not callable")
        return target
    raise _Missing(symbol + ": module not found")

def _run(target, kind, call):
    try:
        if kind == "method":
            owner, name = target
            instance = owner(**(call.get("init") or {}))
            value = getattr(instance, name)(*call["args"], **call["kwargs"])
        else:
            value = target(*call["args"], **call["kwargs"])
    except BaseException as exc:
        return {"case_id": call["case_id"], "outcome": "raised",
                "exception": [klass.__name__ for klass in type(exc).__mro__],
                "repr": (type(exc).__name__ + ": " + str(exc))[:300]}
    entry = {"case_id": call["case_id"], "outcome": "returned", "repr": repr(value)[:300]}
    try:
        entry["value"] = _plain(value)
        entry["encodable"] = True
    except Exception:
        entry["encodable"] = False
    return entry

def main():
    request = json.loads(sys.stdin.read())
    cwd = os.getcwd()
    for path in (os.path.join(cwd, "src"), cwd):
        if path not in sys.path:
            sys.path.insert(0, path)
    out = {"resolve": "ok", "detail": "", "results": []}
    target = None
    try:
        target = _resolve(request["symbol"], request["call_kind"])
    except _Missing as exc:
        out["resolve"] = "missing"
        out["detail"] = str(exc)[:500]
    except BaseException as exc:
        out["resolve"] = "import_error"
        out["detail"] = (type(exc).__name__ + ": " + str(exc))[:500]
    if target is not None:
        for call in request["calls"]:
            out["results"].append(_run(target, request["call_kind"], call))
    sys.stdout.flush()
    sys.stdout.write("\n" + request["nonce"] + " " + json.dumps(out) + "\n")
    sys.stdout.flush()
    os._exit(0)

main()
"""


def _load(name):
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _short(value):
    text = repr(value)
    return text if len(text) <= MAX_REPR else text[:MAX_REPR] + "..."


def _split_args(params, arg_map, args):
    if not arg_map:
        return [], {name: args[name] for name in params}
    positional = {}
    keywords = {}
    for name in params:
        target = arg_map[name]
        if isinstance(target, int):
            positional[target] = args[name]
        else:
            keywords[target] = args[name]
    return [positional[index] for index in sorted(positional)], keywords


def _cli_argv(symbol, params, arg_map, args):
    if symbol.startswith("-m "):
        prefix = [sys.executable, "-m", symbol[3:]]
    elif symbol.endswith(".py"):
        prefix = [sys.executable, symbol]
    else:
        prefix = [os.path.join(os.getcwd(), symbol)]

    def text(value):
        return value if isinstance(value, str) else json.dumps(value)

    positional = {}
    flags = []
    for name in params:
        target = arg_map.get(name, "--" + name) if arg_map else "--" + name
        if isinstance(target, int):
            positional[target] = text(args[name])
        else:
            flags += [target, text(args[name])]
    return prefix + [positional[index] for index in sorted(positional)] + flags


def _equal(expected, observed, approx):
    if approx is not None and isinstance(expected, (int, float)) and not isinstance(expected, bool):
        return (isinstance(observed, (int, float)) and not isinstance(observed, bool)
                and abs(float(observed) - float(expected)) <= approx)
    if isinstance(expected, list) and isinstance(observed, list):
        return len(expected) == len(observed) and all(
            _equal(e, o, approx) for e, o in zip(expected, observed))
    if isinstance(expected, dict) and isinstance(observed, dict):
        return set(expected) == set(observed) and all(
            _equal(expected[k], observed[k], approx) for k in expected)
    if isinstance(expected, bool) or isinstance(observed, bool):
        return type(expected) is type(observed) and expected == observed
    return expected == observed


def _render_call(symbol, args, kwargs):
    parts = [_short(a) for a in args] + [k + "=" + _short(v) for k, v in kwargs.items()]
    return symbol.rsplit(".", 1)[-1] + "(" + ", ".join(parts) + ")"


def _python_cases(spec, binding):
    calls = []
    rendered = {}
    for case in spec["cases"]:
        args, kwargs = _split_args(spec["params"], binding.get("arg_map") or {}, case["args"])
        calls.append({"case_id": case["case_id"], "args": args, "kwargs": kwargs,
                      "init": case.get("init")})
        rendered[case["case_id"]] = _render_call(binding["symbol"], args, kwargs)
    nonce = secrets.token_hex(16)
    request = {"nonce": nonce, "symbol": binding["symbol"], "call_kind": spec["call_kind"],
               "calls": calls}
    try:
        completed = subprocess.run(
            [sys.executable, "-c", CHILD], input=json.dumps(request), capture_output=True,
            text=True, timeout=CHILD_TIMEOUT, cwd=os.getcwd())
    except subprocess.TimeoutExpired:
        return "child_timeout", "the target did not return in time", {}, rendered
    report = None
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith(nonce + " "):
            report = json.loads(line[len(nonce) + 1:])
            break
    if report is None:
        tail = (completed.stdout + completed.stderr)[-500:]
        return "child_crashed", "the call process exited without a report: " + tail, {}, rendered
    observed = {entry["case_id"]: entry for entry in report["results"]}
    return report["resolve"], report["detail"], observed, rendered


def _judge_python(case, entry, call_text):
    expect = case["expect"]
    if entry is None:
        return False, call_text + ": no observation"
    if expect["kind"] == "raises":
        if entry["outcome"] == "raised" and expect["exception"] in entry["exception"]:
            return True, ""
        seen = entry["repr"] if entry["outcome"] == "raised" else "returned " + entry["repr"]
        return False, call_text + ": expected to raise " + expect["exception"] + ", observed " + seen
    if entry["outcome"] != "returned":
        return False, call_text + ": expected " + _short(expect["value"]) + ", raised " + entry["repr"]
    if not entry.get("encodable"):
        return False, (call_text + ": expected " + _short(expect["value"]) + ", observed "
                       + entry["repr"] + " (not a plain value)")
    if _equal(expect["value"], entry["value"], expect.get("approx")):
        return True, ""
    return False, call_text + ": expected " + _short(expect["value"]) + ", observed " + _short(entry["value"])


def _cli_case(spec, binding, case):
    argv = _cli_argv(binding["symbol"], spec["params"], binding.get("arg_map") or {}, case["args"])
    call_text = " ".join(argv[1:] if argv[0] == sys.executable else argv)
    script = binding["symbol"]
    if not script.startswith("-m ") and not os.path.isfile(os.path.join(os.getcwd(), script)):
        return "missing", False, call_text + ": " + script + " not found"
    try:
        completed = subprocess.run(argv, input=case.get("stdin") or "", capture_output=True,
                                   text=True, timeout=CHILD_TIMEOUT, cwd=os.getcwd())
    except subprocess.TimeoutExpired:
        return "child_timeout", False, call_text + ": timed out"
    except OSError as exc:
        return "missing", False, call_text + ": " + str(exc)
    expect = case["expect"]
    problems = []
    if expect.get("exit_code") is not None and completed.returncode != expect["exit_code"]:
        problems.append("exit " + str(completed.returncode) + " (expected " + str(expect["exit_code"]) + ")")
    out = completed.stdout
    if expect.get("stdout") is not None and out.rstrip("\n") != expect["stdout"].rstrip("\n"):
        problems.append("stdout " + _short(out) + " (expected " + _short(expect["stdout"]) + ")")
    if expect.get("stdout_contains") is not None and expect["stdout_contains"] not in out:
        problems.append("stdout " + _short(out) + " lacks " + _short(expect["stdout_contains"]))
    return "ok", not problems, (call_text + ": " + "; ".join(problems)) if problems else ""


def main():
    check_id = sys.argv[1]
    data = _load("oracle.json")
    spec = next(item for item in data["oracles"] if item["check_id"] == check_id)
    bindings = _load("bindings.json") or {}
    binding = bindings.get(check_id)
    source = "declared" if binding is not None else "default"
    binding = binding or spec["default_binding"]
    cases = []
    resolve = "ok"
    detail = ""
    if spec["call_kind"] == "cli":
        for case in spec["cases"]:
            status, passed, text = _cli_case(spec, binding, case)
            if status == "child_timeout":
                resolve = status
            elif status == "missing" and resolve == "ok":
                resolve = "missing"
            cases.append({"case_id": case["case_id"], "held_out": bool(case.get("held_out")),
                          "passed": passed, "detail": text})
    else:
        resolve, detail, observed, rendered = _python_cases(spec, binding)
        for case in spec["cases"]:
            if resolve == "missing":
                passed, text = False, rendered[case["case_id"]] + ": " + detail
            elif resolve != "ok":
                passed, text = False, ""
            else:
                passed, text = _judge_python(case, observed.get(case["case_id"]),
                                             rendered[case["case_id"]])
            cases.append({"case_id": case["case_id"], "held_out": bool(case.get("held_out")),
                          "passed": passed, "detail": text})
    result = {"check_id": check_id, "criterion_key": spec["criterion_key"],
              "binding_source": source, "symbol": binding["symbol"],
              "call_kind": spec["call_kind"], "resolve": resolve, "cases": cases}
    if resolve not in ("ok", "missing"):
        # Setup failure (import error, crash, timeout): no failure signature,
        # so it is never counted as a detected failure.
        print("oracle could not run the target: " + resolve + " " + detail)
        print(RESULT_PREFIX + json.dumps(result, sort_keys=True))
        sys.exit(3)
    failed = [case for case in cases if not case["passed"]]
    if failed:
        print(spec["failure_signature"])
        for case in failed:
            marker = " (held-out)" if case["held_out"] else ""
            print("counterexample" + marker + ": " + case["detail"])
        print(RESULT_PREFIX + json.dumps(result, sort_keys=True))
        sys.exit(1)
    print(RESULT_PREFIX + json.dumps(result, sort_keys=True))
    sys.exit(0)


if __name__ == "__main__":
    main()
'''


__all__ = [
    "BINDINGS_FILE",
    "ORACLE_DATA_PATH",
    "ORACLE_DIR",
    "ORACLE_HARNESS_PATH",
    "ORACLE_HARNESS_SOURCE",
    "ORACLE_RESULT_PREFIX",
    "ORACLE_SCHEMA",
    "OracleCase",
    "OracleExpectation",
    "OracleSpec",
    "bindings_text",
    "case_in_text",
    "failed_heldout_only",
    "failure_signature_for",
    "is_oracle_file",
    "journal_safe_oracle_result",
    "mark_held_out",
    "oracle_data",
    "oracle_data_text",
    "parse_oracle_result",
    "repair_lines",
    "worker_visible_seed_text",
]
