"""Built oracle calls: ``$call`` inputs, receivers, projections, ``no_raise``, case files.

The constructor proposed no oracle at all for library bugs whose call needs
an object built by calls (a fitted model, a figure's sub-part) and whose
result is not JSON, and none for a command that reads a file: the grammar
had no way to state either, so it wrote script checks, whose pass is
advisory, and the package decided nothing. The fixture library below has
the same shape without being any real project: ``tallykit.report.summarize``
takes a ``Counter`` that must be fitted first and returns a ``Report``
object; the buggy version ranks values least frequent first and raises
``IndexError`` when ``top`` exceeds the number of distinct values.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

import pytest

from ouroboros.boundary import harness
from ouroboros.boundary.call_grammar import GrammarError, check_case_files, check_reads
from ouroboros.boundary.check_env import pin_interpreter
from ouroboros.boundary.constructor import CheckConstructor
from ouroboros.boundary.oracle import (
    ORACLE_DATA_PATH,
    ORACLE_HARNESS_PATH,
    ORACLE_HARNESS_SOURCE,
    oracle_data,
    oracle_data_text,
)
from ouroboros.boundary.oracle_build import ReplyError, ReplyFailure, package_from_reply
from ouroboros.boundary.oracle_run import run_oracle_check, write_case_files
from ouroboros.boundary.reference_check import check_references, references_from_reply
from ouroboros.boundary.run_wiring import (
    CheckPackageSettings,
    forget_live_state,
    prepare_check_package,
    verify_check_package,
)
from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata
from ouroboros.persistence.event_store import EventStore

from .test_constructor import FakeRuntime

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX no-follow proof")

COUNTER = (
    "class Counter:\n"
    "    def __init__(self):\n"
    "        self.counts = {}\n"
    "        self.order = []\n"
    "\n"
    "    def fit(self, points):\n"
    "        for point in points:\n"
    "            if point not in self.counts:\n"
    "                self.order.append(point)\n"
    "                self.counts[point] = 0\n"
    "            self.counts[point] += 1\n"
    "        return self\n"
    "\n"
    "    def top(self, n):\n"
    "        ranked = sorted(self.order, key=lambda v: {SIGN}self.counts[v])\n"
    "        return [ranked[i] for i in range({LIMIT})]\n"
)
REPORT = (
    "class Entry:\n"
    "    def __init__(self, value, count):\n"
    "        self.text = f'{value} x{count}'\n"
    "\n"
    "\n"
    "class Report:\n"
    "    def __init__(self, entries):\n"
    "        self._entries = entries\n"
    "\n"
    "    def entries(self):\n"
    "        return list(self._entries)\n"
    "\n"
    "\n"
    "def summarize(counter, top):\n"
    "    return Report([Entry(v, counter.counts[v]) for v in counter.top(top)])\n"
)
BUGGY = COUNTER.replace("{SIGN}", "").replace("{LIMIT}", "n")
FIXED = COUNTER.replace("{SIGN}", "-").replace("{LIMIT}", "min(n, len(ranked))")
REFERENCE = (
    "def summarize(points, top):\n"
    "    counts, order = {}, []\n"
    "    for point in points:\n"
    "        if point not in counts:\n"
    "            order.append(point)\n"
    "            counts[point] = 0\n"
    "        counts[point] += 1\n"
    "    ranked = sorted(order, key=lambda v: -counts[v])\n"
    "    return [f'{v} x{counts[v]}' for v in ranked[:top]]\n"
)
FITTED = {
    "$call": "tallykit.counter.Counter",
    "then": [{"method": "fit", "args": [{"$param": "points"}]}],
}
TEXTS = [{"method": "entries"}, {"each": [{"attr": "text"}]}]


def _write_library(root: Path, counter: str) -> Path:
    package = root / "tallykit"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("")
    (package / "counter.py").write_text(counter)
    (package / "report.py").write_text(REPORT)
    return root


def _seed() -> Seed:
    return Seed(
        goal="tallykit summaries list the most frequent values first.",
        acceptance_criteria=(
            "tallykit.report.summarize(counter, top) lists the top values a fitted "
            "tallykit.counter.Counter saw most often, as entries whose text is "
            "'<value> x<count>', most frequent first, ties in first-seen order, and at most "
            "as many as there are distinct values; for example after fit([3, 1, 3]), "
            "summarize(counter, 1) has the entry texts ['3 x2'].",
        ),
        ontology_schema=OntologySchema(name="tallykit", description="library"),
        metadata=SeedMetadata(seed_id="seed_built_call_oracle", ambiguity_score=0.1),
    )


def _case(held_out: bool, points: list[int], top: int, texts: list[str]) -> dict[str, Any]:
    return {
        "held_out": held_out,
        "args": {"points": points, "top": top},
        "expect": {"kind": "returns", "value": texts},
    }


def _oracle(**changes: Any) -> dict[str, Any]:
    oracle: dict[str, Any] = {
        "criterion": 1,
        "role": "reproduction",
        "call_kind": "function",
        "params": ["points", "top"],
        "inputs": {"counter": FITTED, "top": {"$param": "top"}},
        "default_binding": {"symbol": "tallykit.report.summarize"},
        "target_named_in_criterion": True,
        "project": TEXTS,
        "reference": {"source": REFERENCE, "symbol": "summarize"},
        "cases": [
            _case(False, [3, 1, 3], 1, ["3 x2"]),
            _case(True, [5, 5, 2, 2, 2], 2, ["2 x3", "5 x2"]),
            _case(True, [4, 9], 5, ["4 x1", "9 x1"]),
            _case(True, [7], 1, ["7 x1"]),
        ],
    }
    oracle.update(changes)
    return oracle


def _reply(*oracles: dict[str, Any]) -> dict[str, Any]:
    return {"oracles": list(oracles), "checks": [], "files": [], "uncovered": []}


def _package(oracle: dict[str, Any], seed: Seed | None = None):
    return package_from_reply(_reply(oracle), seed or _seed(), input_digest="1" * 64, generator="t")


async def _run(repo: Path, oracle: dict[str, Any], *, on_base: bool, seed: Seed | None = None):
    (spec,) = _package(oracle, seed).oracles
    files = {ORACLE_HARNESS_PATH: ORACLE_HARNESS_SOURCE, ORACLE_DATA_PATH: oracle_data_text([spec])}
    return spec, await run_oracle_check(
        files,
        spec,
        repo,
        timeout_seconds=60,
        on_base=on_base,
        interpreter=pin_interpreter(sys.executable, "test"),
        binding=None,
    )


# --------------------------------------------------------------------------
# The harness forms, one by one


def test_a_call_chain_builds_its_object_and_applies_then_reads() -> None:
    built = harness._inputs(
        {
            "$call": "collections.OrderedDict",
            "kwargs": {"b": 2},
            "then": [
                {"method": "update", "args": [{"a": 1}], "keep": True},
                {"method": "keys"},
            ],
        }
    )
    assert list(built) == ["b", "a"]
    nested = harness._inputs(
        {"$call": "builtins.sorted", "args": [{"$call": "builtins.set", "args": [[3, 1, 3]]}]}
    )
    assert nested == [1, 3]


def test_reads_cover_attr_method_item_and_each() -> None:
    value = {"rows": [{"name": "x"}, {"name": "yz"}]}
    assert harness._apply_reads(value, [{"item": "rows"}, {"each": [{"item": "name"}]}]) == [
        "x",
        "yz",
    ]
    assert harness._apply_reads("a,b", [{"method": "split", "args": [","]}, {"item": -1}]) == "b"
    assert harness._apply_reads(3 + 4j, [{"attr": "imag"}]) == 4.0


def test_a_param_reference_is_bound_by_the_controller_and_never_reaches_the_target() -> None:
    template = {"$call": "builtins.list", "args": [{"$param": "xs"}]}
    assert harness.bind_params(template, {"xs": [1, {"$symbol": "a.b"}]}) == {
        "$call": "builtins.list",
        "args": [[1, {"$symbol": "a.b"}]],
    }
    with pytest.raises(ValueError):
        harness._inputs({"$param": "xs"})


def test_a_no_raise_case_passes_any_return_and_fails_a_raise() -> None:
    case = {"case_id": "c1", "expect": {"kind": "no_raise"}}
    returned = {"case_id": "c1", "outcome": "returned", "repr": "<obj>", "encodable": False}
    raised = {"case_id": "c1", "outcome": "raised", "exception": ["IndexError"], "repr": "x"}
    assert harness._judge_python(case, returned, "f()") == (True, "")
    passed, detail = harness._judge_python(case, raised, "f()")
    assert not passed and "expected to return" in detail


@pytest.mark.parametrize(
    "reads",
    [
        [{"attr": "__class__"}],
        [{"method": "__getattribute__", "args": ["x"]}],
        [{"attr": "x", "method": "y"}],
        [{"item": True}],
        [{"each": []}],
        [{"method": "f", "keep": "yes"}],
        [{"method": "f", "args": [{"$param": "undeclared"}]}],
        {"attr": "x"},
    ],
)
def test_malformed_reads_are_refused(reads: Any) -> None:
    with pytest.raises(GrammarError):
        check_reads(reads, ["points"])


@pytest.mark.parametrize(
    "files",
    [
        {},
        {"../escape.txt": "x"},
        {"/abs.txt": "x"},
        {"a//b.txt": "x"},
        {".hidden": "x"},
        {"a\\b.txt": "x"},
        {"dir": "x", "dir/inner.txt": "y"},
        {"big.txt": "x" * (300 * 1024)},
    ],
)
def test_malformed_case_files_are_refused(files: dict[str, str]) -> None:
    with pytest.raises(GrammarError):
        check_case_files(files)


def test_case_files_are_written_without_following_a_link(tmp_path: Path) -> None:
    root = write_case_files(tmp_path, {"a.txt": "one", "sub/dir/b.txt": "two"})
    assert root.parent == tmp_path
    assert (root / "a.txt").read_text() == "one"
    assert (root / "sub" / "dir" / "b.txt").read_text() == "two"


# --------------------------------------------------------------------------
# Grammar refusals and the frozen shape


@pytest.mark.parametrize(
    ("oracle", "code"),
    [
        (
            _oracle(inputs={"counter": {"$call": "Counter"}, "top": 1}),
            ReplyFailure.CALL_INVALID,
        ),
        (
            _oracle(inputs={"counter": {**FITTED, "code": "x"}, "top": 1}),
            ReplyFailure.CALL_INVALID,
        ),
        (
            _oracle(inputs={"counter": {"$param": "missing"}, "top": 1}),
            ReplyFailure.CALL_INVALID,
        ),
        (_oracle(inputs=[FITTED]), ReplyFailure.CALL_INVALID),
        (
            _oracle(
                cases=[{**_case(True, [1], 1, ["1 x1"]), "args": {"points": FITTED, "top": 1}}]
            ),
            ReplyFailure.CALL_INVALID,
        ),
        (_oracle(project=[{"attr": "__dict__"}]), ReplyFailure.PROJECT_INVALID),
        (_oracle(project={"attr": "x"}), ReplyFailure.PROJECT_INVALID),
        (
            _oracle(receiver={"plain": "dict"}, call_kind="method"),
            ReplyFailure.CALL_INVALID,
        ),
        (
            _oracle(cases=[{**_case(True, [1], 1, ["1 x1"]), "files": {"../x": "y"}}]),
            ReplyFailure.CASE_FILES_INVALID,
        ),
    ],
)
def test_malformed_built_calls_are_typed_refusals(
    oracle: dict[str, Any], code: ReplyFailure
) -> None:
    with pytest.raises(ReplyError) as raised:
        _package(oracle)
    assert raised.value.code is code


@pytest.mark.parametrize(
    "changes",
    [
        {"receiver": FITTED},  # a receiver is for method oracles only
        {"cases": [{**_case(True, [1], 1, ["1 x1"]), "files": {"a.txt": "x"}}]},  # CLI only
        {
            "cases": [
                {
                    "held_out": True,
                    "args": {"points": [1], "top": 1},
                    "expect": {"kind": "no_raise", "value": 1},
                }
            ]
        },
    ],
)
def test_built_calls_that_do_not_fit_the_oracle_are_refused(changes: dict[str, Any]) -> None:
    with pytest.raises(ReplyError) as raised:
        _package(_oracle(**changes))
    assert raised.value.code is ReplyFailure.ORACLE_INVALID


def test_a_binding_maps_the_call_params_not_the_case_data() -> None:
    binding = {"symbol": "tallykit.report.summarize", "arg_map": {"points": 0, "top": 1}}
    with pytest.raises(ReplyError) as raised:
        _package(_oracle(default_binding=binding))
    assert raised.value.code is ReplyFailure.BINDING_INVALID


def test_an_oracle_without_the_new_fields_freezes_the_same_data_as_before() -> None:
    plain = {k: v for k, v in _oracle().items() if k not in {"inputs", "project", "reference"}} | {
        "params": ["counter", "top"],
        "cases": [
            {
                "held_out": True,
                "args": {"counter": 1, "top": 2},
                "expect": {"kind": "returns", "value": 3},
            }
        ],
    }
    (entry,) = oracle_data(_package(plain).oracles)["oracles"]
    assert set(entry) == {
        "criterion_key",
        "check_id",
        "call_kind",
        "params",
        "failure_signature",
        "default_binding",
        "cases",
    }
    (case,) = entry["cases"]
    assert set(case) == {"case_id", "args", "init", "stdin", "expect", "held_out"}
    assert set(case["expect"]) == {
        "kind",
        "value",
        "approx",
        "exception",
        "exit_code",
        "stdout",
        "stdout_contains",
    }


def test_built_calls_are_frozen_and_the_binding_maps_the_call_params() -> None:
    package = _package(
        _oracle(
            default_binding={
                "symbol": "tallykit.report.summarize",
                "arg_map": {"counter": 0, "top": 1},
            }
        )
    )
    (spec,) = package.oracles
    (entry,) = oracle_data(package.oracles)["oracles"]
    assert entry["inputs"] == {"counter": FITTED, "top": {"$param": "top"}}
    assert entry["project"] == TEXTS
    assert spec.call_params == ("counter", "top")
    assert spec.interface() == {"call_kind": "function", "params": ["counter", "top"]}
    assert package.sha256 != _package(_oracle(project=[{"method": "entries"}])).sha256


# --------------------------------------------------------------------------
# Running built calls


async def test_a_fitted_input_and_a_projection_reproduce_on_the_base_and_pass_the_fix(
    tmp_path: Path,
) -> None:
    repo = _write_library(tmp_path / "repo", BUGGY)
    spec, base = await _run(repo, _oracle(), on_base=True)
    assert base.return_code == 1 and base.result is not None
    assert base.result["resolve"] == "ok" and spec.base_run_tier("ok").value == "A"
    assert [case["passed"] for case in base.result["cases"]] == [False, False, False, True]
    assert "observed ['1 x1']" in base.result["cases"][0]["detail"]
    assert "raised IndexError" in base.result["cases"][2]["detail"]

    _write_library(repo, FIXED)
    _spec, fixed = await _run(repo, _oracle(), on_base=False)
    assert fixed.return_code == 0


async def test_a_method_oracle_calls_the_method_on_a_built_receiver(tmp_path: Path) -> None:
    oracle = _oracle(
        call_kind="method",
        default_binding={"symbol": "tallykit.counter.Counter.top"},
        receiver=FITTED,
        inputs={"n": {"$param": "top"}},
        project=[],
        reference={
            "source": REFERENCE.replace("[f'{v} x{counts[v]}' for v", "[v for v"),
            "symbol": "summarize",
        },
        cases=[
            {
                **case,
                "expect": {
                    "kind": "returns",
                    "value": [int(t.split()[0]) for t in case["expect"]["value"]],
                },
            }
            for case in _oracle()["cases"]
        ],
    )
    repo = _write_library(tmp_path / "repo", BUGGY)
    _spec, base = await _run(repo, oracle, on_base=True)
    assert base.return_code == 1 and base.result is not None
    assert base.result["resolve"] == "ok"
    _write_library(repo, FIXED)
    _spec, fixed = await _run(repo, oracle, on_base=False)
    assert fixed.return_code == 0

    reply = _reply(oracle)
    package = package_from_reply(reply, _seed(), input_digest="1" * 64, generator="t")
    checked, report = await check_references(
        package,
        references_from_reply(reply),
        seed=_seed(),
        interpreter=pin_interpreter(sys.executable, "test"),
        timeout_seconds=60,
    )
    assert report.uncovered == {} and report.excluded == {} and checked is package


async def test_a_receiver_of_another_class_is_undecided_on_the_base(tmp_path: Path) -> None:
    oracle = _oracle(
        call_kind="method",
        default_binding={"symbol": "tallykit.counter.Counter.top"},
        receiver={"$call": "builtins.dict"},
        inputs={"n": {"$param": "top"}},
        project=[],
    )
    repo = _write_library(tmp_path / "repo", BUGGY)
    _spec, base = await _run(repo, oracle, on_base=True)
    assert base.return_code == 3 and base.result is not None
    assert base.result["resolve"] == "target_crashed"


@pytest.mark.parametrize(
    "changes",
    [
        {"project": [{"attr": "missing_attribute"}]},
        {"inputs": {"counter": {"$call": "tallykit.counter.NoSuchFactory"}, "top": 1}},
        {"inputs": {"counter": {**FITTED, "then": [{"method": "no_such_method"}]}, "top": 1}},
    ],
)
async def test_an_input_or_projection_that_fails_is_undecided_on_the_base_never_a_reproduction(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    repo = _write_library(tmp_path / "repo", FIXED)
    _spec, base = await _run(repo, _oracle(**changes), on_base=True)
    assert base.return_code == 3 and base.result is not None
    assert base.result["resolve"] == "target_crashed"
    _spec, candidate = await _run(repo, _oracle(**changes), on_base=False)
    assert candidate.return_code == 1


async def test_the_reference_takes_the_params_and_returns_the_projected_value() -> None:
    reply = _reply(_oracle())
    package = package_from_reply(reply, _seed(), input_digest="1" * 64, generator="t")
    checked, report = await check_references(
        package,
        references_from_reply(reply),
        seed=_seed(),
        interpreter=pin_interpreter(sys.executable, "test"),
        timeout_seconds=60,
    )
    assert report.uncovered == {} and report.excluded == {} and checked is package


async def test_a_built_receiver_needs_a_function_reference() -> None:
    oracle = _oracle(
        call_kind="method",
        default_binding={"symbol": "tallykit.counter.Counter.top"},
        receiver=FITTED,
        inputs={"n": {"$param": "top"}},
        project=[],
        reference={
            "source": "class C:\n    def top(self, **kw):\n        return 1\n",
            "symbol": "C.top",
        },
    )
    reply = _reply(oracle)
    package = package_from_reply(reply, _seed(), input_digest="1" * 64, generator="t")
    _checked, report = await check_references(
        package,
        references_from_reply(reply),
        seed=_seed(),
        interpreter=pin_interpreter(sys.executable, "test"),
        timeout_seconds=60,
    )
    assert set(report.uncovered.values()) == {"reference_unavailable"}


# --------------------------------------------------------------------------
# Command files

TODO_TOOL = (
    "import sys\n"
    "\n"
    "path = sys.argv[1]\n"
    "with open(path, encoding='utf-8') as handle:\n"
    "    lines = handle.read().split('\\n')\n"
    "for number, line in enumerate(lines, start={START}):\n"
    "    if line.startswith('TODO'):\n"
    "        print(f'{path}:{number}')\n"
)
TODO_REFERENCE = (
    "import argparse\n"
    "\n"
    "parser = argparse.ArgumentParser()\n"
    "parser.add_argument('--path')\n"
    "path = parser.parse_args().path\n"
    "with open(path, encoding='utf-8') as handle:\n"
    "    for number, line in enumerate(handle.read().split('\\n'), start=1):\n"
    "        if line.startswith('TODO'):\n"
    "            print(f'{path}:{number}')\n"
)


def _todo_seed() -> Seed:
    return Seed(
        goal="todo_tool.py reports TODO lines by their 1-based line number.",
        acceptance_criteria=(
            "python todo_tool.py <path> prints '<path>:<line>' for every line of the file "
            "that starts with TODO, numbering lines from 1; for example a file plan.txt "
            "holding 'a\\nTODO b\\n' prints 'plan.txt:2'.",
        ),
        ontology_schema=OntologySchema(name="todo", description="tool"),
        metadata=SeedMetadata(seed_id="seed_cli_files", ambiguity_score=0.1),
    )


def _todo_oracle() -> dict[str, Any]:
    return {
        "criterion": 1,
        "role": "reproduction",
        "call_kind": "cli",
        "params": ["path"],
        "default_binding": {"symbol": "todo_tool.py", "arg_map": {"path": 0}},
        "target_named_in_criterion": True,
        "reference": {"source": TODO_REFERENCE, "symbol": ""},
        "cases": [
            {
                "held_out": False,
                "args": {"path": "plan.txt"},
                "files": {"plan.txt": "a\nTODO b\n"},
                "expect": {"kind": "cli", "exit_code": 0, "stdout_contains": "plan.txt:2"},
            },
            {
                "held_out": True,
                "args": {"path": "notes/day.txt"},
                "files": {"notes/day.txt": "TODO one\nb\nTODO three\n"},
                "expect": {"kind": "cli", "stdout": "notes/day.txt:1\nnotes/day.txt:3\n"},
            },
        ],
    }


async def test_command_case_files_reproduce_on_the_base_and_pass_the_fix(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "todo_tool.py").write_text(TODO_TOOL.replace("{START}", "0"))
    _spec, base = await _run(repo, _todo_oracle(), on_base=True, seed=_todo_seed())
    assert base.return_code == 1 and base.result is not None
    assert [case["passed"] for case in base.result["cases"]] == [False, False]
    (repo / "todo_tool.py").write_text(TODO_TOOL.replace("{START}", "1"))
    _spec, fixed = await _run(repo, _todo_oracle(), on_base=False, seed=_todo_seed())
    assert fixed.return_code == 0
    # The files went to the case's working directory, never into the checkout.
    assert sorted(path.name for path in repo.iterdir()) == ["todo_tool.py"]

    reply = _reply(_todo_oracle())
    package = package_from_reply(reply, _todo_seed(), input_digest="1" * 64, generator="t")
    checked, report = await check_references(
        package,
        references_from_reply(reply),
        seed=_todo_seed(),
        interpreter=pin_interpreter(sys.executable, "test"),
        timeout_seconds=60,
    )
    assert report.uncovered == {} and report.excluded == {} and checked is package


# --------------------------------------------------------------------------
# The constructor path end to end


async def _decide(tmp_path: Path, oracle: dict[str, Any]) -> dict[str, Any]:
    repo = _write_library(tmp_path / "repo", BUGGY)
    runtime = FakeRuntime(json.dumps(_reply(oracle)))
    constructor = CheckConstructor(
        runtime_backend="codex",
        model="gpt-test",
        runtime_factory=lambda **_kwargs: runtime,
    )
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    seed = _seed()
    verdicts: dict[str, Any] = {"prompt": None}
    try:
        for name, counter in (("buggy", BUGGY), ("fixed", FIXED)):
            state = await prepare_check_package(
                seed,
                event_store=store,
                constructor=constructor,
                execution_id=f"exec_built_{name}",
                base_checkout=repo,
                worker_workspace=repo,
                runtime_label="codex",
                settings=CheckPackageSettings(True, max_construction_attempts=1),
                store_dir=tmp_path / f"store_{name}",
            )
            assert state.admitted and state.package is not None
            (spec,) = state.package.oracles
            assert state.admission is not None
            assert state.admission.check_tiers == {spec.check_id: "A"}
            candidate = _write_library(tmp_path / f"candidate_{name}", counter)
            verdicts[name] = await verify_check_package(
                state, event_store=store, candidate_checkout=candidate
            )
            forget_live_state(state)
    finally:
        await store.close()
    verdicts["prompt"] = runtime.calls[0]["system_prompt"]
    return verdicts


async def test_the_constructor_path_admits_a_built_call_oracle_that_decides_the_criterion(
    tmp_path: Path,
) -> None:
    verdicts = await _decide(tmp_path, _oracle())
    prompt = verdicts["prompt"]
    assert '"$call"' in prompt and "`project`" in prompt and "`no_raise`" in prompt
    assert verdicts["buggy"].verdict == "fail"
    assert verdicts["fixed"].verdict == "pass"
    (verdict,) = verdicts["fixed"].verdicts.values()
    assert verdict.tier.value == "A" and not verdict.declared_binding_pass


async def test_a_no_raise_held_out_pass_never_verifies_a_criterion(tmp_path: Path) -> None:
    no_raise = _oracle(
        project=[],
        reference={
            "source": "def summarize(points, top):\n    return None\n",
            "symbol": "summarize",
        },
        cases=[
            {"held_out": False, "args": {"points": [1], "top": 3}, "expect": {"kind": "no_raise"}},
            {
                "held_out": True,
                "args": {"points": [2, 2], "top": 4},
                "expect": {"kind": "no_raise"},
            },
        ],
    )
    verdicts = await _decide(tmp_path, no_raise)
    # It still withholds acceptance from the base (which raises)...
    assert verdicts["buggy"].verdict == "fail"
    # ...but a pass that only says "it returned" verifies nothing.
    (verdict,) = verdicts["fixed"].verdicts.values()
    assert verdict.status.value == "unverified"
    assert verdict.reason == "no_held_out_case"
