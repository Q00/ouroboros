"""Library oracles: object inputs, declared setup, and set results.

A library's entry point is an importable callable, but its inputs are often
objects (a class, not a JSON value), it may have to be configured before
its modules import, and it may return a set. The live case was Django's
migration writer (``MigrationWriter.serialize(models.Model)`` returns
``("models.Model", set())`` where the fix returns the models import): with
no way to write any of the three as oracle data, the constructor wrote only
script checks, whose pass is advisory, and the package decided nothing.

The fixture library below has the same shape: ``confpkg.writer`` refuses to
import until ``confpkg.conf.configure`` ran, and ``serialize`` omits an
import for ``confpkg.base.Model``.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

import pytest

from ouroboros.boundary import harness
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
from ouroboros.boundary.oracle_run import run_oracle_check
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

CONF = (
    "_STATE = {}\n"
    "\n"
    "def configure(**options):\n"
    "    _STATE.update(options)\n"
    "    _STATE['ready'] = True\n"
    "\n"
    "def require_configured():\n"
    "    if not _STATE.get('ready'):\n"
    "        raise RuntimeError('confpkg is not configured')\n"
)
BASE = "class Model:\n    pass\n"
WRITER = (
    "from confpkg import conf\n"
    "from confpkg.base import Model\n"
    "\n"
    "conf.require_configured()\n"
    "\n"
    "\n"
    "class Writer:\n"
    "    @classmethod\n"
    "    def serialize(cls, value):\n"
    "        if isinstance(value, list):\n"
    "            parts = [cls.serialize(item) for item in value]\n"
    "            names = ', '.join(name for name, _imports in parts)\n"
    "            return '[' + names + ']', set().union(*(imports for _name, imports in parts))\n"
    "        if value is Model:\n"
    "            return 'base.Model', {IMPORTS}\n"
    "        return value.__name__, set()\n"
)
BUGGY_WRITER = WRITER.replace("{IMPORTS}", "set()")
FIXED_WRITER = WRITER.replace("{IMPORTS}", "{'from confpkg import base'}")
MODEL = {"$symbol": "confpkg.base.Model"}
OBJECT = {"$symbol": "builtins.object"}
REFERENCE = (
    "def serialize(value):\n"
    "    if isinstance(value, list):\n"
    "        parts = [serialize(item) for item in value]\n"
    "        names = ', '.join(name for name, _imports in parts)\n"
    "        return '[' + names + ']', set().union(set(), *(i for _n, i in parts))\n"
    "    if value == 'confpkg.base.Model':\n"
    "        return 'base.Model', {'from confpkg import base'}\n"
    "    return value.rsplit('.', 1)[-1], set()\n"
)
SETUP = [{"symbol": "confpkg.conf.configure", "kwargs": {"debug": False}}]


def _write_library(root: Path, writer: str) -> Path:
    package = root / "confpkg"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("")
    (package / "conf.py").write_text(CONF)
    (package / "base.py").write_text(BASE)
    (package / "writer.py").write_text(writer)
    return root


def _seed() -> Seed:
    return Seed(
        goal="Serialized references to confpkg.base.Model carry the import they need.",
        acceptance_criteria=(
            "Writer.serialize returns the import 'from confpkg import base' for "
            "confpkg.base.Model, also when the model appears inside a list; for example "
            "serialize(Model) == ('base.Model', {'from confpkg import base'}).",
        ),
        ontology_schema=OntologySchema(name="confpkg", description="library"),
        metadata=SeedMetadata(seed_id="seed_library_oracle", ambiguity_score=0.1),
    )


def _oracle(*, setup: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    oracle: dict[str, Any] = {
        "criterion": 1,
        "role": "reproduction",
        "call_kind": "function",
        "params": ["value"],
        "default_binding": {"symbol": "confpkg.writer.Writer.serialize"},
        "target_named_in_criterion": False,
        "reference": {"source": REFERENCE, "symbol": "serialize"},
        "cases": [
            {
                "held_out": False,
                "args": {"value": MODEL},
                "expect": {
                    "kind": "returns",
                    "value": ["base.Model", ["from confpkg import base"]],
                },
            },
            {
                "held_out": True,
                "args": {"value": [MODEL]},
                "expect": {
                    "kind": "returns",
                    "value": ["[base.Model]", ["from confpkg import base"]],
                },
            },
            {
                "held_out": True,
                "args": {"value": [OBJECT, MODEL]},
                "expect": {
                    "kind": "returns",
                    "value": ["[object, base.Model]", ["from confpkg import base"]],
                },
            },
        ],
    }
    if setup is not None:
        oracle["setup"] = setup
    return oracle


def _reply(oracle: dict[str, Any]) -> dict[str, Any]:
    return {"oracles": [oracle], "checks": [], "files": [], "uncovered": []}


# --------------------------------------------------------------------------
# Harness and grammar


def test_a_returned_set_is_its_items_sorted_by_json_text() -> None:
    assert harness._plain({"b", "a"}) == ["a", "b"]
    assert harness._plain(frozenset({2, 10})) == [10, 2]  # "10" < "2" as JSON text
    assert harness._plain(("x", {("b", 1), ("a", 2)})) == ["x", [["a", 2], ["b", 1]]]
    with pytest.raises(TypeError):
        harness._plain({object()})


def test_symbol_references_are_found_and_named_at_any_depth() -> None:
    value = {"value": [OBJECT, {"inner": MODEL}], "plain": {"$symbol": "a.b", "extra": 1}}
    assert harness.symbol_refs(value) == ["builtins.object", "confpkg.base.Model"]
    assert harness.named_inputs(value) == {
        "value": ["builtins.object", {"inner": "confpkg.base.Model"}],
        "plain": {"$symbol": "a.b", "extra": 1},
    }


@pytest.mark.parametrize(
    ("oracle", "code"),
    [
        (
            _oracle() | {"cases": [{**_oracle()["cases"][1], "args": {"value": {"$symbol": "x"}}}]},
            ReplyFailure.SYMBOL_REF_INVALID,
        ),
        (_oracle(setup=[{"symbol": "configure"}]), ReplyFailure.SETUP_INVALID),
        (
            _oracle(setup=[{"symbol": "confpkg.conf.configure", "code": "x"}]),
            ReplyFailure.SETUP_INVALID,
        ),
        (_oracle(setup={"symbol": "confpkg.conf.configure"}), ReplyFailure.SETUP_INVALID),
        (
            _oracle(setup=[{"symbol": "a.b", "kwargs": {"k": {"$symbol": 3}}}]),
            ReplyFailure.SYMBOL_REF_INVALID,
        ),
    ],
)
def test_malformed_references_and_setup_are_typed_refusals(
    oracle: dict[str, Any], code: ReplyFailure
) -> None:
    with pytest.raises(ReplyError) as raised:
        package_from_reply(_reply(oracle), _seed(), input_digest="1" * 64, generator="t")
    assert raised.value.code is code


def test_a_cli_oracle_takes_neither_setup_nor_references() -> None:
    cli = {
        **_oracle(setup=SETUP),
        "call_kind": "cli",
        "default_binding": {"symbol": "tool.py"},
        "cases": [
            {**case, "expect": {"kind": "cli", "exit_code": 0}} for case in _oracle()["cases"]
        ],
    }
    with pytest.raises(ReplyError) as raised:
        package_from_reply(_reply(cli), _seed(), input_digest="1" * 64, generator="t")
    assert raised.value.code is ReplyFailure.ORACLE_INVALID


def test_setup_is_frozen_only_when_declared() -> None:
    plain = package_from_reply(_reply(_oracle()), _seed(), input_digest="1" * 64, generator="t")
    configured = package_from_reply(
        _reply(_oracle(setup=SETUP)), _seed(), input_digest="1" * 64, generator="t"
    )
    assert "setup" not in oracle_data(plain.oracles)["oracles"][0]
    assert oracle_data(configured.oracles)["oracles"][0]["setup"] == [
        {"symbol": "confpkg.conf.configure", "args": [], "kwargs": {"debug": False}}
    ]
    assert configured.sha256 != plain.sha256


# --------------------------------------------------------------------------
# Running the oracle


async def _run(repo: Path, oracle: dict[str, Any], *, on_base: bool):
    package = package_from_reply(_reply(oracle), _seed(), input_digest="1" * 64, generator="t")
    (spec,) = package.oracles
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


async def test_without_setup_the_base_run_is_undecided_never_a_reproduction(
    tmp_path: Path,
) -> None:
    repo = _write_library(tmp_path / "repo", BUGGY_WRITER)
    spec, run = await _run(repo, _oracle(), on_base=True)
    assert run.return_code == 3
    assert run.result is not None and run.result["resolve"] == "import_error"
    assert spec.base_run_tier(run.result["resolve"]).value == "U"


async def test_a_failing_setup_call_is_an_import_error_not_a_missing_target(
    tmp_path: Path,
) -> None:
    repo = _write_library(tmp_path / "repo", BUGGY_WRITER)
    for setup in (
        [{"symbol": "confpkg.conf.no_such_call"}],
        [{"symbol": "confpkg.conf.configure", "args": [1]}],
        [{"symbol": "os.getcwd"}],  # not a callable of the checkout
    ):
        _spec, run = await _run(repo, _oracle(setup=setup), on_base=True)
        assert run.return_code == 3
        assert run.result is not None and run.result["resolve"] == "import_error"


async def test_a_reference_that_names_nothing_is_undecided_on_the_base(tmp_path: Path) -> None:
    repo = _write_library(tmp_path / "repo", BUGGY_WRITER)
    oracle = _oracle(setup=SETUP)
    oracle["cases"][2]["args"] = {"value": [{"$symbol": "confpkg.base.Missing"}]}
    _spec, run = await _run(repo, oracle, on_base=True)
    assert run.return_code == 3 and run.result is not None
    assert run.result["resolve"] == "target_crashed"


async def test_setup_and_references_reproduce_on_the_base_and_pass_the_fix(tmp_path: Path) -> None:
    repo = _write_library(tmp_path / "repo", BUGGY_WRITER)
    spec, base = await _run(repo, _oracle(setup=SETUP), on_base=True)
    assert base.return_code == 1
    assert base.result is not None and base.result["resolve"] == "ok"
    assert spec.base_run_tier("ok").value == "A"
    assert [case["passed"] for case in base.result["cases"]] == [False, False, False]
    assert "observed ['base.Model', []]" in base.result["cases"][0]["detail"]

    _write_library(repo, FIXED_WRITER)
    _spec, fixed = await _run(repo, _oracle(setup=SETUP), on_base=False)
    assert fixed.return_code == 0
    assert fixed.result is not None
    assert [(c["held_out"], c["passed"]) for c in fixed.result["cases"]] == [
        (False, True),
        (True, True),
        (True, True),
    ]


async def test_the_reference_receives_dotted_paths_and_runs_without_setup(
    tmp_path: Path,
) -> None:
    reply = _reply(_oracle(setup=SETUP))
    package = package_from_reply(reply, _seed(), input_digest="1" * 64, generator="t")
    checked, report = await check_references(
        package,
        references_from_reply(reply),
        seed=_seed(),
        interpreter=pin_interpreter(sys.executable, "test"),
        timeout_seconds=60,
    )
    assert report.uncovered == {} and report.excluded == {}
    assert checked is package


# --------------------------------------------------------------------------
# The constructor path end to end


async def test_the_constructor_path_admits_a_library_oracle_that_decides_the_criterion(
    tmp_path: Path,
) -> None:
    repo = _write_library(tmp_path / "repo", BUGGY_WRITER)
    runtime = FakeRuntime(json.dumps(_reply(_oracle(setup=SETUP))))
    constructor = CheckConstructor(
        runtime_backend="codex",
        model="gpt-test",
        runtime_factory=lambda **_kwargs: runtime,
    )
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    seed = _seed()
    verdicts = {}
    try:
        for name, writer in (("buggy", BUGGY_WRITER), ("fixed", FIXED_WRITER)):
            state = await prepare_check_package(
                seed,
                event_store=store,
                constructor=constructor,
                execution_id=f"exec_library_{name}",
                base_checkout=repo,
                worker_workspace=repo,
                runtime_label="codex",
                settings=CheckPackageSettings(True, max_construction_attempts=1),
                store_dir=tmp_path / f"store_{name}",
            )
            assert state.admitted and state.package is not None
            (spec,) = state.package.oracles
            assert [call.symbol for call in spec.setup] == ["confpkg.conf.configure"]
            assert state.admission is not None
            assert state.admission.check_tiers == {spec.check_id: "A"}
            candidate = _write_library(tmp_path / f"candidate_{name}", writer)
            verdicts[name] = await verify_check_package(
                state, event_store=store, candidate_checkout=candidate
            )
            forget_live_state(state)
    finally:
        await store.close()
    # The packaged prompt describes the grammar the reply used.
    prompt = runtime.calls[0]["system_prompt"]
    assert '{"$symbol": "package.module.Name"}' in prompt and "`setup`" in prompt
    assert verdicts["buggy"].verdict == "fail"
    assert verdicts["fixed"].verdict == "pass"
    (verdict,) = verdicts["fixed"].verdicts.values()
    assert verdict.tier.value == "A"
