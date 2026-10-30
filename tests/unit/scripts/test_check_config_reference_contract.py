"""Self-tests for ``scripts/check-config-reference-contract.py``.

The repository-level assertion matters only if its scanner and documentation
markers fail in both directions: an unwired field must be rejected, and a
newly wired field must stop being described as inert.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
import time

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "check-config-reference-contract.py"


@pytest.fixture(scope="module")
def contract():
    spec = importlib.util.spec_from_file_location("check_config_reference_contract", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _audit_as_documented_inert(contract, field, reads):
    return contract.audit_contract(
        fields=frozenset({field}),
        reads=reads,
        rows={
            field: contract.ReferenceRow(
                "true", "Currently inert. Effective control: runtime.stage1_enabled."
            )
        },
        markers={field: contract.InertMarker(field, "runtime.stage1_enabled")},
        allowlist={},
        documented_defaults={},
    )


def test_current_repository_passes_standalone_contract() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Config reference contract OK" in result.stdout


@pytest.mark.parametrize(
    ("parameter", "calls"),
    [
        ("config", "render(Report())"),
        ("config", "render(config=Report())"),
        ("config: Report", "render(Report())"),
        ("value", "render(Report())"),
        ("config", "alias = render; alias(Report())"),
        ("config", "list(map(render, [Report()]))"),
        ("config", "from functools import partial\npartial(render, Report())()"),
    ],
)
def test_public_foreign_calls_do_not_establish_config_dispositions(
    contract, tmp_path: Path, parameter: str, calls: str
) -> None:
    source = f"""
class EvaluationReport:
    stage1_enabled = True
class Report:
    evaluation = EvaluationReport()
def render({parameter}):
    return {parameter.split(":")[0]}.evaluation.stage1_enabled
{calls}
"""
    (tmp_path / "foreign.py").write_text(source, encoding="utf-8")
    field = contract.ConfigField("evaluation", "stage1_enabled")
    reads = contract.runtime_reads(tmp_path, frozenset({field}))

    assert reads == frozenset()
    assert not reads.unresolved_semantics
    assert not _audit_as_documented_inert(contract, field, reads).violations
    report = contract.audit_contract(
        fields=frozenset({field}),
        reads=reads,
        rows={field: contract.ReferenceRow("true", "Domain description.")},
        markers={},
        allowlist={},
        documented_defaults={},
    )
    assert report.violations


@pytest.mark.parametrize(
    "calls",
    [
        "",  # Exported, uncalled config parameters retain the existing convention.
        "render(settings)",
        "render(Report()); render(settings)",
        "render(settings); render(Report())",
        "render(Report()); render(unknown)",
        "render(unknown); render(Report())",
        "render(Report() if unknown else missing)",
        "candidate = Report() if unknown else missing; render(candidate)",
        "render(Report() if unknown else settings)",
        "render(Report()); list(map(render, [settings]))",
    ],
)
def test_public_call_rescan_preserves_config_and_unknown_alternatives(
    contract, tmp_path: Path, calls: str
) -> None:
    (tmp_path / "config_or_unknown.py").write_text(
        f"""
class EvaluationReport:
    stage1_enabled = True
class Report:
    evaluation = EvaluationReport()
def render(config):
    return config.evaluation.stage1_enabled
{calls}
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")
    reads = contract.runtime_reads(tmp_path, frozenset({field}))

    assert reads == frozenset({field})
    assert _audit_as_documented_inert(contract, field, reads).violations


def test_public_foreign_call_preserves_proven_config_default(contract, tmp_path: Path) -> None:
    (tmp_path / "config_default.py").write_text(
        """
class Report:
    evaluation = None
def render(config=settings):
    return config.evaluation.stage1_enabled
render(Report())
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset({field})


@pytest.mark.parametrize("wrapper_name", ["wrapper", "_wrapper"])
def test_public_foreign_call_provenance_survives_wrapper(
    contract, tmp_path: Path, wrapper_name: str
) -> None:
    (tmp_path / "wrapper.py").write_text(
        f"""
class EvaluationReport:
    stage1_enabled = True
class Report:
    evaluation = EvaluationReport()
def render(config):
    return config.evaluation.stage1_enabled
def {wrapper_name}(value):
    return render(value)
{wrapper_name}(Report())
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")
    reads = contract.runtime_reads(tmp_path, frozenset({field}))
    assert reads == frozenset()
    assert not reads.unresolved_semantics
    assert not _audit_as_documented_inert(contract, field, reads).violations


@pytest.mark.parametrize("reader_name", ["a_reader", "z_reader"])
@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("argument", ["Report()", "settings", "unknown"])
def test_public_call_provenance_survives_module_scan_order(
    contract, tmp_path: Path, reader_name: str, argument: str, wrapped: bool
) -> None:
    (tmp_path / f"{reader_name}.py").write_text(
        ("def wrapper(value):\n    return render(value)\n" if wrapped else "")
        + "def render(config):\n    return config.evaluation.stage1_enabled\n",
        encoding="utf-8",
    )
    (tmp_path / "consumer.py").write_text(
        f"""
from {reader_name} import {"wrapper as render" if wrapped else "render"}
class EvaluationReport:
    stage1_enabled = True
class Report:
    evaluation = EvaluationReport()
render({argument})
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")
    reads = contract.runtime_reads(tmp_path, frozenset({field}))
    assert reads == (frozenset() if argument == "Report()" else frozenset({field}))
    assert not reads.unresolved_semantics


@pytest.mark.parametrize("alternative", ["unknown", "settings"])
@pytest.mark.parametrize("foreign_first", [False, True])
def test_wrapper_rescan_preserves_uncertain_calls_independent_of_definition_order(
    contract, tmp_path: Path, alternative: str, foreign_first: bool
) -> None:
    calls = ["wrapper(Report())", f"wrapper({alternative})"]
    if not foreign_first:
        calls.reverse()
    (tmp_path / "wrapper_alternatives.py").write_text(
        """
class EvaluationReport:
    stage1_enabled = True
class Report:
    evaluation = EvaluationReport()
def wrapper(value):
    return render(value)
def render(config):
    return config.evaluation.stage1_enabled
"""
        + "\n".join(calls),
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")
    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset({field})


@pytest.mark.parametrize("reader_name", ["a_reader", "z_reader"])
@pytest.mark.parametrize("wrapper_first", [False, True])
@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize(
    ("arguments", "expected_read"),
    [
        (("Report()",), False),
        (("settings",), True),
        (("unknown",), True),
        (("Report()", "settings"), True),
        (("settings", "Report()"), True),
        (("Report()", "unknown"), True),
        (("unknown", "Report()"), True),
    ],
)
def test_imported_wrapper_keeps_all_call_alternatives(
    contract,
    tmp_path: Path,
    reader_name: str,
    wrapper_first: bool,
    private: bool,
    arguments: tuple[str, ...],
    expected_read: bool,
) -> None:
    wrapper = "_wrapper" if private else "wrapper"
    wrapper_source = f"def {wrapper}(value):\n    return render(value)\n"
    render_source = "def render(config):\n    return config.evaluation.stage1_enabled\n"
    (tmp_path / f"{reader_name}.py").write_text(
        wrapper_source + render_source if wrapper_first else render_source + wrapper_source,
        encoding="utf-8",
    )
    (tmp_path / "consumer.py").write_text(
        f"from {reader_name} import {wrapper}\n"
        "class EvaluationReport:\n    stage1_enabled = True\n"
        "class Report:\n    evaluation = EvaluationReport()\n"
        + "\n".join(f"{wrapper}({argument})" for argument in arguments),
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")
    reads = contract.runtime_reads(tmp_path, frozenset({field}))

    assert reads == (frozenset({field}) if expected_read else frozenset())
    assert not reads.unresolved_semantics
    assert bool(_audit_as_documented_inert(contract, field, reads).violations) == expected_read


def test_runtime_scan_honors_global_and_nonlocal_binding_updates(contract, tmp_path: Path) -> None:
    (tmp_path / "scope_updates.py").write_text(
        """
reader = None

def install_reader():
    global reader
    reader = lambda config: config.evaluation.stage1_enabled

install_reader()
reader(settings)

erased = lambda config: config.evaluation.stage2_enabled
def erase_reader():
    global erased
    erased = None
erase_reader()

def outer():
    nested = lambda config: config.evaluation.stage3_enabled
    def erase_nested():
        nonlocal nested
        nested = None
    erase_nested()
    return nested
outer()
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name)
        for name in ("stage1_enabled", "stage2_enabled", "stage3_enabled")
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {contract.ConfigField("evaluation", "stage1_enabled")}
    )


def test_runtime_scan_executes_invoked_private_helpers_and_methods(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "private_calls.py").write_text(
        """
def _read():
    return settings.evaluation.stage1_enabled

class Reader:
    def _read(self):
        return settings.evaluation.stage2_enabled

_read()
Reader()._read()
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name) for name in ("stage1_enabled", "stage2_enabled")
    )

    assert contract.runtime_reads(tmp_path, fields) == fields


def test_runtime_scan_executes_public_zero_provenance_callables(contract, tmp_path: Path) -> None:
    (tmp_path / "public_calls.py").write_text(
        """
class Reader:
    @staticmethod
    def read():
        return settings.evaluation.stage1_enabled

    @classmethod
    def read_class(cls):
        return settings.evaluation.stage2_enabled

    def __call__(self):
        return settings.evaluation.stage3_enabled

Reader.read()
Reader.read_class()
Reader()()
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name)
        for name in ("stage1_enabled", "stage2_enabled", "stage3_enabled")
    )

    assert contract.runtime_reads(tmp_path, fields) == fields


def test_runtime_scan_executes_public_call_before_later_overwrite(contract, tmp_path: Path) -> None:
    (tmp_path / "called_then_overwritten.py").write_text(
        """
def read():
    return settings.evaluation.stage1_enabled

read()
read = external
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset({field})


def test_runtime_scan_rejects_public_reader_overwritten_before_call(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "overwritten_then_called.py").write_text(
        """
def read():
    return settings.evaluation.stage1_enabled

read = external
read()
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset()


def test_runtime_scan_executes_zero_provenance_iterator_protocol(contract, tmp_path: Path) -> None:
    (tmp_path / "public_iterator.py").write_text(
        """
class Reader:
    def __iter__(self):
        yield settings.evaluation.stage1_enabled

for _ in Reader():
    pass
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset({field})


def test_runtime_scan_requires_generator_consumption(contract, tmp_path: Path) -> None:
    (tmp_path / "deferred.py").write_text(
        """
def generators(config):
    section = config.evaluation
    pending = (section.stage1_enabled for _ in [1])
    consumed = list(section.stage2_enabled for _ in [1])
    for item in (section.stage3_enabled for _ in [1]):
        pass
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name)
        for name in ("stage1_enabled", "stage2_enabled", "stage3_enabled")
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {
            contract.ConfigField("evaluation", "stage2_enabled"),
            contract.ConfigField("evaluation", "stage3_enabled"),
        }
    )


def test_runtime_scan_defers_coroutine_body_until_await(contract, tmp_path: Path) -> None:
    (tmp_path / "coroutines.py").write_text(
        """
async def runtime(config):
    async def ignored():
        return config.evaluation.stage1_enabled
    async def consumed():
        return config.evaluation.stage2_enabled
    ignored()
    return await consumed()

await runtime(settings)
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name) for name in ("stage1_enabled", "stage2_enabled")
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {contract.ConfigField("evaluation", "stage2_enabled")}
    )


def test_runtime_scan_executes_map_callback_only_when_eagerly_consumed(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "map_callback.py").write_text(
        """
def read(section):
    return section.stage1_enabled

pending = map(read, [settings.evaluation])
list(pending)
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")
    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset({field})


def test_runtime_scan_does_not_execute_unconsumed_or_empty_map_callbacks(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "map_callback_negative.py").write_text(
        """
def read(section):
    return section.stage1_enabled

pending = map(read, [settings.evaluation])
consumed_empty = list(map(read, []))
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")
    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset()


def test_runtime_scan_tracks_functools_partial_callable_provenance(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "partial_callback.py").write_text(
        """
import functools

def read(section):
    return section.stage1_enabled

runner = functools.partial(read, settings.evaluation)
runner()
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")
    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset({field})


def test_runtime_scan_tracks_complete_partial_bindings(contract, tmp_path: Path) -> None:
    (tmp_path / "partial_complete.py").write_text(
        """
from functools import partial

def read(prefix, section):
    return section.stage1_enabled

partial(read, "x", settings.evaluation)()
partial(read, section=settings.evaluation)()
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")
    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset({field})


def test_runtime_scan_tracks_aliased_and_qualified_serializers(contract, tmp_path: Path) -> None:
    (tmp_path / "serializers.py").write_text(
        """
import builtins

dump = vars
dump(settings.evaluation)["stage1_enabled"]
builtins.vars(settings.evaluation)["stage1_enabled"]
dump_model = settings.evaluation.model_dump
dump_model()["stage1_enabled"]
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")
    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset({field})


def test_runtime_scan_drops_overwritten_serializer_alias(contract, tmp_path: Path) -> None:
    (tmp_path / "serializer_shadow.py").write_text(
        """
dump = vars
dump = unrelated
dump(settings.evaluation)["stage1_enabled"]
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")
    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset()


def test_runtime_scan_tracks_concrete_mapping_serializers(contract, tmp_path: Path) -> None:
    (tmp_path / "mapping_serializers.py").write_text(
        """
dict(settings.evaluation)["stage1_enabled"]
settings.evaluation.__dict__["stage1_enabled"]
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")
    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset({field})


def test_runtime_scan_tracks_exact_builtin_dict_aliases_and_rejects_shadowing(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "dict_identity.py").write_text(
        """
import builtins

to_mapping = dict
to_mapping(config.evaluation)["stage1_enabled"]
builtins.dict(config.evaluation)["stage1_enabled"]

def shadow(dict):
    return dict(config.evaluation)["stage2_enabled"]

dict = None
dict(config.evaluation)["stage3_enabled"]
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name)
        for name in ("stage1_enabled", "stage2_enabled", "stage3_enabled")
    )

    assert contract.runtime_reads(tmp_path, fields) == {
        contract.ConfigField("evaluation", "stage1_enabled")
    }


@pytest.mark.parametrize(
    "expression",
    (
        'config.model_dump()["evaluation"]["stage1_enabled"]',
        'vars(config)["evaluation"]["stage1_enabled"]',
        'config.__dict__["evaluation"]["stage1_enabled"]',
    ),
)
def test_runtime_scan_tracks_full_root_serialization(
    contract, tmp_path: Path, expression: str
) -> None:
    (tmp_path / "root_serialization.py").write_text(expression, encoding="utf-8")
    field = contract.ConfigField("evaluation", "stage1_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset({field})


def test_runtime_scan_ignores_untracked_root_serialization_keys(contract, tmp_path: Path) -> None:
    (tmp_path / "root_serialization_untracked.py").write_text(
        'config.model_dump()["untracked"]["stage1_enabled"]', encoding="utf-8"
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset()


def test_runtime_scan_tracks_context_manager_entry_values(contract, tmp_path: Path) -> None:
    (tmp_path / "context_entries.py").write_text(
        """
import contextlib
from contextlib import contextmanager

with contextlib.nullcontext(settings.evaluation) as section:
    value = section.stage1_enabled

@contextmanager
def entered(config):
    yield config.evaluation.stage2_enabled

with entered(settings):
    pass
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name) for name in ("stage1_enabled", "stage2_enabled")
    )
    assert contract.runtime_reads(tmp_path, fields) == fields - {
        contract.ConfigField("evaluation", "semantic_model")
    }


def test_runtime_scan_tracks_qualified_and_imported_builtin_protocols(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "builtin_protocols.py").write_text(
        """
import builtins
from builtins import int as integer

class Reader:
    def __len__(self):
        return settings.evaluation.stage1_enabled

    def __bool__(self):
        return settings.evaluation.stage2_enabled

    def __int__(self):
        return settings.evaluation.stage3_enabled

builtins.len(Reader())
builtins.bool(Reader())
integer(Reader())
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name)
        for name in ("stage1_enabled", "stage2_enabled", "stage3_enabled")
    )

    assert contract.runtime_reads(tmp_path, fields) == fields


def test_runtime_scan_does_not_treat_shadowed_builtin_protocol_names_as_builtins(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "shadowed_builtin_protocols.py").write_text(
        """
import builtins
from builtins import int as integer

class Reader:
    def __len__(self):
        return settings.evaluation.stage1_enabled
    def __int__(self):
        return settings.evaluation.stage2_enabled

class FakeBuiltins:
    len = lambda self, value: 0

builtins = FakeBuiltins()
integer = lambda value: 0
builtins.len(Reader())
integer(Reader())
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name) for name in ("stage1_enabled", "stage2_enabled")
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset()


def test_runtime_scan_executes_exact_new_without_fabricating_requested_instance(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "new_protocol.py").write_text(
        """
class UnrelatedCallable:
    def __call__(self):
        return settings.evaluation.stage2_enabled

class Reader:
    def __new__(cls):
        settings.evaluation.stage1_enabled
        return UnrelatedCallable()

    def __init__(self):
        settings.evaluation.stage2_enabled

Reader()
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name) for name in ("stage1_enabled", "stage2_enabled")
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {contract.ConfigField("evaluation", "stage1_enabled")}
    )


def test_runtime_scan_executes_normal_local_initializer(contract, tmp_path: Path) -> None:
    (tmp_path / "initializer.py").write_text(
        """
class Reader:
    def __init__(self, config):
        config.evaluation.uncertainty_threshold

Reader(settings)
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "uncertainty_threshold")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset({field})


def test_runtime_scan_honors_exact_metaclass_construction(contract, tmp_path: Path) -> None:
    (tmp_path / "metaclass_protocol.py").write_text(
        """
class Unrelated:
    pass

class Meta(type):
    def __call__(cls):
        settings.evaluation.stage1_enabled
        return Unrelated()

class Requested(metaclass=Meta):
    def __new__(cls):
        settings.evaluation.stage2_enabled
        return super().__new__(cls)

Requested()
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name) for name in ("stage1_enabled", "stage2_enabled")
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {contract.ConfigField("evaluation", "stage1_enabled")}
    )


def test_runtime_scan_executes_class_creation_protocols_without_overreach(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "class_creation.py").write_text(
        """
class Base:
    def __init_subclass__(cls):
        settings.evaluation.stage1_enabled

class Descriptor:
    def __set_name__(self, owner, name):
        settings.evaluation.stage2_enabled

class Child(Base):
    field = Descriptor()

class PendingBase:
    def __init_subclass__(cls):
        settings.evaluation.stage3_enabled

class PendingDescriptor:
    def __set_name__(self, owner, name):
        settings.evaluation.semantic_model

PendingDescriptor()
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name)
        for name in (
            "stage1_enabled",
            "stage2_enabled",
            "stage3_enabled",
            "semantic_model",
        )
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage2_enabled"),
        }
    )


def test_runtime_scan_threads_context_manager_abrupt_control_flow(contract, tmp_path: Path) -> None:
    fields = frozenset(
        contract.ConfigField("evaluation", name)
        for name in ("stage1_enabled", "stage2_enabled", "stage3_enabled")
    )
    cases = {
        "entry_raises": (
            """
class Manager:
    def __enter__(self):
        raise RuntimeError
    def __exit__(self, *args):
        return False
with Manager():
    settings.evaluation.stage1_enabled
""",
            frozenset(),
        ),
        "exit_suppresses": (
            """
class Manager:
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return True
with Manager():
    raise RuntimeError
settings.evaluation.stage2_enabled
""",
            frozenset({contract.ConfigField("evaluation", "stage2_enabled")}),
        ),
        "exit_raises": (
            """
class Manager:
    def __enter__(self):
        return self
    def __exit__(self, *args):
        raise RuntimeError
with Manager():
    pass
settings.evaluation.stage3_enabled
""",
            frozenset(),
        ),
    }
    for name, (source, expected) in cases.items():
        case_root = tmp_path / name
        case_root.mkdir()
        (case_root / "case.py").write_text(source, encoding="utf-8")
        assert contract.runtime_reads(case_root, fields) == expected


def test_runtime_scan_executes_eager_key_callback_only_for_nonempty_input(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "key_callbacks.py").write_text(
        """
sorted([settings.evaluation], key=lambda section: section.stage1_enabled)
sorted([], key=lambda section: section.stage2_enabled)
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name) for name in ("stage1_enabled", "stage2_enabled")
    )
    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {contract.ConfigField("evaluation", "stage1_enabled")}
    )


def test_runtime_scan_executes_standard_library_callback_consumers(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "stdlib_callbacks.py").write_text(
        """
import functools
import heapq
import itertools

def star(prefix, section):
    return section.stage1_enabled

list(itertools.starmap(star, [("x", settings.evaluation)]))
functools.reduce(
    lambda total, section: total + int(section.stage2_enabled),
    [settings.evaluation],
    0,
)
heapq.nsmallest(1, [settings.evaluation], key=lambda section: section.stage3_enabled)
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name)
        for name in ("stage1_enabled", "stage2_enabled", "stage3_enabled")
    )

    assert contract.runtime_reads(tmp_path, fields) == fields


def test_runtime_scan_consumes_local_iterator_protocols(contract, tmp_path: Path) -> None:
    (tmp_path / "local_iterator.py").write_text(
        """
class _Reader:
    def __iter__(self):
        yield settings.evaluation.stage1_enabled

list(_Reader())
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset({field})


def test_runtime_scan_consumes_local_next_but_not_unconsumed_iterators(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "local_next.py").write_text(
        """
class Reader:
    def __init__(self, section):
        self.section = section

    def __iter__(self):
        return self

    def __next__(self):
        return self.section.stage1_enabled

class Pending:
    def __iter__(self):
        return self

    def __next__(self):
        return settings.evaluation.stage2_enabled

list(Reader(settings.evaluation))
pending = Pending()
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name) for name in ("stage1_enabled", "stage2_enabled")
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {contract.ConfigField("evaluation", "stage1_enabled")}
    )


def test_runtime_scan_stops_sequence_fallback_after_first_index_error(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "empty_sequence.py").write_text(
        """
class Empty:
    def __init__(self, section):
        self.section = section

    def __getitem__(self, index):
        if index == 0:
            raise IndexError
        return self.section.stage1_enabled

list(Empty(settings.evaluation))
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset()


def test_runtime_scan_executes_registered_exit_callbacks(contract, tmp_path: Path) -> None:
    (tmp_path / "exit_callback.py").write_text(
        """
import atexit

def _reader(section):
    return section.stage2_enabled

atexit.register(_reader, settings.evaluation)
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage2_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset({field})


def test_runtime_scan_models_min_max_and_heap_key_callback_cardinality(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "key_callback_cardinality.py").write_text(
        """
import heapq

def read_stage1(section):
    return section.stage1_enabled

def read_stage2(section):
    return section.stage2_enabled

max(config.evaluation, config.evaluation, key=read_stage1)
heapq.nsmallest(0, [config.evaluation], key=read_stage2)
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name) for name in ("stage1_enabled", "stage2_enabled")
    )

    assert contract.runtime_reads(tmp_path, fields) == {
        contract.ConfigField("evaluation", "stage1_enabled")
    }


def test_runtime_scan_does_not_execute_empty_standard_library_callback_consumers(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "empty_stdlib_callbacks.py").write_text(
        """
from functools import reduce
from heapq import nsmallest
from itertools import starmap

list(starmap(lambda section: section.stage1_enabled, []))
reduce(lambda total, section: section.stage2_enabled, [], 0)
nsmallest(1, [], key=lambda section: section.stage3_enabled)
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name)
        for name in ("stage1_enabled", "stage2_enabled", "stage3_enabled")
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset()


def test_runtime_scan_executes_consumed_itertools_accumulate_callbacks(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "accumulate_callbacks.py").write_text(
        """
import itertools
from itertools import accumulate

def pick(left, section):
    return section.stage1_enabled

list(itertools.accumulate((None, config.evaluation), pick))
list(accumulate((None, config.evaluation), lambda left, section: section.stage2_enabled))
list(accumulate((), lambda left, section: section.stage3_enabled))
list(accumulate((config.evaluation,), lambda left, section: section.semantic_model))
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name)
        for name in ("stage1_enabled", "stage2_enabled", "stage3_enabled", "semantic_model")
    )

    assert contract.runtime_reads(tmp_path, fields) == fields - {
        contract.ConfigField("evaluation", "stage3_enabled"),
        contract.ConfigField("evaluation", "semantic_model"),
    }


def test_runtime_scan_tracks_exit_stack_and_async_context_manager_entries(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "context_protocols.py").write_text(
        """
from contextlib import ExitStack, asynccontextmanager, nullcontext

with ExitStack() as stack:
    section = stack.enter_context(nullcontext(settings.evaluation))
    value = section.stage1_enabled

@asynccontextmanager
async def entered(config):
    yield config.evaluation

async def read(config):
    async with entered(config) as section:
        return section.stage2_enabled
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name) for name in ("stage1_enabled", "stage2_enabled")
    )

    reads = contract.runtime_reads(tmp_path, fields)
    assert reads == fields
    for field in fields:
        assert _audit_as_documented_inert(contract, field, reads).violations
    report = _audit_as_documented_inert(
        contract, contract.ConfigField("evaluation", "stage1_enabled"), reads
    )
    assert "evaluation.stage1_enabled: conflicting config-field dispositions" in report.violations
    assert "evaluation.stage1_enabled: production-wired field is still documented inert" in (
        report.violations
    )


def test_runtime_scan_tracks_local_and_closing_context_entry_protocols(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "ordinary_context_entries.py").write_text(
        """
import contextlib

class Box:
    def __init__(self, section):
        self.section = section

    def __enter__(self):
        return self.section

    def __exit__(self, *args):
        return None

with Box(settings.evaluation) as section:
    value = section.stage1_enabled

with contextlib.closing(settings.evaluation) as section:
    value = section.stage2_enabled
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name) for name in ("stage1_enabled", "stage2_enabled")
    )

    reads = contract.runtime_reads(tmp_path, fields)
    assert reads == fields
    for field in fields:
        assert _audit_as_documented_inert(contract, field, reads).violations


def test_runtime_scan_does_not_overreach_class_or_starred_data_provenance(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "inactive_class_and_starred_values.py").write_text(
        """
import operator

def read_stage(section):
    return section.stage2_enabled

class Fields:
    STAGE = "stage1_enabled"

class Registry:
    READERS = {"main": read_stage}

GETATTR_ARGS = (report.evaluation, Fields.STAGE)
GETITEM_ARGS = (report.evaluation.model_dump(), "stage2_enabled")

getattr(*GETATTR_ARGS)
operator.getitem(*GETITEM_ARGS)
Registry.READERS["main"](report.evaluation)
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name) for name in ("stage1_enabled", "stage2_enabled")
    )

    reads = contract.runtime_reads(tmp_path, fields)
    assert reads == frozenset()
    for field in fields:
        assert _audit_as_documented_inert(contract, field, reads).violations == ()


def test_runtime_scan_does_not_enter_unconsumed_context_objects(contract, tmp_path: Path) -> None:
    (tmp_path / "unentered_contexts.py").write_text(
        """
from contextlib import closing

class Box:
    def __init__(self, section):
        self.section = section

    def __enter__(self):
        return self.section.stage1_enabled

Box(settings.evaluation)
closing(settings.evaluation)
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    reads = contract.runtime_reads(tmp_path, frozenset({field}))
    assert reads == frozenset()
    assert _audit_as_documented_inert(contract, field, reads).violations == ()


def test_runtime_scan_executes_context_exit_and_generator_continuation(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "context_cleanup.py").write_text(
        """
from contextlib import contextmanager

class Box:
    def __init__(self, section):
        self.section = section

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self.section.stage1_enabled

@contextmanager
def managed(section):
    yield None
    section.stage2_enabled

class PendingBox:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        settings.evaluation.stage3_enabled

@contextmanager
def pending_managed():
    yield None
    settings.evaluation.semantic_model

with Box(settings.evaluation):
    pass

with managed(settings.evaluation):
    pass

Box(settings.evaluation)
managed(settings.evaluation)
PendingBox()
pending_managed()
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name)
        for name in ("stage1_enabled", "stage2_enabled", "stage3_enabled", "semantic_model")
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage2_enabled"),
        }
    )


def test_runtime_scan_invokes_cached_property_getters(contract, tmp_path: Path) -> None:
    (tmp_path / "cached_property_read.py").write_text(
        """
from functools import cached_property

class Reader:
    def __init__(self, section):
        self.section = section

    @cached_property
    def value(self):
        return self.section.satisfaction_threshold

reader = Reader(settings.evaluation)
captured = reader.value
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "satisfaction_threshold")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset({field})


def test_runtime_scan_rejects_shadowed_property_decorators(contract, tmp_path: Path) -> None:
    (tmp_path / "shadowed_property.py").write_text(
        """
def property(fn):
    return lambda self: None

class Reader:
    def __init__(self, section):
        self.section = section

    @property
    def value(self):
        return self.section.stage1_enabled

Reader(config.evaluation).value()
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset()


def test_runtime_scan_resolves_builtin_descriptor_aliases_and_replacements(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "descriptor_identity.py").write_text(
        """
from builtins import property as prop
from builtins import staticmethod as sm

class Reader:
    @sm
    def static(section):
        return section.stage2_enabled

    def __init__(self, section):
        self.section = section

    @prop
    def value(self):
        return self.section.stage3_enabled

Reader.static(config.evaluation)
Reader(config.evaluation).value

staticmethod = lambda fn: lambda *args: None

class Shadow:
    @staticmethod
    def static(section):
        return section.stage1_enabled

Shadow.static(config.evaluation)
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name)
        for name in ("stage1_enabled", "stage2_enabled", "stage3_enabled")
    )

    assert contract.runtime_reads(tmp_path, fields) == fields - {
        contract.ConfigField("evaluation", "stage1_enabled")
    }


def test_runtime_scan_tracks_exact_serialized_mapping_consumers(contract, tmp_path: Path) -> None:
    (tmp_path / "serialized_consumers.py").write_text(
        """
def read(stage3_enabled):
    return stage3_enabled

match settings.evaluation.model_dump():
    case {"stage1_enabled": value}:
        pass

rendered = "{stage2_enabled}".format_map(settings.evaluation.model_dump())
read(**settings.evaluation.model_dump())
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name)
        for name in ("stage1_enabled", "stage2_enabled", "stage3_enabled")
    )

    assert contract.runtime_reads(tmp_path, fields) == fields


def test_runtime_scan_ignores_nonexact_serialized_mapping_consumers(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "nonexact_serialized_consumers.py").write_text(
        """
def read(**values):
    return values

match settings.evaluation.model_dump():
    case {"untracked": value}:
        pass

rendered = "{{stage1_enabled}} {untracked}".format_map(settings.evaluation.model_dump())
read(**settings.evaluation.model_dump())
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset()


def test_runtime_scan_executes_consumed_itertools_callbacks(contract, tmp_path: Path) -> None:
    (tmp_path / "itertools_callbacks.py").write_text(
        """
import itertools

list(itertools.groupby(
    [settings.evaluation],
    key=lambda section: section.stage1_enabled,
))
list(itertools.dropwhile(
    lambda section: section.stage2_enabled,
    [settings.evaluation],
))
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name) for name in ("stage1_enabled", "stage2_enabled")
    )

    assert contract.runtime_reads(tmp_path, fields) == fields


def test_runtime_scan_does_not_execute_unconsumed_or_empty_itertools_callbacks(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "inert_itertools_callbacks.py").write_text(
        """
from itertools import dropwhile, groupby

groupby([settings.evaluation], key=lambda section: section.stage1_enabled)
list(dropwhile(lambda section: section.stage2_enabled, []))
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name) for name in ("stage1_enabled", "stage2_enabled")
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset()


def test_runtime_scan_tracks_itemgetter_and_partialmethod_factories(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "callable_factories.py").write_text(
        """
import functools
import operator

operator.itemgetter("stage1_enabled")(settings.evaluation.model_dump())

class Reader:
    def read(self, section):
        return section.stage2_enabled

    bound = functools.partialmethod(read, settings.evaluation)

Reader().bound()
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name) for name in ("stage1_enabled", "stage2_enabled")
    )

    assert contract.runtime_reads(tmp_path, fields) == fields


def test_runtime_scan_ignores_shadowed_callable_factories(contract, tmp_path: Path) -> None:
    (tmp_path / "shadowed_callable_factories.py").write_text(
        """
from functools import partialmethod
from operator import itemgetter

itemgetter = external_factory
partialmethod = external_descriptor
itemgetter("stage1_enabled")(settings.evaluation.model_dump())

class Reader:
    def read(self, section):
        return section.stage2_enabled
    bound = partialmethod(read, settings.evaluation)

Reader().bound()
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name) for name in ("stage1_enabled", "stage2_enabled")
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset()


def test_runtime_scan_consumes_coroutines_scheduled_by_asyncio_gather(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "asyncio_scheduler.py").write_text(
        """
import asyncio

async def read(config):
    return config.evaluation.stage1_enabled

async def main():
    await asyncio.gather(read(settings))

asyncio.run(main())
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")
    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset({field})


def test_runtime_scan_generator_expression_next_advances_one_item(contract, tmp_path: Path) -> None:
    (tmp_path / "generator_expression_next.py").write_text(
        """
def runtime(settings):
    generated = (
        settings.evaluation.stage1_enabled
        if index == 0
        else settings.evaluation.stage2_enabled
        for index in [0, 1]
    )
    next(generated)
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name) for name in ("stage1_enabled", "stage2_enabled")
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {contract.ConfigField("evaluation", "stage1_enabled")}
    )


@pytest.mark.parametrize("import_form", ("from collections import deque", "import collections"))
def test_runtime_scan_recognizes_collections_deque_as_eager_consumer(
    contract, tmp_path: Path, import_form: str
) -> None:
    call = (
        "deque(generated, maxlen=0)"
        if import_form.startswith("from")
        else ("collections.deque(generated, maxlen=0)")
    )
    (tmp_path / "deque_consumer.py").write_text(
        f"""
{import_form}
generated = (settings.evaluation.stage2_enabled for _ in [1])
{call}
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage2_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset({field})


def test_runtime_scan_evaluates_generator_expression_outer_iterable_eagerly(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "generator_outer_iter.py").write_text(
        "pending = (item for item in settings.evaluation.stage1_enabled)\n",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset({field})


def test_runtime_scan_propagates_generator_expression_into_local_consumer(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "generator_consumer.py").write_text(
        """
def consume(items):
    for _ in items:
        pass

consume(settings.evaluation.stage2_enabled for _ in [1])
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage2_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset({field})


def test_runtime_scan_one_turn_generator_consumers_stop_at_first_yield(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "one_turn.py").write_text(
        """
def reader(config):
    yield config.evaluation.stage1_enabled
    yield config.evaluation.stage2_enabled

async def async_reader(config):
    yield config.evaluation.stage3_enabled
    yield config.evaluation.satisfaction_threshold

next(reader(settings))

async def advance_once():
    await anext(async_reader(settings))

advance_once()
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name)
        for name in (
            "stage1_enabled",
            "stage2_enabled",
            "stage3_enabled",
            "satisfaction_threshold",
        )
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage3_enabled"),
        }
    )


def test_runtime_scan_models_any_and_all_short_circuiting(contract, tmp_path: Path) -> None:
    (tmp_path / "short_circuit.py").write_text(
        """
def any_stops(config):
    yield True
    yield config.evaluation.stage1_enabled

def any_continues(config):
    yield False
    yield config.evaluation.stage2_enabled

def all_stops(config):
    yield False
    yield config.evaluation.stage3_enabled

def all_continues(config):
    yield True
    yield config.evaluation.satisfaction_threshold

any(any_stops(settings))
any(any_continues(settings))
all(all_stops(settings))
all(all_continues(settings))
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name)
        for name in (
            "stage1_enabled",
            "stage2_enabled",
            "stage3_enabled",
            "satisfaction_threshold",
        )
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {
            contract.ConfigField("evaluation", "stage2_enabled"),
            contract.ConfigField("evaluation", "satisfaction_threshold"),
        }
    )


def test_runtime_scan_send_and_asend_advance_exactly_one_turn(contract, tmp_path: Path) -> None:
    (tmp_path / "send_once.py").write_text(
        """
def reader(config):
    yield config.evaluation.stage1_enabled
    yield config.evaluation.stage2_enabled

async def async_reader(config):
    yield config.evaluation.stage3_enabled
    yield config.evaluation.satisfaction_threshold

reader(settings).send(None)

async def advance_once():
    await async_reader(settings).asend(None)

advance_once()
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name)
        for name in (
            "stage1_enabled",
            "stage2_enabled",
            "stage3_enabled",
            "satisfaction_threshold",
        )
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage3_enabled"),
        }
    )


def test_runtime_scan_preserves_generator_continuation_across_partial_consumers(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "continuations.py").write_text(
        """
def next_reader(config):
    yield 0
    yield config.evaluation.stage1_enabled

def send_reader(config):
    yield 0
    yield config.evaluation.stage2_enabled

def any_reader(config):
    yield True
    yield config.evaluation.stage3_enabled

first = next_reader(settings)
next(first)
next(first)

second = send_reader(settings)
second.send(None)
second.send(None)

third = any_reader(settings)
any(third)
next(third)
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name)
        for name in ("stage1_enabled", "stage2_enabled", "stage3_enabled")
    )

    assert contract.runtime_reads(tmp_path, fields) == fields


def test_runtime_scan_preserves_async_generator_continuation(contract, tmp_path: Path) -> None:
    (tmp_path / "async_continuation.py").write_text(
        """
async def reader(config):
    yield 0
    yield config.evaluation.satisfaction_threshold

async def advance():
    stream = reader(settings)
    await stream.asend(None)
    await stream.asend(None)

advance()
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "satisfaction_threshold")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset({field})


