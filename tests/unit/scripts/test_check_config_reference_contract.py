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
        reads=reads & {field},
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
    assert _audit_as_documented_inert(contract, field, reads).violations == ()


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

    violations = contract.opus_default_violations("claude-opus-4-8", "claude-opus-4-8")

    assert violations == (
        "Opus defaults must use distinct direct and OpenRouter identifiers",
        "consensus.advocate_model: invalid OpenRouter Opus default 'claude-opus-4-8'; "
        "expected 'openrouter/anthropic/claude-opus-<major>.<minor>'",
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