def test_runtime_scan_loop_break_consumes_only_first_generator_turn(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "break_once.py").write_text(
        """
def read_after(config):
    yield 0
    yield config.evaluation.stage1_enabled

def read_before(config):
    yield config.evaluation.stage2_enabled
    yield 0

for value in read_after(settings):
    break

for value in read_before(settings):
    break
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name) for name in ("stage1_enabled", "stage2_enabled")
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {contract.ConfigField("evaluation", "stage2_enabled")}
    )


def test_runtime_scan_tracks_consumed_generator_functions_and_closures(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "deferred_callables.py").write_text(
        """
def reader(config):
    yield config.evaluation.stage1_enabled

list(reader(settings))

def make_reader(config):
    section = config.evaluation
    def inner():
        return section.stage2_enabled
    return inner

make_reader(settings)()

def outer_with_nested_generator(config):
    def nested():
        yield config.evaluation.stage1_enabled
    return config.evaluation.stage3_enabled

outer_with_nested_generator(settings)
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name)
        for name in ("stage1_enabled", "stage2_enabled", "stage3_enabled")
    )

    assert contract.runtime_reads(tmp_path, fields) == fields


def test_runtime_scan_resolves_generator_consumers_by_builtin_identity(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "shadowed.py").write_text(
        """
def reader(config):
    yield config.evaluation.stage1_enabled

def list(value):
    return None

list(reader(settings))
""",
        encoding="utf-8",
    )
    (tmp_path / "consumed.py").write_text(
        """
from builtins import list as builtin_list

def reader_two(config):
    yield config.evaluation.stage2_enabled

def reader_three(config):
    yield config.evaluation.stage3_enabled

consume = list
consume(reader_two(settings))
builtin_list(reader_three(settings))

def delegated(config):
    def inner():
        yield config.evaluation.satisfaction_threshold
    yield from inner()

list(delegated(settings))

def unpacked(config):
    yield config.evaluation.uncertainty_threshold

[*unpacked(settings)]
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name)
        for name in (
            "stage1_enabled",
            "stage2_enabled",
            "stage3_enabled",
            "satisfaction_threshold",
            "uncertainty_threshold",
        )
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        fields - {contract.ConfigField("evaluation", "stage1_enabled")}
    )


def test_runtime_scan_defers_lazy_builtin_generator_wrappers(contract, tmp_path: Path) -> None:
    (tmp_path / "lazy.py").write_text(
        """
def reader(config):
    yield config.evaluation.stage1_enabled

pending_iter = iter(reader(settings))
pending_enumerate = enumerate(reader(settings))
pending_filter = filter(None, reader(settings))
pending_map = map(str, reader(settings))
pending_reversed = reversed(reader(settings))
pending_zip = zip(reader(settings))
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset()


def test_runtime_scan_consumes_qualified_and_awaited_builtins(contract, tmp_path: Path) -> None:
    (tmp_path / "qualified.py").write_text(
        """
import builtins

def sync_reader(config):
    yield config.evaluation.semantic_model

builtins.list(sync_reader(settings))

async def async_reader(config):
    yield config.consensus.advocate_model

async def consume_async():
    await anext(async_reader(settings))

consume_async()
""",
        encoding="utf-8",
    )
    fields = frozenset(
        {
            contract.ConfigField("evaluation", "semantic_model"),
            contract.ConfigField("consensus", "advocate_model"),
        }
    )

    assert contract.runtime_reads(tmp_path, fields) == fields


def test_runtime_scan_finds_attribute_alias_and_literal_getattr_reads(
    contract, tmp_path: Path
) -> None:
    source = tmp_path / "runtime.py"
    source.write_text(
        """
section = settings.evaluation
direct = settings.consensus.models
alias = section.stage1_enabled
dynamic = getattr(section, "stage2_enabled")
settings.evaluation.stage3_enabled = False
text = "settings.evaluation.satisfaction_threshold"

def local_alias(config):
    evaluation = config.evaluation
    return evaluation.uncertainty_threshold

def shadow(section):
    return section.satisfaction_threshold

section = object()
ignored_after_rebind = section.semantic_model
""",
        encoding="utf-8",
    )
    fields = frozenset(
        {
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage2_enabled"),
            contract.ConfigField("evaluation", "stage3_enabled"),
            contract.ConfigField("evaluation", "satisfaction_threshold"),
            contract.ConfigField("evaluation", "uncertainty_threshold"),
            contract.ConfigField("evaluation", "semantic_model"),
            contract.ConfigField("consensus", "models"),
        }
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage2_enabled"),
            contract.ConfigField("evaluation", "uncertainty_threshold"),
            contract.ConfigField("consensus", "models"),
        }
    )


def test_runtime_scan_handles_natural_roots_without_unrelated_object_false_positives(
    contract, tmp_path: Path
) -> None:
    source = tmp_path / "natural_roots.py"
    source.write_text(
        """
factory_read = get_config().evaluation.stage1_enabled
indexed_read = configs[key].evaluation.stage2_enabled
method_read = self.config.consensus.models
loaded = get_config()
aliased_root_read = loaded.evaluation.stage3_enabled

class Runtime:
    def read(self):
        return self.get_config().evaluation.semantic_model

unrelated_attribute = report.evaluation.satisfaction_threshold
unrelated_call = build_report().evaluation.satisfaction_threshold
unrelated_subscript = reports[key].evaluation.uncertainty_threshold
unrelated_factory_method = report.get_config().consensus.threshold
""",
        encoding="utf-8",
    )
    fields = frozenset(
        {
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage2_enabled"),
            contract.ConfigField("evaluation", "stage3_enabled"),
            contract.ConfigField("evaluation", "satisfaction_threshold"),
            contract.ConfigField("evaluation", "semantic_model"),
            contract.ConfigField("evaluation", "uncertainty_threshold"),
            contract.ConfigField("consensus", "models"),
            contract.ConfigField("consensus", "threshold"),
        }
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage2_enabled"),
            contract.ConfigField("evaluation", "stage3_enabled"),
            contract.ConfigField("evaluation", "semantic_model"),
            contract.ConfigField("consensus", "models"),
        }
    )


def test_runtime_scan_joins_conditional_alias_fallbacks_but_honors_reassignment(
    contract, tmp_path: Path
) -> None:
    source = tmp_path / "conditional_aliases.py"
    source.write_text(
        """
def branch_fallback(config, override, enabled):
    section = config.evaluation
    if enabled:
        section = override
    return section.stage1_enabled

def expression_fallback(config, override, enabled):
    section = override if enabled else config.evaluation
    alternate = override or config.evaluation
    return section.stage2_enabled, alternate.stage3_enabled

def unconditional_reassignment(config):
    section = config.evaluation
    section = object()
    return section.satisfaction_threshold
""",
        encoding="utf-8",
    )
    fields = frozenset(
        {
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage2_enabled"),
            contract.ConfigField("evaluation", "stage3_enabled"),
            contract.ConfigField("evaluation", "satisfaction_threshold"),
        }
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage2_enabled"),
            contract.ConfigField("evaluation", "stage3_enabled"),
        }
    )


def test_runtime_scan_preserves_zero_iteration_and_loop_body_provenance(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "loops.py").write_text(
        """
def loop_paths(config, report, enabled):
    section = config.evaluation
    for section in []:
        pass
    zero_iteration_read = section.stage1_enabled

    section = report.evaluation
    while enabled:
        section = config.evaluation
    possible_iteration_read = section.stage2_enabled

    for candidate, ignored in (
        (report.evaluation, 0),
        (config.evaluation, 1),
    ):
        loop_destructured_read = candidate.stage3_enabled

    unrelated = report.config.evaluation.satisfaction_threshold
""",
        encoding="utf-8",
    )
    fields = frozenset(
        {
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage2_enabled"),
            contract.ConfigField("evaluation", "stage3_enabled"),
            contract.ConfigField("evaluation", "satisfaction_threshold"),
        }
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage2_enabled"),
            contract.ConfigField("evaluation", "stage3_enabled"),
        }
    )


def test_runtime_scan_joins_successful_try_handler_and_finally_paths(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "try_paths.py").write_text(
        """
def try_paths(config, report, risky):
    successful = config.evaluation
    try:
        risky()
    except ValueError:
        successful = report.evaluation
    successful_path_read = successful.stage1_enabled

    handler_source = report.evaluation
    try:
        handler_source = config.evaluation
        risky()
        handler_source = report.evaluation
    except RuntimeError:
        handler_read = handler_source.stage2_enabled

    final_source = report.consensus
    try:
        final_source = config.consensus
        risky()
    except LookupError:
        final_source = report.consensus
    finally:
        final_read = final_source.models

    unrelated = report.settings.consensus.threshold
""",
        encoding="utf-8",
    )
    fields = frozenset(
        {
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage2_enabled"),
            contract.ConfigField("consensus", "models"),
            contract.ConfigField("consensus", "threshold"),
        }
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage2_enabled"),
            contract.ConfigField("consensus", "models"),
        }
    )


def test_runtime_scan_joins_match_branches_and_destructured_aliases(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "match_and_destructure.py").write_text(
        """
def match_paths(config, report, selector):
    evaluation_alias, other = config.evaluation, report.evaluation
    assigned_evaluation_read = evaluation_alias.uncertainty_threshold

    [list_alias] = [config.evaluation]
    assigned_list_read = list_alias.satisfaction_threshold

    consensus_alias, ignored_alias = config.consensus, report.evaluation
    assigned_consensus_read = consensus_alias.devil_model

    section = report.evaluation
    match selector:
        case 0:
            section = config.evaluation
        case _:
            section = report.evaluation
    branch_read = section.stage1_enabled

    match (config.evaluation, (config.consensus, report.evaluation)):
        case (evaluation, (consensus, ignored)):
            destructured_evaluation_read = evaluation.stage2_enabled
            destructured_consensus_read = consensus.models

    unrelated = report.config.consensus.threshold
""",
        encoding="utf-8",
    )
    fields = frozenset(
        {
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage2_enabled"),
            contract.ConfigField("evaluation", "satisfaction_threshold"),
            contract.ConfigField("evaluation", "uncertainty_threshold"),
            contract.ConfigField("consensus", "devil_model"),
            contract.ConfigField("consensus", "models"),
            contract.ConfigField("consensus", "threshold"),
        }
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage2_enabled"),
            contract.ConfigField("evaluation", "satisfaction_threshold"),
            contract.ConfigField("evaluation", "uncertainty_threshold"),
            contract.ConfigField("consensus", "devil_model"),
            contract.ConfigField("consensus", "models"),
        }
    )


def test_runtime_scan_preserves_mapping_class_and_rest_pattern_provenance(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "structural_patterns.py").write_text(
        """
def mapping_pattern(config, report):
    match {"section": config.evaluation}:
        case {"section": direct_section}:
            direct_mapping_read = direct_section.stage1_enabled

    mapping_source = {"section": config.evaluation}
    match {**mapping_source}:
        case {"section": expanded_section}:
            expanded_mapping_read = expanded_section.assertion_extraction_model

    payload = {
        "section": config.evaluation,
        "remaining": config.consensus,
        "unrelated": report.evaluation,
    }
    match payload:
        case {"section": section, "unrelated": unrelated, **rest}:
            mapping_read = section.stage2_enabled
            rest_read = rest["remaining"].models
            unrelated_read = unrelated.satisfaction_threshold

class Envelope:
    __match_args__ = ("positional",)

def class_pattern(config, report):
    match Envelope(section=config.evaluation):
        case Envelope(section=direct_section):
            direct_keyword_read = direct_section.stage3_enabled

    keyword_source = {"section": config.consensus}
    match Envelope(**keyword_source):
        case Envelope(section=expanded_section):
            expanded_keyword_read = expanded_section.judge_model

    payload = Envelope(
        config.consensus,
        section=config.evaluation,
        unrelated=report.consensus,
    )
    match payload:
        case Envelope(positional, section=section, unrelated=unrelated):
            positional_read = positional.devil_model
            keyword_read = section.uncertainty_threshold
            unrelated_read = unrelated.threshold
""",
        encoding="utf-8",
    )
    fields = frozenset(
        {
            contract.ConfigField("evaluation", "assertion_extraction_model"),
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage2_enabled"),
            contract.ConfigField("evaluation", "stage3_enabled"),
            contract.ConfigField("evaluation", "satisfaction_threshold"),
            contract.ConfigField("evaluation", "uncertainty_threshold"),
            contract.ConfigField("consensus", "devil_model"),
            contract.ConfigField("consensus", "judge_model"),
            contract.ConfigField("consensus", "models"),
            contract.ConfigField("consensus", "threshold"),
        }
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {
            contract.ConfigField("evaluation", "assertion_extraction_model"),
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage2_enabled"),
            contract.ConfigField("evaluation", "stage3_enabled"),
            contract.ConfigField("evaluation", "uncertainty_threshold"),
            contract.ConfigField("consensus", "devil_model"),
            contract.ConfigField("consensus", "judge_model"),
            contract.ConfigField("consensus", "models"),
        }
    )


def test_runtime_scan_tracks_indirect_loop_containers_and_sequence_star_patterns(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "indirect_containers.py").write_text(
        """
def indirect_loop(config, report):
    sections = [config.evaluation]
    aliases = sections
    for section in aliases:
        loop_read = section.stage1_enabled

    pairs = [(config.evaluation, report.evaluation)]
    for section, unrelated in pairs:
        destructured_loop_read = section.stage2_enabled
        unrelated_read = unrelated.satisfaction_threshold

    match [config.consensus, report.consensus]:
        case [consensus, *rest]:
            head_read = consensus.models
            unrelated_star_read = rest[0].threshold

async def indirect_async_loop(config):
    sections = [config.consensus]
    async for section in sections:
        async_loop_read = section.devil_model
""",
        encoding="utf-8",
    )
    fields = frozenset(
        {
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage2_enabled"),
            contract.ConfigField("evaluation", "satisfaction_threshold"),
            contract.ConfigField("consensus", "devil_model"),
            contract.ConfigField("consensus", "models"),
            contract.ConfigField("consensus", "threshold"),
        }
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage2_enabled"),
            contract.ConfigField("consensus", "devil_model"),
            contract.ConfigField("consensus", "models"),
        }
    )


def test_runtime_scan_models_standard_dict_consumers_key_sensitively(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "dict_consumers.py").write_text(
        """
def dict_consumers(config, report, dynamic_key):
    sections = {
        "primary": config.evaluation,
        "unrelated": report.evaluation,
    }
    for section in sections.values():
        values_read = section.stage1_enabled
    for _, section in sections.items():
        items_read = section.stage2_enabled

    primary_read = sections.get("primary").stage3_enabled
    primary_with_default = sections.get(
        "primary", report.evaluation
    ).uncertainty_threshold
    dynamic_read = sections.get(dynamic_key, report.evaluation).assertion_extraction_model
    missing_read = sections.get("missing").satisfaction_threshold
    missing_default = sections.get("missing", config.consensus).models

    for key in sections.keys():
        non_config_key_read = key.satisfaction_threshold

    reports = {"primary": report.evaluation}
    report_get = reports.get("primary").satisfaction_threshold
    report_dynamic = reports.get(dynamic_key, report.evaluation).satisfaction_threshold
    for report_section in reports.values():
        report_value_read = report_section.satisfaction_threshold

    literals = {"primary": "not config"}
    literal_read = literals.get("primary").satisfaction_threshold

    copied = sections.copy()
    copied_read = copied.get("primary").semantic_model
    constructed = dict(sections)
    constructed_read = constructed["primary"].stage1_enabled

    unioned = reports | {"primary": config.consensus}
    union_read = unioned.get("primary").devil_model
    overridden = sections | {
        "primary": report.evaluation,
        "unrelated": report.evaluation,
    }
    overridden_read = overridden.get("primary").satisfaction_threshold

    defaults = {}
    defaults.setdefault("primary", config.consensus)
    inserted_read = defaults.get("primary").judge_model
""",
        encoding="utf-8",
    )
    fields = frozenset(
        {
            contract.ConfigField("evaluation", "assertion_extraction_model"),
            contract.ConfigField("evaluation", "satisfaction_threshold"),
            contract.ConfigField("evaluation", "semantic_model"),
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage2_enabled"),
            contract.ConfigField("evaluation", "stage3_enabled"),
            contract.ConfigField("evaluation", "uncertainty_threshold"),
            contract.ConfigField("consensus", "devil_model"),
            contract.ConfigField("consensus", "judge_model"),
            contract.ConfigField("consensus", "models"),
        }
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {
            contract.ConfigField("evaluation", "assertion_extraction_model"),
            contract.ConfigField("evaluation", "semantic_model"),
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage2_enabled"),
            contract.ConfigField("evaluation", "stage3_enabled"),
            contract.ConfigField("evaluation", "uncertainty_threshold"),
            contract.ConfigField("consensus", "devil_model"),
            contract.ConfigField("consensus", "judge_model"),
            contract.ConfigField("consensus", "models"),
        }
    )


def test_runtime_scan_propagates_mutable_mapping_identity_across_aliases(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "mutable_mapping_aliases.py").write_text(
        """
def dynamic_setdefault(config, key):
    sections = {}
    sections.setdefault(key, config.evaluation)
    for section in sections.values():
        dynamic_read = section.stage1_enabled

def aliased_setdefault(config):
    sections = {}
    alias = sections
    alias.setdefault("x", config.evaluation)
    alias_read = sections["x"].stage2_enabled

def augmented_union(config):
    sections = {}
    alias = sections
    sections |= {"x": config.evaluation}
    augmented_read = alias["x"].stage3_enabled

def constructors(config):
    source = {"x": config.evaluation}
    expanded = dict(**source)
    expanded_read = expanded["x"].assertion_extraction_model
    pairs = dict([("x", config.evaluation)])
    pairs_read = pairs["x"].uncertainty_threshold
    keywords = dict(x=config.evaluation)
    keyword_read = keywords["x"].semantic_model

def direct_mutations(config):
    sections = {}
    alias = sections
    alias["x"] = config.evaluation
    assigned_read = sections["x"].stage1_enabled

    consensus = {}
    consensus_alias = consensus
    consensus_alias.update({"x": config.consensus})
    updated_read = consensus["x"].models

    pair_updates = {}
    pair_alias = pair_updates
    pair_alias.update([("x", config.consensus)])
    pair_update_read = pair_updates["x"].devil_model

def conditional_alias(config, enabled):
    left = {}
    right = {}
    alias = left if enabled else right
    alias.setdefault("x", config.consensus)
    possible_left_read = left.get("x").advocate_model

def dynamic_assignment(config, key):
    sections = {}
    alias = sections
    alias[key] = config.evaluation
    for section in sections.values():
        dynamic_assignment_read = section.stage3_enabled

def provenance_kills(config):
    cleared = {"x": config.evaluation}
    cleared_alias = cleared
    cleared_alias.clear()
    cleared_read = cleared.get("x").satisfaction_threshold

    popped = {"x": config.evaluation}
    popped.pop("x")
    popped_read = popped.get("x").satisfaction_threshold

def unrelated_mutations(report, key):
    sections = {}
    alias = sections
    alias.setdefault(key, report.evaluation)
    alias["x"] = report.evaluation
    alias |= {"y": report.evaluation}
    alias.update({"z": report.evaluation})
    for section in sections.values():
        unrelated_read = section.satisfaction_threshold

    source = {"x": report.evaluation}
    expanded = dict(**source)
    pairs = dict([("x", report.evaluation)])
    expanded_read = expanded["x"].satisfaction_threshold
    pairs_read = pairs["x"].satisfaction_threshold
""",
        encoding="utf-8",
    )
    fields = frozenset(
        {
            contract.ConfigField("evaluation", "assertion_extraction_model"),
            contract.ConfigField("evaluation", "satisfaction_threshold"),
            contract.ConfigField("evaluation", "semantic_model"),
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage2_enabled"),
            contract.ConfigField("evaluation", "stage3_enabled"),
            contract.ConfigField("evaluation", "uncertainty_threshold"),
            contract.ConfigField("consensus", "models"),
            contract.ConfigField("consensus", "advocate_model"),
            contract.ConfigField("consensus", "devil_model"),
        }
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {
            contract.ConfigField("evaluation", "assertion_extraction_model"),
            contract.ConfigField("evaluation", "semantic_model"),
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage2_enabled"),
            contract.ConfigField("evaluation", "stage3_enabled"),
            contract.ConfigField("evaluation", "uncertainty_threshold"),
            contract.ConfigField("consensus", "models"),
            contract.ConfigField("consensus", "advocate_model"),
            contract.ConfigField("consensus", "devil_model"),
        }
    )


def test_runtime_scan_models_dynamic_key_collisions_in_evaluation_order(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "dynamic_key_collisions.py").write_text(
        """
def dynamic_key_collisions(config, report, key):
    preserved = {"known": report.evaluation}
    preserved.setdefault(key, config.evaluation)
    preserved_known_read = preserved["known"].satisfaction_threshold
    for section in preserved.values():
        inserted_default_read = section.stage1_enabled

    later_dynamic = {
        "known": report.evaluation,
        key: config.evaluation,
    }
    later_dynamic_read = later_dynamic["known"].stage2_enabled

    later_literal = {
        key: config.evaluation,
        "known": report.evaluation,
    }
    later_literal_read = later_literal["known"].satisfaction_threshold

    updated = {"known": report.evaluation}
    updated.update({key: config.evaluation})
    updated_read = updated["known"].stage3_enabled

    unioned = {"known": report.evaluation} | {key: config.evaluation}
    unioned_read = unioned["known"].uncertainty_threshold

    assigned = {"known": report.evaluation}
    assigned[key] = config.evaluation
    assigned_read = assigned["known"].assertion_extraction_model

    missing = {"other": config.evaluation}
    unreachable_missing_read = missing["known"].semantic_model
""",
        encoding="utf-8",
    )
    fields = frozenset(
        {
            contract.ConfigField("evaluation", "assertion_extraction_model"),
            contract.ConfigField("evaluation", "satisfaction_threshold"),
            contract.ConfigField("evaluation", "semantic_model"),
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage2_enabled"),
            contract.ConfigField("evaluation", "stage3_enabled"),
            contract.ConfigField("evaluation", "uncertainty_threshold"),
        }
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {
            contract.ConfigField("evaluation", "assertion_extraction_model"),
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage2_enabled"),
            contract.ConfigField("evaluation", "stage3_enabled"),
            contract.ConfigField("evaluation", "uncertainty_threshold"),
        }
    )


def test_runtime_scan_joins_dynamic_mapping_keys_and_missing_defaults(
    contract, tmp_path: Path
) -> None:
    field = contract.ConfigField("evaluation", "stage1_enabled")
    fields = frozenset({field})

    positive = tmp_path / "positive"
    positive.mkdir()
    (positive / "conditional_mapping.py").write_text(
        """
def conditional_dynamic(config, report, key, enabled):
    sections = {key: config.evaluation} if enabled else {"x": report.evaluation}
    return sections["x"].stage1_enabled

def conditional_default(config, report, enabled):
    sections = {} if enabled else {"x": report.evaluation}
    return sections.get("x", config.evaluation).stage1_enabled
""",
        encoding="utf-8",
    )
    assert contract.runtime_reads(positive, fields) == fields

    negative = tmp_path / "negative"
    negative.mkdir()
    (negative / "missing_or_unrelated.py").write_text(
        """
def conditional_unrelated(report, enabled):
    sections = {} if enabled else {"x": report.evaluation}
    return sections["x"].stage1_enabled
""",
        encoding="utf-8",
    )
    assert contract.runtime_reads(negative, fields) == frozenset()


@pytest.mark.parametrize(
    ("mutation", "receiver_read"),
    [
        (
            'receiver["known"] = report.evaluation',
            'receiver["known"].satisfaction_threshold',
        ),
        (
            'receiver.update({"known": report.evaluation})',
            'receiver["known"].satisfaction_threshold',
        ),
        (
            'receiver |= {"known": report.evaluation}',
            'receiver["known"].satisfaction_threshold',
        ),
        (
            "receiver.clear()",
            'receiver.get("known", report.evaluation).satisfaction_threshold',
        ),
        (
            'receiver.pop("known")',
            'receiver.get("known", report.evaluation).satisfaction_threshold',
        ),
    ],
    ids=["assignment", "update", "union", "clear", "pop"],
)
def test_runtime_scan_strongly_mutates_conditional_receiver_but_not_possible_aliases(
    contract,
    tmp_path: Path,
    mutation: str,
    receiver_read: str,
) -> None:
    (tmp_path / "conditional_receiver.py").write_text(
        f"""
def conditional_receiver(config, report, enabled):
    left = {{"known": config.evaluation}}
    right = {{"known": report.evaluation}}
    receiver = left if enabled else right
    {mutation}
    impossible_receiver_read = {receiver_read}
    possible_alias_read = left.get("known", report.evaluation).stage1_enabled
""",
        encoding="utf-8",
    )
    positive_alias = contract.ConfigField("evaluation", "stage1_enabled")
    false_positive = contract.ConfigField("evaluation", "satisfaction_threshold")

    assert contract.runtime_reads(
        tmp_path,
        frozenset({positive_alias, false_positive}),
    ) == frozenset({positive_alias})


def test_runtime_scan_keeps_provenance_when_mutating_a_may_alias(contract, tmp_path: Path) -> None:
    (tmp_path / "may_alias_mutation.py").write_text(
        """
def may_alias_clear(config, report, enabled):
    left = {"x": config.evaluation}
    right = {"x": report.evaluation}
    alias = left if enabled else right
    alias.clear()
    possible_uncleared_read = left.get("x").stage1_enabled

def independent_report_maps(report):
    left = {"x": report.evaluation}
    right = {"x": report.evaluation}
    alias = left
    alias.clear()
    unrelated_read = right.get("x").satisfaction_threshold

def popped_value(config):
    values = {"x": config.evaluation}
    popped = values.pop("x")
    popped_read = popped.stage2_enabled
    removed_read = values.get("x").satisfaction_threshold
""",
        encoding="utf-8",
    )
    fields = frozenset(
        {
            contract.ConfigField("evaluation", "satisfaction_threshold"),
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage2_enabled"),
        }
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage2_enabled"),
        }
    )


def test_runtime_scan_comprehension_targets_shadow_outer_aliases_in_evaluation_order(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "comprehensions.py").write_text(
        """
def comprehension_shadowing(config, reports):
    section = config.evaluation
    ignored_list = [section.stage1_enabled for section in reports]
    ignored_set = {section.stage2_enabled for section in reports}
    ignored_dict = {section.stage3_enabled: section for section in reports}
    ignored_generator = (section.satisfaction_threshold for section in reports)
    outer_read = section.semantic_model

    sections = [config.evaluation]
    positive_list = [item.uncertainty_threshold for item in sections]
    nested = [
        item.assertion_extraction_model
        for group in [[config.evaluation]]
        for item in group
    ]

async def async_comprehension_shadowing(config, reports):
    section = config.consensus
    ignored = [section.threshold async for section in reports]
    outer_read = section.models
""",
        encoding="utf-8",
    )
    fields = frozenset(
        {
            contract.ConfigField("evaluation", "assertion_extraction_model"),
            contract.ConfigField("evaluation", "satisfaction_threshold"),
            contract.ConfigField("evaluation", "semantic_model"),
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage2_enabled"),
            contract.ConfigField("evaluation", "stage3_enabled"),
            contract.ConfigField("evaluation", "uncertainty_threshold"),
            contract.ConfigField("consensus", "models"),
            contract.ConfigField("consensus", "threshold"),
        }
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {
            contract.ConfigField("evaluation", "assertion_extraction_model"),
            contract.ConfigField("evaluation", "semantic_model"),
            contract.ConfigField("evaluation", "uncertainty_threshold"),
            contract.ConfigField("consensus", "models"),
        }
    )


def test_runtime_scan_import_and_context_manager_bindings_override_config_inference(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "binding_overrides.py").write_text(
        """
def imported_names():
    import report as config
    from report import settings
    imported_config = config.evaluation.stage1_enabled
    imported_settings = settings.consensus.models

def context_names(manager):
    with manager as config:
        context_config = config.evaluation.stage2_enabled
    after_context = config.evaluation.stage3_enabled

async def async_context_names(manager):
    async with manager as settings:
        context_settings = settings.consensus.threshold
""",
        encoding="utf-8",
    )
    fields = frozenset(
        {
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage2_enabled"),
            contract.ConfigField("evaluation", "stage3_enabled"),
            contract.ConfigField("consensus", "models"),
            contract.ConfigField("consensus", "threshold"),
        }
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset()


def test_runtime_scan_excludes_static_dead_and_type_only_branches_without_pruning_dynamic(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "reachable_branches.py").write_text(
        """
from typing import TYPE_CHECKING as CHECKING
import typing as typing_alias

if CHECKING:
    type_only_evaluation = config.evaluation.stage1_enabled
if typing_alias.TYPE_CHECKING:
    type_only_consensus = config.consensus.min_models
if False:
    dead_literal = config.evaluation.stage2_enabled
if True:
    live_literal = config.evaluation.stage3_enabled
else:
    dead_else = config.consensus.threshold
if runtime_flag:
    dynamic_body = config.evaluation.uncertainty_threshold
else:
    dynamic_else = config.consensus.models
maybe_type_checking = CHECKING if runtime_flag else another_runtime_flag
if maybe_type_checking:
    dynamic_type_checking_collision = config.consensus.diversity_required

dead_short_circuit = False and config.evaluation.satisfaction_threshold
live_short_circuit = False or config.consensus.advocate_model
dead_expression = (
    config.consensus.devil_model if False else config.evaluation.semantic_model
)
while False:
    dead_loop = config.consensus.judge_model
""",
        encoding="utf-8",
    )
    fields = frozenset(
        {
            contract.ConfigField("evaluation", "satisfaction_threshold"),
            contract.ConfigField("evaluation", "semantic_model"),
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage2_enabled"),
            contract.ConfigField("evaluation", "stage3_enabled"),
            contract.ConfigField("evaluation", "uncertainty_threshold"),
            contract.ConfigField("consensus", "advocate_model"),
            contract.ConfigField("consensus", "devil_model"),
            contract.ConfigField("consensus", "diversity_required"),
            contract.ConfigField("consensus", "judge_model"),
            contract.ConfigField("consensus", "min_models"),
            contract.ConfigField("consensus", "models"),
            contract.ConfigField("consensus", "threshold"),
        }
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {
            contract.ConfigField("evaluation", "semantic_model"),
            contract.ConfigField("evaluation", "stage3_enabled"),
            contract.ConfigField("evaluation", "uncertainty_threshold"),
            contract.ConfigField("consensus", "advocate_model"),
            contract.ConfigField("consensus", "diversity_required"),
            contract.ConfigField("consensus", "models"),
        }
    )


def test_runtime_scan_tracks_section_annotations_and_local_call_arguments_without_collisions(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "section_helpers.py").write_text(
        """
from typing import TYPE_CHECKING

from ouroboros.config.models import ConsensusConfig, EvaluationConfig
from ouroboros.evaluation.consensus import ConsensusConfig as RuntimeConsensusConfig

EvaluationAlias = EvaluationConfig

if TYPE_CHECKING:
    from ouroboros.config.models import EvaluationConfig as EvaluationSection

def typed_evaluation(section: EvaluationConfig):
    return section.stage1_enabled

def typed_consensus(section: ConsensusConfig | None):
    return section.models

def type_only_alias(section: EvaluationSection):
    return section.stage2_enabled

def assigned_alias(section: EvaluationAlias):
    return section.assertion_extraction_model

def untyped_helper(section):
    return section.stage3_enabled

def identity(section):
    return section

def caller(config):
    untyped_helper(config.evaluation)
    untyped_helper(**{"section": config.evaluation})
    return identity(config.consensus).judge_model

def colliding_runtime_type(section: RuntimeConsensusConfig):
    return section.min_models

def arbitrary_config_annotation(section: ProjectConfig):
    return section.satisfaction_threshold

def wrapped_section_is_not_the_section(sections: list[EvaluationConfig]):
    return sections.uncertainty_threshold
""",
        encoding="utf-8",
    )
    fields = frozenset(
        {
            contract.ConfigField("evaluation", "satisfaction_threshold"),
            contract.ConfigField("evaluation", "assertion_extraction_model"),
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage2_enabled"),
            contract.ConfigField("evaluation", "stage3_enabled"),
            contract.ConfigField("evaluation", "uncertainty_threshold"),
            contract.ConfigField("consensus", "judge_model"),
            contract.ConfigField("consensus", "min_models"),
            contract.ConfigField("consensus", "models"),
        }
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {
            contract.ConfigField("evaluation", "assertion_extraction_model"),
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage2_enabled"),
            contract.ConfigField("evaluation", "stage3_enabled"),
            contract.ConfigField("consensus", "judge_model"),
            contract.ConfigField("consensus", "models"),
        }
    )


def test_runtime_scan_keeps_local_call_cache_context_and_section_defaults(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "call_context.py").write_text(
        """
def identity(value):
    return {"value": value}.get("value")

cache_context_read = identity(config.evaluation).stage1_enabled

def default_identity(value=config.evaluation):
    return value

default_read = default_identity().stage2_enabled
""",
        encoding="utf-8",
    )
    fields = frozenset(
        {
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage2_enabled"),
        }
    )

    assert contract.runtime_reads(tmp_path, fields) == fields


def test_runtime_scan_tracks_local_function_aliases_and_unknown_leading_star_args_but_rebinding(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "call_bindings.py").write_text(
        """
def aliased_reader(value):
    return value.judge_model

reader = aliased_reader
aliased_reader = external
alias_read = reader(config.consensus)

def second_reader(prefix, value):
    return value.models

starred_read = second_reader(*unknown_args, config.consensus)

def rebound_reader(value):
    return value.min_models

rebound_reader = external
rebound_reader(config.consensus)

def dynamic_reader(value):
    return value.devil_model

maybe_reader = external
if runtime_flag:
    maybe_reader = dynamic_reader
maybe_reader(config.consensus)

def statically_dead_reader(value):
    return value.advocate_model

dead_reader = external
if False:
    dead_reader = statically_dead_reader
dead_reader(config.consensus)
""",
        encoding="utf-8",
    )
    fields = frozenset(
        {
            contract.ConfigField("consensus", "judge_model"),
            contract.ConfigField("consensus", "advocate_model"),
            contract.ConfigField("consensus", "devil_model"),
            contract.ConfigField("consensus", "min_models"),
            contract.ConfigField("consensus", "models"),
        }
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {
            contract.ConfigField("consensus", "devil_model"),
            contract.ConfigField("consensus", "judge_model"),
            contract.ConfigField("consensus", "models"),
        }
    )


def test_runtime_scan_propagates_method_and_imported_helper_arguments(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "helpers.py").write_text(
        """
def read_stage(section):
    return section.stage2_enabled

def read_semantic(section):
    return section.semantic_model

def unused_helper(section):
    return section.uncertainty_threshold

def read_threshold(section):
    return section.satisfaction_threshold
""",
        encoding="utf-8",
    )
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "helpers.py").write_text(
        """
def read_stage(section):
    return section.uncertainty_threshold
""",
        encoding="utf-8",
    )
    (tmp_path / "callers.py").write_text(
        """
from helpers import read_stage, read_threshold
import helpers

class Reader:
    def read(self, section):
        return section.stage1_enabled

class BoundReader:
    def read(self, section):
        return section.stage3_enabled

class UnusedReader:
    def read(self, section):
        return section.satisfaction_threshold

class ReboundReader:
    def read(self, section):
        return section.uncertainty_threshold

direct_method = Reader().read(config.evaluation)
reader = BoundReader()
bound_method = reader.read(config.evaluation)
imported_helper = read_stage(config.evaluation)
module_helper = helpers.read_semantic(config.evaluation)
unknown_receiver = report.read(config.evaluation)
unrelated_argument = read_threshold(report.evaluation)
rebound = ReboundReader()
rebound = report
rebound_method = rebound.read(config.evaluation)
""",
        encoding="utf-8",
    )
    fields = frozenset(
        {
            contract.ConfigField("evaluation", "satisfaction_threshold"),
            contract.ConfigField("evaluation", "semantic_model"),
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage2_enabled"),
            contract.ConfigField("evaluation", "stage3_enabled"),
            contract.ConfigField("evaluation", "uncertainty_threshold"),
        }
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {
            contract.ConfigField("evaluation", "semantic_model"),
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage2_enabled"),
            contract.ConfigField("evaluation", "stage3_enabled"),
        }
    )


def test_runtime_scan_defers_lambda_bodies_until_tracked_invocation(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "lambda_calls.py").write_text(
        """
lambda section: section.stage1_enabled
unused = lambda section: section.stage2_enabled

called = lambda section: section.stage3_enabled
called(config.evaluation)

immediate = (lambda section: section.semantic_model)(config.evaluation)
default_is_eager = lambda value=config.evaluation.uncertainty_threshold: value

aliased = lambda section: section.satisfaction_threshold
alias = aliased
alias(config.evaluation)

left = lambda section: section.stage1_enabled
right = lambda section: section.satisfaction_threshold
selected = left
if runtime_condition:
    selected = right
selected(config.evaluation)
""",
        encoding="utf-8",
    )
    fields = frozenset(
        {
            contract.ConfigField("evaluation", "semantic_model"),
            contract.ConfigField("evaluation", "satisfaction_threshold"),
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage2_enabled"),
            contract.ConfigField("evaluation", "stage3_enabled"),
            contract.ConfigField("evaluation", "uncertainty_threshold"),
        }
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {
            contract.ConfigField("evaluation", "semantic_model"),
            contract.ConfigField("evaluation", "satisfaction_threshold"),
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage3_enabled"),
            contract.ConfigField("evaluation", "uncertainty_threshold"),
        }
    )


def test_runtime_scan_preserves_callable_provenance_across_runtime_boundaries(
    contract, tmp_path: Path
) -> None:
    package = tmp_path / "readers"
    package.mkdir()
    (package / "__init__.py").write_text(
        "from .public import read_semantic\n",
        encoding="utf-8",
    )
    (package / "public.py").write_text(
        "from .helpers import read_semantic\n",
        encoding="utf-8",
    )
    (package / "helpers.py").write_text(
        """
def read_semantic(section):
    return section.semantic_model
""",
        encoding="utf-8",
    )
    (tmp_path / "base_reader.py").write_text(
        """
class BaseReader:
    def read(self, section):
        return section.satisfaction_threshold
""",
        encoding="utf-8",
    )
    (tmp_path / "callable_boundaries.py").write_text(
        """
from base_reader import BaseReader
from readers import read_semantic

class Reader:
    def read(self, section):
        return section.stage1_enabled

class InheritedReader(BaseReader):
    pass

class Holder:
    pass

reader = Reader()
callback = reader.read
reader = report
callback(config.evaluation)

callbacks = {"stage": lambda section: section.stage2_enabled}
callbacks["stage"](config.evaluation)
listed_callbacks = [lambda section: section.stage2_enabled]
listed_callbacks[0](config.evaluation)
holder = Holder(callback=lambda section: section.stage2_enabled)
holder.callback(config.evaluation)

def invoke(callback, section):
    return callback(section)

invoke(lambda section: section.stage3_enabled, config.evaluation)

def Choose(callback):
    return callback

chosen = Choose(lambda section: section.stage3_enabled)
chosen(config.evaluation)
read_semantic(config.evaluation)
InheritedReader().read(config.evaluation)

def invoke_without_arguments(callback):
    return callback()

invoke_without_arguments(lambda: config.evaluation.uncertainty_threshold)
""",
        encoding="utf-8",
    )
    fields = frozenset(
        {
            contract.ConfigField("evaluation", "satisfaction_threshold"),
            contract.ConfigField("evaluation", "semantic_model"),
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage2_enabled"),
            contract.ConfigField("evaluation", "stage3_enabled"),
            contract.ConfigField("evaluation", "uncertainty_threshold"),
        }
    )

    assert contract.runtime_reads(tmp_path, fields) == fields


def test_runtime_scan_rejects_callable_binding_overreach(contract, tmp_path: Path) -> None:
    package = tmp_path / "overwritten"
    package.mkdir()
    (package / "__init__.py").write_text(
        """
from .helpers import read_judge
read_judge = external
""",
        encoding="utf-8",
    )
    (package / "helpers.py").write_text(
        """
def read_judge(section):
    return section.judge_model
""",
        encoding="utf-8",
    )
    (tmp_path / "callable_negatives.py").write_text(
        """
from overwritten import read_judge

class Reader:
    def read(self, section):
        return section.models

reader = Reader()
reader = report
unknown_method = reader.read
unknown_method(config.consensus)

callbacks = {
    "unused": lambda section: section.min_models,
    "selected": external,
}
callbacks["selected"](config.consensus)
callbacks["unused"] = external
callbacks["unused"](config.consensus)
callbacks.get("missing", external)(config.consensus)

handlers = [lambda section: section.threshold]
alias = handlers
alias[0] = external
handlers[0](config.consensus)

class Holder:
    pass

holder = Holder(callback=lambda section: section.models)
holder.callback = external
holder.callback(config.consensus)

def invoke(callback, section):
    return callback(section)

invoke(lambda section: section.diversity_required, report.consensus)
read_judge(config.consensus)

class BaseReader:
    def read(self, section):
        return section.advocate_model

class OverrideReader(BaseReader):
    def read(self, section):
        return None

OverrideReader().read(config.consensus)

class FirstReader:
    def read(self, section):
        return None

class SecondReader:
    def read(self, section):
        return section.devil_model

class OrderedReader(FirstReader, SecondReader):
    pass

OrderedReader().read(config.consensus)

class ExternalReader(external.BaseReader):
    pass

class CoincidentalReader:
    def read(self, section):
        return section.models

ExternalReader().read(config.consensus)
""",
        encoding="utf-8",
    )
    fields = frozenset(
        {
            contract.ConfigField("consensus", "advocate_model"),
            contract.ConfigField("consensus", "devil_model"),
            contract.ConfigField("consensus", "diversity_required"),
            contract.ConfigField("consensus", "judge_model"),
            contract.ConfigField("consensus", "min_models"),
            contract.ConfigField("consensus", "models"),
            contract.ConfigField("consensus", "threshold"),
        }
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset()


def test_runtime_scan_resolves_final_class_method_binding(contract, tmp_path: Path) -> None:
    (tmp_path / "overwritten_method.py").write_text(
        """
class Reader:
    def read(self, section):
        return section.stage1_enabled

    read = lambda self, section: None

Reader().read(config.evaluation)
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    reads = contract.runtime_reads(tmp_path, frozenset({field}))

    assert reads == frozenset()
    assert _audit_as_documented_inert(contract, field, reads).violations == ()


def test_runtime_scan_keeps_final_duplicate_method_definition(contract, tmp_path: Path) -> None:
    (tmp_path / "duplicate_method.py").write_text(
        """
class Reader:
    def read(self, section):
        return None

    def read(self, section):
        return section.stage1_enabled

Reader().read(config.evaluation)
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset({field})


def test_runtime_scan_falls_back_to_base_after_deleted_method(contract, tmp_path: Path) -> None:
    (tmp_path / "deleted_method.py").write_text(
        """
class BaseReader:
    def read(self, section):
        return section.stage1_enabled

class Reader(BaseReader):
    def read(self, section):
        return section.stage1_enabled
    del read

Reader().read(config.evaluation)
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset({field})


def test_runtime_scan_joins_callable_bindings_across_compound_statement_paths(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "callable_control_flow.py").write_text(
        """
def for_reader(value):
    return value.judge_model

def for_zero_iteration(config, unknown_items):
    handler = for_reader
    for item in unknown_items:
        handler = external
    return handler(config.consensus)

def while_reader(value):
    return value.models

def while_zero_iteration(config, enabled):
    handler = while_reader
    while enabled:
        handler = external
    return handler(config.consensus)

def try_reader(value):
    return value.devil_model

def try_success(config, risky):
    handler = try_reader
    try:
        risky()
    except RuntimeError:
        handler = external
    return handler(config.consensus)

def match_reader(value):
    return value.advocate_model

def match_unmatched(config, selector):
    handler = match_reader
    match selector:
        case 0:
            handler = external
    return handler(config.consensus)

def unreachable_reader(value):
    return value.min_models

def terminating_paths_do_not_escape(config, report, selector, risky):
    handler = external
    for _ in unknown_items:
        handler = unreachable_reader
        return None
    handler(config.consensus)

    try:
        risky()
    except RuntimeError:
        handler = unreachable_reader
        return None
    handler(config.consensus)

    match selector:
        case 0:
            handler = unreachable_reader
            return None
        case _:
            handler = external
    handler(config.consensus)

def exhaustive_shadowing(config, selector):
    handler = unreachable_reader
    match selector:
        case 0:
            handler = external
        case _:
            handler = external
    return handler(config.consensus)

def exhaustive_termination(config, selector):
    handler = unreachable_reader
    match selector:
        case 0:
            return None
        case _ if True:
            raise RuntimeError
    handler(config.consensus)

def try_termination(config, risky):
    handler = unreachable_reader
    try:
        return risky()
    except RuntimeError:
        raise
    handler(config.consensus)

def guaranteed_iteration_shadowing(config):
    handler = unreachable_reader
    for _ in [1]:
        handler = external
    handler(config.consensus)

    handler = unreachable_reader
    while True:
        handler = external
        break
    handler(config.consensus)
""",
        encoding="utf-8",
    )
    fields = frozenset(
        {
            contract.ConfigField("consensus", "advocate_model"),
            contract.ConfigField("consensus", "devil_model"),
            contract.ConfigField("consensus", "judge_model"),
            contract.ConfigField("consensus", "min_models"),
            contract.ConfigField("consensus", "models"),
        }
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {
            contract.ConfigField("consensus", "advocate_model"),
            contract.ConfigField("consensus", "devil_model"),
            contract.ConfigField("consensus", "judge_model"),
            contract.ConfigField("consensus", "models"),
        }
    )


def test_runtime_scan_tracks_model_dump_subscript_reads(contract, tmp_path: Path) -> None:
    (tmp_path / "model_dump_read.py").write_text(
        """
def read(section):
    return section.model_dump()["stage1_enabled"]

read(config.evaluation)
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset({field})


def test_runtime_scan_tracks_vars_subscript_reads(contract, tmp_path: Path) -> None:
    (tmp_path / "vars_read.py").write_text(
        """
def read(section):
    return vars(section)["stage2_enabled"]

read(config.evaluation)
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage2_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset({field})


def test_runtime_scan_tracks_serialized_mapping_method_reads(contract, tmp_path: Path) -> None:
    (tmp_path / "serialized_mapping_methods.py").write_text(
        """
config.evaluation.model_dump().get("stage1_enabled")
dumped = config.evaluation.model_dump()
dumped.pop("stage2_enabled")
attributes = vars(config.evaluation)
attributes.setdefault("stage3_enabled", False)
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name)
        for name in ("stage1_enabled", "stage2_enabled", "stage3_enabled")
    )

    assert contract.runtime_reads(tmp_path, fields) == fields


def test_runtime_scan_fails_closed_for_unknown_serialized_mapping_keys(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "dynamic_serialized_keys.py").write_text(
        """
def runtime_key():
    return external_key()

key = runtime_key()
config.evaluation.model_dump().get(key)
config.evaluation.model_dump()[key]
""",
        encoding="utf-8",
    )
    fields = contract.schema_fields()

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        field for field in fields if field.section == "evaluation"
    )


def test_runtime_scan_executes_implicit_local_special_method_protocols(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "implicit_protocols.py").write_text(
        """
class Wrapper:
    def __init__(self, section):
        self.section = section

    def __str__(self):
        return self.section.stage1_enabled

    def __len__(self):
        return self.section.stage2_enabled

    def __bool__(self):
        return self.section.stage3_enabled

wrapped = Wrapper(config.evaluation)
str(wrapped)
len(wrapped)
bool(wrapped)

str = external_str
len = external_len
bool = external_bool
str(Wrapper(config.consensus))
len(Wrapper(config.consensus))
bool(Wrapper(config.consensus))
""",
        encoding="utf-8",
    )
    fields = frozenset(
        {
            *(
                contract.ConfigField("evaluation", name)
                for name in (
                    "stage1_enabled",
                    "stage2_enabled",
                    "stage3_enabled",
                )
            ),
            contract.ConfigField("consensus", "models"),
        }
    )

    assert contract.runtime_reads(tmp_path, fields) == fields - {
        contract.ConfigField("consensus", "models")
    }


def test_runtime_scan_executes_implicit_membership_iteration_hash_and_format_protocols(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "implicit_protocols_extended.py").write_text(
        """
class Reader:
    def __init__(self, section):
        self.section = section

    def __contains__(self, item):
        return self.section.stage1_enabled

    def __hash__(self):
        return self.section.stage2_enabled

    def __format__(self, spec):
        return self.section.stage3_enabled

class AsyncReader:
    def __init__(self, section):
        self.section = section

    def __aiter__(self):
        return self

    async def __anext__(self):
        return self.section.stage4_enabled

reader = Reader(config.evaluation)
1 in reader
hash(reader)
format(reader, "")
f"{reader}"

async def consume():
    async for item in AsyncReader(config.evaluation):
        break

unused = Reader(config.consensus)
""",
        encoding="utf-8",
    )
    fields = frozenset(
        {
            *(
                contract.ConfigField("evaluation", name)
                for name in (
                    "stage1_enabled",
                    "stage2_enabled",
                    "stage3_enabled",
                    "stage4_enabled",
                )
            ),
            contract.ConfigField("consensus", "models"),
        }
    )

    assert contract.runtime_reads(tmp_path, fields) == fields - {
        contract.ConfigField("consensus", "models")
    }


def test_runtime_scan_executes_descriptors_only_when_attribute_resolution_selects_them(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "descriptor_protocol.py").write_text(
        """
class Descriptor:
    def __init__(self, section):
        self.section = section

    def __get__(self, instance, owner):
        return self.section.stage1_enabled

class Active:
    value = Descriptor(config.evaluation)

class Unused:
    value = Descriptor(config.consensus)

class Shadowed:
    value = Descriptor(config.consensus)

Active().value
Unused()
shadowed = Shadowed()
shadowed.value = object()
shadowed.value
""",
        encoding="utf-8",
    )
    fields = frozenset(
        {
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("consensus", "models"),
        }
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {contract.ConfigField("evaluation", "stage1_enabled")}
    )


def test_runtime_scan_executes_truth_and_fallback_len_protocols_only_when_tested(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "truth_protocols.py").write_text(
        """
class Reader:
    def __init__(self, section):
        self.section = section

    def __bool__(self):
        return self.section.stage1_enabled

class LenReader:
    def __init__(self, section):
        self.section = section

    def __len__(self):
        return self.section.stage2_enabled

if Reader(config.evaluation):
    pass
if not LenReader(config.evaluation):
    pass
unused = Reader(config.consensus)
""",
        encoding="utf-8",
    )
    fields = frozenset(
        {
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage2_enabled"),
            contract.ConfigField("consensus", "models"),
        }
    )

    assert contract.runtime_reads(tmp_path, fields) == {
        contract.ConfigField("evaluation", "stage1_enabled"),
        contract.ConfigField("evaluation", "stage2_enabled"),
    }


def test_runtime_scan_models_subscription_iter_next_and_unary_protocols(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "ordinary_protocols.py").write_text(
        """
class Reader:
    def __init__(self, section):
        self.section = section

    def __getitem__(self, index):
        return self.section.stage1_enabled

    def __iter__(self):
        return self

    def __next__(self):
        return self.section.stage2_enabled

    def __lt__(self, other):
        return self.section.stage3_enabled

    def __neg__(self):
        return self.section.stage4_enabled

    def __iadd__(self, other):
        return self.section.stage5_enabled

reader = Reader(config.evaluation)
reader[0]
iter(reader)
next(reader)
reader < 1
-reader
reader += 1
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", f"stage{index}_enabled") for index in range(1, 6)
    )

    assert contract.runtime_reads(tmp_path, fields) == fields


def test_runtime_scan_models_subscription_store_and_delete_protocols_without_overreach(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "subscription_mutation_protocols.py").write_text(
        """
class Writer:
    def __init__(self, section):
        self.section = section

    def __setitem__(self, key, value):
        return self.section.stage1_enabled

    def __delitem__(self, key):
        return self.section.stage2_enabled

writer = Writer(config.evaluation)
writer[0] = 1
del writer[0]

overwritten = Writer(config.consensus)
overwritten = {}
overwritten[0] = 1
del overwritten[0]
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField(section, f"stage{index}_enabled")
        for section in ("evaluation", "consensus")
        for index in (1, 2)
    )

    assert contract.runtime_reads(tmp_path, fields) == {
        contract.ConfigField("evaluation", "stage1_enabled"),
        contract.ConfigField("evaluation", "stage2_enabled"),
    }


def test_runtime_scan_models_reflected_augmented_builtin_and_os_exit_protocols(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "dispatch_edges.py").write_text(
        """
import operator
import os
from os import _exit as stop

class Left:
    def __init__(self, section):
        self.section = section

    def __lt__(self, other):
        return self.section.stage1_enabled

class Right(Left):
    def __gt__(self, other):
        return self.section.stage2_enabled

class Fallback:
    def __init__(self, section):
        self.section = section

    def __lt__(self, other):
        return NotImplemented

    def __gt__(self, other):
        return self.section.stage3_enabled

class AddOnly:
    def __init__(self, section):
        self.section = section

    def __add__(self, other):
        return self.section.stage4_enabled

class InPlaceFallback:
    def __init__(self, section):
        self.section = section

    def __iadd__(self, other):
        return NotImplemented

    def __add__(self, other):
        return self.section.stage5_enabled

class Builtins:
    def __init__(self, section):
        self.section = section

    def __abs__(self):
        return self.section.stage6_enabled

    def __round__(self, ndigits=None):
        return self.section.stage7_enabled

    def __divmod__(self, other):
        return self.section.stage8_enabled

    def __reversed__(self):
        return iter((self.section.stage9_enabled,))

    def __index__(self):
        return self.section.stage10_enabled

Left(config.evaluation) < Right(config.evaluation)
Fallback(config.evaluation) < Fallback(config.evaluation)
add_only = AddOnly(config.evaluation)
add_only += 1
in_place = InPlaceFallback(config.evaluation)
in_place += 1
value = Builtins(config.evaluation)
abs(value)
round(value)
divmod(value, 1)
reversed(value)
operator.index(value)
unused = Builtins(config.consensus)

def module_exit(config):
    os._exit(0)
    return config.evaluation.stage11_enabled

def imported_exit(config):
    stop(0)
    return config.evaluation.stage12_enabled

def shadowed(os):
    os._exit(0)
    return config.evaluation.stage13_enabled
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", f"stage{index}_enabled") for index in range(1, 14)
    ) | frozenset(
        contract.ConfigField("consensus", f"stage{index}_enabled") for index in range(6, 11)
    )

    assert contract.runtime_reads(tmp_path, fields) == {
        contract.ConfigField("evaluation", f"stage{index}_enabled") for index in range(2, 11)
    } | {contract.ConfigField("evaluation", "stage13_enabled")}


def test_runtime_scan_selects_context_manager_protocol_by_syntax(contract, tmp_path: Path) -> None:
    (tmp_path / "context_protocols.py").write_text(
        """
import asyncio

class Reader:
    def __init__(self, section):
        self.section = section

    def __enter__(self):
        return self.section.stage1_enabled

    def __exit__(self, exc_type, exc, tb):
        return self.section.stage2_enabled

    async def __aenter__(self):
        return self.section.stage3_enabled

    async def __aexit__(self, exc_type, exc, tb):
        return self.section.stage4_enabled

with Reader(config.evaluation):
    pass

async def consume():
    async with Reader(config.consensus):
        pass

asyncio.run(consume())
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField(section, f"stage{index}_enabled")
        for section in ("evaluation", "consensus")
        for index in range(1, 5)
    )

    assert contract.runtime_reads(tmp_path, fields) == {
        contract.ConfigField("evaluation", "stage1_enabled"),
        contract.ConfigField("evaluation", "stage2_enabled"),
        contract.ConfigField("consensus", "stage3_enabled"),
        contract.ConfigField("consensus", "stage4_enabled"),
    }


def test_runtime_scan_models_membership_fallback_order_without_overreach(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "membership_fallback.py").write_text(
        """
class IterReader:
    def __init__(self, section):
        self.section = section

    def __iter__(self):
        if self.section.stage1_enabled:
            yield 1

class ItemReader:
    def __init__(self, section):
        self.section = section

    def __getitem__(self, index):
        if self.section.stage2_enabled:
            return index
        raise IndexError

class ContainsReader:
    def __init__(self, section):
        self.section = section

    def __contains__(self, item):
        return self.section.stage3_enabled

    def __iter__(self):
        return iter((self.section.stage4_enabled,))

1 in IterReader(config.evaluation)
1 in ItemReader(config.evaluation)
1 in ContainsReader(config.evaluation)
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name)
        for name in (
            "stage1_enabled",
            "stage2_enabled",
            "stage3_enabled",
            "stage4_enabled",
        )
    )

    assert contract.runtime_reads(tmp_path, fields) == fields - {
        contract.ConfigField("evaluation", "stage4_enabled")
    }


def test_runtime_scan_executes_custom_awaitables_only_when_awaited(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "custom_awaitable.py").write_text(
        """
import asyncio

class Awaitable:
    def __init__(self, section):
        self.section = section

    def __await__(self):
        if self.section.stage1_enabled:
            yield
        return None

async def consume():
    await Awaitable(config.evaluation)
    pending = Awaitable(config.consensus)
    pending = object()

Awaitable(config.consensus)
asyncio.run(consume())
""",
        encoding="utf-8",
    )
    fields = frozenset(
        {
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("consensus", "models"),
        }
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {contract.ConfigField("evaluation", "stage1_enabled")}
    )


def test_runtime_scan_dispatches_reflected_operators_only_when_python_would(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "reflected_operator.py").write_text(
        """
class Left:
    def __add__(self, other):
        return 1

class Fallback:
    def __add__(self, other):
        return NotImplemented

class Right:
    def __init__(self, section):
        self.section = section

    def __radd__(self, other):
        return self.section.stage1_enabled

class Base:
    def __init__(self, section):
        self.section = section

    def __add__(self, other):
        return self.section.stage2_enabled

class Child(Base):
    def __radd__(self, other):
        return self.section.stage3_enabled

Left() + Right(config.consensus)
Fallback() + Right(config.evaluation)
Base(config.evaluation) + Child(config.evaluation)
""",
        encoding="utf-8",
    )
    fields = frozenset(
        {
            *(contract.ConfigField("evaluation", f"stage{index}_enabled") for index in range(1, 4)),
            contract.ConfigField("consensus", "models"),
        }
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage3_enabled"),
        }
    )


def test_runtime_scan_executes_numeric_and_getattribute_protocols(contract, tmp_path: Path) -> None:
    (tmp_path / "implicit_numeric_protocols.py").write_text(
        """
class Reader:
    def __init__(self, section):
        self.section = section

    def __int__(self):
        return self.section.stage1_enabled

    def __add__(self, other):
        return self.section.stage2_enabled

    def __getattribute__(self, name):
        return self.section.stage3_enabled

reader = Reader(config.evaluation)
int(reader)
reader + 1
reader.value
unused = Reader(config.consensus)
""",
        encoding="utf-8",
    )
    fields = frozenset(
        {
            *(
                contract.ConfigField("evaluation", name)
                for name in (
                    "stage1_enabled",
                    "stage2_enabled",
                    "stage3_enabled",
                )
            ),
            contract.ConfigField("consensus", "models"),
        }
    )

    assert contract.runtime_reads(tmp_path, fields) == fields - {
        contract.ConfigField("consensus", "models")
    }


def test_runtime_scan_does_not_continue_past_unconditional_recursion(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "recursive_readers.py").write_text(
        """
def self_recursive(config):
    self_recursive(config)
    config.evaluation.stage1_enabled

def first(config):
    second(config)
    config.evaluation.stage2_enabled

def second(config):
    first(config)

self_recursive(config)
first(config)
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name) for name in ("stage1_enabled", "stage2_enabled")
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset()


def test_runtime_scan_skips_statically_unreachable_assert_messages(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "assert_messages.py").write_text(
        """
assert True, config.evaluation.stage1_enabled
assert False, config.evaluation.stage2_enabled
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name) for name in ("stage1_enabled", "stage2_enabled")
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {contract.ConfigField("evaluation", "stage2_enabled")}
    )


def test_runtime_scan_tracks_bound_getattribute_reads(contract, tmp_path: Path) -> None:
    (tmp_path / "bound_getattribute.py").write_text(
        """
config.evaluation.__getattribute__("stage1_enabled")
reader = config.evaluation.__getattribute__
reader("stage2_enabled")
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name) for name in ("stage1_enabled", "stage2_enabled")
    )

    assert contract.runtime_reads(tmp_path, fields) == fields


def test_runtime_scan_tracks_model_copy_identity(contract, tmp_path: Path) -> None:
    (tmp_path / "model_copy.py").write_text(
        "config.evaluation.model_copy().stage1_enabled\n", encoding="utf-8"
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset({field})


def test_runtime_scan_respects_model_dump_include_and_exclude(contract, tmp_path: Path) -> None:
    (tmp_path / "model_dump_filters.py").write_text(
        """
config.evaluation.model_dump(include={"semantic_model"}).get("stage1_enabled")
config.evaluation.model_dump(exclude={"stage1_enabled"}).get("stage2_enabled")
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name) for name in ("stage1_enabled", "stage2_enabled")
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {contract.ConfigField("evaluation", "stage2_enabled")}
    )


def test_runtime_scan_tracks_identity_decorated_callable(contract, tmp_path: Path) -> None:
    (tmp_path / "decorated_reader.py").write_text(
        """
import functools

@functools.cache
def read(config):
    return config.evaluation.stage1_enabled

read(settings)
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset({field})


def test_runtime_scan_tracks_model_dump_json_serialization_filters(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "model_dump_json.py").write_text(
        """
config.evaluation.model_dump_json(include={"stage1_enabled"})
config.evaluation.model_dump_json(exclude={"stage1_enabled", "stage3_enabled"})
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name)
        for name in ("stage1_enabled", "stage2_enabled", "stage3_enabled")
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage2_enabled"),
        }
    )


def test_runtime_scan_tracks_exact_functools_wraps_and_update_wrapper(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "wrapped_readers.py").write_text(
        """
import functools

def template(config):
    return None

@functools.wraps(template)
def decorated(config):
    return config.evaluation.stage1_enabled

def assigned(config):
    return config.evaluation.stage2_enabled

assigned = functools.update_wrapper(assigned, template)
decorated(settings)
assigned(settings)
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name) for name in ("stage1_enabled", "stage2_enabled")
    )
    reads = contract.runtime_reads(tmp_path, fields)

    assert reads == fields
    assert "production-wired field is still documented inert" in "\n".join(
        _audit_as_documented_inert(
            contract, contract.ConfigField("evaluation", "stage1_enabled"), reads
        ).violations
    )


def test_runtime_scan_does_not_treat_shadowed_wraps_as_functools(contract, tmp_path: Path) -> None:
    (tmp_path / "shadowed_wraps.py").write_text(
        """
from functools import wraps
wraps = external_decorator

def template(config):
    return None

@wraps(template)
def reader(config):
    return config.evaluation.stage1_enabled

reader(settings)
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")
    reads = contract.runtime_reads(tmp_path, frozenset({field}))

    assert reads == frozenset()
    assert any(
        "unresolved decorator 'wraps'" in violation
        for violation in _audit_as_documented_inert(contract, field, reads).violations
    )


def test_runtime_scan_tracks_list_sort_callback(contract, tmp_path: Path) -> None:
    (tmp_path / "sort_callback.py").write_text(
        """
def read(section):
    return section.stage1_enabled

sections = [settings.evaluation]
sections.sort(key=read)
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset({field})


def test_runtime_scan_tracks_operator_attrgetter_reads(contract, tmp_path: Path) -> None:
    (tmp_path / "attrgetter_read.py").write_text(
        """
import operator

read_stage = operator.attrgetter("stage3_enabled")
read_stage(config.evaluation)
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage3_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset({field})


def test_runtime_scan_executes_accessor_callbacks_in_eager_consumers(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "accessor_callbacks.py").write_text(
        """
import operator

list(map(operator.attrgetter("stage1_enabled"), [config.evaluation]))
key = operator.attrgetter("stage2_enabled")
sorted([config.evaluation], key=key)
list(map(getattr, [config.evaluation], ["stage3_enabled"]))
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name)
        for name in ("stage1_enabled", "stage2_enabled", "stage3_enabled")
    )

    reads = contract.runtime_reads(tmp_path, fields)

    assert reads == fields
    for field in fields:
        assert _audit_as_documented_inert(contract, field, reads).violations


def test_runtime_scan_does_not_execute_stale_or_shadowed_accessor_callbacks(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "inactive_accessor_callbacks.py").write_text(
        """
import operator

pending = map(operator.attrgetter("stage1_enabled"), [config.evaluation])
key = operator.attrgetter("stage2_enabled")
key = lambda section: None
sorted([config.evaluation], key=key)

def getattr(section, name):
    return None

list(map(getattr, [config.evaluation], ["stage3_enabled"]))
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name)
        for name in ("stage1_enabled", "stage2_enabled", "stage3_enabled")
    )

    reads = contract.runtime_reads(tmp_path, fields)

    assert reads == frozenset()
    for field in fields:
        assert _audit_as_documented_inert(contract, field, reads).violations == ()


def test_runtime_scan_consumes_accessor_maps_through_iterable_protocols(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "iterable_accessor_callbacks.py").write_text(
        """
import operator

for value in map(operator.attrgetter("stage1_enabled"), [config.evaluation]):
    pass
[stage2] = map(operator.attrgetter("stage2_enabled"), [config.evaluation])
None in map(operator.attrgetter("stage3_enabled"), [config.evaluation])
values = []
values.extend(map(operator.attrgetter("semantic_model"), [config.evaluation]))
list(value for value in map(operator.attrgetter("assertion_extraction_model"), [config.evaluation]))
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name)
        for name in (
            "stage1_enabled",
            "stage2_enabled",
            "stage3_enabled",
            "semantic_model",
            "assertion_extraction_model",
        )
    )

    assert contract.runtime_reads(tmp_path, fields) == fields


def test_runtime_scan_keeps_empty_and_unconsumed_accessor_maps_inert(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "inactive_iterable_callbacks.py").write_text(
        """
import operator

pending = map(operator.attrgetter("stage1_enabled"), [config.evaluation])
for value in map(operator.attrgetter("stage2_enabled"), []):
    pass
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name) for name in ("stage1_enabled", "stage2_enabled")
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset()


def test_runtime_scan_propagates_exact_local_raise_reachability(contract, tmp_path: Path) -> None:
    (tmp_path / "exported_nonreturning.py").write_text(
        """
def stop():
    raise RuntimeError("stop")

def exported():
    stop()
    return config.evaluation.stage1_enabled
""",
        encoding="utf-8",
    )
    (tmp_path / "module_nonreturning.py").write_text(
        """
def stop():
    raise RuntimeError("stop")

stop()
config.evaluation.stage2_enabled
""",
        encoding="utf-8",
    )
    (tmp_path / "caught_nonreturning.py").write_text(
        """
def stop():
    raise RuntimeError("stop")

try:
    stop()
except RuntimeError:
    config.evaluation.stage3_enabled
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name)
        for name in ("stage1_enabled", "stage2_enabled", "stage3_enabled")
    )

    reads = contract.runtime_reads(tmp_path, fields)

    assert reads == frozenset({contract.ConfigField("evaluation", "stage3_enabled")})


def test_runtime_scan_consumes_callable_iterators_returned_by_local_protocols(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "local_iterable_protocol.py").write_text(
        """
import operator

class Wrapper:
    def __init__(self, source):
        self.source = source

    def __iter__(self):
        return iter(self.source)

class Fallback:
    def __getitem__(self, index):
        return settings.evaluation.stage2_enabled

list(Wrapper(map(operator.attrgetter("stage1_enabled"), [settings.evaluation])))
list(Fallback())
pending = Wrapper(map(operator.attrgetter("stage3_enabled"), [settings.evaluation]))
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name)
        for name in ("stage1_enabled", "stage2_enabled", "stage3_enabled")
    )

    assert contract.runtime_reads(tmp_path, fields) == fields - {
        contract.ConfigField("evaluation", "stage3_enabled")
    }


def test_runtime_scan_propagates_conditional_exact_local_termination(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "conditional_nonreturning.py").write_text(
        """
def stop():
    if True:
        raise RuntimeError("stop")

def exported():
    stop()
    return settings.evaluation.stage1_enabled

try:
    stop()
except RuntimeError:
    settings.evaluation.stage2_enabled
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name) for name in ("stage1_enabled", "stage2_enabled")
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {contract.ConfigField("evaluation", "stage2_enabled")}
    )


def test_runtime_scan_tracks_constant_and_dotted_operator_getters(contract, tmp_path: Path) -> None:
    (tmp_path / "operator_constants.py").write_text(
        """
import operator
from operator import attrgetter, itemgetter

FIELD = "stage1_enabled"
attrgetter(FIELD)(config.evaluation)
operator.attrgetter("evaluation.stage2_enabled")(config)
itemgetter(FIELD)(config.evaluation.model_dump())
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name) for name in ("stage1_enabled", "stage2_enabled")
    )

    assert contract.runtime_reads(tmp_path, fields) == fields


def test_runtime_scan_resolves_imported_constants_and_callback_containers(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "names.py").write_text('FIELD = "stage1_enabled"\n', encoding="utf-8")
    (tmp_path / "callbacks.py").write_text(
        """
def read_stage(section):
    return section.stage2_enabled

_READERS = {"stage": read_stage}
""",
        encoding="utf-8",
    )
    (tmp_path / "imported_values.py").write_text(
        """
from names import FIELD
from callbacks import _READERS

getattr(settings.evaluation, FIELD)
_READERS["stage"](settings.evaluation)
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name) for name in ("stage1_enabled", "stage2_enabled")
    )

    reads = contract.runtime_reads(tmp_path, fields)
    assert reads == fields
    report = _audit_as_documented_inert(
        contract, contract.ConfigField("evaluation", "stage1_enabled"), reads
    )
    assert "evaluation.stage1_enabled: conflicting config-field dispositions" in report.violations
    assert "evaluation.stage1_enabled: production-wired field is still documented inert" in (
        report.violations
    )


def test_runtime_scan_keeps_imported_values_lazy_when_unused(contract, tmp_path: Path) -> None:
    (tmp_path / "callbacks.py").write_text(
        """
def read_stage(section):
    return section.stage3_enabled

_READERS = {"stage": read_stage}
""",
        encoding="utf-8",
    )
    (tmp_path / "unused_imported_values.py").write_text(
        "from callbacks import _READERS\npending = _READERS\n", encoding="utf-8"
    )
    field = contract.ConfigField("evaluation", "stage3_enabled")

    reads = contract.runtime_reads(tmp_path, frozenset({field}))
    assert reads == frozenset()
    assert _audit_as_documented_inert(contract, field, reads).violations == ()


def test_runtime_scan_resolves_module_qualified_data_values(contract, tmp_path: Path) -> None:
    (tmp_path / "names.py").write_text('FIELD = "stage1_enabled"\n', encoding="utf-8")
    (tmp_path / "callbacks.py").write_text(
        """
def read_stage(section):
    return section.stage2_enabled

READERS = {"stage": read_stage}
""",
        encoding="utf-8",
    )
    (tmp_path / "qualified_values.py").write_text(
        """
import names
import callbacks as cb

getattr(settings.evaluation, names.FIELD)
cb.READERS["stage"](settings.evaluation)
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name) for name in ("stage1_enabled", "stage2_enabled")
    )

    reads = contract.runtime_reads(tmp_path, fields)
    assert reads == fields
    for field in fields:
        report = _audit_as_documented_inert(contract, field, reads)
        assert f"{field.section}.{field.name}: conflicting config-field dispositions" in (
            report.violations
        )
        assert (
            f"{field.section}.{field.name}: production-wired field is still documented inert"
            in (report.violations)
        )


def test_runtime_scan_keeps_module_qualified_data_lazy_when_unused(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "callbacks.py").write_text(
        """
def read_stage(section):
    return section.stage3_enabled

READERS = {"stage": read_stage}
""",
        encoding="utf-8",
    )
    (tmp_path / "unused_qualified_values.py").write_text(
        "import callbacks\npending = callbacks.READERS\n", encoding="utf-8"
    )
    field = contract.ConfigField("evaluation", "stage3_enabled")

    reads = contract.runtime_reads(tmp_path, frozenset({field}))
    assert reads == frozenset()
    assert _audit_as_documented_inert(contract, field, reads).violations == ()


def test_runtime_scan_resolves_final_imported_mutations_and_control_flow(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "mutated_values.py").write_text(
        """
def _old(section):
    return section.stage1_enabled

def _new(section):
    return section.stage2_enabled

READERS = {"main": _old}
READERS["main"] = _new

if runtime_flag:
    FIELD = "stage2_enabled"
else:
    FIELD = "stage3_enabled"
""",
        encoding="utf-8",
    )
    (tmp_path / "use_mutated_values.py").write_text(
        """
from mutated_values import FIELD, READERS

READERS["main"](settings.evaluation)
getattr(settings.evaluation, FIELD)
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name)
        for name in ("stage1_enabled", "stage2_enabled", "stage3_enabled")
    )

    reads = contract.runtime_reads(tmp_path, fields)
    expected = fields - {contract.ConfigField("evaluation", "stage1_enabled")}
    assert reads == expected
    for field in expected:
        assert _audit_as_documented_inert(contract, field, reads).violations
    assert (
        _audit_as_documented_inert(
            contract, contract.ConfigField("evaluation", "stage1_enabled"), reads
        ).violations
        == ()
    )


def test_runtime_scan_resolves_dictionary_comprehension_registry_keys(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "comprehension_registry.py").write_text(
        """
def _read(section):
    return section.stage2_enabled

READERS = {name: callback for name, callback in [("main", _read)]}
READERS["main"](settings.evaluation)
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage2_enabled")

    reads = contract.runtime_reads(tmp_path, frozenset({field}))
    assert reads == frozenset({field})
    assert _audit_as_documented_inert(contract, field, reads).violations


def test_runtime_scan_does_not_overreach_dictionary_comprehension_registry(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "inactive_comprehension_registry.py").write_text(
        """
def _read(section):
    return section.stage2_enabled

READERS = {name: callback for name, callback in [("main", _read)]}
READERS["main"](report.evaluation)
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage2_enabled")

    reads = contract.runtime_reads(tmp_path, frozenset({field}))
    assert reads == frozenset()
    assert _audit_as_documented_inert(contract, field, reads).violations == ()


def test_runtime_scan_tracks_unshadowed_classmethod_and_rejects_shadowing(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "classmethod_read.py").write_text(
        """
class Reader:
    @classmethod
    def read(cls, section):
        return section.stage1_enabled

Reader.read(config.evaluation)

def classmethod(fn):
    return lambda *args: None

class Shadow:
    @classmethod
    def read(cls, section):
        return section.stage2_enabled

Shadow.read(config.evaluation)
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name) for name in ("stage1_enabled", "stage2_enabled")
    )

    assert contract.runtime_reads(tmp_path, fields) == {
        contract.ConfigField("evaluation", "stage1_enabled")
    }


def test_runtime_scan_tracks_partial_wrapped_builtin_attribute_reads(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "partial_builtin_readers.py").write_text(
        """
import functools
import operator

read_stage1 = functools.partial(getattr, config.evaluation, "stage1_enabled")
read_stage2 = functools.partial(operator.attrgetter("stage2_enabled"), config.evaluation)
make_stage3_reader = functools.partial(operator.attrgetter, "stage3_enabled")
read_stage1()
read_stage2()
make_stage3_reader()(config.evaluation)
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name)
        for name in ("stage1_enabled", "stage2_enabled", "stage3_enabled")
    )
    reads = contract.runtime_reads(tmp_path, fields)

    assert reads == fields
    assert "production-wired field is still documented inert" in "\n".join(
        _audit_as_documented_inert(
            contract, contract.ConfigField("evaluation", "stage1_enabled"), reads
        ).violations
    )


def test_runtime_scan_does_not_model_shadowed_partial_builtin_targets(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "shadowed_partial_builtins.py").write_text(
        """
import functools
getattr = external_getattr

reader = functools.partial(getattr, config.evaluation, "stage1_enabled")
reader()
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")
    reads = contract.runtime_reads(tmp_path, frozenset({field}))

    assert reads == frozenset()
    assert _audit_as_documented_inert(contract, field, reads).violations == ()


def test_runtime_scan_respects_postponed_annotation_runtime_semantics(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "postponed_annotation.py").write_text(
        """
from __future__ import annotations
marker: config.evaluation.stage1_enabled
""",
        encoding="utf-8",
    )
    (tmp_path / "eager_annotation.py").write_text(
        "marker: config.evaluation.stage2_enabled\n",
        encoding="utf-8",
    )
    stage1 = contract.ConfigField("evaluation", "stage1_enabled")
    stage2 = contract.ConfigField("evaluation", "stage2_enabled")
    reads = contract.runtime_reads(tmp_path, frozenset({stage1, stage2}))

    assert reads == frozenset({stage2})
    assert _audit_as_documented_inert(contract, stage1, reads).violations == ()
    assert "production-wired field is still documented inert" in "\n".join(
        _audit_as_documented_inert(contract, stage2, reads).violations
    )


def test_runtime_scan_tracks_eager_function_signature_annotations(contract, tmp_path: Path) -> None:
    (tmp_path / "eager_signature.py").write_text(
        """
def reader(
    positional: settings.evaluation.stage1_enabled,
    *args: settings.evaluation.stage2_enabled,
    keyword: settings.evaluation.stage3_enabled,
    **kwargs: settings.consensus.semantic_enabled,
) -> settings.consensus.uncertainty_threshold:
    pass
""",
        encoding="utf-8",
    )
    fields = frozenset(
        {
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "stage2_enabled"),
            contract.ConfigField("evaluation", "stage3_enabled"),
            contract.ConfigField("consensus", "semantic_enabled"),
            contract.ConfigField("consensus", "uncertainty_threshold"),
        }
    )
    reads = contract.runtime_reads(tmp_path, fields)

    assert reads == fields
    for field in fields:
        assert _audit_as_documented_inert(contract, field, reads).violations


def test_runtime_scan_skips_postponed_function_signature_annotations(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "postponed_signature.py").write_text(
        """
from __future__ import annotations

def reader(
    value: settings.evaluation.stage1_enabled,
) -> settings.evaluation.stage2_enabled:
    pass
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name) for name in ("stage1_enabled", "stage2_enabled")
    )
    reads = contract.runtime_reads(tmp_path, fields)

    assert reads == frozenset()
    for field in fields:
        assert _audit_as_documented_inert(contract, field, reads).violations == ()


def test_runtime_scan_skips_function_local_variable_annotations(contract, tmp_path: Path) -> None:
    (tmp_path / "local_annotation.py").write_text(
        """
def reader():
    local: settings.evaluation.stage1_enabled

reader()
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")
    reads = contract.runtime_reads(tmp_path, frozenset({field}))

    assert reads == frozenset()
    assert _audit_as_documented_inert(contract, field, reads).violations == ()


def test_runtime_scan_tracks_alternative_attribute_accessors(contract, tmp_path: Path) -> None:
    (tmp_path / "attribute_accessors.py").write_text(
        """
import builtins
import operator

hasattr(settings.evaluation, "stage1_enabled")
object.__getattribute__(settings.evaluation, "stage2_enabled")
operator.methodcaller("__getattribute__", "stage3_enabled")(settings.evaluation)
""",
        encoding="utf-8",
    )
    (tmp_path / "shadowed_attribute_accessors.py").write_text(
        """
hasattr = lambda *_args: False
object = external_object
methodcaller = external_factory

hasattr(settings.evaluation, "semantic_model")
object.__getattribute__(settings.evaluation, "semantic_model")
methodcaller("__getattribute__", "semantic_model")(settings.evaluation)
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name)
        for name in ("stage1_enabled", "stage2_enabled", "stage3_enabled", "semantic_model")
    )

    assert contract.runtime_reads(tmp_path, fields) == fields - {
        contract.ConfigField("evaluation", "semantic_model")
    }


def test_runtime_scan_tracks_constant_keys_for_serialized_mapping_accessors(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "constant_mapping_keys.py").write_text(
        """
SUBSCRIPT_FIELD = "stage1_enabled"
GET_FIELD = "stage2_enabled"

settings.evaluation.model_dump()[SUBSCRIPT_FIELD]
settings.evaluation.model_dump().get(GET_FIELD)
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name) for name in ("stage1_enabled", "stage2_enabled")
    )

    reads = contract.runtime_reads(tmp_path, fields)
    assert reads == fields
    for field in fields:
        report = _audit_as_documented_inert(contract, field, reads)
        assert (
            f"{field.section}.{field.name}: production-wired field is still documented inert"
            in (report.violations)
        )


def test_runtime_scan_tracks_class_constants_and_callback_registries(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "class_values.py").write_text(
        """
def read_stage(section):
    return section.stage2_enabled

class Fields:
    STAGE = "stage1_enabled"

class Registry:
    READERS = {"main": read_stage}

getattr(config.evaluation, Fields.STAGE)
Registry.READERS["main"](config.evaluation)
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name) for name in ("stage1_enabled", "stage2_enabled")
    )

    reads = contract.runtime_reads(tmp_path, fields)
    assert reads == fields


def test_runtime_scan_expands_static_starred_accessor_arguments(contract, tmp_path: Path) -> None:
    (tmp_path / "starred_accessors.py").write_text(
        """
import operator

GETATTR_ARGS = (config.evaluation, "stage1_enabled")
GETITEM_ARGS = (config.evaluation.model_dump(), "stage2_enabled")

getattr(*GETATTR_ARGS)
operator.getitem(*GETITEM_ARGS)
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name) for name in ("stage1_enabled", "stage2_enabled")
    )

    assert contract.runtime_reads(tmp_path, fields) == fields


def test_runtime_scan_tracks_class_pattern_keyword_attributes(contract, tmp_path: Path) -> None:
    (tmp_path / "class_patterns.py").write_text(
        """
match settings.evaluation:
    case EvaluationConfig(stage1_enabled=True):
        pass

match unrelated:
    case EvaluationConfig(stage2_enabled=True):
        pass

match settings.evaluation:
    case OtherConfig(stage3_enabled=True):
        pass
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name)
        for name in ("stage1_enabled", "stage2_enabled", "stage3_enabled")
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {contract.ConfigField("evaluation", "stage1_enabled")}
    )


def test_runtime_scan_tracks_operator_getitem_over_serialized_sections(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "operator_getitem.py").write_text(
        """
import operator
from operator import getitem as imported_getitem

alias = operator.getitem
operator.getitem(settings.evaluation.model_dump(), "stage1_enabled")
imported_getitem(settings.evaluation.model_dump(), "stage2_enabled")
alias(settings.evaluation.model_dump(), "stage3_enabled")
dynamic = "semantic_model"
operator.getitem(settings.evaluation.model_dump(), dynamic)
imported_getitem = external_getitem
imported_getitem(settings.evaluation.model_dump(), "semantic_model")
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", name)
        for name in ("stage1_enabled", "stage2_enabled", "stage3_enabled", "semantic_model")
    )

    assert contract.runtime_reads(tmp_path, fields) == fields


def test_runtime_scan_resolves_constant_indirected_getattr_and_ignores_shadowed_builtin(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "constant_getattr.py").write_text(
        """
FIELD = "semantic_model"
getattr(config.evaluation, FIELD)
""",
        encoding="utf-8",
    )
    (tmp_path / "shadowed_getattr.py").write_text(
        """
def getattr(section, name):
    return None

getattr(config.evaluation, "stage2_enabled")
""",
        encoding="utf-8",
    )
    fields = frozenset(
        {
            contract.ConfigField("evaluation", "semantic_model"),
            contract.ConfigField("evaluation", "stage2_enabled"),
        }
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {contract.ConfigField("evaluation", "semantic_model")}
    )


def test_runtime_scan_preserves_builtin_getattr_aliases(contract, tmp_path: Path) -> None:
    (tmp_path / "assigned_alias.py").write_text(
        """
reader = getattr
reader(config.evaluation, "stage2_enabled")
""",
        encoding="utf-8",
    )
    (tmp_path / "imported_alias.py").write_text(
        """
from builtins import getattr as reader
reader(config.evaluation, "stage3_enabled")
""",
        encoding="utf-8",
    )
    (tmp_path / "module_alias.py").write_text(
        """
import builtins
reader = builtins.getattr
reader(config.evaluation, "satisfaction_threshold")
""",
        encoding="utf-8",
    )
    fields = frozenset(
        {
            contract.ConfigField("evaluation", "stage2_enabled"),
            contract.ConfigField("evaluation", "stage3_enabled"),
            contract.ConfigField("evaluation", "satisfaction_threshold"),
        }
    )

    assert contract.runtime_reads(tmp_path, fields) == fields


def test_static_comparisons_cannot_make_dead_reads_satisfy_the_contract(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "dead_comparison.py").write_text(
        """
if 1 == 2:
    dead = config.evaluation.stage1_enabled
if "stable" != "stable":
    also_dead = config.evaluation.stage1_enabled
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")
    reads = contract.runtime_reads(tmp_path, frozenset({field}))

    report = contract.audit_contract(
        fields=frozenset({field}),
        reads=reads,
        rows={
            field: contract.ReferenceRow(
                "true", "Currently inert. Effective control: runtime.stage1_enabled."
            )
        },
        markers={field: contract.InertMarker(field, "runtime.stage1_enabled")},
        allowlist={},
        documented_defaults={},
    )

    assert reads == frozenset()
    assert report.violations == ()


def test_constant_string_expressions_establish_getattr_reads_and_reject_stale_inert_docs(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "constant_string_getattr.py").write_text(
        """
FIELD = "semantic_" + "model"
getattr(config.evaluation, FIELD)
getattr(config.evaluation, f"stage{2}_enabled")
getattr(config.evaluation, f"semantic_{dynamic_suffix}")
""",
        encoding="utf-8",
    )
    semantic = contract.ConfigField("evaluation", "semantic_model")
    stage2 = contract.ConfigField("evaluation", "stage2_enabled")
    reads = contract.runtime_reads(tmp_path, frozenset({semantic, stage2}))

    report = contract.audit_contract(
        fields=frozenset({semantic}),
        reads=reads & {semantic},
        rows={
            semantic: contract.ReferenceRow(
                '"claude-opus-4-8"',
                "Currently inert. Effective control: models.semantic.",
            )
        },
        markers={semantic: contract.InertMarker(semantic, "models.semantic")},
        allowlist={},
        documented_defaults={},
    )

    assert reads == frozenset({semantic, stage2})
    assert "evaluation.semantic_model: conflicting config-field dispositions" in (report.violations)
    assert "evaluation.semantic_model: production-wired field is still documented inert" in (
        report.violations
    )


def test_runtime_scan_invokes_captured_property_getters(contract, tmp_path: Path) -> None:
    (tmp_path / "property_read.py").write_text(
        """
class Reader:
    @property
    def value(self):
        return self.section.satisfaction_threshold

reader = Reader(section=config.evaluation)
captured = reader.value
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "satisfaction_threshold")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset({field})


def test_runtime_scan_invokes_callable_object_dunder_call(contract, tmp_path: Path) -> None:
    (tmp_path / "callable_object_read.py").write_text(
        """
class Reader:
    def __call__(self, section):
        return section.stage2_enabled

reader = Reader()
reader(config.evaluation)
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage2_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset({field})


@pytest.mark.parametrize(
    "source",
    (
        """
from typing import cast
from ouroboros.config.models import EvaluationConfig

cast(EvaluationConfig, config.evaluation).stage1_enabled
""",
        """
import copy

copy.copy(config.evaluation).stage1_enabled
copy.deepcopy(config.evaluation).stage1_enabled
""",
        """
from copy import copy as clone
from copy import deepcopy as deep_clone

clone(config.evaluation).stage1_enabled
deep_clone(config.evaluation).stage1_enabled
""",
    ),
)
def test_runtime_scan_preserves_identity_transform_provenance(
    contract, tmp_path: Path, source: str
) -> None:
    (tmp_path / "identity_transform.py").write_text(source, encoding="utf-8")
    field = contract.ConfigField("evaluation", "stage1_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset({field})


@pytest.mark.parametrize(
    "source",
    (
        """
from typing import cast

cast = external
cast(EvaluationConfig, config.evaluation).stage1_enabled
""",
        """
import copy

copy = external
copy.copy(config.evaluation).stage1_enabled
""",
        """
from copy import deepcopy

deepcopy = external
deepcopy(config.evaluation).stage1_enabled
""",
    ),
)
def test_runtime_scan_respects_overwritten_identity_transforms(
    contract, tmp_path: Path, source: str
) -> None:
    (tmp_path / "overwritten_identity_transform.py").write_text(source, encoding="utf-8")
    field = contract.ConfigField("evaluation", "stage1_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset()


def test_runtime_scan_invokes_constructor_captured_callable_receiver(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "captured_callable.py").write_text(
        """
class Reader:
    def __init__(self, section):
        self.section = section

    def __call__(self):
        return self.section.stage1_enabled

Reader(config.evaluation)()
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset({field})


def test_runtime_scan_respects_overwritten_constructor_captured_receiver(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "overwritten_captured_callable.py").write_text(
        """
class Reader:
    def __init__(self, section):
        self.section = section

    def __call__(self):
        return self.section.stage1_enabled

reader = Reader(config.evaluation)
reader.section = external
reader()
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset()


@pytest.mark.parametrize("range_expression", ("range(0)", "range(5, 5)", "range(3, 3, -1)"))
def test_runtime_scan_ignores_statically_empty_range_loops(
    contract, tmp_path: Path, range_expression: str
) -> None:
    (tmp_path / "empty_range.py").write_text(
        f"for _ in {range_expression}:\n    config.evaluation.stage1_enabled\n",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset()


@pytest.mark.parametrize("range_expression", ("range(1)", "range(2, 3)", "range(3, 0, -1)"))
def test_runtime_scan_counts_statically_nonempty_range_loops(
    contract, tmp_path: Path, range_expression: str
) -> None:
    (tmp_path / "nonempty_range.py").write_text(
        f"for _ in {range_expression}:\n    config.evaluation.stage1_enabled\n",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset({field})


def test_runtime_scan_ignores_unreachable_local_reader_before_external_overwrite(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "unreachable_local_reader.py").write_text(
        """
from ouroboros.config.models import EvaluationConfig

def dispatch(config):
    def local_reader(section: EvaluationConfig):
        return section.stage1_enabled

    selected = external
    if False:
        selected = local_reader
    selected = external
    selected(config.evaluation)
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset()


def test_runtime_scan_ignores_module_statements_after_infinite_loop(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "module_unreachable.py").write_text(
        """
while True:
    pass
config.evaluation.stage1_enabled
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset()


def test_runtime_scan_ignores_uninvoked_private_helpers(contract, tmp_path: Path) -> None:
    (tmp_path / "uninvoked_helper.py").write_text(
        """
def _never_called(config):
    return config.evaluation.stage1_enabled
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset()


def test_runtime_scan_ignores_uninvoked_private_class_members(contract, tmp_path: Path) -> None:
    (tmp_path / "uninvoked_private_member.py").write_text(
        """
class PublicReader:
    def _never_called(self, config):
        return config.evaluation.stage1_enabled
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset()


def test_erased_definitions_cannot_satisfy_full_config_audit(contract, tmp_path: Path) -> None:
    (tmp_path / "erased_definitions.py").write_text(
        """
def overwritten(config):
    return config.evaluation.stage1_enabled
overwritten = None

def deleted(config):
    return config.evaluation.stage1_enabled
del deleted

class Readers:
    def replaced(self, config):
        return config.evaluation.stage1_enabled
    replaced = None

    def duplicate(self, config):
        return config.evaluation.stage1_enabled
    def duplicate(self, report):
        return report.evaluation.stage1_enabled
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")
    reads = contract.runtime_reads(tmp_path, frozenset({field}))

    assert reads == frozenset()
    report = contract.audit_contract(
        fields=frozenset({field}),
        reads=reads,
        rows={field: contract.ReferenceRow("true", "Runtime control.")},
        markers={},
        allowlist={},
        documented_defaults={},
    )
    assert report.violations == (
        "evaluation.stage1_enabled: no production read, inert documentation, or schema-only rationale",
    )


def test_runtime_scan_honors_exact_local_replacement_decorator(contract, tmp_path: Path) -> None:
    (tmp_path / "replacement_decorator.py").write_text(
        """
from ouroboros.config.models import EvaluationConfig

def replace_with_noop(function):
    return lambda *args, **kwargs: None

@replace_with_noop
def read_stage(section: EvaluationConfig):
    return section.stage2_enabled

read_stage(config.evaluation)
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage2_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset()


def test_runtime_scan_does_not_visit_erased_body_for_non_callable_decorator(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "non_callable_decorator.py").write_text(
        """
def erase(function):
    return None

@erase
def read_stage(config):
    return config.evaluation.stage2_enabled
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage2_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset()


def test_runtime_scan_fails_closed_for_unresolved_replacement_decorator(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "external_decorator.py").write_text(
        """
from external_package import erase

@erase
def read_stage(config):
    return config.evaluation.stage2_enabled

read_stage(config)
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage2_enabled")

    reads = contract.runtime_reads(tmp_path, frozenset({field}))

    assert reads == frozenset()
    report = contract.audit_contract(
        fields=frozenset({field}),
        reads=reads,
        rows={
            field: contract.ReferenceRow(
                "true", "Currently inert. Effective control: runtime.stage1_enabled."
            )
        },
        markers={field: contract.InertMarker(field, "runtime.stage1_enabled")},
        allowlist={},
        documented_defaults={},
    )
    assert any("unresolved decorator 'erase'" in violation for violation in report.violations)


def test_runtime_scan_keeps_exact_local_identity_decorator(contract, tmp_path: Path) -> None:
    (tmp_path / "identity_decorator.py").write_text(
        """
def preserve(function):
    return function

@preserve
def read_stage(config):
    return config.evaluation.stage2_enabled

read_stage(config)
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage2_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset({field})


def test_runtime_scan_applies_exact_local_class_decorator(contract, tmp_path: Path) -> None:
    (tmp_path / "class_decorator.py").write_text(
        """
def install(cls):
    def read(self):
        return settings.evaluation.stage1_enabled
    cls.read = read
    return cls

@install
class Reader:
    pass

Reader().read()
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")
    reads = contract.runtime_reads(tmp_path, frozenset({field}))

    assert reads == frozenset({field})
    assert _audit_as_documented_inert(contract, field, reads).violations


def test_runtime_scan_honors_exact_class_erasing_decorator(contract, tmp_path: Path) -> None:
    (tmp_path / "erased_class.py").write_text(
        """
def erase(cls):
    return None

@erase
class Reader:
    def read(self):
        return settings.evaluation.stage1_enabled
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset()


def test_runtime_scan_preserves_functools_singledispatch_default(contract, tmp_path: Path) -> None:
    (tmp_path / "singledispatch_reader.py").write_text(
        """
from functools import singledispatch

@singledispatch
def read(value):
    return settings.evaluation.stage1_enabled

read(object())
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")
    reads = contract.runtime_reads(tmp_path, frozenset({field}))

    assert reads == frozenset({field})
    assert _audit_as_documented_inert(contract, field, reads).violations


def test_runtime_scan_resolves_inherited_super_property(contract, tmp_path: Path) -> None:
    (tmp_path / "super_property.py").write_text(
        """
class Base:
    @property
    def section(self):
        return config.evaluation

class Reader(Base):
    def read(self):
        return super().section.stage1_enabled

Reader().read()
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset({field})


@pytest.mark.parametrize(
    "source",
    (
        "match 1:\n    case 2:\n        config.evaluation.stage1_enabled\n",
        "match [1]:\n    case [2]:\n        config.evaluation.stage1_enabled\n",
        'match {"kind": 1}:\n    case {"kind": 2}:\n        config.evaluation.stage1_enabled\n',
    ),
)
def test_runtime_scan_ignores_statically_impossible_match_cases(
    contract, tmp_path: Path, source: str
) -> None:
    (tmp_path / "impossible_match.py").write_text(source, encoding="utf-8")
    field = contract.ConfigField("evaluation", "stage1_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset()


@pytest.mark.parametrize(
    "source",
    (
        "match 1:\n    case 1:\n        config.evaluation.stage1_enabled\n",
        "match [1]:\n    case [1]:\n        config.evaluation.stage1_enabled\n",
        'match {"kind": 1}:\n    case {"kind": 1}:\n        config.evaluation.stage1_enabled\n',
    ),
)
def test_runtime_scan_counts_statically_matching_match_cases(
    contract, tmp_path: Path, source: str
) -> None:
    (tmp_path / "matching_match.py").write_text(source, encoding="utf-8")
    field = contract.ConfigField("evaluation", "stage1_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset({field})


def test_runtime_scan_keeps_for_break_path_separate_from_loop_else(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "for_break_else.py").write_text(
        """
def reader(value):
    return value.min_models

def scan(config, items):
    handler = external
    for _ in items:
        handler = reader
        break
    else:
        handler = external
    return handler(config.consensus)
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("consensus", "min_models")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset({field})


def test_runtime_scan_keeps_while_break_path_separate_from_loop_else(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "while_break_else.py").write_text(
        """
def reader(value):
    return value.threshold

def scan(config, enabled):
    handler = external
    while enabled:
        handler = reader
        break
    else:
        handler = external
    return handler(config.consensus)
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("consensus", "threshold")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset({field})


def test_runtime_scan_runs_finally_with_terminating_try_else_bindings(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "try_else_finally.py").write_text(
        """
def reader(value):
    return value.diversity_required

def scan(config, risky):
    handler = external
    try:
        risky()
    except LookupError:
        handler = external
    else:
        handler = reader
        raise RuntimeError
    finally:
        handler(config.consensus)
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("consensus", "diversity_required")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset({field})


def test_runtime_scan_distinguishes_loop_control_and_nested_break_paths(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "loop_control_neighbors.py").write_text(
        """
def nested_reader(value):
    return value.satisfaction_threshold

def unreachable_reader(value):
    return value.uncertainty_threshold

def nested_break(config, outer_items, inner_items):
    handler = external
    for _ in outer_items:
        for _ in inner_items:
            handler = nested_reader
            break
        else:
            handler = external
        break
    else:
        handler = external
    handler(config.evaluation)

def continue_exhausts_into_else(config, items):
    handler = external
    for _ in items:
        handler = unreachable_reader
        continue
    else:
        handler = external
    handler(config.evaluation)

def return_and_raise_do_not_reach_after_loop(config, items, enabled):
    handler = external
    for _ in items:
        handler = unreachable_reader
        return None
    else:
        handler = external
    handler(config.evaluation)

    while enabled:
        handler = unreachable_reader
        raise RuntimeError
    else:
        handler = external
    handler(config.evaluation)
""",
        encoding="utf-8",
    )
    nested = contract.ConfigField("evaluation", "satisfaction_threshold")
    unreachable = contract.ConfigField("evaluation", "uncertainty_threshold")

    assert contract.runtime_reads(tmp_path, frozenset({nested, unreachable})) == frozenset({nested})


def test_runtime_scan_does_not_route_return_or_continue_into_except_handlers(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "non_exception_abrupts.py").write_text(
        """
def unreachable_reader(value):
    return value.min_models

def bare_return(config):
    handler = unreachable_reader
    try:
        return
    except Exception:
        pass
    handler(config.consensus)

def for_continue(config, items):
    handler = external
    for _ in items:
        try:
            handler = unreachable_reader
            continue
        except Exception:
            break
        else:
            handler = external
    else:
        handler = external
    handler(config.consensus)

def while_continue(config, enabled):
    handler = external
    while enabled:
        try:
            handler = unreachable_reader
            continue
        except Exception:
            break
        else:
            handler = external
    else:
        handler = external
    handler(config.consensus)
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("consensus", "min_models")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset()


def test_runtime_scan_finally_return_overrides_prior_return_provenance(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "finally_return_authority.py").write_text(
        """
def choose(config, report):
    try:
        return config.consensus
    finally:
        return report.consensus

overridden_read = choose(config, report).min_models

def choose_from_except(config, report, risky):
    try:
        risky()
    except Exception:
        return config.consensus
    else:
        return config.consensus
    finally:
        return report.consensus

overridden_except_read = choose_from_except(config, report, risky).threshold

def preserved(config):
    try:
        return config.consensus
    finally:
        pass

preserved_read = preserved(config).models

def nested_override(config, report):
    try:
        try:
            return config.consensus
        finally:
            return report.consensus
    finally:
        pass

nested_overridden_read = nested_override(config, report).diversity_required
""",
        encoding="utf-8",
    )
    fields = frozenset(
        {
            contract.ConfigField("consensus", "diversity_required"),
            contract.ConfigField("consensus", "min_models"),
            contract.ConfigField("consensus", "models"),
            contract.ConfigField("consensus", "threshold"),
        }
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {contract.ConfigField("consensus", "models")}
    )


def test_runtime_scan_keeps_raise_break_and_system_exit_abrupt_kinds_distinct(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "abrupt_kinds.py").write_text(
        """
def raised_reader(value):
    return value.devil_model

def break_reader(value):
    return value.judge_model

def unreachable_reader(value):
    return value.advocate_model

def explicit_raise(config):
    handler = external
    try:
        raise RuntimeError
    except Exception:
        handler = raised_reader
    handler(config.consensus)

def break_skips_handler_and_else(config, items):
    handler = external
    for _ in items:
        try:
            handler = break_reader
            break
        except Exception:
            handler = external
    else:
        handler = external
    handler(config.consensus)

def system_exit_is_not_exception(config):
    handler = external
    try:
        raise SystemExit
    except Exception:
        handler = unreachable_reader
    handler(config.consensus)

def constructed_system_exit_is_not_exception(config):
    handler = external
    try:
        raise SystemExit()
    except Exception:
        handler = unreachable_reader
    handler(config.consensus)
""",
        encoding="utf-8",
    )
    fields = frozenset(
        {
            contract.ConfigField("consensus", "advocate_model"),
            contract.ConfigField("consensus", "devil_model"),
            contract.ConfigField("consensus", "judge_model"),
        }
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {
            contract.ConfigField("consensus", "devil_model"),
            contract.ConfigField("consensus", "judge_model"),
        }
    )


def test_runtime_scan_consumes_known_exceptions_in_first_matching_handler(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "ordered_known_handlers.py").write_text(
        """
def value_reader(value):
    return value.devil_model

def exception_reader(value):
    return value.judge_model

def tuple_reader(value):
    return value.models

def key_reader(value):
    return value.threshold

def outer_reader(value):
    return value.diversity_required

def unreachable_reader(value):
    return value.min_models

class CustomError(Exception):
    pass

def nested_inner_consumes(config):
    handler = external
    try:
        try:
            raise ValueError
        except Exception:
            pass
    except BaseException:
        handler = unreachable_reader
    handler(config.consensus)

def custom_error_is_consumed_by_base_exception(config):
    handler = external
    try:
        try:
            raise CustomError
        except BaseException:
            pass
    except CustomError:
        handler = unreachable_reader
    handler(config.consensus)

def custom_error_is_consumed_by_bare_handler(config):
    handler = external
    try:
        try:
            raise CustomError
        except:
            pass
    except CustomError:
        handler = unreachable_reader
    handler(config.consensus)

def value_error_uses_first_match(config):
    handler = external
    try:
        raise ValueError
    except ValueError:
        handler = value_reader
    except Exception:
        handler = unreachable_reader
    except:
        handler = unreachable_reader
    handler(config.consensus)

def broader_handler_shadows_later_subclass(config):
    handler = external
    try:
        raise ValueError
    except Exception:
        handler = exception_reader
    except ValueError:
        handler = unreachable_reader
    handler(config.consensus)

def broader_handler_subtracts_unknown_exact_domain(config, error_type):
    handler = external
    try:
        raise error_type
    except Exception:
        handler = exception_reader
    except ValueError:
        handler = unreachable_reader
    except:
        handler = outer_reader
    handler(config.consensus)

def unknown_exact_reraise_keeps_handler_domain(config, error_type):
    handler = external
    try:
        try:
            raise error_type
        except Exception:
            raise
        except:
            pass
    except SystemExit:
        handler = unreachable_reader
    handler(config.consensus)

def tuple_uses_static_subclass_match(config):
    handler = external
    try:
        raise KeyError
    except (ValueError, LookupError):
        handler = tuple_reader
    except KeyError:
        handler = unreachable_reader
    except Exception:
        handler = unreachable_reader
    handler(config.consensus)

def bare_reraise_retains_caught_type(config):
    handler = external
    try:
        try:
            raise KeyError
        except (ValueError, LookupError):
            raise
    except KeyError:
        handler = key_reader
    except LookupError:
        handler = unreachable_reader
    handler(config.consensus)

def handler_raise_bypasses_later_sibling(config):
    handler = external
    try:
        try:
            raise ValueError
        except ValueError:
            raise TypeError
        except TypeError:
            handler = unreachable_reader
    except TypeError:
        handler = outer_reader
    handler(config.consensus)
""",
        encoding="utf-8",
    )
    fields = frozenset(
        {
            contract.ConfigField("consensus", "devil_model"),
            contract.ConfigField("consensus", "diversity_required"),
            contract.ConfigField("consensus", "judge_model"),
            contract.ConfigField("consensus", "min_models"),
            contract.ConfigField("consensus", "models"),
            contract.ConfigField("consensus", "threshold"),
        }
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {
            contract.ConfigField("consensus", "devil_model"),
            contract.ConfigField("consensus", "diversity_required"),
            contract.ConfigField("consensus", "judge_model"),
            contract.ConfigField("consensus", "models"),
            contract.ConfigField("consensus", "threshold"),
        }
    )


def test_runtime_scan_partitions_unknown_exceptions_without_shadowed_handlers(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "ordered_unknown_handlers.py").write_text(
        """
def value_reader(value):
    return value.devil_model

def exception_reader(value):
    return value.judge_model

def bare_reader(value):
    return value.advocate_model

def tuple_reader(value):
    return value.models

def key_reader(value):
    return value.threshold

def lookup_reader(value):
    return value.diversity_required

def unreachable_reader(value):
    return value.min_models

def partitions_all_domains(config, risky):
    handler = external
    try:
        risky()
    except ValueError:
        handler = value_reader
    except Exception:
        handler = exception_reader
    except:
        handler = bare_reader
    handler(config.consensus)

def exception_shadows_value_error(config, risky):
    handler = external
    try:
        risky()
    except Exception:
        handler = exception_reader
    except ValueError:
        handler = unreachable_reader
    except:
        handler = bare_reader
    handler(config.consensus)

def tuple_shadows_key_error(config, risky):
    handler = external
    try:
        risky()
    except (LookupError, ValueError):
        handler = tuple_reader
    except KeyError:
        handler = unreachable_reader
    except Exception:
        handler = exception_reader
    handler(config.consensus)

def unknown_bare_reraise_keeps_caught_domain(config, risky):
    handler = external
    try:
        try:
            risky()
        except LookupError:
            raise
    except KeyError:
        handler = key_reader
    except LookupError:
        handler = lookup_reader
    except Exception:
        handler = exception_reader
    handler(config.consensus)
""",
        encoding="utf-8",
    )
    fields = frozenset(
        {
            contract.ConfigField("consensus", "advocate_model"),
            contract.ConfigField("consensus", "devil_model"),
            contract.ConfigField("consensus", "diversity_required"),
            contract.ConfigField("consensus", "judge_model"),
            contract.ConfigField("consensus", "min_models"),
            contract.ConfigField("consensus", "models"),
            contract.ConfigField("consensus", "threshold"),
        }
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {
            contract.ConfigField("consensus", "advocate_model"),
            contract.ConfigField("consensus", "devil_model"),
            contract.ConfigField("consensus", "diversity_required"),
            contract.ConfigField("consensus", "judge_model"),
            contract.ConfigField("consensus", "models"),
            contract.ConfigField("consensus", "threshold"),
        }
    )


def test_runtime_scan_threads_bindings_through_except_star_subgroups(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "except_star_subgroups.py").write_text(
        """
def reader(value):
    return value.min_models

def sequential_subgroups(config, risky):
    handler = external
    try:
        risky()
    except* ValueError:
        handler = reader
    except* TypeError:
        handler(config.consensus)
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("consensus", "min_models")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset({field})


def test_runtime_scan_stops_after_unconditional_exit_but_keeps_dynamic_fallthrough(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "terminators.py").write_text(
        """
def direct_return(config):
    return None
    dead_read = config.evaluation.satisfaction_threshold

def direct_raise(config):
    raise RuntimeError
    dead_read = config.consensus.min_models

def both_branches_exit(config, enabled):
    if enabled:
        return None
    else:
        raise RuntimeError
    dead_read = config.consensus.threshold

def dynamic_fallthrough(config, enabled):
    if enabled:
        return None
    live_read = config.evaluation.uncertainty_threshold

def terminating_alias_path(config, report, enabled):
    section = report.evaluation
    if enabled:
        section = config.evaluation
        return None
    unrelated_fallthrough = section.stage1_enabled
""",
        encoding="utf-8",
    )
    fields = frozenset(
        {
            contract.ConfigField("evaluation", "satisfaction_threshold"),
            contract.ConfigField("evaluation", "stage1_enabled"),
            contract.ConfigField("evaluation", "uncertainty_threshold"),
            contract.ConfigField("consensus", "min_models"),
            contract.ConfigField("consensus", "threshold"),
        }
    )

    assert contract.runtime_reads(tmp_path, fields) == frozenset(
        {contract.ConfigField("evaluation", "uncertainty_threshold")}
    )


def test_runtime_scan_models_exact_sys_exit_aliases_without_shadowing_overreach(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "sys_exit_paths.py").write_text(
        """
import sys
import sys as system
from sys import exit as stop

def module_exit(config):
    sys.exit(0)
    return config.evaluation.stage1_enabled

def aliased_module_exit(config):
    system.exit(0)
    return config.evaluation.stage2_enabled

def imported_exit(config):
    stop(0)
    return config.evaluation.stage3_enabled

def shadowed_module(config, sys):
    sys.exit(0)
    return config.evaluation.stage4_enabled

def shadowed_import(config):
    stop = lambda code: code
    stop(0)
    return config.evaluation.stage5_enabled

try:
    sys.exit(0)
except SystemExit:
    caught = config.evaluation.stage6_enabled
""",
        encoding="utf-8",
    )
    fields = frozenset(
        contract.ConfigField("evaluation", f"stage{index}_enabled") for index in range(1, 7)
    )

    assert contract.runtime_reads(tmp_path, fields) == {
        contract.ConfigField("evaluation", "stage4_enabled"),
        contract.ConfigField("evaluation", "stage5_enabled"),
        contract.ConfigField("evaluation", "stage6_enabled"),
    }


def test_every_schema_field_needs_exactly_one_disposition(contract) -> None:
    active = contract.ConfigField("evaluation", "active")
    inert = contract.ConfigField("evaluation", "inert")
    schema_only = contract.ConfigField("evaluation", "schema_only")
    fields = frozenset({active, inert, schema_only})
    rows = {
        active: contract.ReferenceRow("true", "Active runtime control."),
        inert: contract.ReferenceRow(
            "true", "Currently inert. Effective control: RuntimeConfig.inert."
        ),
        schema_only: contract.ReferenceRow("true", "Compatibility-only schema value."),
    }
    markers = {
        inert: contract.InertMarker(inert, "RuntimeConfig.inert"),
    }

    report = contract.audit_contract(
        fields=fields,
        reads=frozenset({active}),
        rows=rows,
        markers=markers,
        allowlist={schema_only: "Retained to read configuration written by release 0.1.0."},
        documented_defaults={},
    )

    assert report.violations == ()


def test_new_unwired_field_fails_with_actionable_name(contract) -> None:
    field = contract.ConfigField("evaluation", "new_knob")

    report = contract.audit_contract(
        fields=frozenset({field}),
        reads=frozenset(),
        rows={field: contract.ReferenceRow("true", "A new control.")},
        markers={},
        allowlist={},
        documented_defaults={},
    )

    assert report.violations == (
        "evaluation.new_knob: no production read, inert documentation, or schema-only rationale",
    )


def test_wired_field_cannot_remain_documented_inert(contract) -> None:
    field = contract.ConfigField("consensus", "threshold")

    report = contract.audit_contract(
        fields=frozenset({field}),
        reads=frozenset({field}),
        rows={
            field: contract.ReferenceRow(
                "0.67", "Currently inert. Effective control: majority_threshold."
            )
        },
        markers={field: contract.InertMarker(field, "majority_threshold")},
        allowlist={},
        documented_defaults={},
    )

    assert "consensus.threshold: conflicting config-field dispositions" in report.violations
    assert (
        "consensus.threshold: production-wired field is still documented inert" in report.violations
    )


def test_visible_inert_claim_requires_structured_effective_control(contract) -> None:
    field = contract.ConfigField("consensus", "min_models")

    report = contract.audit_contract(
        fields=frozenset({field}),
        reads=frozenset(),
        rows={field: contract.ReferenceRow("3", "Currently inert.")},
        markers={field: contract.InertMarker(field, "two successful votes")},
        allowlist={},
        documented_defaults={},
    )

    assert "consensus.min_models: inert docs do not label the effective control" in (
        report.violations
    )
    assert any("visible docs omit marker effective control" in item for item in report.violations)


def test_reference_parser_uses_sections_and_structured_json_markers(contract) -> None:
    text = """
## `evaluation`

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `stage1_enabled` | `bool` | `true` | **Currently inert. Effective control:** `PipelineConfig.stage1_enabled`. |

<!-- config-field-contract: {"section":"evaluation","field":"stage1_enabled","status":"inert","effective_control":"PipelineConfig.stage1_enabled"} -->
"""
    field = contract.ConfigField("evaluation", "stage1_enabled")

    assert contract.parse_reference_rows(text)[field].default == "`true`"
    assert contract.parse_inert_markers(text)[field].effective_control == (
        "PipelineConfig.stage1_enabled"
    )


def test_opus_defaults_preserve_direct_and_openrouter_formats(contract) -> None:
    assert (
        contract.opus_default_violations("claude-opus-4-8", "openrouter/anthropic/claude-opus-4.8")
        == ()
    )

    assert (
        contract.opus_default_violations("claude-opus-5", "openrouter/anthropic/claude-opus-5")
        == ()
    )

    violations = contract.opus_default_violations("claude-opus-4-8", "claude-opus-4-8")

    assert violations == (
        "Opus defaults must use distinct direct and OpenRouter identifiers",
        "consensus.advocate_model: invalid OpenRouter Opus default 'claude-opus-4-8'; "
        "expected 'openrouter/anthropic/claude-opus-<major>' or "
        "'openrouter/anthropic/claude-opus-<major>.<minor>'",
    )


@pytest.mark.parametrize(
    ("direct_model", "consensus_model", "expected_fragments"),
    [
        (
            "claude-opus-4.8",
            "claude-opus-4.8",
            ("distinct", "invalid direct", "invalid OpenRouter"),
        ),
        (
            "claude-opus-",
            "claude-opus-",
            ("distinct", "invalid direct", "invalid OpenRouter"),
        ),
        (
            "claude-opus-4-8",
            "claude-opus-4-8",
            ("distinct", "invalid OpenRouter"),
        ),
        (
            "claude-opus-4-8",
            "openrouter/anthropic/claude-opus-4-8",
            ("invalid OpenRouter",),
        ),
        (
            "claude-opus-4-8-extra",
            "openrouter/anthropic/claude-opus-4.8",
            ("invalid direct",),
        ),
    ],
)
def test_opus_defaults_reject_collapsed_or_malformed_formats(
    contract,
    direct_model: str,
    consensus_model: str,
    expected_fragments: tuple[str, ...],
) -> None:
    violations = contract.opus_default_violations(direct_model, consensus_model)

    assert all(
        any(fragment in violation for violation in violations) for fragment in expected_fragments
    )


def test_opus_defaults_reject_mismatched_valid_versions(contract) -> None:
    violations = contract.opus_default_violations(
        "claude-opus-4-8", "openrouter/anthropic/claude-opus-4.9"
    )

    assert violations == (
        "consensus.advocate_model: OpenRouter Opus default "
        "'openrouter/anthropic/claude-opus-4.9' does not correspond to direct default "
        "'claude-opus-4-8'; expected 'openrouter/anthropic/claude-opus-4.8'",
    )


def test_documented_default_must_match_its_ssot_value(contract) -> None:
    field = contract.ConfigField("evaluation", "semantic_model")

    report = contract.audit_contract(
        fields=frozenset({field}),
        reads=frozenset({field}),
        rows={field: contract.ReferenceRow('"claude-opus-4-6"', "Wired model.")},
        markers={},
        allowlist={},
        documented_defaults={field: "claude-opus-4-8"},
    )

    assert report.violations == (
        "evaluation.semantic_model: documented default 'claude-opus-4-6' "
        "does not match 'claude-opus-4-8'",
    )


def test_public_model_selection_reads_current_dynamic_paths(contract) -> None:
    reads = contract._public_model_selection_reads(contract.schema_fields())

    assert {field.dotted for field in reads} == {
        "evaluation.semantic_model",
        "evaluation.assertion_extraction_model",
        "consensus.models",
        "consensus.advocate_model",
        "consensus.devil_model",
        "consensus.judge_model",
    }


def test_public_model_selection_probe_rejects_disconnected_config_reads(
    contract, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ouroboros.config import model_selection

    monkeypatch.setattr(model_selection, "_config_value", lambda _config, _path: "auto")

    assert contract._public_model_selection_reads(contract.schema_fields()) == frozenset()


def test_schema_default_parser_handles_provider_fallback_cell(contract) -> None:
    assert (
        contract._literal_default('`"auto"` (`litellm`: `"openrouter/anthropic/claude-opus-5.5"`)')
        == "auto"
    )


def test_current_consensus_fallback_tracks_source_identity(
    contract, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        contract, "runtime_reads", lambda _source_root, _fields: contract._RuntimeReads()
    )
    reference = (REPO_ROOT / "docs/config-reference.md").read_text()
    document = tmp_path / "docs/config-reference.md"
    document.parent.mkdir()
    document.write_text(reference)

    assert not contract.audit_repository(tmp_path).violations

    document.write_text(
        reference.replace(
            contract.DEFAULT_CONSENSUS_OPUS_MODEL,
            "openrouter/anthropic/claude-opus-0.0",
        )
    )
    assert any(
        "documented litellm fallback" in violation
        for violation in contract.audit_repository(tmp_path).violations
    )


def test_deep_container_join_preserves_root_depth(contract, tmp_path: Path) -> None:
    field = contract.ConfigField("evaluation", "stage1_enabled")
    wrapper = "[" * 9 + "config" + "]" * 9
    for indexes, expected in ((8, False), (9, True)):
        source = (
            "def consume(config):\n"
            "    if condition:\n"
            f"        box = {wrapper}\n"
            "    else:\n"
            f"        box = {wrapper}\n"
            f"    return box{'[0]' * indexes}.evaluation.stage1_enabled\n"
        )
        (tmp_path / "deep.py").write_text(source)
        reads = contract.runtime_reads(tmp_path, frozenset({field}))
        assert (field in reads) is expected
        assert not reads.unresolved_semantics


# --- Blocker 1 regression: transitive callable relevance for unresolved decorators ---


def test_unresolved_decorator_fails_closed_with_transitive_callable_relevance(
    contract, tmp_path: Path
) -> None:
    """Blocker 1 repro: an unresolved decorator on a class whose __eq__ calls
    a private helper that reads config must produce an unresolved-semantics
    violation. The class body itself does not directly mention config names,
    but the helper _read() does.
    """
    (tmp_path / "transitive_read.py").write_text(
        """
from external_package import total_ordering

def _read(settings):
    return settings.evaluation.stage1_enabled

@total_ordering
class Reader:
    def __eq__(self, other):
        return _read(self.settings)

Reader() == Reader()
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    reads = contract.runtime_reads(tmp_path, frozenset({field}))

    # The read should not be counted (decorator is unresolved and could
    # replace the class), but the unresolved semantics must be recorded.
    report = contract.audit_contract(
        fields=frozenset({field}),
        reads=reads,
        rows={
            field: contract.ReferenceRow(
                "true", "Currently inert. Effective control: runtime.stage1_enabled."
            )
        },
        markers={field: contract.InertMarker(field, "runtime.stage1_enabled")},
        allowlist={},
        documented_defaults={},
    )
    assert any("unresolved decorator" in v for v in report.violations), (
        f"Expected unresolved decorator violation for transitive callable relevance, "
        f"got: {report.violations}"
    )


def test_unresolved_decorator_fails_closed_with_indirect_method_call(
    contract, tmp_path: Path
) -> None:
    """Blocker 1 variant: unresolved decorator on a function that calls
    another function that reads config — the decorated function itself only
    contains a call expression, not a direct config mention.
    """
    (tmp_path / "indirect_method.py").write_text(
        """
from unknown_package import maybe_replace

def _load_settings(cfg):
    return cfg.consensus.min_models

@maybe_replace
def process(config):
    return _load_settings(config)

process(config)
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("consensus", "min_models")

    reads = contract.runtime_reads(tmp_path, frozenset({field}))

    report = contract.audit_contract(
        fields=frozenset({field}),
        reads=reads,
        rows={
            field: contract.ReferenceRow(
                "true", "Currently inert. Effective control: direct model count."
            )
        },
        markers={field: contract.InertMarker(field, "direct model count")},
        allowlist={},
        documented_defaults={},
    )
    assert any("unresolved decorator" in v for v in report.violations), (
        f"Expected unresolved decorator violation for indirect method call, "
        f"got: {report.violations}"
    )


def test_unresolved_decorator_uses_callsite_section_provenance(contract, tmp_path: Path) -> None:
    (tmp_path / "decorated_section_reader.py").write_text(
        """
from external_package import preserve_or_replace

@preserve_or_replace
def read_stage(section):
    return section.stage1_enabled

read_stage(config.evaluation)
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    reads = contract.runtime_reads(tmp_path, frozenset({field}))
    report = _audit_as_documented_inert(contract, field, reads)

    assert reads == frozenset()
    assert any(
        "unresolved decorator 'preserve_or_replace'" in violation for violation in report.violations
    )


def test_unresolved_decorator_ignores_untracked_callsite_arguments(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "decorated_plain_reader.py").write_text(
        """
from external_package import preserve_or_replace

@preserve_or_replace
def read_stage(section):
    return section.stage1_enabled

read_stage(report.evaluation)
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    reads = contract.runtime_reads(tmp_path, frozenset({field}))
    report = _audit_as_documented_inert(contract, field, reads)

    assert reads == frozenset()
    assert not any("unresolved decorator" in violation for violation in report.violations)


def test_unresolved_decorator_follows_callable_alias_relevance(contract, tmp_path: Path) -> None:
    (tmp_path / "aliased_helper.py").write_text(
        """
from external_package import preserve_or_replace

def _helper(config):
    return config.evaluation.stage1_enabled

_alias = _helper

@preserve_or_replace
def process(config):
    return _alias(config)
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    reads = contract.runtime_reads(tmp_path, frozenset({field}))
    report = _audit_as_documented_inert(contract, field, reads)

    assert reads == frozenset()
    assert any(
        "unresolved decorator 'preserve_or_replace'" in violation for violation in report.violations
    )


def test_unresolved_decorator_resolves_method_receiver_before_relevance(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "same_method_name.py").write_text(
        """
from external_package import preserve_or_replace

class _ConfigReader:
    def read(self, config):
        return config.evaluation.stage1_enabled

class _Harmless:
    def read(self):
        return 42

@preserve_or_replace
def process():
    return _Harmless().read()
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    reads = contract.runtime_reads(tmp_path, frozenset({field}))
    report = _audit_as_documented_inert(contract, field, reads)

    assert reads == frozenset()
    assert not any("unresolved decorator" in violation for violation in report.violations)


def test_unresolved_decorator_follows_proven_method_receiver(contract, tmp_path: Path) -> None:
    (tmp_path / "relevant_method_receiver.py").write_text(
        """
from external_package import preserve_or_replace

class _Reader:
    def read(self, config):
        return config.evaluation.stage1_enabled

@preserve_or_replace
def process(config):
    return _Reader().read(config)
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    reads = contract.runtime_reads(tmp_path, frozenset({field}))
    report = _audit_as_documented_inert(contract, field, reads)

    assert reads == frozenset()
    assert any(
        "unresolved decorator 'preserve_or_replace'" in violation for violation in report.violations
    )


# --- Blocker 2 regression: no basename-only semantics-preserving trust ---


def test_unresolved_external_dataclass_import_does_not_preserve_reads(
    contract, tmp_path: Path
) -> None:
    """Blocker 2 repro: `from external import dataclass` must not be trusted
    by basename alone. An external module could validly replace the class with
    something that performs no read.
    """
    (tmp_path / "fake_dataclass.py").write_text(
        """
from external_package import dataclass

@dataclass
class Reader:
    def read(self, config):
        return config.evaluation.stage2_enabled

Reader().read(config)
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage2_enabled")

    reads = contract.runtime_reads(tmp_path, frozenset({field}))

    # The read should NOT be counted — decorator is unresolved from unknown package
    assert field not in reads
    report = contract.audit_contract(
        fields=frozenset({field}),
        reads=reads,
        rows={
            field: contract.ReferenceRow(
                "true", "Currently inert. Effective control: runtime.stage1_enabled."
            )
        },
        markers={field: contract.InertMarker(field, "runtime.stage1_enabled")},
        allowlist={},
        documented_defaults={},
    )
    # Must flag unresolved decorator since the class has config-relevant methods
    assert any("unresolved decorator 'dataclass'" in v for v in report.violations), (
        f"Expected unresolved decorator violation for untrusted basename import, "
        f"got: {report.violations}"
    )


def test_unresolved_external_command_import_does_not_preserve_reads(
    contract, tmp_path: Path
) -> None:
    """Blocker 2 variant: `from unknown import command` must not be treated as
    semantics-preserving without proven identity.
    """
    (tmp_path / "fake_command.py").write_text(
        """
from unknown_framework import command

@command
def cli_handler(config):
    return config.evaluation.stage1_enabled

cli_handler(settings)
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    reads = contract.runtime_reads(tmp_path, frozenset({field}))

    assert field not in reads
    report = contract.audit_contract(
        fields=frozenset({field}),
        reads=reads,
        rows={
            field: contract.ReferenceRow(
                "true", "Currently inert. Effective control: runtime.stage1_enabled."
            )
        },
        markers={field: contract.InertMarker(field, "runtime.stage1_enabled")},
        allowlist={},
        documented_defaults={},
    )
    assert any("unresolved decorator 'command'" in v for v in report.violations), (
        f"Expected unresolved decorator violation for untrusted 'command' import, "
        f"got: {report.violations}"
    )


# --- Decorator provenance regressions ---


def test_proven_dataclass_import_preserves_class_reads(contract, tmp_path: Path) -> None:
    """Overreach-negative: `from dataclasses import dataclass` has proven
    identity and must NOT trigger unresolved-semantics violations.
    """
    (tmp_path / "real_dataclass.py").write_text(
        """
from dataclasses import dataclass

@dataclass
class Config:
    stage1_enabled: bool = True
    def read(self, settings):
        return settings.evaluation.stage1_enabled

Config().read(settings)
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    reads = contract.runtime_reads(tmp_path, frozenset({field}))

    # Proven identity: reads should be detected and no unresolved violation
    assert field in reads
    report = contract.audit_contract(
        fields=frozenset({field}),
        reads=reads,
        rows={field: contract.ReferenceRow("true", "Wired.")},
        markers={},
        allowlist={},
        documented_defaults={},
    )
    assert not any("unresolved" in v for v in report.violations), (
        f"Proven dataclass import should not produce unresolved violations: {report.violations}"
    )


def test_unresolved_attribute_style_replacement_decorator_fails_closed(
    contract, tmp_path: Path
) -> None:
    """An unresolved attribute factory may replace the decorated function."""
    (tmp_path / "attribute_replacement.py").write_text(
        """
import external_framework

app = external_framework.App()

@app.command()
def handler(config):
    return config.evaluation.stage1_enabled

handler(settings)
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    reads = contract.runtime_reads(tmp_path, frozenset({field}))

    assert field not in reads
    report = contract.audit_contract(
        fields=frozenset({field}),
        reads=reads,
        rows={field: contract.ReferenceRow("true", "Wired.")},
        markers={},
        allowlist={},
        documented_defaults={},
    )
    assert any("unresolved decorator 'command'" in v for v in report.violations)


def test_unresolved_attribute_style_class_replacement_decorator_fails_closed(
    contract, tmp_path: Path
) -> None:
    """An unresolved attribute factory may replace a config-reading class."""
    (tmp_path / "attribute_class_replacement.py").write_text(
        """
import external_framework

registry = external_framework.Registry()

@registry.register()
class Reader:
    def read(self, config):
        return config.evaluation.stage2_enabled

Reader().read(settings)
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage2_enabled")

    reads = contract.runtime_reads(tmp_path, frozenset({field}))

    assert field not in reads
    report = contract.audit_contract(
        fields=frozenset({field}),
        reads=reads,
        rows={field: contract.ReferenceRow("true", "Wired.")},
        markers={},
        allowlist={},
        documented_defaults={},
    )
    assert any("unresolved decorator 'register'" in v for v in report.violations)


def test_proven_local_attribute_identity_decorator_preserves_reads(
    contract, tmp_path: Path
) -> None:
    """Exactly resolved attribute factories may preserve the original function."""
    (tmp_path / "attribute_identity.py").write_text(
        """
class Framework:
    def command(self):
        def preserve(function):
            return function
        return preserve

app = Framework()

@app.command()
def handler(config):
    return config.evaluation.stage1_enabled

handler(settings)
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    reads = contract.runtime_reads(tmp_path, frozenset({field}))

    assert reads == frozenset({field})
    report = contract.audit_contract(
        fields=frozenset({field}),
        reads=reads,
        rows={field: contract.ReferenceRow("true", "Wired.")},
        markers={},
        allowlist={},
        documented_defaults={},
    )
    assert not any("unresolved decorator" in v for v in report.violations)


def test_proven_local_attribute_replacement_decorator_erases_read(contract, tmp_path: Path) -> None:
    """Resolved attribute factories must honor a replacement callable."""
    (tmp_path / "attribute_replacement_resolved.py").write_text(
        """
class Framework:
    def command(self):
        def replace(function):
            return lambda *args, **kwargs: None
        return replace

app = Framework()

@app.command()
def handler(config):
    return config.evaluation.stage1_enabled

handler(settings)
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    assert contract.runtime_reads(tmp_path, frozenset({field})) == frozenset()


def test_proven_pydantic_field_validator_preserves_method_reads(contract, tmp_path: Path) -> None:
    """Overreach-negative: `from pydantic import field_validator` has proven
    identity and must preserve the decorated method's reads.
    """
    (tmp_path / "pydantic_model.py").write_text(
        """
from pydantic import field_validator

class Model:
    @field_validator("value")
    @classmethod
    def validate_value(cls, v, config):
        return config.evaluation.stage1_enabled

Model.validate_value(None, None, settings)
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    reads = contract.runtime_reads(tmp_path, frozenset({field}))

    report = contract.audit_contract(
        fields=frozenset({field}),
        reads=reads,
        rows={field: contract.ReferenceRow("true", "Wired.")},
        markers={},
        allowlist={},
        documented_defaults={},
    )
    assert not any("unresolved" in v for v in report.violations), (
        f"Proven pydantic import should not produce unresolved violations: {report.violations}"
    )


def test_no_transitive_relevance_means_no_unresolved_violation(contract, tmp_path: Path) -> None:
    """Overreach-negative: unresolved decorator on a function that has NO
    transitive callable relevance to config fields must NOT produce a violation.
    """
    (tmp_path / "irrelevant_decorator.py").write_text(
        """
from unknown_package import some_decorator

@some_decorator
def totally_unrelated():
    return 42

totally_unrelated()
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    reads = contract.runtime_reads(tmp_path, frozenset({field}))

    report = contract.audit_contract(
        fields=frozenset({field}),
        reads=reads,
        rows={
            field: contract.ReferenceRow(
                "true", "Currently inert. Effective control: runtime.stage1_enabled."
            )
        },
        markers={field: contract.InertMarker(field, "runtime.stage1_enabled")},
        allowlist={},
        documented_defaults={},
    )
    assert not any("unresolved" in v for v in report.violations), (
        f"Irrelevant decorated function should not produce unresolved violations: "
        f"{report.violations}"
    )


def test_runtime_scan_does_not_infer_root_from_foreign_config_annotation(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "foreign_annotation.py").write_text(
        """
class ForeignConfig:
    pass

def inspect(config: ForeignConfig):
    return config.evaluation.stage1_enabled
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    reads = contract.runtime_reads(tmp_path, frozenset({field}))

    assert reads == frozenset()


def test_runtime_scan_preserves_constructor_assigned_callable_provenance(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "constructor_callback.py").write_text(
        """
def helper(value):
    return value.evaluation.stage1_enabled

class Reader:
    def __init__(self):
        self.callback = helper

    def run(self, value):
        return self.callback(value)

Reader().run(settings)
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    reads = contract.runtime_reads(tmp_path, frozenset({field}))

    assert reads == frozenset({field})


def test_unresolved_decorator_ignores_unproven_section_attribute(contract, tmp_path: Path) -> None:
    (tmp_path / "unrelated_decorated_domain.py").write_text(
        """
from external_package import decorate

@decorate
def render(report):
    return report.evaluation
""",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    reads = contract.runtime_reads(tmp_path, frozenset({field}))
    report = _audit_as_documented_inert(contract, field, reads)

    assert reads == frozenset()
    assert not any("unresolved decorator" in violation for violation in report.violations)


@pytest.mark.parametrize("parameter", ["value", "config"])
@pytest.mark.parametrize("argument", ["Report()", "settings"])
@pytest.mark.parametrize(
    "shape",
    ["immediate", "assigned", "nested", "alias", "import_factory", "import_reader", "depth"],
)
def test_returned_callable_preserves_actual_argument_provenance(
    contract, tmp_path: Path, parameter: str, argument: str, shape: str
) -> None:
    reader = f"def reader({parameter}):\n    return {parameter}.evaluation.stage1_enabled\n"
    factory = "def get():\n    return reader\n"
    prefix = (
        "class EvaluationReport:\n    stage1_enabled = True\n"
        "class Report:\n    evaluation = EvaluationReport()\n"
    )
    call = f"get()({argument})\n"
    if shape == "assigned":
        call = f"callback = get()\ncallback({argument})\n"
    elif shape == "nested":
        factory = "def get():\n" + "".join("    " + line for line in reader.splitlines(True))
        factory += "    return reader\n"
        reader = ""
    elif shape == "alias":
        factory = "def get():\n    callback = reader\n    return callback\n"
    elif shape == "import_factory":
        (tmp_path / "a_reader.py").write_text(reader + factory)
        reader, factory = "from a_reader import get\n", ""
    elif shape == "import_reader":
        (tmp_path / "z_reader.py").write_text(reader)
        reader = "from z_reader import reader\n"
    elif shape == "depth":
        factory = "def factory0():\n    return reader\n"
        for depth in range(1, 13):
            factory += f"def factory{depth}():\n    return factory{depth - 1}()\n"
        factory += "def get():\n    return factory12()\n"
    (tmp_path / "consumer.py").write_text(prefix + reader + factory + call)
    field = contract.ConfigField("evaluation", "stage1_enabled")

    reads = contract.runtime_reads(tmp_path, frozenset({field}))

    assert reads == (frozenset() if argument == "Report()" else frozenset({field}))
    assert not reads.unresolved_semantics
    report = _audit_as_documented_inert(contract, field, reads)
    assert bool(report.violations) == (argument != "Report()")


@pytest.mark.parametrize("reader_module", ["a_reader", "z_reader"])
@pytest.mark.parametrize("reverse_calls", [False, True])
@pytest.mark.parametrize("other", ["settings", "unavailable"])
@pytest.mark.parametrize("assigned", [False, True])
def test_imported_returned_callable_keeps_mixed_argument_alternatives(
    contract, tmp_path: Path, reader_module: str, reverse_calls: bool, other: str, assigned: bool
) -> None:
    (tmp_path / f"{reader_module}.py").write_text(
        "def reader(config):\n    return config.evaluation.stage1_enabled\n"
        "def get():\n    return reader\n"
    )
    calls = ["callback(Report())\n", f"callback({other})\n"]
    if reverse_calls:
        calls.reverse()
    (tmp_path / "consumer.py").write_text(
        f"from {reader_module} import get\n"
        "class EvaluationReport:\n    stage1_enabled = True\n"
        "class Report:\n    evaluation = EvaluationReport()\n"
        + ("callback = get()\n" if assigned else "")
        + "".join(calls).replace("callback(", "callback(" if assigned else "get()(")
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    reads = contract.runtime_reads(tmp_path, frozenset({field}))

    assert reads == frozenset({field})
    assert _audit_as_documented_inert(contract, field, reads).violations


@pytest.mark.parametrize("condition", ["unknown", "True", "False"])
def test_returned_callable_join_uses_reachable_binding(
    contract, tmp_path: Path, condition: str
) -> None:
    (tmp_path / "consumer.py").write_text(
        "def reader(value):\n    return value.evaluation.stage1_enabled\n"
        "def ignore(value):\n    return False\n"
        "def get():\n"
        f"    return reader if {condition} else ignore\n"
        "get()(settings)\n"
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    reads = contract.runtime_reads(tmp_path, frozenset({field}))

    assert reads == (frozenset() if condition == "False" else frozenset({field}))


@pytest.mark.parametrize("argument", ["Report()", "settings"])
def test_bound_method_factory_preserves_returned_callable(
    contract, tmp_path: Path, argument: str
) -> None:
    (tmp_path / "consumer.py").write_text(
        "class EvaluationReport:\n    stage1_enabled = True\n"
        "class Report:\n    evaluation = EvaluationReport()\n"
        "class Reader:\n"
        "    def read(self, value):\n        return value.evaluation.stage1_enabled\n"
        "    def get(self):\n        return self.read\n"
        f"Reader().get()({argument})\n"
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    reads = contract.runtime_reads(tmp_path, frozenset({field}))

    assert reads == (frozenset() if argument == "Report()" else frozenset({field}))


@pytest.mark.parametrize("module_name", ["a_reader", "z_reader"])
@pytest.mark.parametrize("argument", ["Report()", "settings"])
@pytest.mark.parametrize("receiver", ["Reader", "OtherReader"])
@pytest.mark.parametrize("assigned", [False, True])
def test_imported_bound_callable_keeps_receiver_and_defining_module(
    contract, tmp_path: Path, module_name: str, argument: str, receiver: str, assigned: bool
) -> None:
    (tmp_path / f"{module_name}.py").write_text(
        "def _read(value):\n    return value.evaluation.stage1_enabled\n"
        "class Reader:\n"
        "    def _read(self, config):\n        return _read(config)\n"
        "    def get(self):\n        return self._read\n"
        "class OtherReader:\n"
        "    def _read(self, value):\n        return True\n"
        "    def get(self):\n        return self._read\n"
    )
    (tmp_path / "consumer.py").write_text(
        f"from {module_name} import Reader, OtherReader\n"
        "def _read(value):\n    return None\n"
        "class EvaluationReport:\n    stage1_enabled = True\n"
        "class Report:\n    evaluation = EvaluationReport()\n"
        + (
            f"callback = {receiver}().get()\ncallback({argument})\n"
            if assigned
            else f"{receiver}().get()({argument})\n"
        )
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    reads = contract.runtime_reads(tmp_path, frozenset({field}))

    expected = receiver == "Reader" and argument == "settings"
    assert reads == (frozenset({field}) if expected else frozenset())
    assert not reads.unresolved_semantics
    assert bool(_audit_as_documented_inert(contract, field, reads).violations) == expected


@pytest.mark.parametrize("argument", ["Report()", "settings"])
@pytest.mark.parametrize("imported", [False, True])
@pytest.mark.parametrize(
    ("expression", "module_setup", "local_setup"),
    [
        ("(reader,)[0]", "", ""),
        ("[reader][0]", "", ""),
        ("{'reader': reader}['reader']", "", ""),
        ("reader or None", "", ""),
        ("True and reader", "", ""),
        ("callbacks[0]", "callbacks = [reader]\n", ""),
        ("callbacks['reader']", "callbacks = {'reader': reader}\n", ""),
        ("callbacks[0]", "", "    callbacks = [reader]\n"),
        ("callbacks['reader']", "", "    callbacks = {'reader': reader}\n"),
        ("{'nested': [(reader,)]}['nested'][0][0]", "", ""),
        ("(callback := reader)", "", ""),
        ("[*callbacks][0]", "callbacks = [reader]\n", ""),
    ],
)
def test_compound_return_expression_preserves_callable_provenance(
    contract,
    tmp_path: Path,
    argument: str,
    imported: bool,
    expression: str,
    module_setup: str,
    local_setup: str,
) -> None:
    source = (
        "def reader(config):\n    return config.evaluation.stage1_enabled\n"
        + module_setup
        + "def get():\n"
        + local_setup
        + f"    return {expression}\n"
    )
    if imported:
        (tmp_path / "owner.py").write_text(source)
        source = "from owner import get\n"
    (tmp_path / "consumer.py").write_text(
        "class EvaluationReport:\n    stage1_enabled = True\n"
        "class Report:\n    evaluation = EvaluationReport()\n"
        + source
        + f"callback = get()\ncallback({argument})\n"
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    reads = contract.runtime_reads(tmp_path, frozenset({field}))

    expected = argument == "settings"
    assert reads == (frozenset({field}) if expected else frozenset())
    assert not reads.unresolved_semantics
    assert bool(_audit_as_documented_inert(contract, field, reads).violations) == expected


@pytest.mark.parametrize("argument", ["Report()", "settings"])
@pytest.mark.parametrize("expression", ["callbacks[0]", "forward(callbacks)[0]"])
def test_callable_container_arguments_survive_return_gate(
    contract, tmp_path: Path, argument: str, expression: str
) -> None:
    (tmp_path / "consumer.py").write_text(
        "class EvaluationReport:\n    stage1_enabled = True\n"
        "class Report:\n    evaluation = EvaluationReport()\n"
        "def reader(config):\n    return config.evaluation.stage1_enabled\n"
        "def forward(values):\n    return values\n"
        f"def get(callbacks):\n    return {expression}\n"
        f"get([reader])({argument})\n"
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    reads = contract.runtime_reads(tmp_path, frozenset({field}))

    assert reads == (frozenset({field}) if argument == "settings" else frozenset())


@pytest.mark.parametrize(
    "factory_name",
    [
        "build_report",
        "build_config",
        "create_settings",
        "get_configuration",
        "load_config",
        "read_settings",
        "resolve_config",
    ],
)
@pytest.mark.parametrize("returned", ["Report()", "settings"])
@pytest.mark.parametrize("route", ["immediate", "assigned", "imported"])
def test_resolved_factory_return_provenance_precedes_name_convention(
    contract, tmp_path: Path, factory_name: str, returned: str, route: str
) -> None:
    source = (
        "class EvaluationReport:\n    stage1_enabled = True\n"
        "class Report:\n    evaluation = EvaluationReport()\n"
        f"def {factory_name}():\n    return {returned}\n"
    )
    call = f"{factory_name}()"
    if route == "imported":
        (tmp_path / "factory.py").write_text(source)
        source = f"from factory import {factory_name}\n"
    if route == "assigned":
        source += f"loaded = {call}\n"
        call = "loaded"
    (tmp_path / "consumer.py").write_text(source + f"result = {call}.evaluation.stage1_enabled\n")
    field = contract.ConfigField("evaluation", "stage1_enabled")

    reads = contract.runtime_reads(tmp_path, frozenset({field}))

    expected = returned == "settings"
    assert reads == (frozenset({field}) if expected else frozenset())
    assert not reads.unresolved_semantics
    assert bool(_audit_as_documented_inert(contract, field, reads).violations) == expected


@pytest.mark.parametrize("resolved", [False, True])
def test_unknown_config_factory_keeps_name_convention(
    contract, tmp_path: Path, resolved: bool
) -> None:
    (tmp_path / "consumer.py").write_text(
        ("def load_config():\n    return unknown_result\n" if resolved else "")
        + "result = load_config().evaluation.stage1_enabled\n"
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    reads = contract.runtime_reads(tmp_path, frozenset({field}))

    assert reads == frozenset({field})


@pytest.mark.parametrize("argument", ["Report()", "settings"])
def test_factory_name_keeps_returned_callable_instead_of_root(
    contract, tmp_path: Path, argument: str
) -> None:
    (tmp_path / "consumer.py").write_text(
        "class EvaluationReport:\n    stage1_enabled = True\n"
        "class Report:\n    evaluation = EvaluationReport()\n"
        "def reader(config):\n    return config.evaluation.stage1_enabled\n"
        "def load_config():\n    return reader\n"
        f"callback = load_config()\ncallback({argument})\n"
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    reads = contract.runtime_reads(tmp_path, frozenset({field}))

    assert reads == (frozenset({field}) if argument == "settings" else frozenset())


@pytest.mark.parametrize("alternative", ["settings", "unknown_result"])
def test_named_factory_keeps_possible_config_return_alternative(
    contract, tmp_path: Path, alternative: str
) -> None:
    (tmp_path / "consumer.py").write_text(
        "class EvaluationReport:\n    stage1_enabled = True\n"
        "class Report:\n    evaluation = EvaluationReport()\n"
        f"def load_config():\n    return Report() if unknown else {alternative}\n"
        "result = load_config().evaluation.stage1_enabled\n"
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    reads = contract.runtime_reads(tmp_path, frozenset({field}))

    assert reads == frozenset({field})


def test_resolved_self_factory_uses_foreign_return_before_name_convention(
    contract, tmp_path: Path
) -> None:
    (tmp_path / "consumer.py").write_text(
        "class EvaluationReport:\n    stage1_enabled = True\n"
        "class Report:\n    evaluation = EvaluationReport()\n"
        "class Provider:\n"
        "    def load_config(self):\n        return Report()\n"
        "    def read(self):\n        return self.load_config().evaluation.stage1_enabled\n"
        "Provider().read()\n"
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    reads = contract.runtime_reads(tmp_path, frozenset({field}))

    assert reads == frozenset()
    assert not reads.unresolved_semantics


@pytest.mark.parametrize("argument", ["Report()", "settings"])
def test_uncertain_factory_result_keeps_known_callable_alternative(
    contract, tmp_path: Path, argument: str
) -> None:
    (tmp_path / "consumer.py").write_text(
        "class EvaluationReport:\n    stage1_enabled = True\n"
        "class Report:\n    evaluation = EvaluationReport()\n"
        "def reader(config):\n    return config.evaluation.stage1_enabled\n"
        "def load_config():\n    return reader if unknown else unknown_result\n"
        f"load_config()({argument})\n"
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")

    reads = contract.runtime_reads(tmp_path, frozenset({field}))

    assert reads == (frozenset({field}) if argument == "settings" else frozenset())


@pytest.mark.parametrize(
    ("exports", "bridge", "imported", "argument", "expected"),
    [
        ('__all__ = ["_reader"]', "", "*", "settings", True),
        ('__all__ = ("_reader",)', "", "*", "settings", True),
        ('__all__: list[str] = ["_reader"]', "", "*", "settings", True),
        ('__all__ = ["_reader"]', "", "_reader", "settings", True),
        ('__all__ = ["_reader"]', "", "*", "Report()", False),
        ("", "", "*", "None", False),
        ("__all__ = []", "", "*", "None", False),
        (
            '__all__ = ["_reader"]',
            'from source import *\n__all__ = ["_reader"]',
            "*",
            "settings",
            True,
        ),
        (
            '__all__ = ["_reader"]',
            "from source import _reader as callback",
            "callback",
            "settings",
            True,
        ),
        (
            '__all__ = ["_reader"]',
            "from source import *\ncallback = _reader",
            "callback",
            "settings",
            True,
        ),
        ("__all__ = names", "", "*", "settings", True),
        ('__all__ = []\n__all__.append("_reader")', "", "*", "settings", True),
        ('__all__ = ["ignored"]\n__all__[0] = "_reader"', "", "*", "settings", True),
        ("__all__ = unknown", "", "*", "Report()", False),
    ],
)
def test_runtime_scan_preserves_local_star_export_provenance(
    contract,
    tmp_path: Path,
    exports: str,
    bridge: str,
    imported: str,
    argument: str,
    expected: bool,
) -> None:
    (tmp_path / "source.py").write_text(
        exports + "\ndef _reader(config):\n    return config.evaluation.stage1_enabled\n",
        encoding="utf-8",
    )
    target = "source"
    if bridge:
        (tmp_path / "bridge.py").write_text(bridge + "\n", encoding="utf-8")
        target = "bridge"
    callback = "callback" if imported == "callback" else "_reader"
    invocation = "" if argument == "None" else f"result = {callback}({argument})\n"
    (tmp_path / "consumer.py").write_text(
        f"from {target} import {imported}\n"
        "class EvaluationReport:\n    stage1_enabled = True\n"
        "class Report:\n    evaluation = EvaluationReport()\n" + invocation,
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")
    reads = contract.runtime_reads(tmp_path, frozenset({field}))
    assert bool(field in reads) is expected
    assert not reads.unresolved_semantics
    assert bool(_audit_as_documented_inert(contract, field, reads).violations) is expected


@pytest.mark.parametrize(
    "bridge",
    ["from source import _reader, __all__", "from source import *\nfrom source import __all__"],
)
def test_runtime_scan_preserves_imported_export_selector(
    contract, tmp_path: Path, bridge: str
) -> None:
    (tmp_path / "source.py").write_text(
        '__all__ = ["_reader"]\ndef _reader(config):\n    return config.evaluation.stage1_enabled\n'
    )
    (tmp_path / "bridge.py").write_text(bridge + "\n")
    (tmp_path / "consumer.py").write_text("from bridge import *\n_reader(settings)\n")
    field = contract.ConfigField("evaluation", "stage1_enabled")
    reads = contract.runtime_reads(tmp_path, frozenset({field}))
    assert field in reads
    assert _audit_as_documented_inert(contract, field, reads).violations


def test_runtime_scan_preserves_star_imported_class_receiver(contract, tmp_path: Path) -> None:
    (tmp_path / "source.py").write_text(
        '__all__ = ["_Base"]\nclass _Base:\n'
        "    def read(self, config):\n        return config.evaluation.stage1_enabled\n",
        encoding="utf-8",
    )
    (tmp_path / "consumer.py").write_text(
        "from source import *\nclass Reader(_Base):\n    pass\nReader().read(settings)\n",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")
    reads = contract.runtime_reads(tmp_path, frozenset({field}))
    assert field in reads
    assert _audit_as_documented_inert(contract, field, reads).violations


def test_runtime_scan_bounds_star_reexport_cycles(contract, tmp_path: Path) -> None:
    (tmp_path / "source.py").write_text(
        'from bridge import *\n__all__ = ["_reader"]\n'
        "def _reader(config):\n    return config.evaluation.stage1_enabled\n",
        encoding="utf-8",
    )
    (tmp_path / "bridge.py").write_text(
        'from source import *\n__all__ = ["_reader"]\n',
        encoding="utf-8",
    )
    (tmp_path / "consumer.py").write_text(
        "from bridge import *\n_reader(settings)\n",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")
    reads = contract.runtime_reads(tmp_path, frozenset({field}))
    assert field in reads
    assert not reads.unresolved_semantics


@pytest.mark.parametrize(
    ("exports", "expected"),
    [
        ('__all__ = ["_reader"]', {"_reader"}),
        ("__all__ = []", set()),
        ("", {"public"}),
        ("__all__ = unknown", {"_reader", "public", "__all__"}),
    ],
)
def test_star_export_selection_respects_private_and_empty_all(
    contract,
    tmp_path: Path,
    exports: str,
    expected: set[str],
) -> None:
    import ast

    source = tmp_path / "source.py"
    tree = ast.parse(exports + "\ndef _reader(): pass\ndef public(): pass\n")
    index = contract._SourceIndex(tmp_path, {source: tree})
    assert index.star_export_names(index._paths[source]) == frozenset(expected)


@pytest.mark.parametrize("reset", ["[]", '["public"]', '("public",)', "unknown_names"])
def test_runtime_scan_final_star_selector_reset_preserves_foreign_binding(
    contract,
    tmp_path: Path,
    reset: str,
) -> None:
    (tmp_path / "source.py").write_text(
        '__all__ = []\n__all__.append("_reader")\n'
        f"__all__ = {reset}\n"
        "def _reader(config):\n    return config.evaluation.stage1_enabled\n"
        "def public():\n    return 0\n",
        encoding="utf-8",
    )
    (tmp_path / "consumer.py").write_text(
        'def _reader(value):\n    return "foreign"\nfrom source import *\n_reader(settings)\n',
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")
    reads = contract.runtime_reads(tmp_path, frozenset({field}))
    # Dynamic selectors still retain possible private exports conservatively.
    if reset == "unknown_names":
        assert field in reads
    else:
        assert not reads
        assert not reads.unresolved_semantics
        assert not _audit_as_documented_inert(contract, field, reads).violations


@pytest.mark.parametrize(
    "imported",
    [
        "from bridge import *",
        "from bridge import _reader",
        "from source import _reader",
    ],
)
@pytest.mark.parametrize("later_foreign", [False, True])
def test_runtime_scan_cyclic_star_binding_preserves_resolved_origin(
    contract,
    tmp_path: Path,
    imported: str,
    later_foreign: bool,
) -> None:
    (tmp_path / "source.py").write_text(
        '__all__ = ["_reader"]\n'
        "def _reader(config):\n    return config.evaluation.stage1_enabled\n"
        "from bridge import *\n",
        encoding="utf-8",
    )
    (tmp_path / "bridge.py").write_text(
        'from source import *\n__all__ = ["_reader"]\n'
        + ('def _reader(value):\n    return "foreign"\n' if later_foreign else ""),
        encoding="utf-8",
    )
    (tmp_path / "consumer.py").write_text(imported + "\n_reader(settings)\n", encoding="utf-8")
    field = contract.ConfigField("evaluation", "stage1_enabled")
    reads = contract.runtime_reads(tmp_path, frozenset({field}))
    assert bool(field in reads) is not later_foreign
    assert not reads.unresolved_semantics
    assert bool(_audit_as_documented_inert(contract, field, reads).violations) is not later_foreign


@pytest.mark.parametrize("argument", ["settings", "Report()"])
def test_runtime_scan_ambiguous_star_cycle_fails_closed_only_for_config_arguments(
    contract,
    tmp_path: Path,
    argument: str,
) -> None:
    (tmp_path / "source.py").write_text(
        '__all__ = ["_reader"]\ndef _reader(config):\n'
        "    return config.evaluation.stage1_enabled\nfrom bridge import *\n",
        encoding="utf-8",
    )
    (tmp_path / "bridge.py").write_text(
        '__all__ = ["_reader"]\ndef _reader(value):\n    return "foreign"\nfrom source import *\n',
        encoding="utf-8",
    )
    (tmp_path / "consumer.py").write_text(
        f"from bridge import *\nclass Report:\n    pass\n_reader({argument})\n",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")
    reads = contract.runtime_reads(tmp_path, frozenset({field}))
    assert not reads
    assert bool(reads.unresolved_semantics) is (argument == "settings")
    assert bool(_audit_as_documented_inert(contract, field, reads).violations) is (
        argument == "settings"
    )


@pytest.mark.parametrize("indirect", [False, True])
@pytest.mark.parametrize("entry", ["source", "bridge"])
@pytest.mark.parametrize("argument", ["settings", "Report()"])
def test_runtime_scan_ambiguous_cycle_preserves_transitive_helper_relevance(
    contract,
    tmp_path: Path,
    indirect: bool,
    entry: str,
    argument: str,
) -> None:
    helper = "def _helper(config):\n    return config.evaluation.stage1_enabled\n"
    reader = (
        "def _reader(value):\n    return _helper(value)\n"
        if indirect
        else "def _reader(config):\n    return config.evaluation.stage1_enabled\n"
    )
    (tmp_path / "source.py").write_text(
        '__all__ = ["_reader"]\n' + helper + reader + "from bridge import *\n",
        encoding="utf-8",
    )
    (tmp_path / "bridge.py").write_text(
        '__all__ = ["_reader"]\ndef _reader(value):\n    return "foreign"\nfrom source import *\n',
        encoding="utf-8",
    )
    (tmp_path / "consumer.py").write_text(
        f"from {entry} import _reader\n"
        "class EvaluationReport:\n    stage1_enabled = True\n"
        "class Report:\n    evaluation = EvaluationReport()\n"
        f"_reader({argument})\n",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")
    reads = contract.runtime_reads(tmp_path, frozenset({field}))
    assert not reads
    assert bool(reads.unresolved_semantics) is (argument == "settings")
    assert bool(_audit_as_documented_inert(contract, field, reads).violations) is (
        argument == "settings"
    )


@pytest.mark.parametrize("indirect", [False, True])
@pytest.mark.parametrize("entry", ["source", "bridge"])
@pytest.mark.parametrize("argument", ["settings", "Report()"])
def test_runtime_scan_ambiguous_cycle_preserves_renamed_helper_arguments(
    contract,
    tmp_path: Path,
    indirect: bool,
    entry: str,
    argument: str,
) -> None:
    helper = "def _helper(item):\n    return item.evaluation.stage1_enabled\n"
    reader = (
        "def _reader(value):\n    return _helper(value)\n"
        if indirect
        else "def _reader(config):\n    return config.evaluation.stage1_enabled\n"
    )
    (tmp_path / "source.py").write_text(
        '__all__ = ["_reader"]\n' + helper + reader + "from bridge import *\n",
        encoding="utf-8",
    )
    (tmp_path / "bridge.py").write_text(
        '__all__ = ["_reader"]\ndef _reader(value):\n    return "foreign"\nfrom source import *\n',
        encoding="utf-8",
    )
    (tmp_path / "consumer.py").write_text(
        f"from {entry} import _reader\n"
        "class EvaluationReport:\n    stage1_enabled = True\n"
        "class Report:\n    evaluation = EvaluationReport()\n"
        f"_reader({argument})\n",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")
    reads = contract.runtime_reads(tmp_path, frozenset({field}))
    assert not reads
    assert bool(reads.unresolved_semantics) is (argument == "settings")
    assert bool(_audit_as_documented_inert(contract, field, reads).violations) is (
        argument == "settings"
    )


@pytest.mark.parametrize("forwarding", ["keyword", "chain", "foreign"])
@pytest.mark.parametrize("entry", ["source", "bridge"])
@pytest.mark.parametrize("argument", ["settings", "Report()"])
def test_runtime_scan_ambiguous_cycle_preserves_bound_helper_edges(
    contract,
    tmp_path: Path,
    forwarding: str,
    entry: str,
    argument: str,
) -> None:
    helper = "def _helper(item):\n    return item.evaluation.stage1_enabled\n"
    readers = {
        "keyword": "def _reader(value):\n    return _helper(item=value)\n",
        "chain": (
            "def _forward(payload):\n    return _helper(item=payload)\n"
            "def _reader(value):\n    return _forward(value)\n"
        ),
        "foreign": (
            "class EvaluationReport:\n    stage1_enabled = True\n"
            "class Report:\n    evaluation = EvaluationReport()\n"
            "def _reader(value):\n    return _helper(Report())\n"
        ),
    }
    reader = readers[forwarding]
    (tmp_path / "source.py").write_text(
        '__all__ = ["_reader"]\n' + helper + reader + "from bridge import *\n",
        encoding="utf-8",
    )
    (tmp_path / "bridge.py").write_text(
        '__all__ = ["_reader"]\ndef _reader(value):\n    return "foreign"\nfrom source import *\n',
        encoding="utf-8",
    )
    (tmp_path / "consumer.py").write_text(
        f"from {entry} import _reader\n"
        "class EvaluationReport:\n    stage1_enabled = True\n"
        "class Report:\n    evaluation = EvaluationReport()\n"
        f"_reader({argument})\n",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")
    reads = contract.runtime_reads(tmp_path, frozenset({field}))
    assert not reads
    assert bool(reads.unresolved_semantics) is (argument == "settings" and forwarding != "foreign")
    assert bool(_audit_as_documented_inert(contract, field, reads).violations) is (
        argument == "settings" and forwarding != "foreign"
    )


@pytest.mark.parametrize("accessor", ["getattr", "hasattr"])
@pytest.mark.parametrize("binding", ["direct", "alias", "imported"])
@pytest.mark.parametrize(
    ("name_expression", "known_read"),
    [
        ('os.getenv("FIELD", "stage1_enabled")', False),
        ('"stage1_enabled" if unknown else dynamic_field', True),
        ('"untracked_name" if unknown else dynamic_field', False),
    ],
)
def test_builtin_accessor_preserves_unresolved_field_name(
    contract, tmp_path: Path, accessor: str, binding: str, name_expression: str, known_read: bool
) -> None:
    setup = {
        "direct": "",
        "alias": f"reader = {accessor}\n",
        "imported": f"from builtins import {accessor} as reader\n",
    }[binding]
    callee = accessor if binding == "direct" else "reader"
    (tmp_path / "dynamic_accessor.py").write_text(
        f"import os\n{setup}field = {name_expression}\n"
        f"result = {callee}(config.evaluation, field)\n",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")
    reads = contract.runtime_reads(tmp_path, frozenset({field}))

    assert reads == (frozenset({field}) if known_read else frozenset())
    assert any(
        "unresolved builtin accessor field name" in item for item in reads.unresolved_semantics
    )
    assert _audit_as_documented_inert(contract, field, reads).violations


@pytest.mark.parametrize("accessor", ["getattr", "hasattr"])
@pytest.mark.parametrize("control", ["literal", "foreign_literal", "foreign_receiver", "shadowed"])
def test_builtin_accessor_uncertainty_controls(
    contract, tmp_path: Path, accessor: str, control: str
) -> None:
    setup = ""
    if control == "foreign_receiver":
        setup = "class Report:\n    evaluation = object()\nconfig = Report()\n"
    elif control == "shadowed":
        setup = f"def {accessor}(*args):\n    return None\n"
    name = {"literal": '"stage1_enabled"', "foreign_literal": '"untracked_name"'}.get(
        control, 'os.getenv("FIELD", "stage1_enabled")'
    )
    (tmp_path / "accessor_control.py").write_text(
        f"import os\n{setup}field = {name}\nresult = {accessor}(config.evaluation, field)\n",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")
    reads = contract.runtime_reads(tmp_path, frozenset({field}))

    assert reads == (frozenset({field}) if control == "literal" else frozenset())
    assert not reads.unresolved_semantics
    assert bool(_audit_as_documented_inert(contract, field, reads).violations) is (
        control == "literal"
    )


@pytest.mark.parametrize("forwarding", ["map", "partial"])
@pytest.mark.parametrize(
    ("name_expression", "known_read"),
    [
        ('os.getenv("FIELD", "stage1_enabled")', False),
        ('"stage1_enabled" if unknown else dynamic_field', True),
        ('"untracked_name" if unknown else dynamic_field', False),
    ],
)
def test_forwarded_getattr_preserves_unresolved_field_name(
    contract, tmp_path: Path, forwarding: str, name_expression: str, known_read: bool
) -> None:
    call = {
        "map": "list(map(getattr, [config.evaluation], [field]))",
        "partial": "partial(getattr, config.evaluation, field)()",
    }[forwarding]
    (tmp_path / "forwarded_accessor.py").write_text(
        f"import os\nfrom functools import partial\nfield = {name_expression}\n{call}\n",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")
    reads = contract.runtime_reads(tmp_path, frozenset({field}))
    assert reads == (frozenset({field}) if known_read else frozenset())
    assert any(
        "unresolved builtin accessor field name" in item for item in reads.unresolved_semantics
    )
    assert _audit_as_documented_inert(contract, field, reads).violations


@pytest.mark.parametrize("forwarding", ["map", "partial"])
@pytest.mark.parametrize("control", ["literal", "foreign_literal", "foreign_receiver", "shadowed"])
def test_forwarded_getattr_uncertainty_controls(
    contract, tmp_path: Path, forwarding: str, control: str
) -> None:
    setup = ""
    if control == "foreign_receiver":
        setup = "class Report:\n    evaluation = object()\nconfig = Report()\n"
    elif control == "shadowed":
        setup = "def getattr(*args):\n    return None\n"
    name = {"literal": '"stage1_enabled"', "foreign_literal": '"untracked_name"'}.get(
        control, 'os.getenv("FIELD", "stage1_enabled")'
    )
    call = {
        "map": "list(map(getattr, [config.evaluation], [field]))",
        "partial": "partial(getattr, config.evaluation, field)()",
    }[forwarding]
    (tmp_path / "forwarded_control.py").write_text(
        f"import os\nfrom functools import partial\n{setup}field = {name}\n{call}\n",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")
    reads = contract.runtime_reads(tmp_path, frozenset({field}))
    assert reads == (frozenset({field}) if control == "literal" else frozenset())
    assert not reads.unresolved_semantics
    assert bool(_audit_as_documented_inert(contract, field, reads).violations) is (
        control == "literal"
    )


@pytest.mark.parametrize("forwarding", ["map", "partial"])
@pytest.mark.parametrize(
    "name_expression",
    [
        'os.getenv("FIELD", "stage1_enabled")',
        '"stage1_enabled" if unknown else dynamic_field',
        '"untracked_name" if unknown else dynamic_field',
    ],
)
def test_forwarded_hasattr_preserves_only_unresolved_field_name(
    contract, tmp_path: Path, forwarding: str, name_expression: str
) -> None:
    call = {
        "map": "list(map(hasattr, [config.evaluation], [field]))",
        "partial": "partial(hasattr, config.evaluation, field)()",
    }[forwarding]
    (tmp_path / "forwarded_hasattr.py").write_text(
        f"import os\nfrom functools import partial\nfield = {name_expression}\n{call}\n",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")
    reads = contract.runtime_reads(tmp_path, frozenset({field}))
    # Existing hasattr forwarding has no positive-read model. Preserve only its
    # unknown-name uncertainty, including a mixed known/unknown alternative.
    assert not reads
    assert any(
        "unresolved builtin accessor field name" in item for item in reads.unresolved_semantics
    )
    assert _audit_as_documented_inert(contract, field, reads).violations


@pytest.mark.parametrize("forwarding", ["map", "partial"])
@pytest.mark.parametrize("control", ["literal", "foreign_literal", "foreign_receiver", "shadowed"])
def test_forwarded_hasattr_does_not_add_a_positive_read_model(
    contract, tmp_path: Path, forwarding: str, control: str
) -> None:
    setup = ""
    if control == "foreign_receiver":
        setup = "class Report:\n    evaluation = object()\nconfig = Report()\n"
    elif control == "shadowed":
        setup = "def hasattr(*args):\n    return False\n"
    name = {"literal": '"stage1_enabled"', "foreign_literal": '"untracked_name"'}.get(
        control, 'os.getenv("FIELD", "stage1_enabled")'
    )
    call = {
        "map": "list(map(hasattr, [config.evaluation], [field]))",
        "partial": "partial(hasattr, config.evaluation, field)()",
    }[forwarding]
    (tmp_path / "forwarded_hasattr_control.py").write_text(
        f"import os\nfrom functools import partial\n{setup}field = {name}\n{call}\n",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")
    reads = contract.runtime_reads(tmp_path, frozenset({field}))
    assert not reads
    assert not reads.unresolved_semantics
    assert not _audit_as_documented_inert(contract, field, reads).violations


@pytest.mark.parametrize(
    "adapter",
    [
        'map(hasattr, [config.evaluation], ["stage1_enabled"])',
        "filter(hasattr, [config.evaluation])",
        'accumulate([config.evaluation, "stage1_enabled"], hasattr)',
        "dropwhile(hasattr, [config.evaluation])",
        "groupby([config.evaluation], key=hasattr)",
    ],
)
def test_unmodeled_hasattr_adapter_preserves_existing_branch_value(
    contract, tmp_path: Path, monkeypatch, adapter: str
) -> None:
    # A false branch containing the existing unbounded shape join must remain
    # unreachable. Bound the test instrumentation so a regression fails promptly.
    joins = 0
    original = contract._RuntimeReadVisitor._join_binding_snapshots

    def bounded_join(self, *snapshots):
        nonlocal joins
        joins += 1
        assert joins < 12, "unsupported hasattr adapter opened an unreachable branch"
        return original(self, *snapshots)

    monkeypatch.setattr(contract._RuntimeReadVisitor, "_join_binding_snapshots", bounded_join)
    (tmp_path / "adapter_branch.py").write_text(
        "from itertools import accumulate, dropwhile, groupby\n"
        f"flag = {adapter}\n"
        "if flag:\n    pass\nelse:\n"
        "    value = config\n    for item in [1]:\n        value = (value,)\n",
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")
    reads = contract.runtime_reads(tmp_path, frozenset({field}))
    assert not reads
    assert not reads.unresolved_semantics
    assert joins == 0


@pytest.mark.parametrize("consumed", [False, True])
def test_hasattr_adapter_uncertainty_remains_lazy(contract, tmp_path: Path, consumed: bool) -> None:
    (tmp_path / "lazy_hasattr.py").write_text(
        'import os\nfield = os.getenv("FIELD", "stage1_enabled")\n'
        "values = map(hasattr, [config.evaluation], [field])\n"
        + ("list(values)\n" if consumed else ""),
        encoding="utf-8",
    )
    field = contract.ConfigField("evaluation", "stage1_enabled")
    reads = contract.runtime_reads(tmp_path, frozenset({field}))
    assert not reads
    assert bool(reads.unresolved_semantics) is consumed


def test_unresolved_callable_join_is_idempotent_and_preserves_distinct_sources(contract) -> None:
    functions = contract.ast.parse("def first(): pass\ndef second(): pass\n").body
    targets = tuple(
        contract._UnresolvedCallable(function, "opaque", "fixture", function.lineno)
        for function in functions
    )
    first = contract._AbstractValue(unresolved_callables=(targets[0],))
    second = contract._AbstractValue(unresolved_callables=(targets[1],))
    joined = contract._join_values(first, second)
    assert joined.unresolved_callables == targets
    for _ in range(20):
        joined = contract._join_values(joined, second, joined)
        assert joined.unresolved_callables == targets


@pytest.mark.parametrize("edge", ["items", "entries", "attributes", "receiver"])
def test_abstract_value_construction_rejects_excessive_depth(contract, edge: str) -> None:
    value = contract._UNKNOWN_VALUE
    function = contract.ast.parse("def reader(): pass").body[0]
    with pytest.raises(contract._AnalysisLimitError, match="depth"):
        for _ in range(contract._MAX_ABSTRACT_DEPTH + 1):
            if edge == "items":
                value = contract._AbstractValue(items=(value,))
            elif edge == "entries":
                value = contract._AbstractValue(entries=(("key", value),))
            elif edge == "attributes":
                value = contract._AbstractValue(attributes=(("key", value),))
            else:
                value = contract._AbstractValue(
                    callables=(contract._CallableTarget(function, value),)
                )


@pytest.mark.parametrize("receiver", [False, True])
def test_shared_abstract_dag_is_bounded_by_expanded_cost(contract, receiver: bool) -> None:
    value = contract._UNKNOWN_VALUE
    function = contract.ast.parse("def reader(): pass").body[0]
    with pytest.raises(contract._AnalysisLimitError, match="expanded cost"):
        for _ in range(20):
            if receiver:
                target = contract._CallableTarget(function, value)
                value = contract._AbstractValue(callables=(target, target))
            else:
                value = contract._AbstractValue(items=(value, value))


def test_expanded_value_limit_exact_boundary_and_plus_one(contract) -> None:
    value = contract._UNKNOWN_VALUE
    for _ in range(15):
        value = contract._AbstractValue(items=(value, value))
    exact = contract._AbstractValue(items=(value,), origins=frozenset({"one"}))
    assert contract._validate_abstract_values((exact,))[0] == 131072
    with pytest.raises(contract._AnalysisLimitError, match="value expanded cost exceeds 131072"):
        contract._AbstractValue(items=(value,), origins=frozenset({"one", "two"}))


def test_expanded_values_are_accepted_by_all_ordinary_value_consumers(contract) -> None:
    value = contract._AbstractValue(items=(contract._UNKNOWN_VALUE,) * 3000)
    assert contract._validate_abstract_values((value,))[0] == 6001
    target = contract._CallableTarget(contract.ast.parse("def reader(): pass").body[0], value)
    assert target.receiver is value
    assert contract._contained_origins(value) == frozenset()


def test_standalone_validation_cache_reuses_identity_without_structural_work(
    contract, monkeypatch
) -> None:
    leaf = contract._AbstractValue(origins=frozenset({"evaluation"}))
    value = contract._AbstractValue(items=(leaf, leaf))
    expected = contract._validate_abstract_values((value,))
    entry = contract._VALIDATION_CACHE[id(value)]
    assert entry.root is value
    assert any(node.value is leaf for node in entry.nodes)
    assert contract._VALIDATION_CACHE_RETAINED_NODES <= contract._MAX_VALIDATION_CACHE_NODES
    assert contract._VALIDATION_CACHE_RETAINED_METADATA <= contract._MAX_VALIDATION_CACHE_METADATA

    def forbidden(_value):
        raise AssertionError("cache hit expanded the immutable value graph")

    monkeypatch.setattr(contract, "_abstract_children", forbidden)
    assert contract._validate_abstract_values((value,)) == expected


def test_identity_cache_entry_lifetime_is_bounded(contract) -> None:
    contract._VALIDATION_CACHE.clear()
    contract._VALIDATION_CACHE_RETAINED_NODES = 0
    contract._VALIDATION_CACHE_RETAINED_METADATA = 0
    value = contract._AbstractValue(origins=frozenset({"evaluation"}))
    contract._validate_abstract_values((value,))
    entry = contract._VALIDATION_CACHE[id(value)]
    contract._VALIDATION_CACHE[id(value)] = entry._replace(
        created_at=(time.monotonic() - contract._MAX_VALIDATION_CACHE_AGE_SECONDS - 1)
    )
    assert contract._get_validation_cache_entry(id(value)) is None
    assert id(value) not in contract._VALIDATION_CACHE
    assert contract._VALIDATION_CACHE_RETAINED_NODES == 0
    assert contract._VALIDATION_CACHE_RETAINED_METADATA == 0


def test_identity_cache_seeds_shared_memo_with_exact_operation_accounting(contract) -> None:
    leaf = contract._AbstractValue(origins=frozenset({"evaluation"}))
    root = contract._AbstractValue(items=(leaf, leaf))

    def run_with_cache(enabled: bool):
        contract._VALIDATION_CACHE.clear()
        contract._VALIDATION_CACHE_RETAINED_NODES = 0
        contract._VALIDATION_CACHE_RETAINED_METADATA = 0
        if enabled:
            contract._validate_abstract_values((root,))
        work = [0]
        graph_nodes = set()
        graph_edges = [0]
        graph_metadata = [0]
        unique_nodes = set()
        unique_cost = [0]
        result = contract._validate_abstract_values(
            (root, root),
            aggregate_limit=1000,
            _shared_work=work,
            _shared_memo={},
            _unique_node_aggregate=True,
            _unique_aggregate_limit=1000,
            _shared_unique_nodes=unique_nodes,
            _shared_unique_cost=unique_cost,
            _shared_graph_nodes=graph_nodes,
            _shared_graph_edges=graph_edges,
            _shared_graph_metadata=graph_metadata,
        )
        return result, work, graph_nodes, graph_edges, graph_metadata, unique_cost

    uncached = run_with_cache(False)
    cached = run_with_cache(True)
    assert cached == uncached


def test_identity_cache_mixed_memo_preserves_expanded_cost_and_physical_budgets(
    contract, monkeypatch
) -> None:
    leaf = contract._AbstractValue(origins=frozenset({"evaluation"}))
    root = contract._AbstractValue(items=(leaf, leaf))
    contract._VALIDATION_CACHE.clear()
    contract._VALIDATION_CACHE_RETAINED_NODES = 0
    contract._VALIDATION_CACHE_RETAINED_METADATA = 0
    contract._validate_abstract_values((root,))

    work = [0]
    graph_nodes = set()
    graph_edges = [0]
    graph_metadata = [0]
    result = contract._validate_abstract_values(
        (root, root),
        aggregate_limit=1000,
        _shared_work=work,
        _shared_memo={},
        _shared_graph_nodes=graph_nodes,
        _shared_graph_edges=graph_edges,
        _shared_graph_metadata=graph_metadata,
    )
    assert result[0] == 2 * contract._VALIDATION_CACHE[id(root)].cost
    assert work == [2]
    assert (len(graph_nodes), graph_edges[0]) == (2, 2)
    assert graph_metadata[0] == sum(
        contract._abstract_metadata_width(node) for node in (root, leaf)
    )

    monkeypatch.setattr(contract, "_MAX_ABSTRACT_OPERATION_WORK", 1)
    with pytest.raises(contract._AnalysisLimitError, match="traversal exceeds 1"):
        contract._validate_abstract_values(
            (root,),
            aggregate_limit=1000,
            _shared_work=[0],
            _shared_memo={},
            _shared_graph_nodes=set(),
            _shared_graph_edges=[0],
            _shared_graph_metadata=[0],
        )


def test_standalone_validation_cache_rechecks_current_limits(contract, monkeypatch) -> None:
    value = contract._AbstractValue(items=(contract._UNKNOWN_VALUE,) * 2)
    assert contract._validate_abstract_values((value,))[0] > 0

    monkeypatch.setattr(contract, "_MAX_ABSTRACT_DEPTH", 1)
    with pytest.raises(contract._AnalysisLimitError, match="depth exceeds 1"):
        contract._validate_abstract_values((value,))

    monkeypatch.setattr(contract, "_MAX_ABSTRACT_DEPTH", 32)
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_OPERATION_WORK", 1)
    with pytest.raises(contract._AnalysisLimitError, match="traversal exceeds 1"):
        contract._validate_abstract_values((value,))


def test_validator_charges_root_alias_references_to_operation_budget(contract) -> None:
    value = contract._UNKNOWN_VALUE
    assert contract._validate_abstract_values((value,) * 8192)[0] == 8192
    with pytest.raises(contract._AnalysisLimitError, match="root reference work exceeds 8192"):
        contract._validate_abstract_values((value,) * 8193)


def test_receiverless_callable_width_is_also_bounded(contract) -> None:
    target = contract._CallableTarget(contract.ast.parse("def reader(): pass").body[0])
    with pytest.raises(contract._AnalysisLimitError, match="metadata limit"):
        contract._AbstractValue(
            callables=(target,) * (contract._MAX_ABSTRACT_EXPANDED_VALUE_WORK // 2 + 1)
        )


@pytest.mark.parametrize(
    ("limit_name", "value", "message"),
    [
        (
            "_MAX_ABSTRACT_GRAPH_NODES",
            lambda c: c._AbstractValue(items=(c._UNKNOWN_VALUE,)),
            "node limit",
        ),
        (
            "_MAX_ABSTRACT_GRAPH_EDGES",
            lambda c: c._AbstractValue(items=(c._UNKNOWN_VALUE, c._UNKNOWN_VALUE)),
            "edge limit",
        ),
        (
            "_MAX_ABSTRACT_GRAPH_METADATA",
            lambda c: c._AbstractValue(origins=frozenset({"a", "b"})),
            "metadata limit",
        ),
    ],
)
def test_abstract_graph_limits_fail_closed(
    contract, monkeypatch, limit_name, value, message
) -> None:
    monkeypatch.setattr(contract, limit_name, 1)
    with pytest.raises(contract._AnalysisLimitError, match=message):
        value(contract)


def test_abstract_expansion_and_traversal_have_independent_budgets(contract, monkeypatch) -> None:
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_OPERATION_WORK", 1)
    with pytest.raises(contract._AnalysisLimitError, match="traversal exceeds"):
        contract._AbstractValue(items=(contract._UNKNOWN_VALUE, contract._UNKNOWN_VALUE))


def test_abstract_validator_uses_no_structural_equality_or_hash_and_rejects_cycles(
    contract, monkeypatch
) -> None:
    def forbidden(*_args):
        raise AssertionError("validator invoked structural equality/hash")

    monkeypatch.setattr(contract._AbstractValue, "__eq__", forbidden)
    monkeypatch.setattr(contract._AbstractValue, "__hash__", forbidden)
    monkeypatch.setattr(contract._CallableTarget, "__eq__", forbidden)
    monkeypatch.setattr(contract._CallableTarget, "__hash__", forbidden)
    leaf = contract._AbstractValue(origins=frozenset({"evaluation"}))
    target = contract._CallableTarget(contract.ast.parse("def reader(): pass").body[0], leaf)
    value = contract._AbstractValue(items=(leaf, leaf), callables=(target,))
    assert contract._validate_abstract_values((value,))[0] > 0

    # Deliberately violate frozen construction only to exercise the cycle guard.
    cyclic = object.__new__(contract._AbstractValue)
    for name, member in vars(contract._UNKNOWN_VALUE).items():
        object.__setattr__(cyclic, name, member)
    object.__setattr__(cyclic, "items", (cyclic,))
    with pytest.raises(contract._AnalysisLimitError, match="cyclic"):
        contract._validate_abstract_values((cyclic,))


def test_join_identity_fastpath_cannot_bypass_individual_value_budget(
    contract, monkeypatch
) -> None:
    value = contract._AbstractValue(origins=frozenset({"evaluation"}))
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_EXPANDED_VALUE_WORK", 1)
    with pytest.raises(contract._AnalysisLimitError, match="expanded cost"):
        contract._join_values(value, value)


def test_snapshot_budget_includes_annotations(contract, tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "fixture.py"
    index = contract._SourceIndex(tmp_path, {path: contract.ast.parse("")})
    visitor = contract._RuntimeReadVisitor(frozenset(), index, index.module_for_path(path))
    visitor._annotations[-1] = dict.fromkeys(("a", "b", "c"), contract._UNKNOWN_VALUE)
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_OPERATION_WORK", 2)
    with pytest.raises(contract._AnalysisLimitError, match="root reference work"):
        visitor._binding_snapshot()


@pytest.mark.parametrize("loop", ["for", "while", "async_for"])
def test_fixed_point_budget_raises_instead_of_returning_partial_reads(
    contract, tmp_path: Path, monkeypatch, loop: str
) -> None:
    header = {
        "for": "for item in [1]:",
        "while": "while condition:",
        "async_for": "async for item in source:",
    }[loop]
    source = f"{header}\n    alias = config\nalias.evaluation.stage1_enabled\n"
    if loop == "async_for":
        source = "async def consume(config):\n" + "".join(
            "    " + line + "\n" for line in source.splitlines()
        )
    (tmp_path / "limited_loop.py").write_text(source)
    monkeypatch.setattr(contract, "_MAX_ANALYSIS_ITERATIONS", 1)
    with pytest.raises(contract._AnalysisLimitError, match="limited_loop:.*did not converge"):
        contract.runtime_reads(
            tmp_path, frozenset({contract.ConfigField("evaluation", "stage1_enabled")})
        )


def test_analysis_limit_is_not_a_valueerror_fallback(contract, monkeypatch) -> None:
    assert not issubclass(contract._AnalysisLimitError, ValueError)

    def recursive(*_args):
        raise RecursionError("synthetic recursion capacity failure")

    monkeypatch.setattr(contract, "_runtime_reads", recursive)
    with pytest.raises(contract._AnalysisLimitError, match="recursion capacity"):
        contract.runtime_reads(Path("unused"), frozenset())


def test_bound_relevance_checks_receiver_graph_before_key_hash(
    contract, tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "fixture.py"
    tree = contract.ast.parse("def reader(): pass")
    index = contract._SourceIndex(tmp_path, {path: tree})
    visitor = contract._RuntimeReadVisitor(frozenset(), index, index.module_for_path(path))
    target = contract._CallableTarget(tree.body[0], contract._UNKNOWN_VALUE)
    value = contract._AbstractValue(callables=(target,))

    def forbidden(*_args):
        raise AssertionError("unsafe key hash reached")

    monkeypatch.setattr(contract._CallableTarget, "__hash__", forbidden)
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_EXPANDED_VALUE_WORK", 3)
    with pytest.raises(contract._AnalysisLimitError, match="expanded cost"):
        visitor._has_bound_callable_relevance(tree.body[0], {"receiver": value})


def test_analysis_limit_does_not_publish_partial_module_data_cache(
    contract, tmp_path: Path
) -> None:
    path = tmp_path / "fixture.py"
    tree = contract.ast.parse(
        "partial = config\nvalue = config\nfor item in [1]:\n    value = (value,)\n"
    )
    index = contract._SourceIndex(tmp_path, {path: tree})
    module = index.module_for_path(path)
    with pytest.raises(contract._AnalysisLimitError, match="fixture:.*abstract value"):
        index.resolve_data_value(module, "partial", frozenset())
    assert module.name not in index._data_states
    assert module.name not in index._data_states_in_progress


@pytest.mark.parametrize("case", ["shape", "decorated"])
def test_finite_runtime_loops_complete_with_truthful_nonpass_cli(
    contract, tmp_path: Path, case: str
) -> None:
    prefix = "from ouroboros.config.models import OuroborosConfig\n"
    source = {
        "shape": prefix + "def inspect(config: OuroborosConfig):\n"
        "    observed = config.evaluation.stage1_enabled\n"
        "    value = config\n    for item in [1]:\n        value = (value,)\n"
        "    return observed\nresult = inspect(OuroborosConfig())\n",
        "decorated": prefix + "from typing import no_type_check\nimport os\n"
        "@no_type_check\ndef first(config: OuroborosConfig):\n"
        "    return config.evaluation.stage1_enabled\n"
        "@no_type_check\ndef second(config: OuroborosConfig):\n"
        "    return config.evaluation.stage1_enabled\n"
        'value = first\nchoice = os.getenv("FORCE_SECOND", "")\n'
        "for item in [1]:\n    value = second if choice else value\n"
        "result = value(OuroborosConfig())\n",
    }[case]
    scope = {}
    exec(compile(source, "finite_runtime.py", "exec"), scope)
    assert scope["result"] is True
    fixture = tmp_path / "src" / "ouroboros"
    fixture.mkdir(parents=True)
    (fixture / "finite_runtime.py").write_text(source)
    reference = tmp_path / contract.REFERENCE_PATH
    reference.parent.mkdir(parents=True)
    reference.write_text((REPO_ROOT / contract.REFERENCE_PATH).read_text())
    driver = (
        "import importlib.util,sys;from pathlib import Path;"
        "s=importlib.util.spec_from_file_location('cli_fixture',sys.argv[1]);"
        "m=importlib.util.module_from_spec(s);sys.modules[s.name]=m;s.loader.exec_module(m);"
        "audit=m.audit_repository;m.audit_repository=lambda:audit(Path(sys.argv[2]));"
        "sys.exit(m.main())"
    )
    result = subprocess.run(
        [sys.executable, "-c", driver, str(SCRIPT), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == (2 if case == "shape" else 1), result.stdout + result.stderr
    assert "Config reference contract OK" not in result.stdout
    if case == "shape":
        assert "finite_runtime:" in result.stderr
        assert "abstract value" in result.stderr
    else:
        assert "runtime scan unresolved semantics" in result.stdout


def test_large_identity_join_validates_one_value_without_structural_operations(
    contract, monkeypatch
):
    value = contract._AbstractValue(items=(contract._UNKNOWN_VALUE,) * 1322)
    assert contract._validate_abstract_values((value,)) == (2645, 2)

    def forbidden(*_args):
        raise AssertionError("identity join performed structural equality/hash")

    monkeypatch.setattr(contract._AbstractValue, "__eq__", forbidden)
    monkeypatch.setattr(contract._AbstractValue, "__hash__", forbidden)
    assert contract._join_values(value, value) is value


def test_distinct_safe_values_join_under_separate_operation_budget(contract):
    left = contract._AbstractValue(items=(contract._UNKNOWN_VALUE,) * 1322)
    right = contract._AbstractValue(items=(contract._UNKNOWN_VALUE,) * 1322)
    assert left is not right
    joined = contract._join_values(left, right)
    assert contract._validate_abstract_values((joined,)) == (2645, 2)
    assert joined.items == left.items


def test_many_small_uncached_join_operands_use_exact_validation_work(contract):
    values = tuple(contract._AbstractValue() for _ in range(100))
    contract._VALIDATION_CACHE.clear()
    contract._VALIDATION_CACHE_RETAINED_NODES = 0
    contract._VALIDATION_CACHE_RETAINED_METADATA = 0
    context = contract._JoinValidationContext()

    joined = contract._join_values(*values, _validation_context=context)

    assert joined is not contract._UNKNOWN_VALUE
    assert context.output_work[0] < contract._MAX_ABSTRACT_JOIN_OUTPUT_WORK
    assert context.work[0] <= contract._MAX_ABSTRACT_OPERATION_WORK


def test_input_validation_reserves_each_visit_before_continuing(contract, monkeypatch):
    leaf = contract._AbstractValue()
    root = contract._AbstractValue(items=(leaf,))
    contract._VALIDATION_CACHE.clear()
    contract._VALIDATION_CACHE_RETAINED_NODES = 0
    contract._VALIDATION_CACHE_RETAINED_METADATA = 0
    output_work = [0]
    traversal_work = [0]
    exact_root_visit = contract._abstract_overhead(root) + contract._abstract_metadata_width(root)
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", exact_root_visit)

    with pytest.raises(
        contract._AnalysisLimitError,
        match=f"abstract join output work exceeds {exact_root_visit}",
    ):
        contract._validate_abstract_values(
            (root,),
            _shared_work=traversal_work,
            _join_output_work=output_work,
        )

    assert output_work == [exact_root_visit]
    assert traversal_work == [0]


def test_input_validation_charges_verified_operation_memo_cost(contract, monkeypatch):
    leaf = contract._AbstractValue(origins=frozenset({"memo-leaf"}))
    root = contract._AbstractValue(items=(leaf,))
    exact_cost, depth = contract._validate_abstract_values((root,))
    contract._VALIDATION_CACHE.clear()
    contract._VALIDATION_CACHE_RETAINED_NODES = 0
    contract._VALIDATION_CACHE_RETAINED_METADATA = 0
    memo = {id(root): (root, exact_cost, depth)}
    graph_nodes = {id(root)}
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", exact_cost)
    output_work = [0]

    assert contract._validate_abstract_values(
        (root,),
        _shared_memo=memo,
        _shared_graph_nodes=graph_nodes,
        _join_output_work=output_work,
    ) == (exact_cost, depth)
    assert output_work == [exact_cost]

    output_work[0] = 0
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", exact_cost - 1)
    with pytest.raises(contract._AnalysisLimitError, match="abstract join output work"):
        contract._validate_abstract_values(
            (root,),
            _shared_memo=memo,
            _shared_graph_nodes=graph_nodes,
            _join_output_work=output_work,
        )
    assert output_work == [0]


def test_input_validation_charges_verified_memo_child_before_reuse(contract, monkeypatch):
    child = contract._AbstractValue(origins=frozenset({"memo-child"}))
    child_cost, child_depth = contract._validate_abstract_values((child,))
    root = contract._AbstractValue(items=(child,))
    contract._VALIDATION_CACHE.clear()
    contract._VALIDATION_CACHE_RETAINED_NODES = 0
    contract._VALIDATION_CACHE_RETAINED_METADATA = 0
    memo = {id(child): (child, child_cost, child_depth)}
    root_work = contract._abstract_overhead(root) + contract._abstract_metadata_width(root)
    exact_work = root_work + 1 + child_cost
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", exact_work)
    output_work = [0]
    traversal_work = [0]

    assert (
        contract._validate_abstract_values(
            (root,),
            _shared_memo=memo,
            _shared_work=traversal_work,
            _join_output_work=output_work,
        )[0]
        == root_work + child_cost
    )
    assert output_work == [exact_work]

    memo.pop(id(root))
    output_work[0] = 0
    traversal_work[0] = 0
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", exact_work - 1)
    with pytest.raises(contract._AnalysisLimitError, match="abstract join output work"):
        contract._validate_abstract_values(
            (root,),
            _shared_memo=memo,
            _shared_work=traversal_work,
            _join_output_work=output_work,
        )
    assert output_work == [root_work + 1]
    assert traversal_work == [1]


def test_operation_budget_exact_boundary_and_streaming_duplicate_charge(contract):
    value = contract._AbstractValue(origins=frozenset(str(i) for i in range(4095)))
    assert contract._validate_abstract_values(
        (value, value), aggregate_limit=contract._MAX_ABSTRACT_OPERATION_WORK
    ) == (8192, 1)
    consumed = []

    def roots():
        for item in (value, value, contract._UNKNOWN_VALUE):
            consumed.append(item)
            yield item
        raise AssertionError("validator consumed beyond the first excess root")

    with pytest.raises(
        contract._AnalysisLimitError, match="aggregate expanded cost exceeds 8192"
    ) as caught:
        contract._validate_abstract_values(
            roots(),
            aggregate_limit=contract._MAX_ABSTRACT_OPERATION_WORK,
            boundary="fixture_operation",
        )
    assert len(consumed) == 3
    message = str(caught.value)
    assert '"aggregate_cost": 8193' in message
    assert '"root_cost": 1' in message
    assert '"maximum_completed_root_cost": 4096' in message
    assert '"root_count": 3' in message
    assert '"boundary": "fixture_operation"' in message
    assert '"costs_complete": 1' in message


def test_operation_budget_does_not_admit_an_individually_unsafe_root(contract, monkeypatch):
    # Bypass construction only to test the operation boundary's own defense.
    invalid = object.__new__(contract._AbstractValue)
    for name, member in vars(contract._UNKNOWN_VALUE).items():
        object.__setattr__(invalid, name, member)
    object.__setattr__(invalid, "origins", frozenset(str(i) for i in range(101)))
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_EXPANDED_VALUE_WORK", 100)
    with pytest.raises(contract._AnalysisLimitError, match="value expanded cost exceeds 100"):
        contract._validate_abstract_values(
            (invalid,), aggregate_limit=contract._MAX_ABSTRACT_OPERATION_WORK
        )


def test_identity_join_fanin_and_recursive_work_remain_bounded(contract):
    value = contract._UNKNOWN_VALUE
    assert contract._join_values(*((value,) * 8192)) is value
    with pytest.raises(contract._AnalysisLimitError, match="operand count exceeds 8192"):
        contract._join_values(*((value,) * 8193))
    with pytest.raises(contract._AnalysisLimitError, match="join work exceeds 4096"):
        contract._join_values(value, value, _work=[4096])


def test_snapshots_charge_repeated_large_safe_roots_including_annotations(contract, tmp_path):
    path = tmp_path / "fixture.py"
    index = contract._SourceIndex(tmp_path, {path: contract.ast.parse("")})
    visitor = contract._RuntimeReadVisitor(frozenset(), index, index.module_for_path(path))
    value = contract._AbstractValue(items=(contract._UNKNOWN_VALUE,) * 1322)
    visitor._states[-1] = {f"s{i}": value for i in range(8190)}
    visitor._annotations[-1] = {"c": value}
    visitor._binding_snapshot()
    visitor._annotations[-1].update({f"a{i}": value for i in range(2)})
    with pytest.raises(
        contract._AnalysisLimitError, match="root reference work exceeds 8192"
    ) as caught:
        visitor._binding_snapshot()
    assert '"boundary": "binding_snapshot"' in str(caught.value)
    assert '"boundary": "binding_snapshot"' in str(caught.value)


def test_snapshot_identity_union_reuses_shared_traversal_but_charges_root_refs(contract):
    leaf = contract._AbstractValue(origins=frozenset({"evaluation"}))
    root = contract._AbstractValue(items=(leaf,) * 20)
    memo = {}
    work = [0]
    assert (
        contract._validate_abstract_values(
            (root,) * 3,
            aggregate_limit=100,
            _unique_node_aggregate=True,
            _shared_memo=memo,
            _shared_work=work,
        )[0]
        > 0
    )
    assert work == [20]
    with pytest.raises(contract._AnalysisLimitError, match="root reference work exceeds 10"):
        contract._validate_abstract_values(
            (contract._UNKNOWN_VALUE,) * 11,
            aggregate_limit=10,
            _unique_node_aggregate=True,
        )


def test_collection_root_keeps_member_aggregate_and_depth_bounds(contract):
    value = contract._UNKNOWN_VALUE
    for _ in range(contract._MAX_ABSTRACT_DEPTH - 2):
        value = contract._AbstractValue(items=(value,))
    root = contract._AbstractValue(items=(value,), _collection_root=True)
    assert (
        contract._validate_abstract_values(
            (root,),
            aggregate_limit=contract._MAX_ABSTRACT_OPERATION_WORK,
            allow_collection_roots=True,
        )[1]
        == contract._MAX_ABSTRACT_DEPTH
    )
    with pytest.raises(contract._AnalysisLimitError, match="depth exceeds 32"):
        contract._AbstractValue(
            items=(contract._AbstractValue(items=(value,)),), _collection_root=True
        )
    wide = object.__new__(contract._AbstractValue)
    for name, member in vars(contract._UNKNOWN_VALUE).items():
        object.__setattr__(wide, name, member)
    object.__setattr__(wide, "items", (contract._UNKNOWN_VALUE,) * 9000)
    object.__setattr__(wide, "_collection_root", True)
    with pytest.raises(contract._AnalysisLimitError, match="aggregate expanded cost exceeds 8192"):
        contract._validate_abstract_values(
            (wide,),
            aggregate_limit=contract._MAX_ABSTRACT_OPERATION_WORK,
            allow_collection_roots=True,
        )


def test_unique_snapshot_traversal_still_fails_closed(contract, monkeypatch):
    value = contract._AbstractValue(items=(contract._UNKNOWN_VALUE,) * 3)
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_OPERATION_WORK", 2)
    with pytest.raises(contract._AnalysisLimitError, match="traversal exceeds 2"):
        contract._validate_abstract_values(
            (value,),
            aggregate_limit=100,
            _unique_node_aggregate=True,
        )


def test_snapshot_unique_union_counts_distinct_marked_members(contract, tmp_path):
    path = tmp_path / "fixture.py"
    index = contract._SourceIndex(tmp_path, {path: contract.ast.parse("")})
    visitor = contract._RuntimeReadVisitor(frozenset(), index, index.module_for_path(path))
    first = contract._AbstractValue(
        items=tuple(contract._AbstractValue(origins=frozenset({f"a{i}"})) for i in range(2200)),
        _collection_root=True,
    )
    second = contract._AbstractValue(
        items=tuple(contract._AbstractValue(origins=frozenset({f"b{i}"})) for i in range(2200)),
        _collection_root=True,
    )
    visitor._states[-1] = {"first": first, "second": second}
    with pytest.raises(contract._AnalysisLimitError, match="graph node limit"):
        visitor._binding_snapshot()


def test_snapshot_rechecks_cached_member_depth_with_collection_offset(contract, tmp_path):
    path = tmp_path / "fixture.py"
    index = contract._SourceIndex(tmp_path, {path: contract.ast.parse("")})
    visitor = contract._RuntimeReadVisitor(frozenset(), index, index.module_for_path(path))
    deep = contract._UNKNOWN_VALUE
    for _ in range(contract._MAX_ABSTRACT_DEPTH - 1):
        deep = contract._AbstractValue(items=(deep,))
    marked = object.__new__(contract._AbstractValue)
    for name, member in vars(contract._UNKNOWN_VALUE).items():
        object.__setattr__(marked, name, member)
    object.__setattr__(marked, "items", (deep,))
    object.__setattr__(marked, "_collection_root", True)
    visitor._states[-1] = {"deep": deep, "marked": marked}
    with pytest.raises(contract._AnalysisLimitError, match="depth exceeds 32"):
        visitor._binding_snapshot()


def test_join_snapshot_shared_identity_plan_is_bounded_and_reused(contract, tmp_path):
    path = tmp_path / "fixture.py"
    index = contract._SourceIndex(tmp_path, {path: contract.ast.parse("")})
    visitor = contract._RuntimeReadVisitor(frozenset(), index, index.module_for_path(path))
    root = contract._AbstractValue(items=(contract._UNKNOWN_VALUE,) * 1200)
    snapshots = tuple(({"x": root}, {}, {"x": root}) for _ in range(64))
    state, _, annotations = visitor._join_binding_snapshots(*snapshots)
    assert state["x"] is root
    assert annotations["x"] is root
    assert visitor._join_binding_snapshots((state, {}, annotations))[0]["x"] is root


def test_join_snapshot_uses_only_its_prevalidated_identity_plan(contract, tmp_path, monkeypatch):
    path = tmp_path / "fixture.py"
    index = contract._SourceIndex(tmp_path, {path: contract.ast.parse("")})
    visitor = contract._RuntimeReadVisitor(frozenset(), index, index.module_for_path(path))
    root = contract._AbstractValue(items=(contract._UNKNOWN_VALUE,) * 8)
    calls: list[str] = []
    original = contract._validate_abstract_values

    def observe(values, **kwargs):
        calls.append(kwargs.get("boundary", "value"))
        return original(values, **kwargs)

    monkeypatch.setattr(contract, "_validate_abstract_values", observe)
    state, _, annotations = visitor._join_binding_snapshots(
        ({"x": root}, {}, {"x": root}),
        ({"x": root}, {}, {"x": root}),
    )
    assert state["x"] is root
    assert annotations["x"] is root
    assert calls.count("joined_binding_snapshot") == 2
    assert "join_identity" not in calls
    assert "join_input" not in calls


def test_join_snapshot_rejects_foreign_prevalidation_token(contract, monkeypatch):
    context = contract._JoinValidationContext()
    value = contract._UNKNOWN_VALUE
    foreign = contract._JoinOperandGroup((value, value), True, object())
    boundaries: list[str] = []
    original = contract._validate_abstract_values

    def observe(values, **kwargs):
        boundaries.append(kwargs.get("boundary", "value"))
        return original(values, **kwargs)

    monkeypatch.setattr(contract, "_validate_abstract_values", observe)
    assert (
        contract._join_values(
            value,
            value,
            _validation_context=context,
            _same_identity=True,
            _prevalidated_group=foreign,
        )
        is value
    )
    assert "join_identity" in boundaries


def test_join_snapshot_high_fanin_reference_budget_boundary(contract, tmp_path, monkeypatch):
    path = tmp_path / "fixture.py"
    index = contract._SourceIndex(tmp_path, {path: contract.ast.parse("")})
    visitor = contract._RuntimeReadVisitor(frozenset(), index, index.module_for_path(path))
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_OPERATION_WORK", 12)
    snapshots = tuple(({"x": contract._UNKNOWN_VALUE}, {}, {}) for _ in range(6))
    assert visitor._join_binding_snapshots(*snapshots)[0]["x"] is contract._UNKNOWN_VALUE
    over = snapshots + (({"x": contract._UNKNOWN_VALUE}, {}, {}),)
    with pytest.raises(
        contract._AnalysisLimitError, match="operand-reference cost exceeds 12"
    ) as caught:
        visitor._join_binding_snapshots(*over)
    assert '"operand_references": 7' in str(caught.value)


def test_join_snapshot_mixed_work_above_8192_is_bounded_by_output_budget(
    contract, tmp_path, monkeypatch
):
    path = tmp_path / "fixture.py"
    index = contract._SourceIndex(tmp_path, {path: contract.ast.parse("")})
    visitor = contract._RuntimeReadVisitor(frozenset(), index, index.module_for_path(path))

    first = contract._AbstractValue(origins=frozenset(f"a{i}" for i in range(4100)))
    second = contract._AbstractValue(origins=frozenset(f"b{i}" for i in range(4100)))
    input_cost = sum(contract._validate_abstract_values((value,))[0] for value in (first, second))
    assert input_cost > 8192
    snapshots = (({"x": first}, {}, {}), ({"x": second}, {}, {}))
    reserves = []
    original_reserve = contract._reserve_join_output

    def observe_reserve(work, amount, boundary):
        reserves.append((work, work[0], amount, boundary))
        return original_reserve(work, amount, boundary)

    monkeypatch.setattr(contract, "_reserve_join_output", observe_reserve)
    assert "x" in visitor._join_binding_snapshots(*snapshots)[0]
    result_validation = [item for item in reserves if item[3] == "result_validation_walks"]
    assert result_validation
    outer_reserve = result_validation[-1]
    # Projected result cost reflects the retained union shape rather than
    # repeating the summed expanded input cost as a result-validation proxy.
    assert 0 < outer_reserve[2] < input_cost * 2
    assert outer_reserve[0][0] <= contract._MAX_ABSTRACT_JOIN_OUTPUT_WORK

    # The same input must fail at the pre-construction reservation when the
    # remaining operation budget is one unit too small.
    capped = outer_reserve[1] + outer_reserve[2] - 1
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", capped)
    reserves.clear()
    with pytest.raises(contract._AnalysisLimitError, match="join output work") as caught:
        visitor._join_binding_snapshots(*snapshots)
    assert '"boundary": "result_validation_walks"' in str(caught.value)


def test_join_snapshot_mixed_cost_is_not_double_charged_as_unique_overhead(contract, tmp_path):
    path = tmp_path / "fixture.py"
    index = contract._SourceIndex(tmp_path, {path: contract.ast.parse("")})
    visitor = contract._RuntimeReadVisitor(frozenset(), index, index.module_for_path(path))
    first = contract._AbstractValue(items=(contract._UNKNOWN_VALUE,) * 1250)
    second = contract._AbstractValue(items=(contract._UNKNOWN_VALUE,) * 1250)
    assert first is not second
    state, _, _ = visitor._join_binding_snapshots(({"x": first}, {}, {}), ({"x": second}, {}, {}))
    assert len(state["x"].items or ()) == 1250


def test_unknown_sentinel_comparison_matches_dataclass_equality_for_every_field(contract):
    function = contract.ast.parse("def reader(): pass").body[0]
    class_node = contract.ast.parse("class Reader: pass").body[0]
    unresolved = contract._UnresolvedCallable(function, "decorator", "module", 1)
    cases = {
        "origins": frozenset({"origin"}),
        "items": (contract._UNKNOWN_VALUE,),
        "entries": (("key", contract._UNKNOWN_VALUE),),
        "attributes": (("name", contract._UNKNOWN_VALUE),),
        "identity": frozenset({1}),
        "literal": "literal",
        "string_value": "value",
        "string_values": frozenset({"value"}),
        "truth": True,
        "classes": frozenset({class_node}),
        "instance_classes": frozenset({class_node}),
        "modules": frozenset({"module"}),
        "callables": (contract._CallableTarget(function),),
        "serialized_sections": frozenset({"section"}),
        "accessed_attributes": frozenset({"attribute"}),
        "unresolved_callables": (unresolved,),
        "unknown_alternative": True,
    }
    for expected_comparisons, (field_name, field_value) in enumerate(cases.items(), start=1):
        value = contract._AbstractValue(**{field_name: field_value})
        work = [0]
        assert contract._equals_unknown_value(value, work) == (value == contract._UNKNOWN_VALUE), (
            field_name
        )
        assert work[0] == expected_comparisons, field_name


@pytest.mark.parametrize(
    "value",
    [
        None,
        pytest.param((), id="empty-tuple"),
        pytest.param("child", id="one-child-tuple"),
    ],
)
def test_unknown_sentinel_comparison_preserves_none_empty_and_large_shape_semantics(
    contract, value
):
    if value == "child":
        value = (contract._UNKNOWN_VALUE,)
    instance = contract._AbstractValue(items=value)
    work = [0]
    assert contract._equals_unknown_value(instance, work) == (instance == contract._UNKNOWN_VALUE)
    assert work[0] == (17 if instance == contract._UNKNOWN_VALUE else 2)

    large = contract._AbstractValue(
        origins=frozenset(f"origin-{index}" for index in range(4000)),
        items=(contract._UNKNOWN_VALUE,) * 4000,
    )
    large_work = [0]
    assert not contract._equals_unknown_value(large, large_work)
    assert large_work[0] == 1


def test_unknown_sentinel_comparison_excludes_collection_root_and_short_circuits(
    contract,
):
    root = contract._AbstractValue(items=(), _collection_root=True)
    root_work = [0]
    ordinary = contract._AbstractValue(items=())
    assert root == ordinary
    assert contract._equals_unknown_value(root, root_work) == (root == contract._UNKNOWN_VALUE)
    assert root_work[0] == 2

    subclass = type("AbstractValueSubclass", (contract._AbstractValue,), {})()
    subclass_work = [0]
    assert (subclass == contract._UNKNOWN_VALUE) is False
    assert not contract._equals_unknown_value(subclass, subclass_work)
    assert subclass_work[0] == 0


def test_unknown_sentinel_comparison_charges_one_unit_per_field_before_compare(
    contract, monkeypatch
):
    work = [0]
    assert contract._equals_unknown_value(contract._UNKNOWN_VALUE, work)
    assert work[0] == 17

    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", 16)
    capped = [0]
    with pytest.raises(contract._AnalysisLimitError) as caught:
        contract._equals_unknown_value(contract._UNKNOWN_VALUE, capped)
    assert '"boundary": "unknown_alternative_field_comparison"' in str(caught.value)
    assert capped[0] == 16


def test_join_snapshot_mixed_structurally_unequal_values_keep_both_shapes(contract, tmp_path):
    path = tmp_path / "fixture.py"
    index = contract._SourceIndex(tmp_path, {path: contract.ast.parse("")})
    visitor = contract._RuntimeReadVisitor(frozenset(), index, index.module_for_path(path))
    shared = contract._AbstractValue(origins=frozenset({"evaluation"}))
    first = contract._AbstractValue(origins=frozenset({"evaluation"}), items=(shared, shared))
    second = contract._AbstractValue(origins=frozenset({"consensus"}), items=(shared,))
    state, _, _ = visitor._join_binding_snapshots(({"x": first}, {}, {}), ({"x": second}, {}, {}))
    assert state["x"].items == (shared,)
    assert state["x"].origins == frozenset({"evaluation", "consensus"})


def test_append_abrupt_uses_join_snapshot_plan_for_matching_paths(contract, tmp_path):
    path = tmp_path / "fixture.py"
    index = contract._SourceIndex(tmp_path, {path: contract.ast.parse("")})
    visitor = contract._RuntimeReadVisitor(frozenset(), index, index.module_for_path(path))
    first = contract._AbstractValue(origins=frozenset({"evaluation"}))
    second = contract._AbstractValue(origins=frozenset({"consensus"}))
    left = ({"value": first}, {}, {"value": first})
    right = ({"value": second}, {}, {"value": second})
    target = [contract._AbruptPath("return", left)]
    visitor._append_abrupt(target, contract._AbruptPath("return", right))
    assert len(target) == 1
    assert target[0].bindings[0]["value"].origins == frozenset({"evaluation", "consensus"})


def test_join_snapshot_repeated_mixed_vector_reuses_the_join_result(contract, tmp_path):
    path = tmp_path / "fixture.py"
    index = contract._SourceIndex(tmp_path, {path: contract.ast.parse("")})
    visitor = contract._RuntimeReadVisitor(frozenset(), index, index.module_for_path(path))
    values = tuple(
        contract._AbstractValue(items=(contract._UNKNOWN_VALUE,) * 750) for _ in range(3)
    )
    snapshots = tuple(({"x": value, "y": value}, {}, {}) for value in values)
    state, _, _ = visitor._join_binding_snapshots(*snapshots)
    assert state["x"] is state["y"]


def test_join_snapshot_mixed_memo_stays_within_separate_operation_and_graph_budgets(
    contract, tmp_path, monkeypatch
):
    path = tmp_path / "fixture.py"
    index = contract._SourceIndex(tmp_path, {path: contract.ast.parse("")})
    visitor = contract._RuntimeReadVisitor(frozenset(), index, index.module_for_path(path))
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_OPERATION_WORK", 29)
    shared = contract._AbstractValue(items=(contract._UNKNOWN_VALUE,))
    mixed = tuple(contract._AbstractValue(items=(shared,)) for _ in range(3))
    snapshots = tuple(({"mixed": value}, {}, {"same": shared}) for value in mixed)
    state, _, annotation = visitor._join_binding_snapshots(*snapshots)
    assert state["mixed"] is not shared
    assert annotation["same"] is shared


def test_join_snapshot_reuses_mixed_preflight_memo_for_actual_join(contract, tmp_path, monkeypatch):
    path = tmp_path / "fixture.py"
    index = contract._SourceIndex(tmp_path, {path: contract.ast.parse("")})
    visitor = contract._RuntimeReadVisitor(frozenset(), index, index.module_for_path(path))
    leaf = contract._AbstractValue()
    first = contract._AbstractValue(items=(leaf,) * 200)
    second = contract._AbstractValue(items=(leaf,) * 200)
    traversals: list[tuple[str, int]] = []
    original = contract._validate_abstract_values

    def observe(values, **kwargs):
        work = kwargs.get("_shared_work")
        before = work[0] if work is not None else 0
        result = original(values, **kwargs)
        after = work[0] if work is not None else before
        traversals.append((kwargs.get("boundary", ""), after - before))
        return result

    monkeypatch.setattr(contract, "_validate_abstract_values", observe)
    state, _, _ = visitor._join_binding_snapshots(({"x": first}, {}, {}), ({"x": second}, {}, {}))
    assert len(state["x"].items or ()) == 200
    assert any(boundary == "joined_binding_snapshot" and work > 0 for boundary, work in traversals)
    assert not any(boundary in {"join_input", "join_identity"} for boundary, _ in traversals)


def test_join_snapshot_memo_reuses_identical_mixed_results_across_bindings(contract, tmp_path):
    path = tmp_path / "fixture.py"
    index = contract._SourceIndex(tmp_path, {path: contract.ast.parse("")})
    visitor = contract._RuntimeReadVisitor(frozenset(), index, index.module_for_path(path))
    operands = tuple(
        contract._AbstractValue(origins=frozenset(f"{index}-{item}" for item in range(2100)))
        for index in range(3)
    )
    snapshots = tuple(
        ({"left": value, "right": value}, {}, {"annotation": value}) for value in operands
    )
    joined, _, annotations = visitor._join_binding_snapshots(*snapshots)
    expected = contract._join_values(*operands)
    assert joined["left"] is joined["right"]
    assert joined["left"].origins == expected.origins
    assert annotations["annotation"].origins == expected.origins


def test_join_result_memo_replays_work_and_keeps_operand_order(contract, monkeypatch):
    context = contract._JoinValidationContext()
    first = contract._AbstractValue(origins=frozenset({"first"}))
    second = contract._AbstractValue(origins=frozenset({"second"}))
    forward = contract._join_values(first, second, _validation_context=context)
    assert contract._join_values(first, second, _validation_context=context) is forward
    reverse = contract._join_values(second, first, _validation_context=context)
    assert reverse is not forward
    assert reverse.origins == forward.origins

    nested_first = contract._AbstractValue(items=(first,))
    nested_second = contract._AbstractValue(items=(second,))
    shallow = contract._join_values(
        nested_first, nested_second, _validation_context=context, _depth=0
    )
    deep = contract._join_values(nested_first, nested_second, _validation_context=context, _depth=8)
    assert shallow is not deep
    assert shallow.items is not None
    assert deep.items is None

    limited = contract._JoinValidationContext()
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_WORK", 1)
    contract._join_values(first, second, _validation_context=limited)
    with pytest.raises(contract._AnalysisLimitError, match="abstract join work exceeds 1"):
        contract._join_values(first, second, _validation_context=limited)


def test_join_output_budget_reserves_metadata_union_before_construction(contract, monkeypatch):
    first = contract._AbstractValue(origins=frozenset({"a" * 64, "b" * 64}))
    second = contract._AbstractValue(origins=frozenset({"c" * 64, "d" * 64}))
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", 8)
    with pytest.raises(contract._AnalysisLimitError, match="join output work") as caught:
        contract._join_values(first, second)
    assert '"boundary": "origins_payload_scan"' in str(caught.value)


def test_join_output_budget_reserves_mapping_copy_and_key_product(contract, monkeypatch):
    leaf = contract._AbstractValue(origins=frozenset({"source"}))
    names = tuple((f"key-{index}", leaf) for index in range(4))
    first = contract._AbstractValue(entries=names)
    second = contract._AbstractValue(entries=names)
    context = contract._JoinValidationContext()
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", 275)
    with pytest.raises(contract._AnalysisLimitError, match="join output work") as caught:
        contract._join_values(first, second, _validation_context=context)
    assert '"boundary": "entry_key_lookup_and_candidates"' in str(caught.value)
    assert '"requested_work": 24' in str(caught.value)
    assert context.output_work[0] <= 285


def test_join_snapshot_rejects_raw_reference_lower_bound_before_key_union(contract, monkeypatch):
    class KeysForbidden(dict):
        def keys(self):
            raise AssertionError("key union ran before raw-reference admission")

    state = KeysForbidden({f"name-{index}": contract._UNKNOWN_VALUE for index in range(3)})
    snapshots = ((state, {}, {}), (state, {}, {}))
    path = Path("fixture.py")
    index = contract._SourceIndex(Path("."), {path: contract.ast.parse("")})
    visitor = contract._RuntimeReadVisitor(frozenset(), index, index.module_for_path(path))
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_OPERATION_WORK", 4)
    with pytest.raises(contract._AnalysisLimitError, match="operand-reference cost") as caught:
        visitor._join_binding_snapshots(*snapshots)
    assert '"boundary": "join_operand_reference_lower_bound"' in str(caught.value)


def test_join_output_budget_reserves_deep_callable_hash_work(contract, monkeypatch):
    function = contract.ast.parse("def reader(): pass").body[0]
    receiver = contract._AbstractValue(origins=frozenset({"receiver-" + "x" * 256}))
    target = contract._CallableTarget(function, receiver)
    first = contract._AbstractValue(callables=(target,))
    second = contract._AbstractValue(callables=(target,))
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", 1024)

    def forbidden_hash(_value):
        raise AssertionError("callable hashing began before receiver work was reserved")

    monkeypatch.setattr(contract._AbstractValue, "__hash__", forbidden_hash)
    with pytest.raises(contract._AnalysisLimitError, match="join output work") as caught:
        contract._join_values(first, second)
    assert '"boundary": "callable_hash_and_equality"' in str(caught.value)


def test_join_uncached_shared_deep_receivers_charge_actual_validation_work(contract, monkeypatch):
    reader_a, reader_b = contract.ast.parse("def reader_a(): pass\ndef reader_b(): pass").body
    leaf = contract._AbstractValue(origins=frozenset({"receiver"}))
    receiver = contract._AbstractValue(items=(leaf,) * 8)
    targets = (
        contract._CallableTarget(reader_a, receiver),
        contract._CallableTarget(reader_b, receiver),
    )
    first = contract._AbstractValue(callables=targets)
    second = contract._AbstractValue(callables=targets)
    monkeypatch.setattr(contract, "_get_validation_cache_entry", lambda _key: None)

    original_validate = contract._validate_abstract_values
    profile_validations: list[tuple[int, int]] = []

    def observe_validation(values, **kwargs):
        if kwargs.get("boundary") != "callable_receiver_profile":
            return original_validate(values, **kwargs)
        output_work = kwargs.get("_join_output_work")
        assert output_work is not None
        before = output_work[0]
        result = original_validate(values, **kwargs)
        profile_validations.append((before, output_work[0] - before))
        return result

    reserves: list[tuple[int, str]] = []
    original_reserve = contract._reserve_join_output

    def observe_reserve(work, amount, boundary):
        reserves.append((amount, boundary))
        return original_reserve(work, amount, boundary)

    monkeypatch.setattr(contract, "_validate_abstract_values", observe_validation)
    monkeypatch.setattr(contract, "_reserve_join_output", observe_reserve)
    joined = contract._join_values(first, second)

    assert joined.callables == targets
    assert len(profile_validations) == 2
    assert all(before >= 0 and actual > 0 for before, actual in profile_validations)
    assert any(name == "join_input_validation_node" for _, name in reserves)
    assert any(name == "join_input_validation_edge" for _, name in reserves)
    assert not any(name == "callable_receiver_validation_upper_bound" for _, name in reserves)

    before, actual_receiver_work = profile_validations[0]
    monkeypatch.setattr(
        contract,
        "_MAX_ABSTRACT_JOIN_OUTPUT_WORK",
        before + actual_receiver_work - 1,
    )

    def forbidden_hash(_value):
        raise AssertionError("callable hashing began before receiver validation")

    monkeypatch.setattr(contract._AbstractValue, "__hash__", forbidden_hash)
    with pytest.raises(contract._AnalysisLimitError, match="join output work") as caught:
        contract._join_values(first, second)
    assert '"boundary": "join_input_validation_' in str(caught.value)


def test_join_cached_callable_receiver_uses_bounded_cache_profile(contract, monkeypatch):
    function = contract.ast.parse("def reader(): pass").body[0]
    receiver = contract._AbstractValue(origins=frozenset({"cached receiver"}))
    target = contract._CallableTarget(function, receiver)
    first = contract._AbstractValue(callables=(target,))
    second = contract._AbstractValue(callables=(target,))
    receiver_entry = contract._get_validation_cache_entry(id(receiver))
    assert receiver_entry is not None and receiver_entry.root is receiver

    original_get = contract._get_validation_cache_entry
    monkeypatch.setattr(
        contract,
        "_get_validation_cache_entry",
        lambda key: receiver_entry if key == id(receiver) else original_get(key),
    )
    profile_scans: list[str] = []
    original_reserve = contract._reserve_join_output

    def observe_reserve(work, amount, boundary):
        if boundary.startswith("callable_receiver_"):
            profile_scans.append(boundary)
        return original_reserve(work, amount, boundary)

    monkeypatch.setattr(contract, "_reserve_join_output", observe_reserve)
    joined = contract._join_values(first, second)

    assert joined.callables == (target,)
    assert "callable_receiver_profile_scan" in profile_scans
    assert "callable_receiver_validation_upper_bound" not in profile_scans


def test_callable_receiver_validation_output_work_exact_and_one_under(contract, monkeypatch):
    leaf = contract._AbstractValue(origins=frozenset({"deep"}))
    receiver = contract._AbstractValue(items=(leaf,) * 4)
    monkeypatch.setattr(contract, "_get_validation_cache_entry", lambda _key: None)

    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", 10000)
    measured = [0]
    contract._validate_abstract_values(
        (receiver,),
        allow_collection_roots=True,
        boundary="callable_receiver_profile",
        _join_output_work=measured,
    )
    exact_work = measured[0]
    assert exact_work > 0

    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", exact_work)
    exact = [0]
    contract._validate_abstract_values(
        (receiver,),
        allow_collection_roots=True,
        boundary="callable_receiver_profile",
        _join_output_work=exact,
    )
    assert exact[0] == exact_work

    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", exact_work - 1)
    one_under = [0]
    with pytest.raises(contract._AnalysisLimitError, match="join output work") as caught:
        contract._validate_abstract_values(
            (receiver,),
            allow_collection_roots=True,
            boundary="callable_receiver_profile",
            _join_output_work=one_under,
        )
    assert one_under[0] < exact_work
    assert '"boundary": "join_input_validation_' in str(caught.value)


def test_join_result_memo_hit_charges_lookup_and_retains_bounded_result(contract, monkeypatch):
    first = contract._AbstractValue(origins=frozenset({"first"}))
    second = contract._AbstractValue(origins=frozenset({"second"}))
    context = contract._JoinValidationContext()
    result = contract._join_values(first, second, _validation_context=context)
    entry = next(iter(context.result_memo.values()))
    assert entry.output_work > 0
    context.output_work[0] = 0
    # Even a memo hit must reserve the bounded operand validation and identity
    # lookup scans performed before the memo table can be consulted.
    hit_budget = len(entry.operands) * 3
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", hit_budget)
    assert contract._join_values(first, second, _validation_context=context) is result
    assert context.output_work[0] == hit_budget
    context.output_work[0] = 0
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", hit_budget - 1)
    with pytest.raises(contract._AnalysisLimitError, match="join output work"):
        contract._join_values(first, second, _validation_context=context)


def test_join_result_memo_is_operation_local_and_does_not_admit_foreign_context(contract):
    first = contract._AbstractValue(origins=frozenset({"first"}))
    second = contract._AbstractValue(origins=frozenset({"second"}))
    left_context = contract._JoinValidationContext()
    right_context = contract._JoinValidationContext()
    first_result = contract._join_values(first, second, _validation_context=left_context)
    second_result = contract._join_values(first, second, _validation_context=right_context)
    assert second_result is not first_result
    assert not right_context.result_memo or all(
        entry.result is not first_result for entry in right_context.result_memo.values()
    )


def test_join_snapshot_missing_names_use_unknown_operand_and_count_refs(
    contract, tmp_path, monkeypatch
):
    path = tmp_path / "fixture.py"
    index = contract._SourceIndex(tmp_path, {path: contract.ast.parse("")})
    visitor = contract._RuntimeReadVisitor(frozenset(), index, index.module_for_path(path))
    value = contract._AbstractValue(origins=frozenset({"evaluation"}))
    snapshots = (({"left": value}, {}, {}), ({"right": value}, {}, {}))
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_OPERATION_WORK", 4)
    with pytest.raises(contract._AnalysisLimitError, match="operand-reference cost"):
        visitor._join_binding_snapshots(*snapshots)


def test_join_snapshot_validates_direct_and_embedded_marked_roots(contract, tmp_path):
    path = tmp_path / "fixture.py"
    index = contract._SourceIndex(tmp_path, {path: contract.ast.parse("")})
    visitor = contract._RuntimeReadVisitor(frozenset(), index, index.module_for_path(path))
    marked = contract._AbstractValue(items=(contract._UNKNOWN_VALUE,) * 2050, _collection_root=True)
    assert (
        contract._validate_abstract_values(
            (marked,),
            aggregate_limit=contract._MAX_ABSTRACT_OPERATION_WORK,
            allow_collection_roots=True,
        )[0]
        > contract._MAX_ABSTRACT_WORK
    )
    embedded = contract._AbstractValue(items=(marked,))
    assert embedded.items is not None and embedded.items[0] is marked
    snapshots = (({"direct": marked}, {}, {}), ({"direct": marked}, {}, {}))
    state, _, _ = visitor._join_binding_snapshots(*snapshots)
    assert state["direct"] is marked


@pytest.mark.parametrize("receiver", [False, True])
def test_operation_change_preserves_shared_dag_rejection_without_eq_or_hash(
    contract, monkeypatch, receiver
):
    def forbidden(*_args):
        raise AssertionError("DAG rejection reached structural equality/hash")

    monkeypatch.setattr(contract._AbstractValue, "__eq__", forbidden)
    monkeypatch.setattr(contract._AbstractValue, "__hash__", forbidden)
    monkeypatch.setattr(contract._CallableTarget, "__eq__", forbidden)
    monkeypatch.setattr(contract._CallableTarget, "__hash__", forbidden)
    value = contract._UNKNOWN_VALUE
    function = contract.ast.parse("def reader(): pass").body[0]
    with pytest.raises(contract._AnalysisLimitError, match="value expanded cost exceeds 131072"):
        for _ in range(20):
            if receiver:
                target = contract._CallableTarget(function, value)
                value = contract._AbstractValue(callables=(target, target))
            else:
                value = contract._AbstractValue(items=(value, value))


def test_validator_traversal_budget_is_independent_of_aggregate_allowance(contract, monkeypatch):
    value = contract._AbstractValue(items=(contract._UNKNOWN_VALUE,) * 3)
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_OPERATION_WORK", 2)
    with pytest.raises(contract._AnalysisLimitError, match="traversal exceeds 2"):
        contract._validate_abstract_values((value,), aggregate_limit=100)


def test_join_scalar_comparison_budget_tracks_short_circuit_prefix(contract, monkeypatch):
    first = contract._AbstractValue(literal="same")
    equal_copy = contract._AbstractValue(literal=b"same".decode())
    third = contract._AbstractValue(literal="different")
    output_work = [0]
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", 19)
    assert (
        contract._joined_scalar_string((first, equal_copy, third), "literal", output_work) is None
    )
    assert output_work == [19]

    output_work[0] = 0
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", 18)
    with pytest.raises(contract._AnalysisLimitError, match="join output work"):
        contract._joined_scalar_string((first, equal_copy, third), "literal", output_work)
    assert output_work == [18]

    equal_values = tuple(contract._AbstractValue(literal=b"long-text".decode()) for _ in range(3))
    output_work[0] = 0
    all_equal_work = 1 + 2 * (1 + 4 * len("long-text"))
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", all_equal_work)
    assert contract._joined_scalar_string(equal_values, "literal", output_work) == "long-text"
    assert output_work == [all_equal_work]

    output_work[0] = 0
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", all_equal_work - 1)
    with pytest.raises(contract._AnalysisLimitError, match="join output work"):
        contract._joined_scalar_string(equal_values, "literal", output_work)
    assert output_work == [1 + (1 + 4 * len("long-text"))]

    class UnsafeString(str):
        def __eq__(self, other):
            raise AssertionError("comparison after the first mismatch must not run")

    output_work[0] = 0
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", 18)
    assert (
        contract._joined_scalar_string(
            (
                contract._AbstractValue(literal="same"),
                contract._AbstractValue(literal="else"),
                contract._AbstractValue(literal=UnsafeString("later")),
            ),
            "literal",
            output_work,
        )
        is None
    )
    assert output_work == [18]


def test_join_scalar_comparison_rejects_string_subclass_before_custom_equality(
    contract,
):
    class UnsafeString(str):
        def __eq__(self, other):
            raise AssertionError("custom equality must not run")

    with pytest.raises(contract._AnalysisLimitError, match="unsupported literal"):
        contract._joined_scalar_string(
            (contract._AbstractValue(literal=UnsafeString("x")),),
            "literal",
            [0],
        )


def test_join_string_value_hash_is_identity_cached_and_insertions_are_per_operand(
    contract, monkeypatch
):
    shared = "shared-value"
    repeated = tuple(contract._AbstractValue(string_value=shared) for _ in range(3))
    exact_work = 3 + 4 * len(shared) + 2 * 3
    output_work = [0]
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", exact_work)
    contract._reserve_join_string_value_work(repeated, output_work)
    assert output_work == [exact_work]

    output_work[0] = 0
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", exact_work - 1)
    with pytest.raises(contract._AnalysisLimitError, match="join output work"):
        contract._reserve_join_string_value_work(repeated, output_work)
    assert output_work == [3 + 4 * len(shared)]


def test_join_string_value_hash_budget_reserves_before_hash(contract, monkeypatch):
    text = "hash-before-insert"
    value = contract._AbstractValue(string_value=text)
    hash_calls = []
    monkeypatch.setattr(
        contract,
        "hash",
        lambda candidate: hash_calls.append(candidate) or 0,
        raising=False,
    )
    output_work = [0]
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", 4 * len(text))
    with pytest.raises(contract._AnalysisLimitError) as caught:
        contract._reserve_join_string_value_work((value,), output_work)
    assert '"boundary": "string_value_hash_payload"' in str(caught.value)
    assert output_work == [1]
    assert hash_calls == []

    output_work[0] = 0
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", 1 + 4 * len(text))
    with pytest.raises(contract._AnalysisLimitError) as caught:
        contract._reserve_join_string_value_work((value,), output_work)
    assert '"boundary": "string_value_set_insertions"' in str(caught.value)
    assert output_work == [1 + 4 * len(text)]
    assert hash_calls == [text]


def test_join_string_value_candidate_scan_exact_and_one_under(contract, monkeypatch):
    value = contract._AbstractValue()
    output_work = [0]
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", 1)
    contract._reserve_join_string_value_work((value,), output_work)
    assert output_work == [1]

    output_work[0] = 0
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", 0)
    with pytest.raises(contract._AnalysisLimitError) as caught:
        contract._reserve_join_string_value_work((value,), output_work)
    assert '"boundary": "string_value_candidate_scan"' in str(caught.value)
    assert output_work == [0]


def test_join_equal_distinct_strings_dedupe_retained_semantic_payload(contract):
    left_text = "equal-but-distinct-text"
    right_text = left_text.encode().decode()
    assert left_text == right_text and left_text is not right_text
    left = contract._AbstractValue(string_value=left_text, string_values=frozenset({left_text}))
    right = contract._AbstractValue(string_value=right_text, string_values=frozenset({right_text}))

    joined = contract._join_values(left, right)

    assert joined.string_value == left_text
    assert len(joined.string_values) == 1
    assert joined.string_values == frozenset({left_text})

    output_work = [0]
    exact_hash_work = 2 + 8 * len(left_text) + 4
    contract._reserve_join_string_value_work(
        (
            contract._AbstractValue(string_value=left_text),
            contract._AbstractValue(string_value=right_text),
        ),
        output_work,
    )
    assert output_work == [exact_hash_work]


def test_join_retained_payload_reservation_exact_and_one_under(contract, monkeypatch):
    text = "x" * 65535
    retained = frozenset({text})
    exact_work = 1 + 4 * len(text)
    output_work = [0]
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", exact_work)
    contract._reserve_join_retained_string_payload(retained, None, output_work)
    assert output_work == [exact_work]

    output_work[0] = 0
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", exact_work - 1)
    with pytest.raises(contract._AnalysisLimitError, match="join output work"):
        contract._reserve_join_retained_string_payload(retained, None, output_work)
    assert output_work == [1]


def test_join_retained_payload_scan_exact_and_one_under(contract, monkeypatch):
    text = "x"
    output_work = [0]
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", 1)
    with pytest.raises(contract._AnalysisLimitError) as caught:
        contract._reserve_join_retained_string_payload(frozenset({text}), None, output_work)
    assert '"boundary": "retained_string_payload"' in str(caught.value)
    assert output_work == [1]

    output_work[0] = 0
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", 0)
    with pytest.raises(contract._AnalysisLimitError) as caught:
        contract._reserve_join_retained_string_payload(frozenset({text}), None, output_work)
    assert '"boundary": "retained_string_values_scan"' in str(caught.value)
    assert output_work == [0]


def test_join_retained_payload_dedupes_equal_distinct_literal(contract, monkeypatch):
    text = "same-payload"
    literal = text.encode().decode()
    assert literal == text and literal is not text
    retained = frozenset({text})
    exact_work = 1 + (1 + 8 * len(literal)) + (4 * len(text))
    output_work = [0]
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", exact_work)
    contract._reserve_join_retained_string_payload(retained, literal, output_work)
    assert output_work == [exact_work]

    output_work[0] = 0
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", exact_work - 1)
    with pytest.raises(contract._AnalysisLimitError, match="join output work"):
        contract._reserve_join_retained_string_payload(retained, literal, output_work)
    assert output_work == [1 + (1 + 8 * len(literal))]

    output_work[0] = 0
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", 8 * len(literal))
    with pytest.raises(contract._AnalysisLimitError) as caught:
        contract._reserve_join_retained_string_payload(frozenset(), literal, output_work)
    assert '"boundary": "retained_literal_membership"' in str(caught.value)
    assert output_work == [0]


def test_join_r14_shape_admits_under_unchanged_output_cap(contract):
    def text_with_length(index, length):
        return chr(0x1000 + index) + ("x" * (length - 1))

    literal_lengths = [214, 214] + [241] * 3 + [240] * 26
    value_lengths = [221 if index < 6833 % 31 else 220 for index in range(31)]
    literals = [text_with_length(index, length) for index, length in enumerate(literal_lengths)]
    string_values = [
        text_with_length(100 + index, length) for index, length in enumerate(value_lengths)
    ]
    literal_identities = literals + [literals[0].encode().decode(), literals[1].encode().decode()]
    joined_values = tuple(
        contract._AbstractValue(
            literal=literal_identities[index],
            string_value=string_values[index] if index < 31 else None,
            string_values=(frozenset({string_values[index]}) if index < 31 else frozenset()),
        )
        for index in range(33)
    )
    operands = tuple(joined_values[index % 33] for index in range(143))
    assert len({id(value) for value in operands}) == 33
    assert operands[0] is not operands[1]
    assert operands[0].literal != operands[1].literal
    assert operands[0].string_value != operands[1].string_value
    assert (
        sum(len(value.literal or "") + len(value.string_value or "") for value in joined_values)
        == 14652
    )

    output_work = [0]
    joined = contract._join_values(*operands, _output_work=output_work)

    assert contract._MAX_ABSTRACT_JOIN_OUTPUT_WORK == 262144
    assert output_work[0] <= contract._MAX_ABSTRACT_JOIN_OUTPUT_WORK
    assert joined.literal is None
    assert joined.string_value is None
    assert len(joined.string_values) == 31
    assert sum(map(len, joined.string_values)) == 6833


def test_join_large_string_values_fail_closed_before_result_construction(contract):
    value = contract._AbstractValue(string_values=frozenset({"x" * 70000}))
    with pytest.raises(contract._AnalysisLimitError, match="join output work") as caught:
        contract._join_values(value, contract._AbstractValue())
    assert '"boundary": "metadata_union"' in str(caught.value)


def _empty_join_result_fields(contract):
    from dataclasses import fields

    empty = contract._AbstractValue()
    return {
        item.name: getattr(empty, item.name)
        for item in fields(contract._AbstractValue)
        if item.name != "_collection_root"
    }


def test_join_result_validation_projection_uses_collapsed_prepared_shape(contract):
    text = "scalar-input" * 100
    inputs = (
        contract._AbstractValue(literal=text, string_value=text),
        contract._AbstractValue(literal=text.encode().decode(), string_value=None),
    )
    input_cost = sum(contract._validate_abstract_values((value,))[0] for value in inputs)
    fields = _empty_join_result_fields(contract)
    fields.update(literal=None, string_value=None)

    output_work = [0]
    projected_cost = contract._reserve_join_result_validation_work(
        fields, output_work, memo_admission=False
    )

    assert (
        projected_cost
        == contract._validate_abstract_values(
            [contract.SimpleNamespace(**fields, _collection_root=False)]
        )[0]
    )
    assert projected_cost < input_cost
    assert output_work[0] > projected_cost  # projection scan plus reserved constructor pass


def test_join_result_validation_projection_counts_shared_child_per_reference(contract):
    child = contract._AbstractValue(string_values=frozenset({"shared child"}))
    fields = _empty_join_result_fields(contract)
    fields["items"] = (child, child)

    projected_cost = contract._reserve_join_result_validation_work(
        fields, [0], memo_admission=False
    )
    constructed = contract._AbstractValue(**fields)
    actual_cost = contract._validate_abstract_values((constructed,))[0]

    assert projected_cost == actual_cost
    assert projected_cost >= contract._abstract_overhead(
        constructed
    ) + 2 * contract._abstract_overhead(child)


@pytest.mark.parametrize("memo_admission", [False, True])
def test_join_result_validation_projection_reserves_exact_or_one_under(
    contract, monkeypatch, memo_admission
):
    child = contract._AbstractValue(string_value="memo child")
    fields = _empty_join_result_fields(contract)
    fields["entries"] = (("key", child),)
    multiplier = 2 if memo_admission else 1

    output_work = [0]
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", 100000)
    projected_cost = contract._reserve_join_result_validation_work(
        fields, output_work, memo_admission=memo_admission
    )
    exact_total = output_work[0]
    projection_scan = exact_total - multiplier * projected_cost
    assert projection_scan > 0

    output_work[0] = 0
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", exact_total)
    assert (
        contract._reserve_join_result_validation_work(
            fields, output_work, memo_admission=memo_admission
        )
        == projected_cost
    )
    assert output_work == [exact_total]

    output_work[0] = 0
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", exact_total - 1)
    with pytest.raises(contract._AnalysisLimitError) as caught:
        contract._reserve_join_result_validation_work(
            fields, output_work, memo_admission=memo_admission
        )
    assert '"boundary": "result_validation_walks"' in str(caught.value)
    assert output_work == [projection_scan]


@pytest.mark.parametrize("cached", [False, True])
def test_callable_receiver_string_payload_rejects_before_hash(contract, monkeypatch, cached):
    receiver = contract._AbstractValue(literal="x" * 70000)
    function = contract.ast.parse("def reader(): pass").body[0]
    target = contract._CallableTarget(function, receiver)
    first = contract._AbstractValue(callables=(target,))
    second = contract._AbstractValue(callables=(target,))
    if not cached:
        monkeypatch.setattr(contract, "_get_validation_cache_entry", lambda _key: None)

    def forbidden_hash(_value):
        raise AssertionError("receiver hashing preceded its string payload reservation")

    monkeypatch.setattr(contract._AbstractValue, "__hash__", forbidden_hash)
    with pytest.raises(contract._AnalysisLimitError, match="join output work") as caught:
        contract._join_values(first, second)
    assert '"boundary": "callable_hash_and_equality"' in str(caught.value)
    assert '"requested_work": 280001' in str(caught.value)


@pytest.mark.parametrize("cached", [False, True])
def test_callable_receiver_profile_counts_shared_string_payload_once(contract, monkeypatch, cached):
    leaf = contract._AbstractValue(literal="payload")
    receiver = contract._AbstractValue(items=(leaf, leaf))
    function = contract.ast.parse("def reader(): pass").body[0]
    target = contract._CallableTarget(function, receiver)
    first = contract._AbstractValue(callables=(target,))
    second = contract._AbstractValue(callables=(target,))
    cost = contract._validate_abstract_values((receiver,))[0]
    if not cached:
        monkeypatch.setattr(contract, "_get_validation_cache_entry", lambda _key: None)
    reservations = []
    original_reserve = contract._reserve_join_output

    def observe(work, amount, boundary):
        if boundary == "callable_hash_and_equality":
            reservations.append(amount)
        return original_reserve(work, amount, boundary)

    monkeypatch.setattr(contract, "_reserve_join_output", observe)
    result = contract._join_values(first, second)
    assert result.callables == (target,)
    assert reservations == [cost + 4 * len("payload")]


@pytest.mark.parametrize("slot", [0, 2])
def test_join_snapshot_reuses_admitted_long_binding_names(contract, tmp_path, monkeypatch, slot):
    path = tmp_path / "fixture.py"
    index = contract._SourceIndex(tmp_path, {path: contract.ast.parse("")})
    visitor = contract._RuntimeReadVisitor(frozenset(), index, index.module_for_path(path))
    value = contract._AbstractValue(origins=frozenset({"evaluation"}))
    names = {f"setting_{number:03d}_" + "x" * 130: value for number in range(174)}
    first = ({}, {}, {})
    second = ({}, {}, {})
    first[slot].update(names)
    second[slot].update(names)
    result = visitor._join_binding_snapshots(first, second)
    assert set(result[slot]) == set(names)
    assert all(joined is value for joined in result[slot].values())

    # Admission still charges the complete payload before any key union.
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", 100_000)
    with pytest.raises(contract._AnalysisLimitError, match="binding_snapshot_key_bytes"):
        visitor._join_binding_snapshots(first, second)


@pytest.mark.parametrize("method", ["_join_states", "_join_annotation_bindings"])
def test_join_binding_foreign_plan_cannot_hide_missing_names(contract, method):
    context = contract._JoinValidationContext()
    value = contract._AbstractValue(origins=frozenset({"evaluation"}))
    with pytest.raises(KeyError, match="tracked"):
        getattr(contract._RuntimeReadVisitor, method)(
            {"tracked": value}, _plan={}, _context=context
        )


def _snapshot_proof_visitor(contract, tmp_path):
    path = tmp_path / "snapshot-proof.py"
    index = contract._SourceIndex(tmp_path, {path: contract.ast.parse("")})
    return contract._RuntimeReadVisitor(frozenset(), index, index.module_for_path(path))


def _capture_proven_snapshot(visitor, state, annotations=None):
    visitor._states[-1] = state
    visitor._annotations[-1] = annotations or {}
    return visitor._binding_snapshot()


def test_completed_snapshot_proof_replaces_join_preflight_and_survives_outputs(
    contract, tmp_path, monkeypatch
):
    visitor = _snapshot_proof_visitor(contract, tmp_path)
    first = _capture_proven_snapshot(
        visitor, {"x": contract._AbstractValue(origins=frozenset({"evaluation"}))}
    )
    second = _capture_proven_snapshot(
        visitor, {"x": contract._AbstractValue(origins=frozenset({"consensus"}))}
    )
    calls = []
    original = contract._validate_abstract_values

    def observe(values, **kwargs):
        calls.append(kwargs.get("boundary", "value"))
        return original(values, **kwargs)

    monkeypatch.setattr(contract, "_validate_abstract_values", observe)
    joined = visitor._join_binding_snapshots(first, second)
    assert type(joined) is contract._ProvenBindingSnapshot
    assert joined[0]["x"].origins == frozenset({"evaluation", "consensus"})
    assert visitor._join_binding_snapshots(joined, joined)[0]["x"] is joined[0]["x"]
    assert "joined_binding_snapshot" not in calls
    assert len(joined) == 3
    state, functions, annotations = joined
    assert (state, functions, annotations) == joined.slots
    with pytest.raises(AttributeError):
        joined.proof.nodes[0].cost = 0


def test_completed_snapshot_proof_changed_map_uses_legacy_validation(
    contract, tmp_path, monkeypatch
):
    visitor = _snapshot_proof_visitor(contract, tmp_path)
    snapshot = _capture_proven_snapshot(visitor, {"x": contract._UNKNOWN_VALUE})
    snapshot[0]["added"] = contract._AbstractValue(origins=frozenset({"evaluation"}))
    calls = []
    original = contract._validate_abstract_values

    def observe(values, **kwargs):
        calls.append(kwargs.get("boundary", "value"))
        return original(values, **kwargs)

    monkeypatch.setattr(contract, "_validate_abstract_values", observe)
    assert "added" in visitor._join_binding_snapshots(snapshot, snapshot)[0]
    assert "joined_binding_snapshot" in calls


@pytest.mark.parametrize("also_change_map", [False, True])
def test_completed_snapshot_proof_detects_forced_node_mutation(contract, tmp_path, also_change_map):
    visitor = _snapshot_proof_visitor(contract, tmp_path)
    leaf = contract._AbstractValue(origins=frozenset({"evaluation"}))
    snapshot = _capture_proven_snapshot(visitor, {"x": contract._AbstractValue(items=(leaf,))})
    object.__setattr__(leaf, "items", (contract._UNKNOWN_VALUE,) * 30)
    if also_change_map:
        snapshot[0]["added"] = contract._UNKNOWN_VALUE
    with pytest.raises(contract._AnalysisLimitError, match="snapshot proof value changed"):
        visitor._join_binding_snapshots(snapshot, snapshot)


def test_completed_snapshot_proof_detects_callable_receiver_mutation(contract, tmp_path):
    visitor = _snapshot_proof_visitor(contract, tmp_path)
    target = contract._CallableTarget(
        contract.ast.parse("def f(): pass").body[0], contract._UNKNOWN_VALUE
    )
    snapshot = _capture_proven_snapshot(
        visitor, {"x": contract._AbstractValue(callables=(target,))}
    )
    object.__setattr__(
        target, "receiver", contract._AbstractValue(origins=frozenset({"evaluation"}))
    )
    with pytest.raises(contract._AnalysisLimitError, match="snapshot proof callable changed"):
        visitor._join_binding_snapshots(snapshot, snapshot)


def test_completed_snapshot_proof_declines_mutable_fields_and_build_overflow(
    contract, tmp_path, monkeypatch
):
    visitor = _snapshot_proof_visitor(contract, tmp_path)
    mutable = contract._AbstractValue(items=[contract._UNKNOWN_VALUE])
    assert type(_capture_proven_snapshot(visitor, {"x": mutable})) is tuple
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", 10)
    assert type(_capture_proven_snapshot(visitor, {"x": contract._UNKNOWN_VALUE})) is tuple


def test_completed_snapshot_proof_preserves_mixed_root_multiplicity(
    contract, tmp_path, monkeypatch
):
    visitor = _snapshot_proof_visitor(contract, tmp_path)
    first_value = contract._AbstractValue(items=(contract._UNKNOWN_VALUE,) * 5)
    second_value = contract._AbstractValue(items=(contract._UNKNOWN_VALUE,) * 5)
    first = _capture_proven_snapshot(visitor, {"x": first_value})
    second = _capture_proven_snapshot(visitor, {"x": second_value})
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_EXPANDED_VALUE_WORK", 15)
    with pytest.raises(contract._AnalysisLimitError, match="aggregate expanded cost exceeds 15"):
        visitor._join_binding_snapshots(first, second)


def test_completed_snapshot_proof_rechecks_current_depth_limit(contract, tmp_path, monkeypatch):
    visitor = _snapshot_proof_visitor(contract, tmp_path)
    value = contract._AbstractValue(
        items=(contract._AbstractValue(items=(contract._UNKNOWN_VALUE,)),)
    )
    snapshot = _capture_proven_snapshot(visitor, {"x": value})
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_DEPTH", 2)
    with pytest.raises(contract._AnalysisLimitError, match="depth exceeds 2"):
        visitor._join_binding_snapshots(snapshot, snapshot)


def test_completed_snapshot_proof_missing_name_validates_unknown_root(
    contract, tmp_path, monkeypatch
):
    visitor = _snapshot_proof_visitor(contract, tmp_path)
    first = _capture_proven_snapshot(
        visitor, {"x": contract._AbstractValue(origins=frozenset({"evaluation"}))}
    )
    second = _capture_proven_snapshot(visitor, {})
    calls = []
    original = contract._validate_abstract_values

    def observe(values, **kwargs):
        calls.append(kwargs.get("boundary", "value"))
        return original(values, **kwargs)

    monkeypatch.setattr(contract, "_validate_abstract_values", observe)
    assert "x" in visitor._join_binding_snapshots(first, second)[0]
    assert "joined_binding_snapshot" not in calls
    assert "value" in calls


def test_completed_snapshot_proof_overlap_preserves_supported_shared_graph(contract, tmp_path):
    visitor = _snapshot_proof_visitor(contract, tmp_path)
    first_value = contract._AbstractValue(items=(contract._UNKNOWN_VALUE,) * 3000)
    second_value = contract._AbstractValue(items=(contract._UNKNOWN_VALUE,) * 3000)
    first = _capture_proven_snapshot(visitor, {"same": first_value, "mixed": first_value})
    second = _capture_proven_snapshot(visitor, {"same": first_value, "mixed": second_value})
    assert visitor._join_binding_snapshots(first, second)[0]["same"] is first_value


def test_completed_snapshot_proof_equality_uses_only_legacy_slots(contract, tmp_path):
    visitor = _snapshot_proof_visitor(contract, tmp_path)
    first = _capture_proven_snapshot(
        visitor, {"x": contract._AbstractValue(items=(contract._UNKNOWN_VALUE,))}
    )
    second = _capture_proven_snapshot(
        visitor, {"x": contract._AbstractValue(items=(contract._AbstractValue(),))}
    )
    assert first == second
    assert first == first.slots
    assert first.slots == first
    different_proof = contract._ProvenBindingSnapshot(first.slots, first.proof._replace(nodes=()))
    assert first == different_proof
    with pytest.raises(TypeError):
        hash(first)


def _scoped_result_proof_fixture(contract, tmp_path):
    visitor = _snapshot_proof_visitor(contract, tmp_path)
    leaf = contract._AbstractValue()
    branch = contract._AbstractValue(items=(leaf, leaf))
    snapshot = _capture_proven_snapshot(visitor, {"x": branch})
    context = contract._JoinValidationContext()
    context.snapshot_profiles = {
        id(node.value): (node.value, node.cost, node.depth) for node in snapshot.proof.nodes
    }
    context.snapshot_factory_edges = sum(len(node.children) for node in snapshot.proof.nodes)
    context.snapshot_factory_metadata = sum(node.metadata for node in snapshot.proof.nodes)
    fields = {
        name: getattr(contract._UNKNOWN_VALUE, name)
        for name in contract._AbstractValue.__dataclass_fields__
        if name != "_collection_root"
    }
    fields.update(
        items=(branch, branch),
        entries=(("e", leaf),),
        attributes=(("a", leaf),),
        callables=(contract._CallableTarget(contract.ast.parse("def f(): pass").body[0], branch),),
    )
    return context, fields


def test_scoped_result_proof_preserves_all_child_kinds_and_expanded_multiplicity(
    contract, tmp_path
):
    context, fields = _scoped_result_proof_fixture(contract, tmp_path)
    result = contract._join_result_from_scoped_proofs(fields, [0], context, context)
    assert result is not None
    profile = context.snapshot_profiles[id(result)]
    assert profile[0] is result
    assert (profile[1], profile[2]) == contract._validate_abstract_values((result,))
    assert profile[1] == 26
    context.result_memo[((), 0, False)] = contract._JoinResultMemoEntry((), result, 1, 1)
    contract._flush_snapshot_memo_graph(context, [0])
    nodes, edges, metadata = set(), [0], [0]
    contract._validate_abstract_values(
        (result,),
        _shared_graph_nodes=nodes,
        _shared_graph_edges=edges,
        _shared_graph_metadata=metadata,
    )
    assert context.result_graph_nodes == nodes
    assert context.result_graph_edges == edges
    assert context.result_graph_metadata == metadata
    assert not context.snapshot_memo_deferred


@pytest.mark.parametrize(
    ("limit", "value"), [("_MAX_ABSTRACT_EXPANDED_VALUE_WORK", 25), ("_MAX_ABSTRACT_DEPTH", 2)]
)
def test_scoped_result_proof_rejects_expanded_or_depth_before_materialization(
    contract, tmp_path, monkeypatch, limit, value
):
    context, fields = _scoped_result_proof_fixture(contract, tmp_path)
    monkeypatch.setattr(contract, limit, value)
    boundaries = []
    original = contract._reserve_join_output

    def observe(work, amount, boundary):
        boundaries.append(boundary)
        return original(work, amount, boundary)

    monkeypatch.setattr(contract, "_reserve_join_output", observe)
    with pytest.raises(contract._AnalysisLimitError):
        contract._join_result_from_scoped_proofs(fields, [0], context, None)
    assert "result_proof_materialization" not in boundaries


@pytest.mark.parametrize(
    "limit",
    ["_MAX_ABSTRACT_GRAPH_NODES", "_MAX_ABSTRACT_GRAPH_EDGES", "_MAX_ABSTRACT_GRAPH_METADATA"],
)
def test_scoped_result_proof_cumulative_overflow_declines_without_publishing(
    contract, tmp_path, monkeypatch, limit
):
    context, fields = _scoped_result_proof_fixture(contract, tmp_path)
    before = dict(context.snapshot_profiles)
    monkeypatch.setattr(contract, limit, 2)
    assert contract._join_result_from_scoped_proofs(fields, [0], context, None) is None
    assert context.snapshot_profiles == before
    assert not context.snapshot_memo_deferred


def test_scoped_result_proof_unproven_child_declines(contract, tmp_path):
    context, fields = _scoped_result_proof_fixture(contract, tmp_path)
    fields["items"] = (contract._AbstractValue(origins=frozenset({"foreign"})),)
    assert contract._join_result_from_scoped_proofs(fields, [0], context, None) is None


def test_scoped_result_proof_fallback_flushes_existing_memo_and_stops_reuse(
    contract, tmp_path, monkeypatch
):
    visitor = _snapshot_proof_visitor(contract, tmp_path)
    first = _capture_proven_snapshot(
        visitor,
        {
            "x": contract._AbstractValue(origins=frozenset({"a"})),
            "y": contract._AbstractValue(origins=frozenset({"c"})),
        },
    )
    second = _capture_proven_snapshot(
        visitor,
        {
            "x": contract._AbstractValue(origins=frozenset({"b"})),
            "y": contract._AbstractValue(origins=frozenset({"d"})),
        },
    )
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_GRAPH_METADATA", 7)
    boundaries = []
    contexts = []
    original_reserve, original_init = (
        contract._reserve_join_output,
        contract._JoinValidationContext.__init__,
    )

    def observe(work, amount, boundary):
        boundaries.append(boundary)
        return original_reserve(work, amount, boundary)

    def capture(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        contexts.append(self)

    monkeypatch.setattr(contract, "_reserve_join_output", observe)
    monkeypatch.setattr(contract._JoinValidationContext, "__init__", capture)
    joined = visitor._join_binding_snapshots(first, second)
    assert joined[0]["x"].origins == frozenset({"a", "b"})
    assert joined[0]["y"].origins == frozenset({"c", "d"})
    assert "result_proof_memo_walk" in boundaries
    assert "result_validation_walks" in boundaries
    assert contexts[-1].snapshot_profiles is None
    assert not contexts[-1].snapshot_memo_deferred
    assert contexts[-1].result_graph_metadata == [4]


def test_function_snapshot_plan_reuses_exact_keys_and_rejects_foreign_or_changed_maps(
    contract, tmp_path, monkeypatch
):
    visitor = _snapshot_proof_visitor(contract, tmp_path)
    name = "function_plan_shared_long_name"
    states = ({name: contract._UNKNOWN_VALUE}, {name: contract._UNKNOWN_VALUE})
    functions = ({name: frozenset()}, {name: frozenset()})
    contexts = []
    boundaries = []
    original_init, original_reserve = (
        contract._JoinValidationContext.__init__,
        contract._reserve_join_output,
    )

    def capture(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        contexts.append(self)

    def observe(work, amount, boundary):
        boundaries.append(boundary)
        return original_reserve(work, amount, boundary)

    monkeypatch.setattr(contract._JoinValidationContext, "__init__", capture)
    monkeypatch.setattr(contract, "_reserve_join_output", observe)
    visitor._join_binding_snapshots((states[0], functions[0], {}), (states[1], functions[1], {}))
    context = contexts[-1]
    assert "binding_function_plan_coverage" in boundaries
    assert "binding_function_plan_identity_check" in boundaries
    assert "binding_function_key_bytes" not in boundaries
    boundaries.clear()
    assert name in visitor._join_function_bindings(*functions, _context=context)
    assert "binding_function_key_bytes" not in boundaries
    boundaries.clear()
    assert name in visitor._join_function_bindings(
        *(dict(state) for state in functions), _context=context
    )
    assert "binding_function_key_bytes" in boundaries
    functions[0].clear()
    functions[0]["replacement_name"] = frozenset()
    boundaries.clear()
    joined = visitor._join_function_bindings(*functions, _context=context)
    assert "replacement_name" in joined
    assert "binding_function_key_bytes" in boundaries


def test_function_snapshot_plan_declines_equal_but_distinct_key_objects(
    contract, tmp_path, monkeypatch
):
    visitor = _snapshot_proof_visitor(contract, tmp_path)
    state_key = "same_text_but_distinct_name_object"
    function_key = state_key.encode().decode()
    assert state_key == function_key and state_key is not function_key
    boundaries = []
    original = contract._reserve_join_output

    def observe(work, amount, boundary):
        boundaries.append(boundary)
        return original(work, amount, boundary)

    monkeypatch.setattr(contract, "_reserve_join_output", observe)
    visitor._join_binding_snapshots(
        ({state_key: contract._UNKNOWN_VALUE}, {function_key: frozenset()}, {})
    )
    assert "binding_function_key_bytes" in boundaries


def test_completed_snapshot_duplicate_profiles_must_agree(contract, tmp_path):
    visitor = _snapshot_proof_visitor(contract, tmp_path)
    value = contract._AbstractValue(origins=frozenset({"evaluation"}))
    first = _capture_proven_snapshot(visitor, {"x": value})
    second = _capture_proven_snapshot(visitor, {"x": value})
    bad_nodes = tuple(
        node._replace(cost=node.cost + 1) if node.value is value else node
        for node in second.proof.nodes
    )
    second = contract._ProvenBindingSnapshot(second.slots, second.proof._replace(nodes=bad_nodes))
    with pytest.raises(contract._AnalysisLimitError, match="snapshot proof profiles disagree"):
        visitor._join_binding_snapshots(first, second)


@pytest.mark.parametrize("mutation", ["value", "callable"])
def test_scoped_output_proof_detects_later_forced_mutation(contract, tmp_path, mutation):
    visitor = _snapshot_proof_visitor(contract, tmp_path)
    target = contract._CallableTarget(
        contract.ast.parse("def f(): pass").body[0], contract._UNKNOWN_VALUE
    )
    first = _capture_proven_snapshot(
        visitor, {"x": contract._AbstractValue(origins=frozenset({"a"}), callables=(target,))}
    )
    second = _capture_proven_snapshot(
        visitor, {"x": contract._AbstractValue(origins=frozenset({"b"}), callables=(target,))}
    )
    joined = visitor._join_binding_snapshots(first, second)
    assert type(joined) is contract._ProvenBindingSnapshot
    assert any(node.value is joined[0]["x"] for node in joined.proof.nodes)
    if mutation == "value":
        object.__setattr__(joined[0]["x"], "items", (contract._UNKNOWN_VALUE,) * 30)
    else:
        object.__setattr__(
            target, "receiver", contract._AbstractValue(origins=frozenset({"changed"}))
        )
    with pytest.raises(contract._AnalysisLimitError, match="snapshot proof .* changed"):
        visitor._join_binding_snapshots(joined, joined)


@pytest.mark.parametrize("coverage", ["exact", "equal_distinct", "missing"])
def test_join_reuses_only_exact_scalar_string_already_in_frozenset(
    contract, monkeypatch, coverage
):
    text = "already-retained-scalar-text"
    equal_copy = text.encode().decode()
    assert equal_copy == text and equal_copy is not text
    group = frozenset({text if coverage == "exact" else equal_copy})
    if coverage == "missing":
        group = frozenset()
    value = contract._AbstractValue(string_value=text, string_values=group)
    hashes = []
    reserve_scalar = contract._reserve_join_string_value_work

    def observe_scalar(values, work):
        hashes.extend(value.string_value for value in values if value.string_value is not None)
        return reserve_scalar(values, work)

    monkeypatch.setattr(contract, "_reserve_join_string_value_work", observe_scalar)
    result = contract._join_values(value, contract._AbstractValue())
    assert result.string_values == frozenset({text})
    assert result.string_value is None
    assert hashes == ([] if coverage == "exact" else [text])


def test_join_covered_and_uncovered_shared_scalar_keep_uncovered_hash_path(contract, monkeypatch):
    text = "shared-covered-and-missing-text"
    covered = contract._AbstractValue(string_value=text, string_values=frozenset({text}))
    missing = contract._AbstractValue(string_value=text)
    hashes = []
    monkeypatch.setattr(
        contract, "hash", lambda candidate: hashes.append(candidate) or 0, raising=False
    )
    result = contract._join_values(covered, missing)
    assert result.string_values == frozenset({text})
    assert result.string_value is text
    assert hashes == [text]


@pytest.mark.parametrize("covered", [False, True])
def test_join_large_scalar_string_preserves_metadata_and_prehash_limits(
    contract, monkeypatch, covered
):
    text = "x" * 70000
    value = contract._AbstractValue(
        string_value=text, string_values=frozenset({text}) if covered else frozenset()
    )

    def forbidden_hash(_value):
        raise AssertionError("large scalar hashed before admission")

    monkeypatch.setattr(contract, "hash", forbidden_hash, raising=False)
    with pytest.raises(contract._AnalysisLimitError) as caught:
        contract._join_values(value, contract._AbstractValue())
    boundary = "metadata_union" if covered else "string_value_hash_payload"
    assert f'"boundary": "{boundary}"' in str(caught.value)


def test_cached_string_union_reserves_plan_before_cached_hash(contract, monkeypatch):
    text = ""
    values = (contract._AbstractValue(string_values=frozenset({text})),)
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", 2)
    hashes = []
    monkeypatch.setattr(
        contract, "hash", lambda candidate: hashes.append(candidate) or 0, raising=False
    )
    with pytest.raises(contract._AnalysisLimitError) as caught:
        contract._plan_cached_string_union(values, [None], [0], len(text) * 4)
    assert '"boundary": "string_values_cached_plan"' in str(caught.value)
    assert hashes == []


def test_cached_string_union_equal_distinct_compares_under_exact_budget(contract, monkeypatch):
    text = "🟦"
    copy = text.encode().decode()
    assert text == copy and text is not copy
    values = tuple(
        contract._AbstractValue(string_values=frozenset({item})) for item in (text, copy)
    )
    exact = 6 + len(text) * 4
    work = [0]
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", exact)
    plan = contract._plan_cached_string_union(values, [None, None], work, len(text) * 8)
    assert plan is not None and list(plan.values()) == [text]
    assert work == [exact]
    monkeypatch.setattr(contract, "_MAX_ABSTRACT_JOIN_OUTPUT_WORK", exact - 1)
    with pytest.raises(contract._AnalysisLimitError) as caught:
        contract._plan_cached_string_union(values, [None, None], [0], len(text) * 8)
    assert '"boundary": "string_values_cached_equality"' in str(caught.value)


@pytest.mark.parametrize("texts", [("first", "other"), ("short", "much-longer")])
def test_cached_string_union_unequal_collision_declines_to_full_payload(
    contract, monkeypatch, texts
):
    # Force planner buckets to collide. This is a controlled fallback probe,
    # not a claim to reproduce a real CPython 64-bit string-hash collision.
    values = tuple(
        contract._AbstractValue(string_values=frozenset({text})) for text in texts
    )
    monkeypatch.setattr(contract, "hash", lambda _candidate: 7, raising=False)
    work = [0]
    assert contract._plan_cached_string_union(values, [None, None], work, sum(4 * len(item) for item in texts)) is None
    assert work[0] == 6 + (4 * len(texts[0]) if len(texts[0]) == len(texts[1]) else 0)
    observed = []
    reserve = contract._reserve_join_output
    def track(counter, amount, boundary):
        observed.append((boundary, amount))
        return reserve(counter, amount, boundary)
    monkeypatch.setattr(contract, "_reserve_join_output", track)
    result = contract._join_values(*values)
    assert result.string_values == frozenset(texts)
    assert ("metadata_union", 4 + sum(4 * len(text) for text in texts)) in observed
    assert ("retained_string_payload", sum(4 * len(text) for text in texts)) in observed


def test_cached_string_union_non_cpython_keeps_full_legacy_payload(contract, monkeypatch):
    monkeypatch.setattr(
        contract, "sys", contract.SimpleNamespace(implementation=contract.SimpleNamespace(name="pypy"))
    )
    text = "legacy-runtime-text"
    values = (contract._AbstractValue(string_values=frozenset({text})), contract._AbstractValue())
    assert contract._plan_cached_string_union(values, [None, None], [0], len(text) * 8) is None
    observed = []
    reserve = contract._reserve_join_output
    def track(counter, amount, boundary):
        observed.append((boundary, amount))
        return reserve(counter, amount, boundary)
    monkeypatch.setattr(contract, "_reserve_join_output", track)
    result = contract._join_values(*values)
    assert result.string_values == frozenset({text})
    assert ("metadata_union", 2 + len(text) * 4) in observed
    assert ("retained_string_payload", len(text) * 4) in observed


def test_cached_string_union_nonexact_group_and_missing_scalar_decline(contract):
    class DerivedSet(frozenset):
        pass
    text = "uncovered"
    subclass_value = contract._AbstractValue(string_values=DerivedSet({text}))
    missing_value = contract._AbstractValue(string_value=text)
    assert contract._plan_cached_string_union((subclass_value,), [None], [0], len(text) * 4) is None
    assert contract._plan_cached_string_union((missing_value,), [None], [0], 0) is None
