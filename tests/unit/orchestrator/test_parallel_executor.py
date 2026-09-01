"""Tests for staged result handling in ParallelACExecutor."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import textwrap
from types import SimpleNamespace
from typing import Any, NamedTuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.core.seed import (
    AcceptanceCriterionSpec,
    InvestmentSpec,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.events.base import BaseEvent
from ouroboros.harness.journal import EvidenceEntry, EvidenceKind, EvidenceManifest
from ouroboros.mcp.types import MCPToolDefinition
from ouroboros.orchestrator.adapter import (
    FULL_CAPABILITIES,
    AgentMessage,
    ParamSupport,
    RuntimeCapabilities,
    RuntimeHandle,
)
from ouroboros.orchestrator.adaptive_concurrency import (
    MAX_ADAPTIVE_CONCURRENCY_COOLDOWN_SECONDS,
    AdaptiveConcurrencyController,
    BackendPressureKind,
    ConcurrencyObservation,
    observe_provider_messages,
)
from ouroboros.orchestrator.coordinator import CoordinatorReview, FileConflict, LevelCoordinator
from ouroboros.orchestrator.coordinator_quota import consume_published_coordinator_pause
from ouroboros.orchestrator.decomposition_limits import MAX_DECOMPOSITION_DEPTH
from ouroboros.orchestrator.decomposition_policy import (
    BounceCause,
    DecompositionChild,
    DecompositionDecisionRecord,
    DecompositionDisposition,
    DecompositionSource,
    SemanticAttestationStatus,
    StructuralCheckStatus,
)
from ouroboros.orchestrator.dependency_analyzer import ACNode, DependencyGraph
from ouroboros.orchestrator.evidence.claims import (
    _python_c_command_file_claim_match,
    _runtime_messages_support_file_claim,
    _shell_command_mutation_targets,
    _text_needs_shell_expansion,
)
from ouroboros.orchestrator.evidence.shell_parsing import _looks_like_test_command
from ouroboros.orchestrator.evidence_schema import EvidenceRecord, ValidationResult
from ouroboros.orchestrator.execution_authority import (
    runtime_effect_capabilities_contract,
)
from ouroboros.orchestrator.execution_runtime_scope import (
    ExecutionNodeIdentity,
    build_level_coordinator_runtime_scope,
)
from ouroboros.orchestrator.failure_taxonomy import FailureClass
from ouroboros.orchestrator.leaf_dispatcher import (
    _attach_bash_filesystem_effects,
    _BashFilesystemLeaseTracker,
    _close_pending_targets,
    _correlated_tool_result_name,
    _pending_bash_filesystem_targets,
    _stat_fingerprint,
)
from ouroboros.orchestrator.level_context import ACContextSummary, LevelContext
from ouroboros.orchestrator.parallel_executor import (
    MAX_STALL_RETRIES,
    STALL_TIMEOUT_SECONDS,
    ACExecutionOutcome,
    ACExecutionResult,
    CoordinatorQuotaPause,
    ParallelACExecutor,
    ParallelExecutionResult,
    StageExecutionOutcome,
    _build_governed_parent_summary,
    _complete_sibling_acs_from_evidence,
    _criterion_satisfied_by_evidence,
    _effective_evidence_schema_for_ac,
    _matching_journal_entries,
    _message_contains_test_success,
    _runtime_messages_have_masked_test_command_form,
    _runtime_messages_support_command_claim,
    _runtime_messages_support_test_claim,
    _standard_deliver_facts,
    render_parallel_completion_message,
    render_parallel_verification_report,
)
from ouroboros.orchestrator.profile_loader import EvidenceSchema, load_profile
from ouroboros.orchestrator.recoverable_failure import UsageLimitPauseConsequence
from ouroboros.orchestrator.verifier import VerifierStatus, VerifierVerdict
from ouroboros.persistence.checkpoint import CheckpointStore
from tests.unit.orchestrator.parallel_executor_test_support import ProcessLocalTestExecutor


def _trusted_python_c(source: str) -> str:
    return shlex.join([str(Path(sys.executable).resolve()), "-I", "-S", "-c", source])


def _filesystem_effect(path: Path, *, reported_path: str) -> dict[str, object]:
    identity = path.lstat()
    return {
        "capture": "ouroboros.leaf-dispatch.v1",
        "path": reported_path,
        "st_dev": identity.st_dev,
        "st_ino": identity.st_ino,
        "st_mode": identity.st_mode,
    }


def _trusted_preflight_split(
    node_id: str,
    *descriptions: str,
) -> DecompositionDecisionRecord:
    """Build a verified historical preflight decision for replay-only tests."""
    return DecompositionDecisionRecord(
        node_id=node_id,
        source=DecompositionSource.PREFLIGHT,
        disposition=DecompositionDisposition.SPLIT,
        children=tuple(
            DecompositionChild(
                description=description,
                coverage_claims=(f"scope-{index}",),
                verification_hint=f"verify scope {index}",
            )
            for index, description in enumerate(descriptions)
        ),
        structural_status=StructuralCheckStatus.PASSED,
        semantic_status=SemanticAttestationStatus.ESTABLISHED,
        trustworthy=True,
    )


def test_stall_timeout_default_allows_realistic_test_suites() -> None:
    """The default stall watchdog should not kill long quiet test commands too early."""
    assert STALL_TIMEOUT_SECONDS == 900.0


def _journal_entry(
    *,
    handle: str,
    command: str,
    result_preview: str | None = None,
) -> EvidenceEntry:
    payload: dict[str, object] = {"tool_name": "Bash", "command": command}
    if result_preview is not None:
        payload["result_preview"] = result_preview
    return _journal_payload_entry(
        handle=handle,
        payload=payload,
    )


def _journal_payload_entry(*, handle: str, payload: dict[str, object]) -> EvidenceEntry:
    return EvidenceEntry(
        handle=handle,
        kind=EvidenceKind.COMMAND_EXECUTED,
        ok=True,
        started_at=datetime.now(UTC),
        ended_at=datetime.now(UTC),
        payload=payload,
        source_event_ids=(f"event-{handle}",),
    )


def test_deliver_matching_uses_verifier_command_aliases() -> None:
    """A shell-wrapped command has the same backing evidence as its clean claim."""
    manifest = EvidenceManifest(
        ac_id="AC-1",
        entries=(
            _journal_entry(
                handle="ev_wrapped",
                command="/bin/zsh -lc 'pytest tests/test_app.py'",
            ),
        ),
    )

    matches = _matching_journal_entries(
        manifest,
        field="commands_run",
        value="pytest tests/test_app.py",
    )

    assert tuple(entry.handle for entry in matches) == ("ev_wrapped",)


@pytest.mark.parametrize(
    "command",
    (
        # Command text alone cannot prove historical filesystem identity.
        _trusted_python_c("from pathlib import Path; Path('src/generated.py').write_text('x')"),
        _trusted_python_c("from pathlib import Path; Path('src/generated.py').write_bytes(b'x')"),
        # Aliases, variable writes, injections, and malformed payloads.
        "python -c \"from pathlib import Path as P; P('src/generated.py').write_text('x')\"",
        "python -c \"from pathlib import Path; p = Path('src/generated.py'); p.write_text('x')\"",
        "python -c \"from pathlib import Path; 'src/generated.py'; Path('other.py').write_text('x')\"",
        "python -c \"from pathlib import Path; # Path('src/generated.py').write_text('x')\"",
        "python -c \"from pathlib import Path; raise SystemExit(0); Path('src/generated.py').write_text('x')\"",
        "python -c \"from pathlib import Path; Path := object; Path('src/generated.py').write_text('x')\"",
        "python -c \"from pathlib import Path; Path.write_text = lambda *args: None; Path('src/generated.py').write_text('x')\"",
        "python -c \"from pathlib import Path; import os; os.chdir('src'); Path('generated.py').write_text('x')\"",
        "python -c \"from pathlib import Path; other = 1; Path('src/generated.py').write_text('x')\"",
        "PAYLOAD=x python -c \"from pathlib import Path; Path('src/generated.py').write_text('$PAYLOAD')\"",
        "python -c \"from pathlib import Path; Path('src/generated.py').write_text('x')\" | cat",
        "python -c \"from pathlib import Path; Path('src/generated.py').write_text('x')",
        "/tmp/fake/python -c \"from pathlib import Path; Path('src/generated.py').write_text('x')\"",
        "./python3 -c \"from pathlib import Path; Path('src/generated.py').write_text('x')\"",
        "python -c \"from pathlib import Path; Path('other.py') . write_text('x')\"",
        'python -c ""',
        "python -c from pathlib import Path",
        'python -c "' + ("(" * 5000) + '"',
        "python -c \"from pathlib import Path; Path('src/\\x00generated.py').write_text('x')\"",
        # Broader Python programs that need runtime evidence.
        'python -c "from pathlib import Path; Path = lambda _value: None; '
        "from pathlib import Path; Path('src/generated.py').write_text('x')\"",
        'python -c "from pathlib import Path; def helper():\n'
        "    Path = lambda _value: None\nPath('src/generated.py').write_text('x')\"",
        # ``-I`` alone can still run site customization, so it is not static proof.
        pytest.param(
            shlex.join(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    "from pathlib import Path; Path('src/generated.py').write_text('x')",
                ]
            ),
            id="isolated-without-no-site",
        ),
        # Receiver extraction fails closed when the pathlib AST is too deep.
        pytest.param(
            _trusted_python_c(
                "from pathlib import Path; "
                + ("Path('src')" + " / 'nested'" * 1_000 + " / 'generated.py'")
                + ".write_text('x')"
            ),
            id="deep-receiver-without-exception",
        ),
    ),
)
def test_python_c_pathlib_static_proof_rejects_unprovable_payloads(tmp_path, command) -> None:
    """Only a statically provable top-level pathlib write counts as static file proof.

    Python command text alone cannot prove historical filesystem identity, and
    aliases, variable receivers, rebinding, nested statements, injections,
    untrusted interpreters, and malformed payloads all fail closed.
    """
    generated = tmp_path / "src" / "generated.py"
    generated.parent.mkdir()
    generated.write_text("VALUE = 1\n", encoding="utf-8")

    assert (
        _python_c_command_file_claim_match(
            command,
            task_cwd=str(tmp_path),
        )
        is False
    )


def test_files_touched_rejects_reconstructed_pathlib_with_inert_touch_argv(tmp_path) -> None:
    """Python ``-c`` argv cannot fall through to generic shell mutation proof."""
    claimed_file = tmp_path / "claimed.py"
    other_file = tmp_path / "other.py"
    claimed_file.write_text("original\n", encoding="utf-8")
    argv = [
        str(Path(sys.executable).resolve()),
        "-I",
        "-S",
        "-c",
        "getattr(__import__('path' 'lib'), 'Pa' 'th')('other.py').write_text('changed')",
        "touch",
        "claimed.py",
    ]
    command = shlex.join(argv)

    completed = subprocess.run(argv, cwd=tmp_path, shell=False, check=False)

    assert completed.returncode == 0
    assert claimed_file.read_text(encoding="utf-8") == "original\n"
    assert other_file.read_text(encoding="utf-8") == "changed"
    assert _shell_command_mutation_targets(command) == ()
    messages = (
        AgentMessage(
            type="tool",
            content=f"Bash: {command}",
            tool_name="Bash",
            data={"tool_input": {"command": command}},
        ),
        AgentMessage(
            type="tool_result",
            content="command completed with exit code 0",
            data={"subtype": "tool_result", "exit_code": 0},
        ),
    )
    assert (
        _runtime_messages_support_file_claim("claimed.py", messages, task_cwd=str(tmp_path))
        is False
    )


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX /bin/bash")
def test_files_touched_rejects_wrapped_reconstructed_pathlib_with_inert_touch_argv(
    tmp_path,
) -> None:
    """A supported shell body receives the same Python ``-c`` classification."""
    claimed_file = tmp_path / "claimed.py"
    other_file = tmp_path / "other.py"
    claimed_file.write_text("original\n", encoding="utf-8")
    inner = shlex.join(
        [
            str(Path(sys.executable).resolve()),
            "-I",
            "-S",
            "-c",
            "getattr(__import__('path' 'lib'), 'Pa' 'th')('other.py').write_text('changed')",
            "touch",
            "claimed.py",
        ]
    )
    command = f"/bin/bash -lc {shlex.quote(inner)}"

    completed = subprocess.run(command, cwd=tmp_path, shell=True, check=False)  # noqa: S602

    assert completed.returncode == 0
    assert claimed_file.read_text(encoding="utf-8") == "original\n"
    assert other_file.read_text(encoding="utf-8") == "changed"
    messages = (
        AgentMessage(
            type="tool",
            content=f"Bash: {command}",
            tool_name="Bash",
            data={"tool_input": {"command": command}},
        ),
        AgentMessage(
            type="tool_result",
            content="command completed with exit code 0",
            data={"subtype": "tool_result", "exit_code": 0},
        ),
    )
    assert (
        _runtime_messages_support_file_claim("claimed.py", messages, task_cwd=str(tmp_path))
        is False
    )


def test_files_touched_rejects_env_python_quoted_redirection_argv(tmp_path) -> None:
    """System ``env`` cannot expose a quoted Python argv value as redirection."""
    claimed_file = tmp_path / "claimed.py"
    other_file = tmp_path / "other.py"
    claimed_file.write_text("original\n", encoding="utf-8")
    command = shlex.join(
        [
            "/usr/bin/env",
            str(Path(sys.executable).resolve()),
            "-I",
            "-S",
            "-c",
            "getattr(__import__('path' 'lib'), 'Pa' 'th')('other.py').write_text('changed')",
            ">",
            "claimed.py",
        ]
    )

    completed = subprocess.run(command, cwd=tmp_path, shell=True, check=False)  # noqa: S602

    assert completed.returncode == 0
    assert claimed_file.read_text(encoding="utf-8") == "original\n"
    assert other_file.read_text(encoding="utf-8") == "changed"
    assert _shell_command_mutation_targets(command) == ()
    messages = (
        AgentMessage(
            type="tool",
            content=f"Bash: {command}",
            tool_name="Bash",
            data={"tool_input": {"command": command}},
        ),
        AgentMessage(
            type="tool_result",
            content="command completed with exit code 0",
            data={"subtype": "tool_result", "exit_code": 0},
        ),
    )
    assert not _runtime_messages_support_file_claim(
        "claimed.py",
        messages,
        task_cwd=str(tmp_path),
    )


def test_files_touched_allows_python_c_literal_shell_redirect_with_local_lease(tmp_path) -> None:
    """Python source stays inert while its independent shell receiver is proven."""
    command = _trusted_python_c("print('generated')") + " > claimed.py"
    call = AgentMessage(
        type="tool",
        content=f"Bash: {command}",
        tool_name="Bash",
        data={"tool_input": {"command": command}, "tool_call_id": "python-redirect"},
    )
    completion = AgentMessage(
        type="tool_result",
        content="command completed with exit code 0",
        data={
            "subtype": "tool_result",
            "exit_code": 0,
            "tool_call_id": "python-redirect",
        },
    )

    with _BashFilesystemLeaseTracker(task_cwd=str(tmp_path)) as tracker:
        observed_call = tracker.observe(call)
        completed = subprocess.run(command, cwd=tmp_path, shell=True, check=False)  # noqa: S602
        observed_completion = tracker.observe(completion)

    assert completed.returncode == 0
    assert (tmp_path / "claimed.py").read_text(encoding="utf-8") == "generated\n"
    assert _runtime_messages_support_file_claim(
        "claimed.py",
        (observed_call, observed_completion),
        task_cwd=str(tmp_path),
    )


def test_files_touched_python_c_redirect_does_not_prove_internal_receiver(tmp_path) -> None:
    """A leased stdout redirect cannot authenticate a Python-internal write."""
    source = "from pathlib import Path; Path('other.py').write_text('internal'); print('out')"
    command = f"{_trusted_python_c(source)} > claimed.py"
    call = AgentMessage(
        type="tool",
        content=f"Bash: {command}",
        tool_name="Bash",
        data={"tool_input": {"command": command}},
    )
    completion = AgentMessage(
        type="tool_result",
        content="command completed with exit code 0",
        data={"subtype": "tool_result", "exit_code": 0},
    )

    with _BashFilesystemLeaseTracker(task_cwd=str(tmp_path)) as tracker:
        observed_call = tracker.observe(call)
        completed = subprocess.run(command, cwd=tmp_path, shell=True, check=False)  # noqa: S602
        observed_completion = tracker.observe(completion)

    messages = (observed_call, observed_completion)
    assert completed.returncode == 0
    assert _runtime_messages_support_file_claim("claimed.py", messages, task_cwd=str(tmp_path))
    assert not _runtime_messages_support_file_claim("other.py", messages, task_cwd=str(tmp_path))


@pytest.mark.parametrize("operator", ("'>'", r"\>"))
def test_files_touched_rejects_python_c_quoted_or_escaped_redirect_argv(
    tmp_path,
    operator,
) -> None:
    """A quoted or escaped greater-than byte remains Python argv, not a receiver."""
    source = "from pathlib import Path; Path('other.py').write_text('internal')"
    command = f"{_trusted_python_c(source)} {operator} claimed.py"
    call = AgentMessage(
        type="tool",
        content=f"Bash: {command}",
        tool_name="Bash",
        data={"tool_input": {"command": command}},
    )
    completion = AgentMessage(
        type="tool_result",
        content="command completed with exit code 0",
        data={"subtype": "tool_result", "exit_code": 0},
    )

    with _BashFilesystemLeaseTracker(task_cwd=str(tmp_path)) as tracker:
        observed_call = tracker.observe(call)
        completed = subprocess.run(command, cwd=tmp_path, shell=True, check=False)  # noqa: S602
        observed_completion = tracker.observe(completion)

    assert completed.returncode == 0
    assert not (tmp_path / "claimed.py").exists()
    assert not _runtime_messages_support_file_claim(
        "claimed.py",
        (observed_call, observed_completion),
        task_cwd=str(tmp_path),
    )


@pytest.mark.parametrize("comment_prefix", (" ", " \\\n"))
def test_files_touched_rejects_python_c_redirection_inside_shell_comment(
    tmp_path,
    comment_prefix,
) -> None:
    """A comment cannot turn a Python-internal write into shell receiver proof."""
    source = "from pathlib import Path; Path('claimed.py').write_text('internal')"
    command = f"{_trusted_python_c(source)}{comment_prefix}# > claimed.py"
    call = AgentMessage(
        type="tool",
        content=f"Bash: {command}",
        tool_name="Bash",
        data={"tool_input": {"command": command}, "tool_call_id": "commented-redirect"},
    )
    completion = AgentMessage(
        type="tool_result",
        content="command completed with exit code 0",
        data={
            "subtype": "tool_result",
            "exit_code": 0,
            "tool_call_id": "commented-redirect",
        },
    )

    with _BashFilesystemLeaseTracker(task_cwd=str(tmp_path)) as tracker:
        observed_call = tracker.observe(call)
        completed = subprocess.run(command, cwd=tmp_path, shell=True, check=False)  # noqa: S602
        observed_completion = tracker.observe(completion)

    assert completed.returncode == 0
    assert (tmp_path / "claimed.py").read_text(encoding="utf-8") == "internal"
    assert _shell_command_mutation_targets(command, redirections_only=True) == ()
    assert "filesystem_effects" not in observed_completion.data
    assert not _runtime_messages_support_file_claim(
        "claimed.py",
        (observed_call, observed_completion),
        task_cwd=str(tmp_path),
    )


def test_shell_comment_parser_rejects_redirect_after_backslash_crlf() -> None:
    """Non-POSIX CRLF continuation remains fail-closed for receiver discovery."""
    source = "from pathlib import Path; Path('claimed.py').write_text('internal')"
    command = f"{_trusted_python_c(source)} \\\r\n# > claimed.py"

    assert _shell_command_mutation_targets(command, redirections_only=True) == ()


@pytest.mark.parametrize(
    ("command", "expected"),
    (
        ("printf '%s\\n' 'quoted # value' > claimed.py", "quoted # value\n"),
        (r"printf '%s\n' escaped\#value > claimed.py", "escaped#value\n"),
        ("printf '%s\\n' foo#bar > claimed.py", "foo#bar\n"),
    ),
)
def test_files_touched_preserves_literal_hash_before_real_shell_redirect(
    tmp_path,
    command,
    expected,
) -> None:
    """Quoted, escaped, and token-internal hashes remain ordinary argv data."""
    call = AgentMessage(
        type="tool",
        content=f"Bash: {command}",
        tool_name="Bash",
        data={"tool_input": {"command": command}},
    )
    completion = AgentMessage(
        type="tool_result",
        content="command completed with exit code 0",
        data={"subtype": "tool_result", "exit_code": 0},
    )

    with _BashFilesystemLeaseTracker(task_cwd=str(tmp_path)) as tracker:
        observed_call = tracker.observe(call)
        completed = subprocess.run(command, cwd=tmp_path, shell=True, check=False)  # noqa: S602
        observed_completion = tracker.observe(completion)

    assert completed.returncode == 0
    assert (tmp_path / "claimed.py").read_text(encoding="utf-8") == expected
    assert _runtime_messages_support_file_claim(
        "claimed.py",
        (observed_call, observed_completion),
        task_cwd=str(tmp_path),
    )


@pytest.mark.parametrize(
    ("command", "stdin"),
    (
        ("touch first.py second.py", None),
        ("truncate -s 0 first.py second.py", None),
        ("tee first.py second.py", "generated\n"),
    ),
)
def test_files_touched_authenticates_every_stable_multi_receiver(
    tmp_path,
    command,
    stdin,
) -> None:
    """Every stable-identity operand receives its own execution-span lease."""
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("before first\n", encoding="utf-8")
    second.write_text("before second\n", encoding="utf-8")
    os.utime(first, ns=(1_000_000_000, 1_000_000_000))
    os.utime(second, ns=(1_000_000_000, 1_000_000_000))
    call = AgentMessage(
        type="tool",
        content=f"Bash: {command}",
        tool_name="Bash",
        data={"tool_input": {"command": command}, "tool_call_id": "multi-receiver"},
    )
    completion = AgentMessage(
        type="tool_result",
        content="command completed with exit code 0",
        data={
            "subtype": "tool_result",
            "exit_code": 0,
            "tool_call_id": "multi-receiver",
        },
    )

    with _BashFilesystemLeaseTracker(task_cwd=str(tmp_path)) as tracker:
        observed_call = tracker.observe(call)
        assert len(tracker._pending_by_id["multi-receiver"]) == 2
        completed = subprocess.run(  # noqa: S602
            command,
            cwd=tmp_path,
            shell=True,
            check=False,
            input=stdin,
            text=True,
        )
        observed_completion = tracker.observe(completion)

    assert completed.returncode == 0
    assert _shell_command_mutation_targets(command) == ("first.py", "second.py")
    assert {effect["path"] for effect in observed_completion.data["filesystem_effects"]} == {
        "first.py",
        "second.py",
    }
    messages = (observed_call, observed_completion)
    assert _runtime_messages_support_file_claim("first.py", messages, task_cwd=str(tmp_path))
    assert _runtime_messages_support_file_claim("second.py", messages, task_cwd=str(tmp_path))


@pytest.mark.parametrize(
    ("command", "receivers"),
    (
        ("touch > log.txt first.py second.py", ("log.txt", "first.py", "second.py")),
        ("touch first.py > log.txt second.py", ("log.txt", "first.py", "second.py")),
        ("> log.txt touch first.py second.py", ("log.txt", "first.py", "second.py")),
        ("touch first.py second.py 2> error.txt", ("error.txt", "first.py", "second.py")),
        ("touch first.py second.py 2>&1", ("first.py", "second.py")),
    ),
)
def test_files_touched_authenticates_interspersed_redirect_and_operands(
    tmp_path,
    command,
    receivers,
) -> None:
    """Redirection clauses are removed without truncating later utility operands."""
    for receiver in receivers:
        target = tmp_path / receiver
        target.write_text("before\n", encoding="utf-8")
        os.utime(target, ns=(1_000_000_000, 1_000_000_000))
    call = AgentMessage(
        type="tool",
        content=f"Bash: {command}",
        tool_name="Bash",
        data={"tool_input": {"command": command}},
    )
    completion = AgentMessage(
        type="tool_result",
        content="command completed with exit code 0",
        data={"subtype": "tool_result", "exit_code": 0},
    )

    with _BashFilesystemLeaseTracker(task_cwd=str(tmp_path)) as tracker:
        observed_call = tracker.observe(call)
        completed = subprocess.run(command, cwd=tmp_path, shell=True, check=False)  # noqa: S602
        observed_completion = tracker.observe(completion)

    assert completed.returncode == 0
    assert _shell_command_mutation_targets(command) == receivers
    messages = (observed_call, observed_completion)
    for receiver in receivers:
        assert _runtime_messages_support_file_claim(receiver, messages, task_cwd=str(tmp_path))


def test_tee_input_redirection_is_not_a_mutation_receiver(tmp_path) -> None:
    """tee mutates every output operand but does not authenticate its stdin source."""
    source = tmp_path / "source.py"
    output = tmp_path / "output.py"
    source.write_text("source\n", encoding="utf-8")
    output.write_text("before\n", encoding="utf-8")
    os.utime(output, ns=(1_000_000_000, 1_000_000_000))
    command = "tee output.py < source.py"
    call = AgentMessage(
        type="tool",
        content=f"Bash: {command}",
        tool_name="Bash",
        data={"tool_input": {"command": command}},
    )
    completion = AgentMessage(
        type="tool_result",
        content="command completed with exit code 0",
        data={"subtype": "tool_result", "exit_code": 0},
    )

    with _BashFilesystemLeaseTracker(task_cwd=str(tmp_path)) as tracker:
        observed_call = tracker.observe(call)
        completed = subprocess.run(command, cwd=tmp_path, shell=True, check=False)  # noqa: S602
        observed_completion = tracker.observe(completion)

    assert completed.returncode == 0
    assert _shell_command_mutation_targets(command) == ("output.py",)
    messages = (observed_call, observed_completion)
    assert _runtime_messages_support_file_claim("output.py", messages, task_cwd=str(tmp_path))
    assert not _runtime_messages_support_file_claim("source.py", messages, task_cwd=str(tmp_path))


@pytest.mark.parametrize(
    ("command", "receivers", "stdin"),
    (
        ("touch 123 > log.txt", ("log.txt", "123"), None),
        ("tee 123 < source.txt", ("123",), None),
    ),
)
def test_numeric_filename_separated_from_redirect_remains_receiver(
    tmp_path,
    command,
    receivers,
    stdin,
) -> None:
    """Whitespace-separated digits are argv operands, never IO-number selectors."""
    source = tmp_path / "source.txt"
    source.write_text("source\n", encoding="utf-8")
    for receiver in receivers:
        target = tmp_path / receiver
        target.write_text("before\n", encoding="utf-8")
        os.utime(target, ns=(1_000_000_000, 1_000_000_000))
    call = AgentMessage(
        type="tool",
        content=f"Bash: {command}",
        tool_name="Bash",
        data={"tool_input": {"command": command}},
    )
    completion = AgentMessage(
        type="tool_result",
        content="command completed with exit code 0",
        data={"subtype": "tool_result", "exit_code": 0},
    )

    with _BashFilesystemLeaseTracker(task_cwd=str(tmp_path)) as tracker:
        observed_call = tracker.observe(call)
        completed = subprocess.run(  # noqa: S602
            command,
            cwd=tmp_path,
            shell=True,
            check=False,
            input=stdin,
            text=True,
        )
        observed_completion = tracker.observe(completion)

    assert completed.returncode == 0
    assert _shell_command_mutation_targets(command) == receivers
    messages = (observed_call, observed_completion)
    for receiver in receivers:
        assert _runtime_messages_support_file_claim(receiver, messages, task_cwd=str(tmp_path))
    assert not _runtime_messages_support_file_claim("source.txt", messages, task_cwd=str(tmp_path))


@pytest.mark.parametrize(
    ("command", "expected"),
    (
        # Only an adjacent IO-number is stripped from utility argv.
        ("touch first.py 2> error.txt", ("error.txt", "first.py")),
        ("touch first.py 2>&1", ("first.py",)),
        ("touch first.py 2 > log.txt", ("log.txt", "first.py", "2")),
        ("tee 123<source.txt", ()),
        # Option values are inputs/configuration, never mutation receivers.
        ("touch -r reference.py first.py second.py", ("first.py", "second.py")),
        ("touch --date yesterday first.py second.py", ("first.py", "second.py")),
        ("truncate -r reference.py first.py second.py", ("first.py", "second.py")),
        ("truncate --size 0 first.py second.py", ("first.py", "second.py")),
        ("tee --output-error=warn first.py second.py", ("first.py", "second.py")),
        # Required values, optional values, and ``--`` preserve real operands.
        ("touch -- -first.py -second.py", ("-first.py", "-second.py")),
        ("truncate -s0 -- -first.py -second.py", ("-first.py", "-second.py")),
        (
            "sed -i '' -f rewrite.sed first.py second.py",
            ("first.py", "second.py"),
        ),
        (
            "perl -I lib -M File::Path -F pattern -pi -e rewrite first.py second.py",
            ("first.py", "second.py"),
        ),
        (
            "perl -0 -C -d -D -V -x -pi -e rewrite first.py second.py",
            ("first.py", "second.py"),
        ),
        # Unknown utility options cannot donate their values as file receivers.
        ("touch --unknown first.py second.py", ()),
        ("truncate --unknown first.py second.py", ()),
        ("tee --unknown first.py second.py", ()),
        ("sed --unknown -i '' rewrite first.py second.py", ()),
        ("perl --unknown -pi -e rewrite first.py second.py", ()),
        # In-place editors: scripts, backup suffixes, and option values are
        # excluded from receivers.
        ("sed -i '' 's/before/after/' first.py second.py", ("first.py", "second.py")),
        ("sed -i '' -e 's/before/after/' first.py second.py", ("first.py", "second.py")),
        ("perl -pi -e 's/before/after/' first.py second.py", ("first.py", "second.py")),
        ("perl -p -i.bak -e 's/before/after/' first.py second.py", ("first.py", "second.py")),
    ),
)
def test_shell_command_mutation_target_parsing(command, expected) -> None:
    """Receiver parsing keeps real file operands and drops option/redirection noise."""
    assert _shell_command_mutation_targets(command) == expected


def test_clobber_redirection_authenticates_without_becoming_pipeline(tmp_path) -> None:
    """The pipe byte in ``>|`` is redirection syntax, not compound control."""
    command = "printf x >| log.txt"
    call = AgentMessage(
        type="tool",
        content=f"Bash: {command}",
        tool_name="Bash",
        data={"tool_input": {"command": command}},
    )
    completion = AgentMessage(
        type="tool_result",
        content="command completed with exit code 0",
        data={"subtype": "tool_result", "exit_code": 0},
    )

    with _BashFilesystemLeaseTracker(task_cwd=str(tmp_path)) as tracker:
        observed_call = tracker.observe(call)
        completed = subprocess.run(command, cwd=tmp_path, shell=True, check=False)  # noqa: S602
        observed_completion = tracker.observe(completion)

    assert completed.returncode == 0
    assert (tmp_path / "log.txt").read_text(encoding="utf-8") == "x"
    assert _shell_command_mutation_targets(command) == ("log.txt",)
    assert _runtime_messages_support_file_claim(
        "log.txt",
        (observed_call, observed_completion),
        task_cwd=str(tmp_path),
    )


@pytest.mark.parametrize(
    "command",
    (
        pytest.param(
            "sed -i '' 's/before/after/' first.py second.py",
            id="bsd-explicit-empty-suffix",
        ),
        pytest.param(
            "sed -i 's/before/after/' first.py second.py",
            id="gnu-suffix-free",
        ),
    ),
)
def test_sed_in_place_grammar_enumerates_every_operand_on_linux(monkeypatch, command) -> None:
    """Both ``-i`` spellings stay unambiguous on Linux and retain every receiver."""
    monkeypatch.setattr(sys, "platform", "linux")

    assert _shell_command_mutation_targets(command) == (
        "first.py",
        "second.py",
    )


@pytest.mark.parametrize("editor", ("sed", "perl"))
def test_in_place_editor_inode_replacement_remains_fail_closed(tmp_path, editor) -> None:
    """Enumerating all operands does not weaken regular-inode continuity."""
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("before\n", encoding="utf-8")
    second.write_text("before\n", encoding="utf-8")
    before = (first.stat().st_ino, second.stat().st_ino)
    if editor == "sed":
        argv = ["sed", "-i"]
        if not sys.platform.startswith("linux"):
            argv.append("")
        argv.extend(("s/before/after/", "first.py", "second.py"))
    else:
        argv = ["perl", "-pi", "-e", "s/before/after/", "first.py", "second.py"]
    command = shlex.join(argv)
    call = AgentMessage(
        type="tool",
        content=f"Bash: {command}",
        tool_name="Bash",
        data={"tool_input": {"command": command}},
    )
    completion = AgentMessage(
        type="tool_result",
        content="command completed with exit code 0",
        data={"subtype": "tool_result", "exit_code": 0},
    )

    with _BashFilesystemLeaseTracker(task_cwd=str(tmp_path)) as tracker:
        observed_call = tracker.observe(call)
        completed = subprocess.run(command, cwd=tmp_path, shell=True, check=False)  # noqa: S602
        observed_completion = tracker.observe(completion)

    assert completed.returncode == 0
    assert (first.stat().st_ino, second.stat().st_ino) != before
    assert "filesystem_effects" not in observed_completion.data
    messages = (observed_call, observed_completion)
    assert not _runtime_messages_support_file_claim("first.py", messages, task_cwd=str(tmp_path))
    assert not _runtime_messages_support_file_claim("second.py", messages, task_cwd=str(tmp_path))


def test_perl_module_name_cannot_impersonate_in_place_switch(tmp_path) -> None:
    """A lowercase i inside an unrelated option cannot lease Perl-internal writes."""
    source = "open(F, q(>), q(claimed.py)); print F q(internal); close F"
    command = shlex.join(["perl", "-MFile::Path", "-e", source, "claimed.py"])
    call = AgentMessage(
        type="tool",
        content=f"Bash: {command}",
        tool_name="Bash",
        data={"tool_input": {"command": command}},
    )
    completion = AgentMessage(
        type="tool_result",
        content="command completed with exit code 0",
        data={"subtype": "tool_result", "exit_code": 0},
    )

    with _BashFilesystemLeaseTracker(task_cwd=str(tmp_path)) as tracker:
        observed_call = tracker.observe(call)
        completed = subprocess.run(command, cwd=tmp_path, shell=True, check=False)  # noqa: S602
        observed_completion = tracker.observe(completion)

    assert completed.returncode == 0
    assert (tmp_path / "claimed.py").read_text(encoding="utf-8") == "internal"
    assert _shell_command_mutation_targets(command) == ()
    assert "filesystem_effects" not in observed_completion.data
    assert not _runtime_messages_support_file_claim(
        "claimed.py",
        (observed_call, observed_completion),
        task_cwd=str(tmp_path),
    )


def test_shell_expansion_detection_respects_single_quoted_perl_source() -> None:
    """A literal regex dollar does not mask otherwise valid receiver parsing."""
    assert not _text_needs_shell_expansion("perl -pi -e 's/$/x/' first.py second.py")
    assert _text_needs_shell_expansion('perl -pi -e "s/$/x/" first.py second.py')
    assert _text_needs_shell_expansion("perl -pi -e s/$/x/ first.py second.py")


def test_files_touched_rejects_shell_receiver_symlink_replaced_after_execution(tmp_path) -> None:
    """Recorded symlink identity cannot authenticate a later regular file."""
    outside = tmp_path.parent / f"{tmp_path.name}-outside.py"
    claimed_file = tmp_path / "claimed.py"
    outside.write_text("outside\n", encoding="utf-8")
    try:
        os.symlink(outside, claimed_file)
    except (OSError, NotImplementedError):  # pragma: no cover - unprivileged/Windows
        pytest.skip("symlink creation not permitted in this environment")
    command = "touch claimed.py"
    call = AgentMessage(
        type="tool",
        content=f"Bash: {command}",
        tool_name="Bash",
        data={"tool_input": {"command": command}},
    )
    pending_targets = _pending_bash_filesystem_targets(call, task_cwd=str(tmp_path))

    completed = subprocess.run(command, cwd=tmp_path, shell=True, check=False)  # noqa: S602
    claimed_file.unlink()
    claimed_file.write_text("replacement\n", encoding="utf-8")
    result = _attach_bash_filesystem_effects(
        AgentMessage(
            type="tool_result",
            content="command completed with exit code 0",
            data={"subtype": "tool_result", "exit_code": 0},
        ),
        pending_targets,
    )

    assert completed.returncode == 0
    assert "filesystem_effects" not in result.data
    messages = (call, result)
    assert not _runtime_messages_support_file_claim(
        "claimed.py",
        messages,
        task_cwd=str(tmp_path),
    )


def test_files_touched_rejects_parent_symlink_swap_restored_before_completion(tmp_path) -> None:
    """A held parent dirfd detects swap/restore around the actual shell execution."""
    link_dir = tmp_path / "link"
    original_dir = tmp_path / "original-link"
    outside_dir = tmp_path.parent / f"{tmp_path.name}-outside"
    link_dir.mkdir()
    outside_dir.mkdir()
    inside_file = link_dir / "claimed.py"
    outside_file = outside_dir / "claimed.py"
    inside_file.write_text("inside\n", encoding="utf-8")
    outside_file.write_text("outside\n", encoding="utf-8")
    os.utime(inside_file, ns=(1_000_000_000, 1_000_000_000))
    os.utime(outside_file, ns=(1_000_000_000, 1_000_000_000))
    inside_before = inside_file.stat()
    outside_before = outside_file.stat()
    command = "touch link/claimed.py"
    call = AgentMessage(
        type="tool",
        content=f"Bash: {command}",
        tool_name="Bash",
        data={"tool_input": {"command": command}},
    )
    pending_targets = _pending_bash_filesystem_targets(call, task_cwd=str(tmp_path))
    if not pending_targets:  # pragma: no cover - fail-closed platform fallback
        pytest.skip("dirfd/no-follow receiver leases are unavailable")
    link_dir.rename(original_dir)
    try:
        os.symlink(outside_dir, link_dir)
    except (OSError, NotImplementedError):  # pragma: no cover - unprivileged/Windows
        original_dir.rename(link_dir)
        _close_pending_targets(pending_targets)
        pytest.skip("symlink creation not permitted in this environment")

    completed = subprocess.run(command, cwd=tmp_path, shell=True, check=False)  # noqa: S602
    link_dir.unlink()
    original_dir.rename(link_dir)
    result = _attach_bash_filesystem_effects(
        AgentMessage(
            type="tool_result",
            content="command completed with exit code 0",
            data={"subtype": "tool_result", "exit_code": 0},
        ),
        pending_targets,
    )

    assert completed.returncode == 0
    assert inside_file.read_text(encoding="utf-8") == "inside\n"
    assert inside_file.stat().st_mtime_ns == inside_before.st_mtime_ns
    assert outside_file.stat().st_mtime_ns > outside_before.st_mtime_ns
    assert "filesystem_effects" not in result.data
    assert not _runtime_messages_support_file_claim(
        "link/claimed.py",
        (call, result),
        task_cwd=str(tmp_path),
    )


def test_capture_rejects_intermediate_workspace_ancestor_swap(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-linked external inode cannot survive a task_cwd ancestor swap."""
    ancestor = tmp_path / "trusted-ancestor"
    workspace = ancestor / "workspace"
    workspace.mkdir(parents=True)
    artifact = workspace / "claimed.py"
    artifact.write_text("before\n", encoding="utf-8")
    displaced_ancestor = tmp_path / "displaced-ancestor"
    outside_ancestor = tmp_path / "outside-ancestor"
    outside_workspace = outside_ancestor / workspace.name
    outside_workspace.mkdir(parents=True)
    outside_artifact = outside_workspace / artifact.name
    os.link(artifact, outside_artifact)
    original_open = os.open
    swapped = False
    matching_open_count = 0
    fingerprint_before_swap = None

    def adversarial_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal fingerprint_before_swap, matching_open_count, swapped
        fd = original_open(path, flags, mode, dir_fd=dir_fd)
        if dir_fd is not None and os.fspath(path) == ancestor.name:
            matching_open_count += 1
            if not swapped and matching_open_count == 2:
                fingerprint_before_swap = _stat_fingerprint(artifact.lstat())
                ancestor.rename(displaced_ancestor)
                ancestor.symlink_to(outside_ancestor, target_is_directory=True)
                swapped = True
        return fd

    monkeypatch.setattr(os, "open", adversarial_open)
    monkeypatch.setattr(os, "supports_dir_fd", {*os.supports_dir_fd, adversarial_open})
    call = AgentMessage(
        type="tool",
        content="Bash: touch claimed.py",
        tool_name="Bash",
        data={
            "tool_input": {"command": "touch claimed.py"},
            "tool_call_id": "ancestor-swap",
        },
    )
    completion = AgentMessage(
        type="tool_result",
        content="command completed with exit code 0",
        data={
            "subtype": "tool_result",
            "exit_code": 0,
            "tool_call_id": "ancestor-swap",
        },
    )

    with _BashFilesystemLeaseTracker(task_cwd=str(workspace)) as tracker:
        tracker.observe(call)
        pending = tracker._pending_by_id["ancestor-swap"][0]
        assert fingerprint_before_swap == pending.pre_fingerprint
        completed = subprocess.run(  # noqa: S602
            "touch claimed.py",
            cwd=workspace,
            shell=True,
            check=False,
        )
        observed_completion = tracker.observe(completion)

    assert swapped is True
    assert completed.returncode == 0
    assert _stat_fingerprint(outside_artifact.lstat()) != fingerprint_before_swap
    assert "filesystem_effects" not in observed_completion.data
    assert not _runtime_messages_support_file_claim(
        "claimed.py",
        (call, observed_completion),
        task_cwd=str(workspace),
    )


def test_bash_receiver_lease_cleanup_is_idempotent(tmp_path) -> None:
    """Unmatched/cancelled dispatch cleanup closes every held receiver dirfd."""
    target = tmp_path / "claimed.py"
    target.write_text("value\n", encoding="utf-8")
    call = AgentMessage(
        type="tool",
        content="Bash: touch claimed.py",
        tool_name="Bash",
        data={"tool_input": {"command": "touch claimed.py"}},
    )
    pending_targets = _pending_bash_filesystem_targets(call, task_cwd=str(tmp_path))
    if not pending_targets:  # pragma: no cover - fail-closed platform fallback
        pytest.skip("dirfd/no-follow receiver leases are unavailable")
    fds = tuple(target.parent_fd for target in pending_targets)

    _close_pending_targets(pending_targets)
    _close_pending_targets(pending_targets)

    for fd in fds:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.skipif(sys.platform == "win32", reason="RLIMIT_NOFILE is POSIX-only")
def test_bash_receiver_fd_budget_preserves_pipe_headroom_under_low_rlimit(tmp_path) -> None:
    """A wide command abandons capture before it can starve subprocess pipes."""
    child_script = textwrap.dedent(
        r"""
        import os
        from pathlib import Path
        import resource
        import subprocess
        import sys

        from ouroboros.orchestrator.adapter import AgentMessage
        from ouroboros.orchestrator.leaf_dispatcher import _BashFilesystemLeaseTracker

        workspace = Path(sys.argv[1])
        _soft_limit, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
        limit = 256 if hard_limit == resource.RLIM_INFINITY else min(256, hard_limit)
        if limit < 128:
            raise SystemExit(77)
        resource.setrlimit(resource.RLIMIT_NOFILE, (limit, hard_limit))

        fd_directory = Path("/proc/self/fd")
        if not fd_directory.exists():
            fd_directory = Path("/dev/fd")
        if not fd_directory.exists():
            raise SystemExit(77)

        def fd_count():
            return len(os.listdir(fd_directory))

        baseline = fd_count()
        receivers = [f"claimed_{index}.txt" for index in range(64)]
        command = "touch " + " ".join(receivers)
        call = AgentMessage(
            type="tool",
            content=f"Bash: {command}",
            tool_name="Bash",
            data={"tool_input": {"command": command}, "tool_call_id": "fd-budget"},
        )
        completion = AgentMessage(
            type="tool_result",
            content="command completed with exit code 0",
            tool_name="Bash",
            data={"subtype": "tool_result", "exit_code": 0, "tool_call_id": "fd-budget"},
        )

        with _BashFilesystemLeaseTracker(task_cwd=str(workspace)) as tracker:
            tracker.observe(call)
            assert tracker._pending_by_id["fd-budget"] == ()
            assert fd_count() < limit // 2
            completed = subprocess.run(
                ["/bin/sh", "-c", command],
                cwd=workspace,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            assert completed.stdout == ""
            assert completed.stderr == ""
            observed = tracker.observe(completion)
            assert "filesystem_effects" not in observed.data
            probe = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys; print('stdout-ok'); print('stderr-ok', file=sys.stderr)",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            assert probe.stdout == "stdout-ok\n"
            assert probe.stderr == "stderr-ok\n"

        assert fd_count() <= baseline + 1
        print("fd-budget-ok")
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", child_script, str(tmp_path)],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode == 77:  # pragma: no cover - constrained host fallback
        pytest.skip("host cannot provide the isolated RLIMIT_NOFILE regression")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "fd-budget-ok\n"


@pytest.mark.skipif(sys.platform == "win32", reason="RLIMIT_NOFILE is POSIX-only")
def test_bash_receiver_fd_budget_is_process_wide_across_trackers(tmp_path) -> None:
    """Concurrent trackers share headroom and cannot collectively starve pipes."""
    child_script = textwrap.dedent(
        r"""
        import os
        from pathlib import Path
        import resource
        import subprocess
        import sys

        from ouroboros.orchestrator.adapter import AgentMessage
        from ouroboros.orchestrator.leaf_dispatcher import _BashFilesystemLeaseTracker

        workspace = Path(sys.argv[1])
        _soft_limit, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
        limit = 256 if hard_limit == resource.RLIM_INFINITY else min(256, hard_limit)
        if limit < 128:
            raise SystemExit(77)
        resource.setrlimit(resource.RLIMIT_NOFILE, (limit, hard_limit))

        fd_directory = Path("/proc/self/fd")
        if not fd_directory.exists():
            fd_directory = Path("/dev/fd")
        if not fd_directory.exists():
            raise SystemExit(77)

        def fd_count():
            return len(os.listdir(fd_directory))

        baseline = fd_count()
        trackers = [
            _BashFilesystemLeaseTracker(task_cwd=str(workspace)) for _index in range(4)
        ]
        retained = 0
        abandoned = 0
        try:
            for tracker_index, tracker in enumerate(trackers):
                tracker.__enter__()
                for call_index in range(20):
                    receiver = f"tracker_{tracker_index}_{call_index}.txt"
                    call_id = f"tracker-{tracker_index}-call-{call_index}"
                    tracker.observe(
                        AgentMessage(
                            type="tool",
                            content=f"Bash: touch {receiver}",
                            tool_name="Bash",
                            data={
                                "tool_input": {"command": f"touch {receiver}"},
                                "tool_call_id": call_id,
                            },
                        )
                    )
                    lease_count = len(tracker._pending_by_id[call_id])
                    assert lease_count in {0, 1}
                    retained += lease_count
                    abandoned += lease_count == 0

            assert retained > 0
            assert abandoned > 0
            assert fd_count() <= limit // 2
            probe = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys; print('stdout-ok'); print('stderr-ok', file=sys.stderr)",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            assert probe.stdout == "stdout-ok\n"
            assert probe.stderr == "stderr-ok\n"
        finally:
            for tracker in reversed(trackers):
                tracker.close()

        assert fd_count() <= baseline + 1
        print("multi-fd-budget-ok")
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", child_script, str(tmp_path)],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode == 77:  # pragma: no cover - constrained host fallback
        pytest.skip("host cannot provide the isolated RLIMIT_NOFILE regression")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "multi-fd-budget-ok\n"


def test_completed_receiver_lease_cannot_close_reused_fd(tmp_path) -> None:
    """A completed lease invalidates ownership before its fd number is reused."""
    target = tmp_path / "claimed.py"
    target.write_text("before\n", encoding="utf-8")
    call = AgentMessage(
        type="tool",
        content="Bash: touch claimed.py",
        tool_name="Bash",
        data={"tool_input": {"command": "touch claimed.py"}},
    )
    pending_targets = _pending_bash_filesystem_targets(call, task_cwd=str(tmp_path))
    if not pending_targets:  # pragma: no cover - fail-closed platform fallback
        pytest.skip("dirfd/no-follow receiver leases are unavailable")
    leased_fd = pending_targets[0].parent_fd
    assert leased_fd is not None
    os.utime(target, ns=(3_000_000_000, 3_000_000_000))
    _attach_bash_filesystem_effects(
        AgentMessage(
            type="tool_result",
            content="command completed with exit code 0",
            data={"subtype": "tool_result", "exit_code": 0},
        ),
        pending_targets,
    )
    assert pending_targets[0].parent_fd is None
    probe_fd = os.open(os.devnull, os.O_RDONLY)
    if probe_fd != leased_fd:
        os.dup2(probe_fd, leased_fd)
        os.close(probe_fd)
    try:
        _close_pending_targets(pending_targets)
        os.fstat(leased_fd)
    finally:
        os.close(leased_fd)


def test_receiver_lease_rejects_regular_inode_replacement(tmp_path) -> None:
    """A different post-execution inode is not mutation proof for the pre receiver."""
    target = tmp_path / "claimed.py"
    original = tmp_path / "original.py"
    target.write_text("before\n", encoding="utf-8")
    call = AgentMessage(
        type="tool",
        content="Bash: touch claimed.py",
        tool_name="Bash",
        data={"tool_input": {"command": "touch claimed.py"}},
    )
    pending_targets = _pending_bash_filesystem_targets(call, task_cwd=str(tmp_path))
    if not pending_targets:  # pragma: no cover - fail-closed platform fallback
        pytest.skip("dirfd/no-follow receiver leases are unavailable")
    target.rename(original)
    target.write_text("replacement\n", encoding="utf-8")

    result = _attach_bash_filesystem_effects(
        AgentMessage(
            type="tool_result",
            content="command completed with exit code 0",
            data={"subtype": "tool_result", "exit_code": 0},
        ),
        pending_targets,
    )

    assert "filesystem_effects" not in result.data


@pytest.mark.parametrize("exit_kind", ("normal", "exception", "cancellation"))
def test_receiver_lease_tracker_closes_unmatched_calls_on_every_exit(
    tmp_path,
    exit_kind,
) -> None:
    """Normal end, adapter failure, and cancellation all release unmatched dirfds."""
    target = tmp_path / "claimed.py"
    target.write_text("value\n", encoding="utf-8")
    call = AgentMessage(
        type="tool",
        content="Bash: touch claimed.py",
        tool_name="Bash",
        data={"tool_input": {"command": "touch claimed.py"}, "tool_call_id": "call-1"},
    )
    tracker = _BashFilesystemLeaseTracker(task_cwd=str(tmp_path))
    captured_fds: tuple[int, ...] = ()
    try:
        with tracker:
            tracker.observe(call)
            leased = tracker._pending_by_id["call-1"]
            captured_fds = tuple(
                pending.parent_fd for pending in leased if pending.parent_fd is not None
            )
            if exit_kind == "exception":
                raise RuntimeError("adapter failed")
            if exit_kind == "cancellation":
                raise asyncio.CancelledError
    except (RuntimeError, asyncio.CancelledError):
        pass

    assert captured_fds
    for fd in captured_fds:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_receiver_lease_tracker_rejects_duplicate_idless_pairing(tmp_path) -> None:
    """Two id-less Bash starts cannot donate either receiver to one completion."""
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("first\n", encoding="utf-8")
    second.write_text("second\n", encoding="utf-8")
    first_call = AgentMessage(
        type="tool",
        content="Bash: touch first.py",
        tool_name="Bash",
        data={"tool_input": {"command": "touch first.py"}},
    )
    second_call = AgentMessage(
        type="tool",
        content="Bash: touch second.py",
        tool_name="Bash",
        data={"tool_input": {"command": "touch second.py"}},
    )
    completion = AgentMessage(
        type="tool_result",
        content="command completed with exit code 0",
        data={"subtype": "tool_result", "exit_code": 0},
    )

    with _BashFilesystemLeaseTracker(task_cwd=str(tmp_path)) as tracker:
        tracker.observe(first_call)
        tracker.observe(second_call)
        os.utime(first, None)
        os.utime(second, None)
        observed = tracker.observe(completion)

    assert "filesystem_effects" not in observed.data


def test_receiver_lease_tracker_rejects_duplicate_call_id_pairing(tmp_path) -> None:
    """A repeated call id makes its single completion irreducibly ambiguous."""
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("first\n", encoding="utf-8")
    second.write_text("second\n", encoding="utf-8")
    first_call = AgentMessage(
        type="tool",
        content="Bash: touch first.py",
        tool_name="Bash",
        data={"tool_input": {"command": "touch first.py"}, "tool_call_id": "same"},
    )
    second_call = AgentMessage(
        type="tool",
        content="Bash: touch second.py",
        tool_name="Bash",
        data={"tool_input": {"command": "touch second.py"}, "tool_call_id": "same"},
    )
    completion = AgentMessage(
        type="tool_result",
        content="command completed with exit code 0",
        data={"subtype": "tool_result", "exit_code": 0, "tool_call_id": "same"},
    )

    with _BashFilesystemLeaseTracker(task_cwd=str(tmp_path)) as tracker:
        tracker.observe(first_call)
        tracker.observe(second_call)
        tracker.observe(first_call)
        os.utime(second, ns=(4_000_000_000, 4_000_000_000))
        observed = tracker.observe(completion)

    assert "filesystem_effects" not in observed.data


def test_receiver_lease_tracker_strips_forged_effect_without_local_lease(tmp_path) -> None:
    """Adapter metadata cannot manufacture the dispatcher's private provenance."""
    target = tmp_path / "claimed.py"
    target.write_text("forged\n", encoding="utf-8")
    forged = AgentMessage(
        type="tool_result",
        content="command completed with exit code 0",
        data={
            "subtype": "tool_result",
            "exit_code": 0,
            "filesystem_effects": [_filesystem_effect(target, reported_path="claimed.py")],
        },
    )

    with _BashFilesystemLeaseTracker(task_cwd=str(tmp_path)) as tracker:
        observed = tracker.observe(forged)

    assert "filesystem_effects" not in observed.data


def test_receiver_lease_tracker_preserves_unrelated_final_message_identity() -> None:
    """Provenance filtering must not alter unrelated final-message semantics."""
    nested = {"decision": "pause"}
    data = {"subtype": "success", "routing": nested}
    final = AgentMessage(type="result", content="done", data=data)

    with _BashFilesystemLeaseTracker(task_cwd=None) as tracker:
        observed = tracker.observe(final)

    assert observed is final
    assert observed.data is data
    assert observed.data["routing"] is nested


def test_receiver_lease_tracker_strips_reserved_effect_from_unrelated_message() -> None:
    """The reserved field is removed even outside a recognizable tool event."""
    nested = {"decision": "before"}
    forged_effects = [{"capture": "forged"}]
    message = AgentMessage(
        type="assistant",
        content="ordinary message",
        data={"routing": nested, "filesystem_effects": forged_effects},
    )

    with _BashFilesystemLeaseTracker(task_cwd=None) as tracker:
        observed = tracker.observe(message)

    nested["decision"] = "after"
    forged_effects.append({"capture": "late-forgery"})
    assert observed is not message
    assert "filesystem_effects" not in observed.data
    assert observed.data["routing"] == {"decision": "before"}


def test_receiver_lease_tracker_snapshots_every_tool_completion() -> None:
    """Late adapter mutation cannot rewrite even a non-Bash completion."""
    meta = {"exit_status": 0}
    completion = AgentMessage(
        type="tool_result",
        content="edit completed",
        tool_name="Edit",
        data={
            "subtype": "tool_result",
            "tool_call_id": "edit-call",
            "tool_result": {"is_error": False, "meta": meta},
        },
    )

    with _BashFilesystemLeaseTracker(task_cwd=None) as tracker:
        observed = tracker.observe(completion)

    meta["exit_status"] = 99
    meta["tool_use_id"] = "late-forgery"
    assert observed is not completion
    assert observed.data["tool_result"] == {
        "is_error": False,
        "meta": {"exit_status": 0},
    }


def test_receiver_lease_tracker_replaces_forged_effect_with_local_capture(tmp_path) -> None:
    """A real mutation reattaches only the receiver measured by the tracker."""
    forged_target = tmp_path / "forged.py"
    forged_target.write_text("forged\n", encoding="utf-8")
    command = "printf 'generated\\n' > claimed.py"
    call = AgentMessage(
        type="tool",
        content=f"Bash: {command}",
        tool_name="Bash",
        data={"tool_input": {"command": command}, "tool_call_id": "local-only"},
    )
    completion = AgentMessage(
        type="tool_result",
        content="command completed with exit code 0",
        data={
            "subtype": "tool_result",
            "exit_code": 0,
            "tool_call_id": "local-only",
            "filesystem_effects": [_filesystem_effect(forged_target, reported_path="forged.py")],
        },
    )

    with _BashFilesystemLeaseTracker(task_cwd=str(tmp_path)) as tracker:
        tracker.observe(call)
        completed = subprocess.run(command, cwd=tmp_path, shell=True, check=False)  # noqa: S602
        observed = tracker.observe(completion)

    assert completed.returncode == 0
    effects = observed.data["filesystem_effects"]
    assert isinstance(effects, list)
    assert [effect["path"] for effect in effects] == ["claimed.py"]


def test_receiver_lease_tracker_accepts_repeated_identical_id_aliases(tmp_path) -> None:
    """Equivalent aliases retain compatibility and correlate one local effect."""
    command = "printf 'generated\\n' > claimed.py"
    call = AgentMessage(
        type="tool",
        content=f"Bash: {command}",
        tool_name="Bash",
        data={
            "tool_input": {"command": command},
            "tool_call_id": "same-id",
            "tool_use_id": "same-id",
        },
    )
    completion = AgentMessage(
        type="tool_result",
        content="command completed with exit code 0",
        data={
            "subtype": "tool_result",
            "exit_code": 0,
            "tool_result": {
                "call_id": "same-id",
                "meta": {"tool_use_id": "same-id"},
            },
        },
    )

    with _BashFilesystemLeaseTracker(task_cwd=str(tmp_path)) as tracker:
        tracker.observe(call)
        completed = subprocess.run(command, cwd=tmp_path, shell=True, check=False)  # noqa: S602
        observed = tracker.observe(completion)

    assert completed.returncode == 0
    assert observed.data["filesystem_effects"][0]["path"] == "claimed.py"


def test_conflicting_completion_is_not_consumed_as_idless_result(tmp_path) -> None:
    """Alias conflict cannot take an id-less lease through the fallback path."""
    command = "printf 'generated\\n' > claimed.py"
    call = AgentMessage(
        type="tool",
        content=f"Bash: {command}",
        tool_name="Bash",
        data={"tool_input": {"command": command}},
    )
    conflict = AgentMessage(
        type="tool_result",
        content="ambiguous completion",
        data={
            "subtype": "tool_result",
            "exit_code": 0,
            "tool_call_id": "first",
            "tool_result": {"meta": {"call_id": "second"}},
            "filesystem_effects": [{"capture": "ouroboros.leaf-dispatch.v1", "path": "forged.py"}],
        },
    )
    idless_completion = AgentMessage(
        type="tool_result",
        content="command completed with exit code 0",
        data={"subtype": "tool_result", "exit_code": 0},
    )

    with _BashFilesystemLeaseTracker(task_cwd=str(tmp_path)) as tracker:
        tracker.observe(call)
        completed = subprocess.run(command, cwd=tmp_path, shell=True, check=False)  # noqa: S602
        rejected = tracker.observe(conflict)
        accepted = tracker.observe(idless_completion)

    assert completed.returncode == 0
    assert "filesystem_effects" not in rejected.data
    assert accepted.data["filesystem_effects"][0]["path"] == "claimed.py"


def test_conflicting_call_aliases_poison_related_existing_lease(tmp_path) -> None:
    """An ambiguous call neither consumes nor donates a prior Bash receiver."""
    target = tmp_path / "claimed.py"
    target.write_text("before\n", encoding="utf-8")
    original = AgentMessage(
        type="tool",
        content="Bash: touch claimed.py",
        tool_name="Bash",
        data={"tool_input": {"command": "touch claimed.py"}, "tool_call_id": "first"},
    )
    conflict = AgentMessage(
        type="tool",
        content="Bash: touch other.py",
        tool_name="Bash",
        data={
            "tool_input": {"command": "touch other.py"},
            "tool_call_id": "first",
            "meta": {"tool_use_id": "second"},
        },
    )
    completion = AgentMessage(
        type="tool_result",
        content="command completed with exit code 0",
        data={"subtype": "tool_result", "exit_code": 0, "tool_call_id": "first"},
    )

    with _BashFilesystemLeaseTracker(task_cwd=str(tmp_path)) as tracker:
        tracker.observe(original)
        tracker.observe(conflict)
        os.utime(target, ns=(7_000_000_000, 7_000_000_000))
        observed = tracker.observe(completion)

    assert "filesystem_effects" not in observed.data


def test_malformed_call_alias_poison_cannot_be_revived(tmp_path) -> None:
    """A present non-string alias closes the named lease before projection."""
    target = tmp_path / "claimed.py"
    target.write_text("before\n", encoding="utf-8")
    call = AgentMessage(
        type="tool",
        content="Bash: touch claimed.py",
        tool_name="Bash",
        data={"tool_input": {"command": "touch claimed.py"}, "tool_call_id": "call-x"},
    )
    malformed = AgentMessage(
        type="tool_result",
        content="malformed completion",
        data={
            "subtype": "tool_result",
            "exit_code": 0,
            "tool_call_id": "call-x",
            "tool_use_id": 7,
        },
    )
    valid = AgentMessage(
        type="tool_result",
        content="later valid completion",
        data={"subtype": "tool_result", "exit_code": 0, "tool_call_id": "call-x"},
    )

    with _BashFilesystemLeaseTracker(task_cwd=str(tmp_path)) as tracker:
        tracker.observe(call)
        os.utime(target, ns=(8_000_000_000, 8_000_000_000))
        rejected = tracker.observe(malformed)
        revived = tracker.observe(valid)

    assert "filesystem_effects" not in rejected.data
    assert "filesystem_effects" not in revived.data


def test_unidentified_malformed_completion_poison_cannot_be_revived(tmp_path) -> None:
    """A malformed completion with no usable ID closes every pending Bash lease."""
    target = tmp_path / "claimed.py"
    target.write_text("before\n", encoding="utf-8")
    call = AgentMessage(
        type="tool",
        content="Bash: touch claimed.py",
        tool_name="Bash",
        data={"tool_input": {"command": "touch claimed.py"}, "tool_call_id": "call-x"},
    )
    malformed = AgentMessage(
        type="tool_result",
        content="unidentified malformed completion",
        data={"subtype": "tool_result", "exit_code": 0, "tool_use_id": 7},
    )
    valid = AgentMessage(
        type="tool_result",
        content="later valid completion",
        data={"subtype": "tool_result", "exit_code": 0, "tool_call_id": "call-x"},
    )

    with _BashFilesystemLeaseTracker(task_cwd=str(tmp_path)) as tracker:
        tracker.observe(call)
        os.utime(target, ns=(9_000_000_000, 9_000_000_000))
        rejected = tracker.observe(malformed)
        revived = tracker.observe(valid)

    assert "filesystem_effects" not in rejected.data
    assert "filesystem_effects" not in revived.data


@pytest.mark.parametrize("completion_tool_name", ("Bash", None))
def test_orphan_idless_bash_terminal_poisons_all_named_leases(
    tmp_path,
    completion_tool_name,
) -> None:
    """One unassignable terminal closes, rather than donates, every named lease."""
    targets = (tmp_path / "first.py", tmp_path / "second.py")
    for target in targets:
        target.write_text("before\n", encoding="utf-8")
    calls = tuple(
        AgentMessage(
            type="tool",
            content=f"Bash: touch {target.name}",
            tool_name="Bash",
            data={
                "tool_input": {"command": f"touch {target.name}"},
                "tool_call_id": f"named-{index}",
            },
        )
        for index, target in enumerate(targets)
    )
    orphan = AgentMessage(
        type="tool_result",
        content="unassigned command completed",
        tool_name=completion_tool_name,
        data={"subtype": "tool_result", "exit_code": 0},
    )
    completions = tuple(
        AgentMessage(
            type="tool_result",
            content="late matching completion",
            tool_name="Bash",
            data={
                "subtype": "tool_result",
                "exit_code": 0,
                "tool_call_id": f"named-{index}",
            },
        )
        for index in range(len(targets))
    )

    with _BashFilesystemLeaseTracker(task_cwd=str(tmp_path)) as tracker:
        for call in calls:
            tracker.observe(call)
        tracker.observe(orphan)
        for index, target in enumerate(targets, start=11):
            os.utime(target, ns=(index * 1_000_000_000, index * 1_000_000_000))
        observed = tuple(tracker.observe(completion) for completion in completions)

    assert all("filesystem_effects" not in completion.data for completion in observed)


def test_legitimate_idless_bash_pair_does_not_poison_named_lease(tmp_path) -> None:
    """A unique id-less Bash start owns its terminal without closing a named peer."""
    idless_target = tmp_path / "idless.py"
    named_target = tmp_path / "named.py"
    idless_target.write_text("before\n", encoding="utf-8")
    named_target.write_text("before\n", encoding="utf-8")
    idless_call = AgentMessage(
        type="tool",
        content="Bash: touch idless.py",
        tool_name="Bash",
        data={"tool_input": {"command": "touch idless.py"}},
    )
    named_call = AgentMessage(
        type="tool",
        content="Bash: touch named.py",
        tool_name="Bash",
        data={
            "tool_input": {"command": "touch named.py"},
            "tool_call_id": "named-peer",
        },
    )
    idless_completion = AgentMessage(
        type="tool_result",
        content="idless command completed",
        data={"subtype": "tool_result", "exit_code": 0},
    )
    named_completion = AgentMessage(
        type="tool_result",
        content="named command completed",
        tool_name="Bash",
        data={
            "subtype": "tool_result",
            "exit_code": 0,
            "tool_call_id": "named-peer",
        },
    )

    with _BashFilesystemLeaseTracker(task_cwd=str(tmp_path)) as tracker:
        tracker.observe(idless_call)
        tracker.observe(named_call)
        os.utime(idless_target, ns=(13_000_000_000, 13_000_000_000))
        observed_idless = tracker.observe(idless_completion)
        os.utime(named_target, ns=(14_000_000_000, 14_000_000_000))
        observed_named = tracker.observe(named_completion)

    assert observed_idless.data["filesystem_effects"][0]["path"] == "idless.py"
    assert observed_named.data["filesystem_effects"][0]["path"] == "named.py"


def test_unrelated_idless_non_bash_terminal_preserves_named_lease(tmp_path) -> None:
    """An explicitly other-tool terminal cannot close a named Bash command."""
    target = tmp_path / "claimed.py"
    target.write_text("before\n", encoding="utf-8")
    call = AgentMessage(
        type="tool",
        content="Bash: touch claimed.py",
        tool_name="Bash",
        data={
            "tool_input": {"command": "touch claimed.py"},
            "tool_call_id": "named-bash",
        },
    )
    edit_completion = AgentMessage(
        type="tool_result",
        content="Edit completed",
        tool_name="Edit",
        data={"subtype": "tool_result", "exit_code": 0},
    )
    bash_completion = AgentMessage(
        type="tool_result",
        content="Bash completed",
        tool_name="Bash",
        data={
            "subtype": "tool_result",
            "exit_code": 0,
            "tool_call_id": "named-bash",
        },
    )

    with _BashFilesystemLeaseTracker(task_cwd=str(tmp_path)) as tracker:
        tracker.observe(call)
        tracker.observe(edit_completion)
        os.utime(target, ns=(15_000_000_000, 15_000_000_000))
        observed = tracker.observe(bash_completion)

    assert observed.data["filesystem_effects"][0]["path"] == "claimed.py"


@pytest.mark.parametrize("call_id", (None, "call-1"))
def test_receiver_lease_tracker_rejects_explicit_non_bash_completion(
    tmp_path,
    call_id,
) -> None:
    """An Edit completion cannot consume a Bash lease even with the same id."""
    target = tmp_path / "claimed.py"
    target.write_text("before\n", encoding="utf-8")
    call_data: dict[str, object] = {"tool_input": {"command": "touch claimed.py"}}
    completion_data: dict[str, object] = {
        "subtype": "tool_result",
        "exit_code": 0,
    }
    if call_id is not None:
        call_data["tool_call_id"] = call_id
        completion_data["tool_call_id"] = call_id
    call = AgentMessage(
        type="tool",
        content="Bash: touch claimed.py",
        tool_name="Bash",
        data=call_data,
    )
    completion = AgentMessage(
        type="tool_result",
        content="Edit completed",
        tool_name="Edit",
        data=completion_data,
    )

    with _BashFilesystemLeaseTracker(task_cwd=str(tmp_path)) as tracker:
        tracker.observe(call)
        os.utime(target, ns=(5_000_000_000, 5_000_000_000))
        observed = tracker.observe(completion)

    assert "filesystem_effects" not in observed.data


@pytest.mark.parametrize("call_id", (None, "shared-call"))
@pytest.mark.parametrize("bash_first", (False, True))
def test_receiver_lease_tracker_rejects_mixed_tool_call_ownership(
    tmp_path,
    call_id,
    bash_first,
) -> None:
    """A mixed Bash/Edit call identity cannot feed a nameless completion."""
    target = tmp_path / "claimed.py"
    target.write_text("before\n", encoding="utf-8")
    bash_data: dict[str, object] = {"tool_input": {"command": "touch claimed.py"}}
    edit_data: dict[str, object] = {"tool_input": {"file_path": "other.py"}}
    completion_data: dict[str, object] = {
        "subtype": "tool_result",
        "exit_code": 0,
    }
    if call_id is not None:
        bash_data["tool_call_id"] = call_id
        edit_data["tool_call_id"] = call_id
        completion_data["tool_call_id"] = call_id
    bash_call = AgentMessage(
        type="tool",
        content="Bash: touch claimed.py",
        tool_name="Bash",
        data=bash_data,
    )
    edit_call = AgentMessage(
        type="tool",
        content="Edit: other.py",
        tool_name="Edit",
        data=edit_data,
    )
    completion = AgentMessage(
        type="tool_result",
        content="completed",
        data=completion_data,
    )
    calls = (bash_call, edit_call) if bash_first else (edit_call, bash_call)

    with _BashFilesystemLeaseTracker(task_cwd=str(tmp_path)) as tracker:
        for call in calls:
            tracker.observe(call)
        os.utime(target, ns=(6_000_000_000, 6_000_000_000))
        observed = tracker.observe(completion)

    assert "filesystem_effects" not in observed.data


def test_bash_receiver_leases_do_not_leak_fds_across_repeated_completions(tmp_path) -> None:
    """Repeated successful capture/close cycles retain no receiver descriptors."""
    fd_directory = Path("/proc/self/fd")
    if not fd_directory.exists():
        fd_directory = Path("/dev/fd")
    if not fd_directory.exists():  # pragma: no cover - platform-specific observability
        pytest.skip("open descriptor directory is unavailable")
    target = tmp_path / "claimed.py"
    target.write_text("value\n", encoding="utf-8")
    call = AgentMessage(
        type="tool",
        content="Bash: touch claimed.py",
        tool_name="Bash",
        data={"tool_input": {"command": "touch claimed.py"}},
    )
    before = len(tuple(fd_directory.iterdir()))

    for iteration in range(64):
        pending_targets = _pending_bash_filesystem_targets(call, task_cwd=str(tmp_path))
        if not pending_targets:  # pragma: no cover - fail-closed platform fallback
            pytest.skip("dirfd/no-follow receiver leases are unavailable")
        timestamp = 2_000_000_000 + iteration
        os.utime(target, ns=(timestamp, timestamp))
        _attach_bash_filesystem_effects(
            AgentMessage(
                type="tool_result",
                content="command completed with exit code 0",
                data={"subtype": "tool_result", "exit_code": 0},
            ),
            pending_targets,
        )

    after = len(tuple(fd_directory.iterdir()))
    assert after <= before + 1


def test_bash_receiver_lease_fails_closed_without_dirfd_support(tmp_path, monkeypatch) -> None:
    """Platforms without safe dirfd traversal do not emit command-text proof."""
    target = tmp_path / "claimed.py"
    target.write_text("value\n", encoding="utf-8")
    call = AgentMessage(
        type="tool",
        content="Bash: touch claimed.py",
        tool_name="Bash",
        data={"tool_input": {"command": "touch claimed.py"}},
    )
    monkeypatch.setattr(os, "supports_dir_fd", set())

    assert _pending_bash_filesystem_targets(call, task_cwd=str(tmp_path)) == ()


def test_files_touched_allows_expanded_payload_with_literal_redirect_target(tmp_path) -> None:
    """Payload expansion preserves direct proof from a literal output target."""
    command = "printf '%s\\n' \"$VALUE\" > claimed.py"
    call = AgentMessage(
        type="tool",
        content=f"Bash: {command}",
        tool_name="Bash",
        data={"tool_input": {"command": command}},
    )
    pending_targets = _pending_bash_filesystem_targets(call, task_cwd=str(tmp_path))
    completed = subprocess.run(  # noqa: S602
        command,
        cwd=tmp_path,
        shell=True,
        check=False,
        env={**os.environ, "VALUE": "expanded"},
    )

    assert completed.returncode == 0
    assert (tmp_path / "claimed.py").read_text(encoding="utf-8") == "expanded\n"
    result = _attach_bash_filesystem_effects(
        AgentMessage(
            type="tool_result",
            content="command completed with exit code 0",
            data={"subtype": "tool_result", "exit_code": 0},
        ),
        pending_targets,
    )
    messages = (
        call,
        result,
    )
    assert (
        _runtime_messages_support_file_claim("claimed.py", messages, task_cwd=str(tmp_path)) is True
    )


def test_files_touched_resolves_shell_target_from_recorded_command_cwd(tmp_path) -> None:
    """A relative shell target authenticates only its command-cwd path."""
    root_claim = tmp_path / "claimed.py"
    command_cwd = tmp_path / "sub"
    root_claim.write_text("original\n", encoding="utf-8")
    command_cwd.mkdir()
    command = "touch claimed.py"

    completed = subprocess.run(command, cwd=command_cwd, shell=True, check=False)  # noqa: S602

    assert completed.returncode == 0
    assert root_claim.read_text(encoding="utf-8") == "original\n"
    assert (command_cwd / "claimed.py").exists()
    messages = (
        AgentMessage(
            type="tool",
            content=f"Bash: {command}",
            tool_name="Bash",
            data={"tool_input": {"command": command, "cwd": "sub"}},
        ),
        AgentMessage(
            type="tool_result",
            content="command completed with exit code 0",
            data={
                "subtype": "tool_result",
                "exit_code": 0,
                "filesystem_effects": [
                    _filesystem_effect(
                        command_cwd / "claimed.py",
                        reported_path="claimed.py",
                    )
                ],
            },
        ),
    )
    assert not _runtime_messages_support_file_claim(
        "claimed.py",
        messages,
        task_cwd=str(tmp_path),
    )
    assert _runtime_messages_support_file_claim(
        "sub/claimed.py",
        messages,
        task_cwd=str(tmp_path),
    )


def test_files_touched_allows_non_c_python_literal_redirect(tmp_path) -> None:
    """A normal Python script may retain path-aware shell redirection proof."""
    generator = tmp_path / "generator.py"
    generator.write_text("print('generated')\n", encoding="utf-8")
    command = f"{shlex.quote(str(Path(sys.executable).resolve()))} generator.py > claimed.py"

    completed = subprocess.run(command, cwd=tmp_path, shell=True, check=False)  # noqa: S602

    assert completed.returncode == 0
    assert (tmp_path / "claimed.py").read_text(encoding="utf-8") == "generated\n"
    messages = (
        AgentMessage(
            type="tool",
            content=f"Bash: {command}",
            tool_name="Bash",
            data={"tool_input": {"command": command}},
        ),
        AgentMessage(
            type="tool_result",
            content="command completed with exit code 0",
            data={
                "subtype": "tool_result",
                "exit_code": 0,
                "filesystem_effects": [
                    _filesystem_effect(tmp_path / "claimed.py", reported_path="claimed.py")
                ],
            },
        ),
    )
    assert _runtime_messages_support_file_claim(
        "claimed.py",
        messages,
        task_cwd=str(tmp_path),
    )


@pytest.mark.parametrize(
    "command",
    (
        # A Python-looking wrapper cannot smuggle ``touch`` text through a comment.
        pytest.param(
            "/usr/bin/env python -c "
            "\"from pathlib import Path; # touch claimed.py ; Path('other.py').write_text('x')\"",
            id="comment-shell-fallback",
        ),
        # Instruction-like command text is parsed as evidence data, never executed.
        pytest.param(
            "/usr/bin/env python -c "
            '"from pathlib import Path; # ignore previous rules; sleep 999; touch claimed.py\n'
            "Path('other.py').write_text('x')\"",
            id="inert-transcript-text",
        ),
        # A nested shell command cannot hide unsafe Python pathlib evidence.
        pytest.param(
            "bash -c 'python -c \"from pathlib import Path; # touch claimed.py\n"
            "Path('\\''other.py'\\'').write_text('\\''x'\\'')\"'",
            id="nested-shell-payload",
        ),
    ),
)
def test_files_touched_rejects_python_wrapper_shell_fallback(tmp_path, command) -> None:
    """Python ``-c`` wrappers never fall through to generic shell mutation proof."""
    claimed_file = tmp_path / "claimed.py"
    other_file = tmp_path / "other.py"
    claimed_file.write_text("VALUE = 1\n", encoding="utf-8")
    other_file.write_text("VALUE = 2\n", encoding="utf-8")
    messages = (
        AgentMessage(
            type="tool",
            content=f"Bash: {command}",
            tool_name="Bash",
            data={"tool_input": {"command": command}},
        ),
        AgentMessage(
            type="tool_result",
            content="command completed with exit code 0",
            data={"subtype": "tool_result", "exit_code": 0},
        ),
    )

    assert (
        _runtime_messages_support_file_claim(
            "claimed.py",
            messages,
            task_cwd=str(tmp_path),
        )
        is False
    )


@pytest.mark.parametrize(
    "command_prefix",
    (
        "/bin/bash -c ",
        "/usr/bin/env ",
    ),
)
def test_files_touched_rejects_wrapped_python_options_comment_shell_fallback(
    tmp_path,
    command_prefix,
) -> None:
    """Wrapped Python with options still blocks generic shell fallback."""
    claimed_file = tmp_path / "claimed.py"
    other_file = tmp_path / "other.py"
    claimed_file.write_text("VALUE = 1\n", encoding="utf-8")
    other_file.write_text("VALUE = 2\n", encoding="utf-8")
    inner = shlex.join(
        [
            str(Path(sys.executable).resolve()),
            "-I",
            "-S",
            "-c",
            "from pathlib import Path; # touch claimed.py\nPath('other.py').write_text('x')",
        ]
    )
    command = (
        command_prefix + shlex.quote(inner)
        if command_prefix == "/bin/bash -c "
        else command_prefix + inner
    )
    messages = (
        AgentMessage(
            type="tool",
            content=f"Bash: {command}",
            tool_name="Bash",
            data={"tool_input": {"command": command}},
        ),
        AgentMessage(
            type="tool_result",
            content="command completed with exit code 0",
            data={"subtype": "tool_result", "exit_code": 0},
        ),
    )

    assert (
        _runtime_messages_support_file_claim(
            "claimed.py",
            messages,
            task_cwd=str(tmp_path),
        )
        is False
    )


def test_files_touched_rejects_shell_quoted_absolute_python_fallback(tmp_path) -> None:
    """Shell quotes around an absolute interpreter cannot hide Python evidence."""
    claimed_file = tmp_path / "claimed.py"
    other_file = tmp_path / "other.py"
    claimed_file.write_text("VALUE = 1\n", encoding="utf-8")
    other_file.write_text("VALUE = 2\n", encoding="utf-8")
    source = "from pathlib import Path; # touch claimed.py\nPath('other.py').write_text('x')"
    inner = f'"{Path(sys.executable).resolve()}" -I -S -c {shlex.quote(source)}'
    command = f"/bin/bash -c {shlex.quote(inner)}"
    messages = (
        AgentMessage(
            type="tool",
            content=f"Bash: {command}",
            tool_name="Bash",
            data={"tool_input": {"command": command}},
        ),
        AgentMessage(
            type="tool_result",
            content="command completed with exit code 0",
            data={"subtype": "tool_result", "exit_code": 0},
        ),
    )

    assert not _runtime_messages_support_file_claim(
        "claimed.py",
        messages,
        task_cwd=str(tmp_path),
    )


def _fragment_quoted_python_command() -> str:
    """Shell-concatenated interpreter spelling cannot reach generic touch matching."""
    source = "from pathlib import Path; # touch claimed.py\nPath('other.py').write_text('changed')"
    executable = str(Path(sys.executable).resolve())
    fragmented_executable = executable.replace("python", 'py"thon"', 1)
    assert fragmented_executable != executable
    inner = f"{fragmented_executable} -c {shlex.quote(source)}"
    return f"/bin/bash -c {shlex.quote(inner)}"


def _dead_compound_mutation_branch_command() -> str:
    """A successful compound command cannot prove an unexecuted write branch."""
    source = "from pathlib import Path; getattr(Path('other.py'), 'write_' + 'text')('changed')"
    python_command = shlex.join([str(Path(sys.executable).resolve()), "-I", "-S", "-c", source])
    inner = f"false && touch claimed.py; {python_command}"
    return f"/bin/bash -c {shlex.quote(inner)}"


def _shell_expanded_python_pathlib_command() -> str:
    """Dynamic shell tokens cannot fall through to generic mutation matching."""
    inner = (
        '"${P}"thon -"c" "from path${L} import P${A}th; # touch claimed.py\n'
        "P${A}th('other.py').write_text('changed')\""
    )
    return f"P=py L=lib A=a /bin/bash -c {shlex.quote(inner)}"


@pytest.mark.parametrize(
    "build_command",
    (
        pytest.param(_fragment_quoted_python_command, id="fragment-quoted-interpreter"),
        pytest.param(_dead_compound_mutation_branch_command, id="dead-compound-branch"),
        pytest.param(_shell_expanded_python_pathlib_command, id="shell-expanded-tokens"),
    ),
)
def test_files_touched_rejects_obfuscated_python_shell_fallback(tmp_path, build_command) -> None:
    """Obfuscated Python invocations never fall through to generic mutation matching.

    Each command really runs: it writes ``other.py`` and leaves ``claimed.py``
    untouched, so the ``claimed.py`` file claim must stay unproven.
    """
    claimed_file = tmp_path / "claimed.py"
    other_file = tmp_path / "other.py"
    claimed_file.write_text("VALUE = 1\n", encoding="utf-8")
    other_file.write_text("VALUE = 2\n", encoding="utf-8")
    command = build_command()

    completed = subprocess.run(
        command,
        cwd=tmp_path,
        shell=True,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    assert claimed_file.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert other_file.read_text(encoding="utf-8") == "changed"

    messages = (
        AgentMessage(
            type="tool",
            content=f"Bash: {command}",
            tool_name="Bash",
            data={"tool_input": {"command": command}},
        ),
        AgentMessage(
            type="tool_result",
            content="command completed with exit code 0",
            data={"subtype": "tool_result", "exit_code": 0},
        ),
    )

    assert not _runtime_messages_support_file_claim(
        "claimed.py",
        messages,
        task_cwd=str(tmp_path),
    )


def test_files_touched_rejects_parser_failure_with_tabbed_python_c_payload(tmp_path) -> None:
    """Malformed shell text with tabbed Python ``-c`` evidence fails closed."""
    claimed_file = tmp_path / "claimed.py"
    claimed_file.write_text("VALUE = 1\n", encoding="utf-8")
    command = (
        "cat <<'PY\n"
        'python\t-c "from pathlib import Path; # touch claimed.py\n'
        "Path('other.py').write_text('x')\"\n"
    )
    messages = (
        AgentMessage(
            type="tool",
            content=f"Bash: {command}",
            tool_name="Bash",
            data={"tool_input": {"command": command}},
        ),
        AgentMessage(
            type="tool_result",
            content="command completed with exit code 0",
            data={"subtype": "tool_result", "exit_code": 0},
        ),
    )

    assert (
        _runtime_messages_support_file_claim(
            "claimed.py",
            messages,
            task_cwd=str(tmp_path),
        )
        is False
    )


def test_files_touched_rejects_relative_python_executable_even_when_final_state_matches(
    tmp_path, monkeypatch
) -> None:
    """Workspace-controlled Python executable paths are mutable final-state evidence."""
    command_cwd = tmp_path / "runner"
    command_cwd.mkdir()
    claimed_file = command_cwd / "claimed.py"
    claimed_file.write_text("VALUE = 1\n", encoding="utf-8")
    fake_python = command_cwd / "python3"
    fake_python.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(fake_python))
    command = "./python3 -I -S -c \"from pathlib import Path; Path('claimed.py').write_text('x')\""
    messages = (
        AgentMessage(
            type="tool",
            content=f"Bash: {command}",
            tool_name="Bash",
            data={"tool_input": {"command": command, "cwd": "runner"}},
        ),
        AgentMessage(
            type="tool_result",
            content="command completed with exit code 0",
            data={"subtype": "tool_result", "exit_code": 0},
        ),
    )

    assert (
        _runtime_messages_support_file_claim(
            "runner/claimed.py",
            messages,
            task_cwd=str(tmp_path),
        )
        is False
    )


def test_files_touched_rejects_absolute_python_symlink_final_state_spoof(tmp_path) -> None:
    """An arbitrary absolute symlink cannot authenticate an earlier executable."""
    claimed_file = tmp_path / "claimed.py"
    claimed_file.write_text("VALUE = 1\n", encoding="utf-8")
    fake_python = tmp_path / "python3"
    try:
        os.symlink(Path(sys.executable).resolve(), fake_python)
    except (OSError, NotImplementedError):  # pragma: no cover - unprivileged/Windows
        pytest.skip("symlink creation not permitted in this environment")
    command = shlex.join(
        [
            str(fake_python),
            "-I",
            "-S",
            "-c",
            "from pathlib import Path; Path('claimed.py').write_text('x')",
        ]
    )
    messages = (
        AgentMessage(
            type="tool",
            content=f"Bash: {command}",
            tool_name="Bash",
            data={"tool_input": {"command": command}},
        ),
        AgentMessage(
            type="tool_result",
            content="command completed with exit code 0",
            data={"subtype": "tool_result", "exit_code": 0},
        ),
    )

    assert (
        _runtime_messages_support_file_claim(
            "claimed.py",
            messages,
            task_cwd=str(tmp_path),
        )
        is False
    )


def test_files_touched_rejects_exact_sys_executable_symlink_final_state_spoof(
    tmp_path, monkeypatch
) -> None:
    """Even the exact sys.executable path is unsafe when it is a mutable symlink."""
    claimed_file = tmp_path / "claimed.py"
    claimed_file.write_text("VALUE = 1\n", encoding="utf-8")
    fake_python = tmp_path / "python3"
    try:
        os.symlink(Path(sys.executable).resolve(), fake_python)
    except (OSError, NotImplementedError):  # pragma: no cover - unprivileged/Windows
        pytest.skip("symlink creation not permitted in this environment")
    monkeypatch.setattr(sys, "executable", str(fake_python))
    command = shlex.join(
        [
            str(fake_python),
            "-I",
            "-S",
            "-c",
            "from pathlib import Path; Path('claimed.py').write_text('x')",
        ]
    )
    messages = (
        AgentMessage(
            type="tool",
            content=f"Bash: {command}",
            tool_name="Bash",
            data={"tool_input": {"command": command}},
        ),
        AgentMessage(
            type="tool_result",
            content="command completed with exit code 0",
            data={"subtype": "tool_result", "exit_code": 0},
        ),
    )

    assert (
        _runtime_messages_support_file_claim(
            "claimed.py",
            messages,
            task_cwd=str(tmp_path),
        )
        is False
    )


def test_files_touched_rejects_command_cwd_symlink_final_state_spoof(tmp_path) -> None:
    """A retargetable cwd symlink cannot prove where a historical command ran."""
    outside = tmp_path / "outside"
    outside.mkdir()
    subdir = tmp_path / "sub"
    subdir.mkdir()
    claimed_file = subdir / "claimed.py"
    claimed_file.write_text("VALUE = 1\n", encoding="utf-8")
    run_link = tmp_path / "run"
    try:
        os.symlink(outside, run_link)
        run_link.unlink()
        os.symlink(subdir, run_link)
    except (OSError, NotImplementedError):  # pragma: no cover - unprivileged/Windows
        pytest.skip("symlink creation not permitted in this environment")
    command = _trusted_python_c("from pathlib import Path; Path('claimed.py').write_text('x')")
    messages = (
        AgentMessage(
            type="tool",
            content=f"Bash: {command}",
            tool_name="Bash",
            data={"tool_input": {"command": command, "cwd": "run"}},
        ),
        AgentMessage(
            type="tool_result",
            content="command completed with exit code 0",
            data={"subtype": "tool_result", "exit_code": 0},
        ),
    )

    assert (
        _runtime_messages_support_file_claim(
            "sub/claimed.py",
            messages,
            task_cwd=str(tmp_path),
        )
        is False
    )


def test_files_touched_rejects_command_cwd_symlink_replaced_by_directory(tmp_path) -> None:
    """A cwd symlink replaced by a real directory still lacks execution-time identity."""
    outside = tmp_path / "outside"
    outside.mkdir()
    run_path = tmp_path / "run"
    try:
        os.symlink(outside, run_path)
        run_path.unlink()
    except (OSError, NotImplementedError):  # pragma: no cover - unprivileged/Windows
        pytest.skip("symlink creation not permitted in this environment")
    run_path.mkdir()
    claimed_file = run_path / "claimed.py"
    claimed_file.write_text("VALUE = 1\n", encoding="utf-8")
    command = _trusted_python_c("from pathlib import Path; Path('claimed.py').write_text('x')")
    messages = (
        AgentMessage(
            type="tool",
            content=f"Bash: {command}",
            tool_name="Bash",
            data={"tool_input": {"command": command, "cwd": "run"}},
        ),
        AgentMessage(
            type="tool_result",
            content="command completed with exit code 0",
            data={"subtype": "tool_result", "exit_code": 0},
        ),
    )

    assert (
        _runtime_messages_support_file_claim(
            "run/claimed.py",
            messages,
            task_cwd=str(tmp_path),
        )
        is False
    )


def test_files_touched_rejects_receiver_symlink_replaced_by_regular_file(tmp_path) -> None:
    """The exact claimed receiver can be replaced after execution, so text fails closed."""
    outside = tmp_path / "outside.py"
    outside.write_text("VALUE = 1\n", encoding="utf-8")
    claimed_file = tmp_path / "claimed.py"
    try:
        os.symlink(outside, claimed_file)
        claimed_file.unlink()
    except (OSError, NotImplementedError):  # pragma: no cover - unprivileged/Windows
        pytest.skip("symlink creation not permitted in this environment")
    claimed_file.write_text("VALUE = 2\n", encoding="utf-8")
    command = _trusted_python_c("from pathlib import Path; Path('claimed.py').write_text('x')")
    messages = (
        AgentMessage(
            type="tool",
            content=f"Bash: {command}",
            tool_name="Bash",
            data={"tool_input": {"command": command}},
        ),
        AgentMessage(
            type="tool_result",
            content="command completed with exit code 0",
            data={"subtype": "tool_result", "exit_code": 0},
        ),
    )

    assert (
        _runtime_messages_support_file_claim(
            "claimed.py",
            messages,
            task_cwd=str(tmp_path),
        )
        is False
    )


def test_files_touched_rejects_pathlib_receiver_symlink_final_state_spoof(tmp_path) -> None:
    """A symlink retargeted after execution must not authenticate the old receiver."""
    claimed_file = tmp_path / "claimed.py"
    other_file = tmp_path / "other.py"
    alias = tmp_path / "alias.py"
    claimed_file.write_text("VALUE = 1\n", encoding="utf-8")
    other_file.write_text("VALUE = 2\n", encoding="utf-8")
    try:
        os.symlink(other_file, alias)
        alias.unlink()
        os.symlink(claimed_file, alias)
    except (OSError, NotImplementedError):  # pragma: no cover - unprivileged/Windows
        pytest.skip("symlink creation not permitted in this environment")
    command = _trusted_python_c("from pathlib import Path; Path('alias.py').write_text('x')")
    messages = (
        AgentMessage(
            type="tool",
            content=f"Bash: {command}",
            tool_name="Bash",
            data={"tool_input": {"command": command}},
        ),
        AgentMessage(
            type="tool_result",
            content="command completed with exit code 0",
            data={"subtype": "tool_result", "exit_code": 0},
        ),
    )

    assert (
        _runtime_messages_support_file_claim(
            "claimed.py",
            messages,
            task_cwd=str(tmp_path),
        )
        is False
    )


def test_files_touched_rejects_production_shaped_goose_command_text_only(tmp_path) -> None:
    """Goose success correlation does not make command text a file proof."""
    generated_file = tmp_path / "src" / "generated.py"
    generated_file.parent.mkdir()
    generated_file.write_text("VALUE = 1\n", encoding="utf-8")
    command = _trusted_python_c(
        "from pathlib import Path; Path('src/generated.py').write_text('VALUE = 2')"
    )
    messages = (
        AgentMessage(
            type="tool",
            content="tool.started Bash",
            tool_name="Bash",
            data={
                "runtime_event_type": "tool.started",
                "tool_call_id": "goose-call-1",
                "tool_input": {"cmd": command},
            },
        ),
        AgentMessage(
            type="tool_result",
            content="tool.output",
            data={
                "runtime_event_type": "tool.output",
                "tool_call_id": "goose-call-1",
                "is_error": False,
                "output": "",
            },
        ),
    )

    assert (
        _runtime_messages_support_file_claim(
            "src/generated.py",
            messages,
            task_cwd=str(tmp_path),
        )
        is False
    )


def test_files_touched_rejects_symlink_loop_claim_without_exception(tmp_path) -> None:
    """Symlink-loop resolution errors are malformed evidence, not verifier crashes."""
    loop = tmp_path / "a"
    try:
        os.symlink(tmp_path / "b", loop)
        os.symlink(loop, tmp_path / "b")
    except (OSError, NotImplementedError):  # pragma: no cover - unprivileged/Windows
        pytest.skip("symlink creation not permitted in this environment")
    message = AgentMessage(
        type="tool",
        content="Edit: a/generated.py",
        tool_name="Edit",
        data={
            "tool_input": {"file_path": "a/generated.py"},
            "exit_code": 0,
        },
    )

    assert (
        _runtime_messages_support_file_claim(
            "a/generated.py",
            (message,),
            task_cwd=str(tmp_path),
        )
        is False
    )


def test_files_touched_accepts_structured_bash_output_proof(tmp_path) -> None:
    """Structured Bash output remains valid when command text is not a direct write."""
    generated = tmp_path / "src" / "generated.py"
    generated.parent.mkdir()
    generated.write_text("VALUE = 1\n", encoding="utf-8")
    messages = (
        AgentMessage(
            type="tool",
            content="Bash: python scripts/generate.py",
            tool_name="Bash",
            data={
                "tool_input": {"command": "python scripts/generate.py"},
                "output": "generated src/generated.py",
                "exit_code": 0,
            },
        ),
    )

    assert (
        _runtime_messages_support_file_claim(
            "src/generated.py",
            messages,
            task_cwd=str(tmp_path),
        )
        is True
    )


@pytest.mark.parametrize(
    ("observed", "claim"),
    (
        ("pytest Tests/test_app.py", "pytest tests/test_app.py"),
        ('python script.py "a b"', "python script.py a b"),
        ("echo $HOME", "echo '$HOME'"),
        ("echo x > out", "echo x '>' out"),
        ("echo *.py", "echo '*.py'"),
        ("echo x; touch out", "echo 'x;' touch out"),
        ("FOO=bar command", "'FOO=bar' command"),
        ("if true", "'if' true"),
    ),
)
def test_deliver_matching_preserves_case_and_argument_boundaries(
    observed: str,
    claim: str,
) -> None:
    manifest = EvidenceManifest(
        ac_id="AC-1",
        entries=(_journal_entry(handle="ev_distinct", command=observed),),
    )

    assert not _matching_journal_entries(
        manifest,
        field="commands_run",
        value=claim,
    )


def test_deliver_matching_allows_inert_quote_spelling_differences() -> None:
    manifest = EvidenceManifest(
        ac_id="AC-1",
        entries=(_journal_entry(handle="ev_quoted", command='python script.py "a b"'),),
    )

    matches = _matching_journal_entries(
        manifest,
        field="commands_run",
        value="python script.py 'a b'",
    )

    assert tuple(entry.handle for entry in matches) == ("ev_quoted",)


@pytest.mark.parametrize(
    ("observed", "malformed_claim"),
    (
        (
            "pytest tests/test_app.py --basetemp='cache > out'",
            "pytest tests/test_app.py --basetemp='cache",
        ),
        (
            "pytest tests/test_app.py -k 'unit|integration'",
            "pytest tests/test_app.py -k 'unit",
        ),
    ),
)
def test_deliver_matching_does_not_strip_quoted_shell_metacharacters(
    observed: str,
    malformed_claim: str,
) -> None:
    manifest = EvidenceManifest(
        ac_id="AC-1",
        entries=(_journal_entry(handle="ev_quoted_meta", command=observed),),
    )

    assert not _matching_journal_entries(
        manifest,
        field="commands_run",
        value=malformed_claim,
    )


def test_deliver_matching_keeps_equivalent_quoted_metacharacter_argument() -> None:
    observed = "pytest tests/test_app.py --basetemp='cache > out'"
    manifest = EvidenceManifest(
        ac_id="AC-1",
        entries=(_journal_entry(handle="ev_quoted_meta", command=observed),),
    )

    matches = _matching_journal_entries(
        manifest,
        field="commands_run",
        value='pytest tests/test_app.py --basetemp="cache > out"',
    )

    assert tuple(entry.handle for entry in matches) == ("ev_quoted_meta",)


@pytest.mark.parametrize(
    ("tool_input", "claim"),
    (
        ({"cmd": "pytest tests/test_a.py"}, "pytest tests/test_a.py"),
        (
            {"cmd": ["python", "-m", "unittest", "test_slugify.py"]},
            "python -m unittest test_slugify.py",
        ),
        (
            {"cmd": ["/bin/zsh", "-lc", "pytest tests/test_app.py"]},
            "pytest tests/test_app.py",
        ),
        ({"cmd": ["pytest", "--maxfail=1"]}, "pytest --maxfail=1"),
        ({"cmd": ["pytest", "-k", "time"]}, "pytest -k time"),
        ({"cmd": ["python", "script.py", ""]}, "python script.py ''"),
        ({"command_line": "ruff check src"}, "ruff check src"),
    ),
)
def test_deliver_matching_reads_structured_command_shapes_from_args_preview(
    tool_input: dict[str, object],
    claim: str,
) -> None:
    manifest = EvidenceManifest(
        ac_id="AC-1",
        entries=(
            _journal_payload_entry(
                handle="ev_structured",
                payload={
                    "tool_name": "Bash",
                    "args_preview": json.dumps(tool_input, separators=(",", ":")),
                },
            ),
        ),
    )

    matches = _matching_journal_entries(
        manifest,
        field="commands_run",
        value=claim,
    )

    assert tuple(entry.handle for entry in matches) == ("ev_structured",)


@pytest.mark.parametrize(
    "payload",
    (
        {"tool_name": "Bash", "command": "bash /dev/null -c 'pytest tests/test_app.py'"},
        {
            "tool_name": "Bash",
            "args_preview": json.dumps(
                {"cmd": ["bash", "/dev/null", "-c", "pytest tests/test_app.py"]},
                separators=(",", ":"),
            ),
        },
        {"tool_name": "Bash", "command": "bash -n -c 'pytest tests/test_app.py'"},
        {
            "tool_name": "Bash",
            "args_preview": json.dumps(
                {"cmd": ["bash", "-n", "-c", "pytest tests/test_app.py"]},
                separators=(",", ":"),
            ),
        },
        {"tool_name": "Bash", "command": "bash --version -c 'pytest tests/test_app.py'"},
        {
            "tool_name": "Bash",
            "args_preview": json.dumps(
                {"cmd": ["bash", "--version", "-c", "pytest tests/test_app.py"]},
                separators=(",", ":"),
            ),
        },
        {"tool_name": "Bash", "command": "bash -o noexec -c 'pytest tests/test_app.py'"},
        {
            "tool_name": "Bash",
            "args_preview": json.dumps(
                {"cmd": ["bash", "-o", "noexec", "-c", "pytest tests/test_app.py"]},
                separators=(",", ":"),
            ),
        },
        {"tool_name": "Bash", "command": "bash -onoexec -c 'pytest tests/test_app.py'"},
        {
            "tool_name": "Bash",
            "command": "bash -c 'pytest tests/test_app.py' || true",
        },
        {
            "tool_name": "Bash",
            "command": "bash -c 'pytest tests/test_app.py' | cat",
        },
    ),
)
def test_deliver_matching_does_not_extract_shell_body_after_script_operand(
    payload: dict[str, object],
) -> None:
    manifest = EvidenceManifest(
        ac_id="AC-1",
        entries=(_journal_payload_entry(handle="ev_script", payload=payload),),
    )

    assert not _matching_journal_entries(
        manifest,
        field="commands_run",
        value="pytest tests/test_app.py",
    )


def test_deliver_matching_preserves_legacy_json_scalar_preview() -> None:
    manifest = EvidenceManifest(
        ac_id="AC-1",
        entries=(
            _journal_payload_entry(
                handle="ev_true",
                payload={"tool_name": "Bash", "args_preview": "true"},
            ),
        ),
    )

    matches = _matching_journal_entries(
        manifest,
        field="commands_run",
        value="true",
    )

    assert tuple(entry.handle for entry in matches) == ("ev_true",)


def test_deliver_matching_rejects_conflicting_command_aliases() -> None:
    manifest = EvidenceManifest(
        ac_id="AC-1",
        entries=(
            _journal_payload_entry(
                handle="ev_conflicting",
                payload={
                    "tool_name": "Bash",
                    "command": "echo ready",
                    "args_preview": json.dumps(
                        {"cmd": "pytest tests/not-run.py"},
                        separators=(",", ":"),
                    ),
                },
            ),
        ),
    )

    assert not _matching_journal_entries(
        manifest,
        field="commands_run",
        value="echo ready",
    )
    assert not _matching_journal_entries(
        manifest,
        field="tests_passed",
        value="pytest tests/not-run.py",
    )


def test_tests_passed_requires_a_successful_test_command() -> None:
    manifest = EvidenceManifest(
        ac_id="AC-1",
        entries=(
            _journal_entry(handle="ev_echo", command="echo ready"),
            _journal_entry(
                handle="ev_pytest",
                command="pytest tests/test_app.py",
                result_preview="1 passed in 0.01s",
            ),
        ),
    )

    assert not _matching_journal_entries(
        manifest,
        field="tests_passed",
        value="echo ready",
    )
    matches = _matching_journal_entries(
        manifest,
        field="tests_passed",
        value="pytest tests/test_app.py",
    )
    assert tuple(entry.handle for entry in matches) == ("ev_pytest",)

    facts = _standard_deliver_facts(
        EvidenceRecord(data={"commands_run": ["echo ready"], "tests_passed": ["echo ready"]}),
        manifest,
        task_cwd=None,
        verifier_passed=True,
    )
    assert facts is not None
    assert tuple(fact.evidence_handle for fact in facts) == (
        "ev_echo",
        "missing:tests_passed:0",
    )


@pytest.mark.parametrize(
    "command",
    (
        "pytest tests/does_not_exist.py >/dev/null 2>&1 || true",
        "pytest tests/does_not_exist.py||true",
        "pytest tests/does_not_exist.py; true",
    ),
)
def test_tests_passed_rejects_status_masking_shell_chain(command: str) -> None:
    manifest = EvidenceManifest(
        ac_id="AC-1",
        entries=(_journal_entry(handle="ev_masked", command=command),),
    )

    command_matches = _matching_journal_entries(
        manifest,
        field="commands_run",
        value=command,
    )
    test_matches = _matching_journal_entries(
        manifest,
        field="tests_passed",
        value=command,
    )

    assert tuple(entry.handle for entry in command_matches) == ("ev_masked",)
    assert test_matches == ()


@pytest.mark.parametrize(
    "command",
    (
        "pytest --version",
        "pytest --collect-only",
        "pytest --collectonly",
        "pytest --funcargs",
        "pytest --setup-plan",
        "pytest --setuponly",
        "pytest --setupplan",
        "python -m unittest --help",
        "tox --showconfig",
        "nox --list",
        "gradle test --dry-run",
    ),
)
def test_tests_passed_rejects_non_executing_runner_modes(command: str) -> None:
    manifest = EvidenceManifest(
        ac_id="AC-1",
        entries=(_journal_entry(handle="ev_no_tests", command=command),),
    )

    assert tuple(
        entry.handle
        for entry in _matching_journal_entries(
            manifest,
            field="commands_run",
            value=command,
        )
    ) == ("ev_no_tests",)
    assert not _matching_journal_entries(
        manifest,
        field="tests_passed",
        value=command,
    )


def test_npm_if_present_accepts_positive_test_execution_output() -> None:
    command = "npm test --if-present"
    manifest = EvidenceManifest(
        ac_id="AC-1",
        entries=(
            _journal_entry(
                handle="ev_npm",
                command=command,
                result_preview="Tests: 2 passed, 2 total",
            ),
        ),
    )

    assert tuple(
        entry.handle
        for entry in _matching_journal_entries(
            manifest,
            field="tests_passed",
            value=command,
        )
    ) == ("ev_npm",)


def test_tests_passed_requires_positive_execution_output_despite_environment_mode() -> None:
    command = "PYTEST_ADDOPTS=--collect-only pytest tests/test_app.py"
    manifest = EvidenceManifest(
        ac_id="AC-1",
        entries=(
            _journal_entry(
                handle="ev_collected",
                command=command,
                result_preview="collected 1 item\n\n1 test collected",
            ),
        ),
    )

    assert tuple(
        entry.handle
        for entry in _matching_journal_entries(
            manifest,
            field="commands_run",
            value=command,
        )
    ) == ("ev_collected",)
    assert not _matching_journal_entries(
        manifest,
        field="tests_passed",
        value=command,
    )


def test_standard_deliver_facts_distinguishes_ambiguous_matches() -> None:
    """Multiple journal matches remain rejected and are not labelled missing."""
    manifest = EvidenceManifest(
        ac_id="AC-1",
        entries=(
            _journal_entry(handle="ev_1", command="pytest tests/test_app.py"),
            _journal_entry(handle="ev_2", command="pytest tests/test_app.py"),
        ),
    )

    facts = _standard_deliver_facts(
        EvidenceRecord(data={"commands_run": ["pytest tests/test_app.py"]}),
        manifest,
        task_cwd=None,
        verifier_passed=True,
    )

    assert facts is not None
    assert len(facts) == 1
    assert facts[0].evidence_handle == "ambiguous:commands_run:0"


@pytest.mark.parametrize(
    "observed",
    (
        "bash -c 'pytest \"$@\"' ignored tests/test_a.py",
        ("bash", "-c", 'pytest "$@"', "ignored", "tests/test_a.py"),
    ),
)
def test_shell_wrapper_with_post_body_argv_does_not_expose_inner_alias(
    observed: object,
) -> None:
    manifest = EvidenceManifest(
        ac_id="AC-1",
        entries=(
            _journal_payload_entry(
                handle="ev_wrapper",
                payload={"tool_name": "Bash", "cmd": observed},
            ),
        ),
    )

    assert not _matching_journal_entries(
        manifest,
        field="commands_run",
        value='pytest "$@"',
    )


class _RateGateStubAdapter:
    """Minimal adapter exposing only what the dispatch rate gate inspects."""

    def __init__(self, *, runtime_backend: str, self_governs: bool) -> None:
        self.runtime_backend = runtime_backend
        self.self_governs_rate_limit = self_governs
        self.working_directory = "/workspace"
        self.permission_mode = "acceptEdits"


def _make_rate_gate_executor(adapter: _RateGateStubAdapter) -> ParallelACExecutor:
    return ParallelACExecutor(
        adapter=adapter,
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
    )


class TestDispatchRateGate:
    """The executor governs delivery fan-out within the backend's rate budget."""

    def test_gate_dormant_for_self_governing_adapter(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        # Claude self-governs via its own bucket — the executor must not add a
        # second gate, even though "claude" declares an RPM in the registry.
        monkeypatch.setenv("OUROBOROS_BACKEND_LIMITS", str(tmp_path / "absent.yaml"))
        adapter = _RateGateStubAdapter(runtime_backend="claude", self_governs=True)

        executor = _make_rate_gate_executor(adapter)

        assert executor._dispatch_rate_gate.enabled is False

    def test_gate_dormant_for_cli_backend_without_configuration(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        # Default behavior is unchanged: no configured RPM/TPM → no pacing.
        monkeypatch.setenv("OUROBOROS_BACKEND_LIMITS", str(tmp_path / "absent.yaml"))
        monkeypatch.delenv("OUROBOROS_OPENCODE_RPM", raising=False)
        adapter = _RateGateStubAdapter(runtime_backend="opencode", self_governs=False)

        executor = _make_rate_gate_executor(adapter)

        assert executor._dispatch_rate_gate.enabled is False

    def test_gate_activates_for_cli_backend_with_configured_rpm(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        monkeypatch.setenv("OUROBOROS_BACKEND_LIMITS", str(tmp_path / "absent.yaml"))
        monkeypatch.setenv("OUROBOROS_OPENCODE_RPM", "2")
        adapter = _RateGateStubAdapter(runtime_backend="opencode", self_governs=False)

        executor = _make_rate_gate_executor(adapter)

        assert executor._dispatch_rate_gate.enabled is True

    @pytest.mark.asyncio
    async def test_await_dispatch_rate_budget_no_op_when_dormant(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        monkeypatch.setenv("OUROBOROS_BACKEND_LIMITS", str(tmp_path / "absent.yaml"))
        adapter = _RateGateStubAdapter(runtime_backend="opencode", self_governs=False)
        executor = _make_rate_gate_executor(adapter)

        # Dormant gate: returns immediately, no error.
        await executor._await_dispatch_rate_budget(prompt="hello", system_prompt=None)

    @pytest.mark.asyncio
    async def test_await_dispatch_rate_budget_rejects_postconstruction_gate_semantic_drift(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        monkeypatch.setenv("OUROBOROS_BACKEND_LIMITS", str(tmp_path / "absent.yaml"))
        monkeypatch.setenv("OUROBOROS_OPENCODE_RPM", "1")
        adapter = _RateGateStubAdapter(runtime_backend="opencode", self_governs=False)
        executor = _make_rate_gate_executor(adapter)

        # Replacing behavior-changing collaborators after construction is no
        # longer a valid deterministic-test seam: the authority guard must
        # reject it before rate admission. RateLimitGate itself owns separate
        # deterministic timing tests with injected collaborators at creation.
        clock = {"now": 0.0}
        slept: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)
            clock["now"] += seconds

        executor._dispatch_rate_gate._bucket._time = lambda: clock["now"]
        executor._dispatch_rate_gate._sleep = fake_sleep
        executor._dispatch_rate_gate._heartbeat_seconds = 120.0

        with pytest.raises(ValueError, match="execution authority drifted"):
            await executor._await_dispatch_rate_budget(prompt="a", system_prompt=None)

        assert slept == []


def _make_seed(*acceptance_criteria: str | AcceptanceCriterionSpec) -> Seed:
    """Build a minimal seed for parallel executor tests."""
    return Seed(
        goal="Implement staged AC execution",
        constraints=(),
        acceptance_criteria=acceptance_criteria,
        ontology_schema=OntologySchema(
            name="ParallelExecution",
            description="Test schema",
        ),
        metadata=SeedMetadata(ambiguity_score=0.05),
    )


def _make_executor(
    *,
    reasoning_effort: str | None = None,
    run_verify_commands: bool = True,
) -> ParallelACExecutor:
    """Create an executor with mocked dependencies and muted event emitters."""
    executor = ProcessLocalTestExecutor(
        adapter=MagicMock(),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        reasoning_effort=reasoning_effort,
        run_verify_commands=run_verify_commands,
    )
    executor._coordinator.detect_file_conflicts = MagicMock(return_value=[])
    executor._event_store.query_events = AsyncMock(return_value=[])
    executor._emit_workflow_progress = AsyncMock()
    executor._emit_level_started = AsyncMock()
    executor._emit_level_completed = AsyncMock()
    executor._emit_subtask_event = AsyncMock()
    return executor


class _CooldownDriftRuntime:
    """Runtime seam whose declared capabilities can drift during admission."""

    runtime_backend = "opencode"
    working_directory = "/tmp/project"
    permission_mode = "acceptEdits"

    def __init__(self) -> None:
        self.capabilities = FULL_CAPABILITIES
        self.calls = 0

    async def execute_task(self, **_kwargs: Any):
        self.calls += 1
        yield AgentMessage(
            type="result",
            content="[TASK_COMPLETE]",
            data={"subtype": "success"},
        )


def _make_cooldown_drift_executor(
    runtime: _CooldownDriftRuntime,
    controller: AdaptiveConcurrencyController,
    *,
    enable_decomposition: bool = False,
) -> ProcessLocalTestExecutor:
    """Create a one-slot executor with a deterministic admission controller."""

    executor = ProcessLocalTestExecutor(
        adapter=runtime,
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=enable_decomposition,
        max_concurrent=1,
        adaptive_max_concurrent=1,
        expected_runtime_effect_capabilities=runtime_effect_capabilities_contract(runtime),
    )
    # The static policy is identical to the constructor-owned controller; only
    # deterministic clock and sleep seams differ.
    executor._adaptive_concurrency = controller
    executor._event_store.query_events = AsyncMock(return_value=[])
    executor._emit_workflow_progress = AsyncMock()
    executor._emit_level_started = AsyncMock()
    executor._emit_level_completed = AsyncMock()
    executor._emit_subtask_event = AsyncMock()
    return executor


def test_executor_freezes_coordinator_effort_from_its_execution_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The parallel owner, coordinator, and durable policy share one effort value."""

    monkeypatch.setattr("ouroboros.config.get_agent_reasoning_effort", lambda: "high")
    executor = _make_executor(reasoning_effort="low")

    assert executor._reasoning_effort == "low"
    assert executor._coordinator._reasoning_effort == "low"
    policy = executor._execution_authority_policy()
    assert policy["reasoning_effort"] == "low"
    assert policy["coordinator_reasoning_effort"] == "low"


def test_criterion_satisfied_by_exact_runtime_evidence() -> None:
    files = {"hello_auto.py", "tests/test_hello_auto.py"}
    commands = {"uv run pytest tests/test_hello_auto.py"}

    assert _criterion_satisfied_by_evidence("`hello_auto.py` exists.", files, commands)
    assert _criterion_satisfied_by_evidence("`tests/test_hello_auto.py` exists.", files, commands)
    assert not _criterion_satisfied_by_evidence("`src/hello_auto.py` exists.", files, commands)
    assert not _criterion_satisfied_by_evidence("`Hello_Auto.py` exists.", files, commands)
    assert not _criterion_satisfied_by_evidence(
        "`tests/test_hello_auto.py` exists and imports `hello_auto`.",
        files,
        commands,
    )
    assert not _criterion_satisfied_by_evidence(
        "`hello_auto.py` is created with exact content.",
        files,
        commands,
    )
    assert not _criterion_satisfied_by_evidence(
        "`tests/test_hello_auto.py` imports `hello_auto` and asserts the exact return value.",
        files,
        commands,
    )
    assert _criterion_satisfied_by_evidence(
        "The exact command `uv run pytest tests/test_hello_auto.py` passes.",
        files,
        commands,
        commands,
    )
    assert _criterion_satisfied_by_evidence(
        "Run the exact command `uv run pytest tests/test_hello_auto.py`.",
        files,
        commands,
    )
    assert _criterion_satisfied_by_evidence(
        "Run `uv run pytest tests/test_hello_auto.py`.",
        files,
        commands,
    )
    assert _criterion_satisfied_by_evidence(
        "Execute `uv run pytest tests/test_hello_auto.py`.",
        files,
        commands,
    )
    assert _criterion_satisfied_by_evidence(
        "The exact command `uv run pytest tests/test_hello_auto.py` exits with code 0.",
        files,
        commands,
        commands,
    )
    assert not _criterion_satisfied_by_evidence(
        "The exact command `uv run pytest tests/test_hello_auto.py` passes and covers edge cases.",
        files,
        commands,
        commands,
    )
    assert not _criterion_satisfied_by_evidence(
        "The exact command `uv run pytest Tests/test_hello_auto.py` passes.",
        files,
        commands,
        commands,
    )
    assert not _criterion_satisfied_by_evidence(
        "Run the exact command `uv run pytest tests/test_hello_auto.py` and inspect output.",
        files,
        commands,
    )
    assert not _criterion_satisfied_by_evidence(
        "The exact command `uv run pytest tests/test_hello_auto.py` passes.",
        files,
        commands,
        set(),
    )
    assert not _criterion_satisfied_by_evidence("`other.py` exists.", files, commands)


def test_complete_sibling_acs_from_successful_runtime_evidence(tmp_path: Any) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_hello_auto.py").write_text("def test_hello(): pass\n")
    success = ACExecutionResult(
        ac_index=0,
        ac_content="`hello_auto.py` defines `hello_auto() -> str` returning exactly `hello from ooo auto`.",
        success=True,
        messages=(
            AgentMessage(
                type="tool_use",
                content="write hello_auto",
                tool_name="Write",
                data={"tool_input": {"file_path": "hello_auto.py"}},
            ),
            AgentMessage(
                type="tool_use",
                content="write test",
                tool_name="Write",
                data={"tool_input": {"file_path": "tests/test_hello_auto.py"}},
            ),
            AgentMessage(
                type="tool_use",
                content="run pytest",
                tool_name="Bash",
                data={"tool_input": {"command": "uv run pytest tests/test_hello_auto.py"}},
            ),
        ),
        typed_evidence=EvidenceRecord(
            data={
                "files_touched": ["hello_auto.py", "tests/test_hello_auto.py"],
                "commands_run": ["uv run pytest tests/test_hello_auto.py"],
                "tests_passed": ["uv run pytest tests/test_hello_auto.py"],
            }
        ),
        runtime_handle=RuntimeHandle(backend="codex_cli", cwd=str(tmp_path)),
    )
    failed_test_file = ACExecutionResult(
        ac_index=1,
        ac_content="`tests/test_hello_auto.py` exists.",
        success=False,
        error="worker did not update this AC separately",
        outcome=ACExecutionOutcome.FAILED,
    )
    failed_pytest = ACExecutionResult(
        ac_index=2,
        ac_content="The exact command `uv run pytest tests/test_hello_auto.py` passes.",
        success=False,
        error="worker did not update this AC separately",
        outcome=ACExecutionOutcome.FAILED,
    )
    failed_indices = {1, 2}
    ac_statuses = {0: "completed", 1: "failed", 2: "failed"}

    completed_count, level_success, level_failed, results = _complete_sibling_acs_from_evidence(
        level_results=[success, failed_test_file, failed_pytest],
        ac_statuses=ac_statuses,
        failed_indices=failed_indices,
        completed_count=1,
        level_success=1,
        level_failed=2,
    )

    assert completed_count == 3
    assert level_success == 3
    assert level_failed == 0
    assert failed_indices == set()
    assert ac_statuses == {0: "completed", 1: "completed", 2: "completed"}
    assert [result.outcome for result in results] == [
        ACExecutionOutcome.SUCCEEDED,
        ACExecutionOutcome.SATISFIED_EXTERNALLY,
        ACExecutionOutcome.SATISFIED_EXTERNALLY,
    ]


def test_complete_sibling_acs_requires_runtime_test_success_for_pass_claim() -> None:
    success_without_test_output = ACExecutionResult(
        ac_index=0,
        ac_content="`hello_auto.py` defines `hello_auto() -> str`.",
        success=True,
        messages=(
            AgentMessage(
                type="tool_use",
                content="run pytest",
                tool_name="Bash",
                data={
                    "tool_input": {
                        "command": "/bin/zsh -lc 'uv run pytest tests/test_hello_auto.py'"
                    }
                },
            ),
            AgentMessage(
                type="result",
                content="success",
                data={"subtype": "success"},
            ),
        ),
    )
    failed_pytest = ACExecutionResult(
        ac_index=1,
        ac_content="The exact command `uv run pytest tests/test_hello_auto.py` passes.",
        success=False,
        error="worker did not update this AC separately",
        outcome=ACExecutionOutcome.FAILED,
    )

    completed_count, level_success, level_failed, results = _complete_sibling_acs_from_evidence(
        level_results=[success_without_test_output, failed_pytest],
        ac_statuses={0: "completed", 1: "failed"},
        failed_indices={1},
        completed_count=1,
        level_success=1,
        level_failed=1,
    )

    assert completed_count == 1
    assert level_success == 1
    assert level_failed == 1
    assert [result.outcome for result in results] == [
        ACExecutionOutcome.SUCCEEDED,
        ACExecutionOutcome.FAILED,
    ]


def test_complete_sibling_acs_does_not_use_later_tool_success_as_test_proof() -> None:
    success_with_unrelated_tool_success = ACExecutionResult(
        ac_index=0,
        ac_content="Run pytest and edit a file.",
        success=True,
        messages=(
            AgentMessage(
                type="tool_use",
                content="run pytest",
                tool_name="Bash",
                data={"tool_input": {"command": "uv run pytest tests/test_hello_auto.py"}},
            ),
            AgentMessage(
                type="tool_use",
                content="write file",
                tool_name="Write",
                data={"tool_input": {"file_path": "hello_auto.py"}},
            ),
            AgentMessage(
                type="tool_result",
                content="success",
                data={"subtype": "tool_result", "stdout": "success"},
            ),
        ),
    )
    failed_pytest = ACExecutionResult(
        ac_index=1,
        ac_content="The exact command `uv run pytest tests/test_hello_auto.py` passes.",
        success=False,
        error="worker did not update this AC separately",
        outcome=ACExecutionOutcome.FAILED,
    )

    completed_count, level_success, level_failed, results = _complete_sibling_acs_from_evidence(
        level_results=[success_with_unrelated_tool_success, failed_pytest],
        ac_statuses={0: "completed", 1: "failed"},
        failed_indices={1},
        completed_count=1,
        level_success=1,
        level_failed=1,
    )

    assert completed_count == 1
    assert level_success == 1
    assert level_failed == 1
    assert [result.outcome for result in results] == [
        ACExecutionOutcome.SUCCEEDED,
        ACExecutionOutcome.FAILED,
    ]


def test_complete_sibling_acs_keeps_exact_command_case_sensitive() -> None:
    success = ACExecutionResult(
        ac_index=0,
        ac_content="The exact command `uv run pytest tests/test_hello_auto.py` passes.",
        success=True,
        typed_evidence=EvidenceRecord(
            data={"tests_passed": ["uv run pytest tests/test_hello_auto.py"]}
        ),
    )
    failed_wrong_case_pytest = ACExecutionResult(
        ac_index=1,
        ac_content="The exact command `uv run pytest Tests/test_hello_auto.py` passes.",
        success=False,
        error="worker did not update this AC separately",
        outcome=ACExecutionOutcome.FAILED,
    )

    completed_count, level_success, level_failed, results = _complete_sibling_acs_from_evidence(
        level_results=[success, failed_wrong_case_pytest],
        ac_statuses={0: "completed", 1: "failed"},
        failed_indices={1},
        completed_count=1,
        level_success=1,
        level_failed=1,
    )

    assert completed_count == 1
    assert level_success == 1
    assert level_failed == 1
    assert [result.outcome for result in results] == [
        ACExecutionOutcome.SUCCEEDED,
        ACExecutionOutcome.FAILED,
    ]


def test_complete_sibling_acs_normalizes_absolute_typed_file_evidence(tmp_path: Any) -> None:
    test_file = tmp_path / "tests" / "test_hello_auto.py"
    test_file.parent.mkdir()
    test_file.write_text("def test_hello(): pass\n")
    success = ACExecutionResult(
        ac_index=0,
        ac_content="`tests/test_hello_auto.py` exists.",
        success=True,
        typed_evidence=EvidenceRecord(data={"files_touched": [str(test_file)]}),
        runtime_handle=RuntimeHandle(backend="codex_cli", cwd=str(tmp_path)),
    )
    failed_file_presence = ACExecutionResult(
        ac_index=1,
        ac_content="`tests/test_hello_auto.py` exists.",
        success=False,
        error="worker did not update this AC separately",
        outcome=ACExecutionOutcome.FAILED,
    )

    completed_count, level_success, level_failed, results = _complete_sibling_acs_from_evidence(
        level_results=[success, failed_file_presence],
        ac_statuses={0: "completed", 1: "failed"},
        failed_indices={1},
        completed_count=1,
        level_success=1,
        level_failed=1,
    )

    assert completed_count == 2
    assert level_success == 2
    assert level_failed == 0
    assert [result.outcome for result in results] == [
        ACExecutionOutcome.SUCCEEDED,
        ACExecutionOutcome.SATISFIED_EXTERNALLY,
    ]


def test_complete_sibling_acs_requires_file_end_state_existence(tmp_path: Any) -> None:
    success_without_current_file = ACExecutionResult(
        ac_index=0,
        ac_content="`tests/test_hello_auto.py` exists.",
        success=True,
        typed_evidence=EvidenceRecord(data={"files_touched": ["tests/test_hello_auto.py"]}),
        runtime_handle=RuntimeHandle(backend="codex_cli", cwd=str(tmp_path)),
    )
    failed_file_presence = ACExecutionResult(
        ac_index=1,
        ac_content="`tests/test_hello_auto.py` exists.",
        success=False,
        error="worker did not update this AC separately",
        outcome=ACExecutionOutcome.FAILED,
    )

    completed_count, level_success, level_failed, results = _complete_sibling_acs_from_evidence(
        level_results=[success_without_current_file, failed_file_presence],
        ac_statuses={0: "completed", 1: "failed"},
        failed_indices={1},
        completed_count=1,
        level_success=1,
        level_failed=1,
    )

    assert completed_count == 1
    assert level_success == 1
    assert level_failed == 1
    assert [result.outcome for result in results] == [
        ACExecutionOutcome.SUCCEEDED,
        ACExecutionOutcome.FAILED,
    ]


def test_complete_sibling_acs_rejects_invalid_typed_evidence() -> None:
    success_with_invalid_typed_evidence = ACExecutionResult(
        ac_index=0,
        ac_content="`tests/test_hello_auto.py` exists.",
        success=True,
        typed_evidence=EvidenceRecord(data={"files_touched": ["tests/test_hello_auto.py"]}),
        typed_evidence_validation=ValidationResult(ok=False, missing_fields=("tests_passed",)),
    )
    failed_file_presence = ACExecutionResult(
        ac_index=1,
        ac_content="`tests/test_hello_auto.py` exists.",
        success=False,
        error="worker did not update this AC separately",
        outcome=ACExecutionOutcome.FAILED,
    )

    completed_count, level_success, level_failed, results = _complete_sibling_acs_from_evidence(
        level_results=[success_with_invalid_typed_evidence, failed_file_presence],
        ac_statuses={0: "completed", 1: "failed"},
        failed_indices={1},
        completed_count=1,
        level_success=1,
        level_failed=1,
    )

    assert completed_count == 1
    assert level_success == 1
    assert level_failed == 1
    assert [result.outcome for result in results] == [
        ACExecutionOutcome.SUCCEEDED,
        ACExecutionOutcome.FAILED,
    ]


def test_complete_sibling_acs_rejects_verifier_failed_typed_evidence() -> None:
    success_with_rejected_typed_evidence = ACExecutionResult(
        ac_index=0,
        ac_content="The exact command `uv run pytest tests/test_hello_auto.py` passes.",
        success=True,
        typed_evidence=EvidenceRecord(
            data={"tests_passed": ["uv run pytest tests/test_hello_auto.py"]}
        ),
        atomic_verifier_verdict=VerifierVerdict(
            passed=False,
            reasons=("fabricated test output",),
            failure_class="FABRICATION_SUSPECTED",
        ),
    )
    failed_pytest = ACExecutionResult(
        ac_index=1,
        ac_content="The exact command `uv run pytest tests/test_hello_auto.py` passes.",
        success=False,
        error="worker did not update this AC separately",
        outcome=ACExecutionOutcome.FAILED,
    )

    completed_count, level_success, level_failed, results = _complete_sibling_acs_from_evidence(
        level_results=[success_with_rejected_typed_evidence, failed_pytest],
        ac_statuses={0: "completed", 1: "failed"},
        failed_indices={1},
        completed_count=1,
        level_success=1,
        level_failed=1,
    )

    assert completed_count == 1
    assert level_success == 1
    assert level_failed == 1
    assert [result.outcome for result in results] == [
        ACExecutionOutcome.SUCCEEDED,
        ACExecutionOutcome.FAILED,
    ]


def test_complete_sibling_acs_accepts_shell_wrapped_successful_test_command() -> None:
    success_with_test_output = ACExecutionResult(
        ac_index=0,
        ac_content="`hello_auto.py` defines `hello_auto() -> str`.",
        success=True,
        messages=(
            AgentMessage(
                type="tool_use",
                content="run pytest",
                tool_name="Bash",
                data={
                    "tool_input": {
                        "command": "/bin/zsh -lc 'uv run pytest tests/test_hello_auto.py'"
                    }
                },
            ),
            AgentMessage(
                type="tool_result",
                content="1 passed in 0.01s",
                data={"stdout": "1 passed in 0.01s"},
            ),
        ),
    )
    failed_pytest = ACExecutionResult(
        ac_index=1,
        ac_content="The exact command `uv run pytest tests/test_hello_auto.py` passes.",
        success=False,
        error="worker did not update this AC separately",
        outcome=ACExecutionOutcome.FAILED,
    )

    completed_count, level_success, level_failed, results = _complete_sibling_acs_from_evidence(
        level_results=[success_with_test_output, failed_pytest],
        ac_statuses={0: "completed", 1: "failed"},
        failed_indices={1},
        completed_count=1,
        level_success=1,
        level_failed=1,
    )

    assert completed_count == 2
    assert level_success == 2
    assert level_failed == 0
    assert [result.outcome for result in results] == [
        ACExecutionOutcome.SUCCEEDED,
        ACExecutionOutcome.SATISFIED_EXTERNALLY,
    ]


def test_complete_sibling_acs_accepts_named_tool_result_test_output() -> None:
    success_with_named_tool_result = ACExecutionResult(
        ac_index=0,
        ac_content="Run the requested test command.",
        success=True,
        messages=(
            AgentMessage(
                type="tool_use",
                content="run pytest",
                tool_name="Bash",
                data={"tool_input": {"command": "uv run pytest tests/test_hello_auto.py"}},
            ),
            AgentMessage(
                type="tool",
                content="1 passed in 0.01s",
                tool_name="Bash",
                data={"subtype": "tool_result", "stdout": "1 passed in 0.01s"},
            ),
        ),
    )
    failed_pytest = ACExecutionResult(
        ac_index=1,
        ac_content="The exact command `uv run pytest tests/test_hello_auto.py` passes.",
        success=False,
        error="worker did not update this AC separately",
        outcome=ACExecutionOutcome.FAILED,
    )

    completed_count, level_success, level_failed, results = _complete_sibling_acs_from_evidence(
        level_results=[success_with_named_tool_result, failed_pytest],
        ac_statuses={0: "completed", 1: "failed"},
        failed_indices={1},
        completed_count=1,
        level_success=1,
        level_failed=1,
    )

    assert completed_count == 2
    assert level_success == 2
    assert level_failed == 0
    assert [result.outcome for result in results] == [
        ACExecutionOutcome.SUCCEEDED,
        ACExecutionOutcome.SATISFIED_EXTERNALLY,
    ]


def test_complete_sibling_acs_rejects_compound_runtime_command_alias() -> None:
    success_with_compound_runtime_command = ACExecutionResult(
        ac_index=0,
        ac_content="Run the requested test command.",
        success=True,
        messages=(
            AgentMessage(
                type="tool_use",
                content="run pytest and postprocess",
                tool_name="Bash",
                data={
                    "tool_input": {
                        "command": (
                            "/bin/zsh -lc 'uv run pytest tests/test_hello_auto.py "
                            "&& python scripts/postprocess.py'"
                        )
                    }
                },
            ),
            AgentMessage(
                type="tool_result",
                content="1 passed in 0.01s",
                data={"stdout": "1 passed in 0.01s"},
            ),
        ),
    )
    failed_pytest = ACExecutionResult(
        ac_index=1,
        ac_content="The exact command `uv run pytest tests/test_hello_auto.py` passes.",
        success=False,
        error="worker did not update this AC separately",
        outcome=ACExecutionOutcome.FAILED,
    )

    completed_count, level_success, level_failed, results = _complete_sibling_acs_from_evidence(
        level_results=[success_with_compound_runtime_command, failed_pytest],
        ac_statuses={0: "completed", 1: "failed"},
        failed_indices={1},
        completed_count=1,
        level_success=1,
        level_failed=1,
    )

    assert completed_count == 1
    assert level_success == 1
    assert level_failed == 1
    assert [result.outcome for result in results] == [
        ACExecutionOutcome.SUCCEEDED,
        ACExecutionOutcome.FAILED,
    ]


def test_complete_sibling_acs_rejects_compound_typed_command_alias() -> None:
    success_with_compound_typed_command = ACExecutionResult(
        ac_index=0,
        ac_content="Run the requested test command.",
        success=True,
        typed_evidence=EvidenceRecord(
            data={
                "tests_passed": [
                    (
                        "/bin/zsh -lc 'uv run pytest tests/test_hello_auto.py "
                        "&& python scripts/postprocess.py'"
                    )
                ]
            }
        ),
    )
    failed_pytest = ACExecutionResult(
        ac_index=1,
        ac_content="The exact command `uv run pytest tests/test_hello_auto.py` passes.",
        success=False,
        error="worker did not update this AC separately",
        outcome=ACExecutionOutcome.FAILED,
    )

    completed_count, level_success, level_failed, results = _complete_sibling_acs_from_evidence(
        level_results=[success_with_compound_typed_command, failed_pytest],
        ac_statuses={0: "completed", 1: "failed"},
        failed_indices={1},
        completed_count=1,
        level_success=1,
        level_failed=1,
    )

    assert completed_count == 1
    assert level_success == 1
    assert level_failed == 1
    assert [result.outcome for result in results] == [
        ACExecutionOutcome.SUCCEEDED,
        ACExecutionOutcome.FAILED,
    ]


def test_complete_sibling_acs_reuses_structured_command_aliases() -> None:
    success_with_goose_command_shape = ACExecutionResult(
        ac_index=0,
        ac_content="Run the requested test command.",
        success=True,
        messages=(
            AgentMessage(
                type="tool_use",
                content="run pytest",
                tool_name="Bash",
                data={"tool_input": {"cmd": ["uv", "run", "pytest", "tests/test_hello_auto.py"]}},
            ),
        ),
    )
    failed_run_command = ACExecutionResult(
        ac_index=1,
        ac_content="Run the exact command `uv run pytest tests/test_hello_auto.py`.",
        success=False,
        error="worker did not update this AC separately",
        outcome=ACExecutionOutcome.FAILED,
    )

    completed_count, level_success, level_failed, results = _complete_sibling_acs_from_evidence(
        level_results=[success_with_goose_command_shape, failed_run_command],
        ac_statuses={0: "completed", 1: "failed"},
        failed_indices={1},
        completed_count=1,
        level_success=1,
        level_failed=1,
    )

    assert completed_count == 2
    assert level_success == 2
    assert level_failed == 0
    assert [result.outcome for result in results] == [
        ACExecutionOutcome.SUCCEEDED,
        ACExecutionOutcome.SATISFIED_EXTERNALLY,
    ]


def test_complete_sibling_acs_does_not_rewrite_blocked_results() -> None:
    success = ACExecutionResult(
        ac_index=0,
        ac_content="`hello_auto.py` exists.",
        success=True,
        typed_evidence=EvidenceRecord(data={"files_touched": ["tests/test_hello_auto.py"]}),
    )
    blocked = ACExecutionResult(
        ac_index=1,
        ac_content="`tests/test_hello_auto.py` exists.",
        success=False,
        error="Skipped: dependency failed",
        outcome=ACExecutionOutcome.BLOCKED,
    )

    completed_count, level_success, level_failed, results = _complete_sibling_acs_from_evidence(
        level_results=[success, blocked],
        ac_statuses={0: "completed", 1: "blocked"},
        failed_indices=set(),
        completed_count=1,
        level_success=1,
        level_failed=0,
    )

    assert completed_count == 1
    assert level_success == 1
    assert level_failed == 0
    assert [result.outcome for result in results] == [
        ACExecutionOutcome.SUCCEEDED,
        ACExecutionOutcome.BLOCKED,
    ]


def test_complete_sibling_acs_does_not_use_bare_write_call_as_file_proof() -> None:
    success_with_write_call_only = ACExecutionResult(
        ac_index=0,
        ac_content="Attempt a file write.",
        success=True,
        messages=(
            AgentMessage(
                type="tool_use",
                content="write file",
                tool_name="Write",
                data={"tool_input": {"file_path": "hello_auto.py"}},
            ),
        ),
    )
    failed_file_presence = ACExecutionResult(
        ac_index=1,
        ac_content="`hello_auto.py` exists.",
        success=False,
        error="worker did not update this AC separately",
        outcome=ACExecutionOutcome.FAILED,
    )

    completed_count, level_success, level_failed, results = _complete_sibling_acs_from_evidence(
        level_results=[success_with_write_call_only, failed_file_presence],
        ac_statuses={0: "completed", 1: "failed"},
        failed_indices={1},
        completed_count=1,
        level_success=1,
        level_failed=1,
    )

    assert completed_count == 1
    assert level_success == 1
    assert level_failed == 1
    assert [result.outcome for result in results] == [
        ACExecutionOutcome.SUCCEEDED,
        ACExecutionOutcome.FAILED,
    ]


def _make_replaying_event_store() -> tuple[AsyncMock, list[BaseEvent]]:
    """Create an async event-store mock that replays previously appended events."""
    event_store = AsyncMock()
    appended_events: list[BaseEvent] = []

    async def _append(event: BaseEvent) -> None:
        appended_events.append(event)

    async def _replay(aggregate_type: str, aggregate_id: str) -> list[BaseEvent]:
        return [
            event
            for event in appended_events
            if event.aggregate_type == aggregate_type and event.aggregate_id == aggregate_id
        ]

    event_store.append.side_effect = _append
    event_store.replay.side_effect = _replay
    return event_store, appended_events


@pytest.mark.asyncio
async def test_dispatch_append_failure_does_not_cache_phantom_predecessor() -> None:
    """A failed dispatch append must leave the next attempt with no fake predecessor."""
    event_store, appended_events = _make_replaying_event_store()
    fail_dispatch_append = True

    async def _append(event: BaseEvent) -> None:
        nonlocal fail_dispatch_append
        if event.type == "execution.ac.attempt.dispatched" and fail_dispatch_append:
            fail_dispatch_append = False
            raise RuntimeError("dispatch append failed")
        appended_events.append(event)

    event_store.append = AsyncMock(side_effect=_append)
    executor = ParallelACExecutor(
        adapter=_FinalMessageRuntime("done", native_session_id="session-1"),
        event_store=event_store,
        console=MagicMock(),
        enable_decomposition=False,
    )
    kwargs = {
        "ac_index": 0,
        "ac_content": "Implement AC 1",
        "session_id": "orch_123",
        "tools": ["Read"],
        "system_prompt": "system",
        "seed_goal": "Ship the feature",
        "depth": 0,
        "start_time": datetime.now(UTC),
    }

    with pytest.raises(RuntimeError, match="dispatch append failed"):
        await executor._execute_atomic_ac(**kwargs)
    assert executor._ac_runtime_handles == {}

    result = await executor._execute_atomic_ac(**kwargs)

    assert result.success is True
    dispatch_events = [
        event for event in appended_events if event.type == "execution.ac.attempt.dispatched"
    ]
    assert len(dispatch_events) == 1
    assert dispatch_events[0].data["previous_ac_dispatch_id"] is None


@pytest.mark.parametrize(
    ("content", "expected"),
    (
        ("3 passed in 1.2s", True),
        ("0 failed, 3 passed", True),
        ("0 failed, 0 errors, 1 passed", True),
        ("Tests run: 3, Failures: 0, Errors: 0, Skipped: 0", False),
        ("Tests run: 3, Failures: 1, Errors: 0, Skipped: 0", False),
        ("Tests run: 3, Failures: 0, Errors: 0, Skipped: 0\n[INFO] BUILD SUCCESS", True),
        ("Tests run: 3, Failures=0, Errors=0, Skipped=0\n[INFO] BUILD SUCCESS", True),
        ("no errors, 3 passed", True),
        ("no tests failed, 3 passed", True),
        ("exit code 0", False),
        ("Ran 4 tests in 0.000s\nOK", True),
        ("python -m unittest test_slugify.py: Ran 4 tests in 0.000s OK", True),
        ("success", False),
        ("collected 1 item\n1 test collected\nexit code 0", False),
        ("PASS tests/test_app.py", True),
        ("tests/test_app.py::test_auth PASSED", True),
        ("FAILED (failures=1)\nRan 4 tests in 0.000s", False),
        ("1 failed, 3 passed", False),
        ("2 errors, 1 passed", False),
        ("FAILED tests/test_app.py::test_auth", False),
        ("tests failed", False),
    ),
)
def test_message_contains_test_success_handles_zero_failure_summaries(
    content: str,
    expected: bool,
) -> None:
    """Verifier accepts explicit zero-failure summaries without allowing failures."""
    message = AgentMessage(
        type="tool_result",
        content=content,
        data={"subtype": "tool_result"},
    )
    assert _message_contains_test_success(message) is expected


@pytest.mark.parametrize("python_executable", ("python3", "python3.12", "/usr/bin/python3"))
def test_tests_passed_accepts_versioned_python_codex_command(
    python_executable: str,
) -> None:
    """Sol's shell-wrapped versioned Python pytest run is formal test evidence."""
    claim = f"{python_executable} -m pytest --doctest-modules -q hello.py"
    message = AgentMessage(
        type="assistant",
        content=f"Calling tool: Bash: /bin/zsh -lc '{claim}'",
        tool_name="Bash",
        data={
            "tool_input": {"command": f"/bin/zsh -lc '{claim}'"},
            "output": ". [100%]\n1 passed in 0.01s",
            "exit_code": 0,
            "status": "completed",
        },
    )

    assert _runtime_messages_support_test_claim(
        value=claim,
        backed_commands=(claim,),
        messages=(message,),
        task_cwd=None,
    )


@pytest.mark.parametrize(("is_error", "expected"), ((False, True), (True, False)))
def test_tests_passed_respects_correlated_bash_result_status(
    is_error: bool,
    expected: bool,
) -> None:
    """A failed Bash result vetoes success text from the same tool call."""
    command = "pytest tests/test_app.py"
    started = AgentMessage(
        type="tool",
        content="run tests",
        tool_name="Bash",
        data={
            "tool_call_id": "bash_test_1",
            "tool_input": {"command": command},
        },
    )
    completed = AgentMessage(
        type="tool_result",
        content="1 passed in 0.01s",
        data={
            "subtype": "tool_result",
            "tool_call_id": "bash_test_1",
            "is_error": is_error,
            "output": "1 passed in 0.01s",
        },
    )

    assert (
        _runtime_messages_support_test_claim(
            value=command,
            backed_commands=(command,),
            messages=(started, completed),
            task_cwd=None,
        )
        is expected
    )


def test_tests_passed_rejects_collect_only_from_environment_configuration() -> None:
    command = "PYTEST_ADDOPTS=--collect-only pytest tests/test_app.py"
    message = AgentMessage(
        type="tool",
        content="run tests",
        tool_name="Bash",
        data={
            "tool_input": {"command": command},
            "output": "collected 1 item\n\n1 test collected",
            "exit_code": 0,
        },
    )

    assert not _runtime_messages_support_test_claim(
        value=command,
        backed_commands=(command,),
        messages=(message,),
        task_cwd=None,
    )


def test_tests_passed_rejects_tool_call_narration_without_runtime_result() -> None:
    command = "pytest tests/test_app.py"
    message = AgentMessage(
        type="tool",
        content="Running pytest; 1 passed",
        tool_name="Bash",
        data={"tool_input": {"command": command}},
    )

    assert not _runtime_messages_support_test_claim(
        value=command,
        backed_commands=(command,),
        messages=(message,),
        task_cwd=None,
    )


def test_atomic_verifier_rejects_intermediate_result_narration_as_test_proof() -> None:
    command = "pytest tests/test_app.py"
    executor = ParallelACExecutor(
        adapter=MagicMock(),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        execution_profile=load_profile("code"),
        fat_harness_mode=True,
    )

    verdict = executor._verify_atomic_evidence_against_runtime_messages(
        messages=(
            AgentMessage(
                type="tool",
                content="Bash command started",
                tool_name="Bash",
                data={"tool_input": {"command": command}},
            ),
            AgentMessage(type="result", content="1 passed", data={"subtype": "success"}),
            AgentMessage(type="result", content="Evidence follows", data={}),
        ),
        typed_evidence=EvidenceRecord(data={"commands_run": [command], "tests_passed": [command]}),
        ac_content="Run the focused test.",
    )

    assert verdict.passed is False


def test_tests_passed_rejects_node_id_from_assistant_narration_only() -> None:
    """A broad successful run cannot prove a node-id mentioned only by the agent."""
    started = AgentMessage(
        type="tool",
        content="run suite",
        tool_name="Bash",
        data={
            "tool_input": {"command": "pytest"},
            "exit_code": 0,
            "output": "1 passed in 0.01s",
        },
    )
    narration = AgentMessage(
        type="assistant",
        content="tests/test_unobserved.py::test_unobserved passed",
        data={},
    )

    assert not _runtime_messages_support_test_claim(
        value="tests/test_unobserved.py::test_unobserved",
        backed_commands=("pytest",),
        messages=(started, narration),
        task_cwd=None,
    )


def test_codex_completion_receipts_prove_exact_test_and_file_claims(tmp_path) -> None:
    from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime

    runtime = CodexCliRuntime(cli_path="codex", cwd=tmp_path)
    command = "pytest -q tests/test_example.py"
    success_messages = tuple(
        runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "cmd-42",
                    "type": "command_execution",
                    "command": command,
                    "stdout": "1 passed in 0.01s",
                    "exit_code": 0,
                },
            },
            current_handle=None,
        )
        + runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "change-7",
                    "type": "file_change",
                    "path": "src/example.py",
                    "status": "completed",
                },
            },
            current_handle=None,
        )
    )

    assert _runtime_messages_support_test_claim(
        value=command,
        backed_commands=(command,),
        messages=success_messages,
        task_cwd=str(tmp_path),
    )
    assert _runtime_messages_support_file_claim(
        "src/example.py",
        success_messages,
        task_cwd=str(tmp_path),
    )
    failed_messages = tuple(
        runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "cmd-failed",
                    "type": "command_execution",
                    "command": command,
                    "stderr": "1 failed in 0.01s",
                    "exit_code": 1,
                },
            },
            current_handle=None,
        )
    )
    assert not _runtime_messages_support_test_claim(
        value=command,
        backed_commands=(command,),
        messages=failed_messages,
        task_cwd=str(tmp_path),
    )
    assert (
        runtime._convert_event(
            {"type": "item.completed", "item": {"id": "cmd-empty", "type": "command_execution"}},
            current_handle=None,
        )
        == []
    )


def test_gemini_native_shell_result_passes_fat_harness_test_verifier() -> None:
    from ouroboros.orchestrator.gemini_cli_runtime import GeminiCLIRuntime

    command = "pytest -q tests/test_example.py"
    runtime = GeminiCLIRuntime(cli_path="gemini")
    messages: list[AgentMessage] = []
    for raw_event in (
        {"type": "tool_use", "name": "run_shell", "input": {"command": command}},
        {
            "type": "tool_result",
            "name": "run_shell",
            "output": "1 passed in 0.01s",
            "is_error": False,
        },
    ):
        event = runtime._parse_json_event(json.dumps(raw_event))
        assert event is not None
        messages.extend(runtime._convert_event(event, current_handle=None))

    executor = ParallelACExecutor(
        adapter=MagicMock(),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        execution_profile=load_profile("code"),
        fat_harness_mode=True,
    )
    verdict = executor._verify_atomic_evidence_against_runtime_messages(
        messages=tuple(messages),
        typed_evidence=EvidenceRecord(data={"commands_run": [command], "tests_passed": [command]}),
        ac_content="Run the focused test.",
    )

    assert verdict.passed is True
    assert all(message.tool_name == "Bash" for message in messages)
    assert isinstance(messages[1].data["tool_result"], dict)
    malformed_event = runtime._parse_json_event(
        json.dumps(
            {
                "type": "tool_result",
                "name": "run_shell",
                "output": "1 passed in 0.01s",
                "is_error": "true",
            }
        )
    )
    assert malformed_event is not None
    malformed_result = runtime._convert_event(malformed_event, current_handle=None)[0]
    malformed_verdict = executor._verify_atomic_evidence_against_runtime_messages(
        messages=(messages[0], malformed_result),
        typed_evidence=EvidenceRecord(data={"commands_run": [command], "tests_passed": [command]}),
        ac_content="Run the focused test.",
    )
    assert malformed_verdict.passed is False


@pytest.mark.parametrize(
    "ac_content",
    (
        "Run python -m unittest test_todo.py successfully.",
        "Verify the unit tests pass.",
        "Ensure test_todo.py passes.",
        "Validate the test suite.",
        "Without modifying any files, verify the existing todo implementation by running python -m unittest test_todo.py successfully.",
        "Confirm the current implementation by running python -m unittest test_todo.py.",
        "Check the already-satisfied test suite with python -m unittest test_todo.py.",
    ),
)
def test_validation_only_ac_drops_files_touched_requirement(ac_content: str) -> None:
    """Validation-only ACs prove command/test evidence without requiring file mutation."""
    schema = _effective_evidence_schema_for_ac(load_profile("code"), ac_content)
    assert schema.required == ("commands_run", "tests_passed")


@pytest.mark.parametrize(
    "ac_content",
    (
        "Create test_todo.py with unittest coverage.",
        "Add tests for invalid index handling.",
        "Update test_todo.py to cover the done command.",
        "Implement TodoList.add and run tests.",
        "Modify parser.py and ensure tests pass.",
        "Update parser.py and verify the existing test suite passes.",
        "Modify parser.py and confirm the current implementation by running pytest.",
        "Without modifying files, update parser.py and run pytest.",
        "Without modifying any files, ensure tests cover invalid inputs.",
        "Verify the existing test_todo.py coverage without modifying any files. Run python -m unittest test_todo.py successfully.",
        "Document CLI usage in README.md and update tests for invalid inputs.",
        "Update README.md with usage and run python -m unittest test_todo.py.",
        "Document CLI usage in README.md and verify the test suite.",
        "Document CLI usage in README.md; update tests for invalid inputs.",
        "Update README.md with usage: run python -m unittest test_todo.py.",
        "Refactor the validator and verify unit tests pass.",
        "Change the runtime workflow and run pytest.",
        "Ensure tests cover invalid inputs.",
        "Check tests into the repo for the new parser.",
        "Check the existing tests into the repo for the parser.",
        "Check in tests for the new parser.",
        "Update existing test_todo.py coverage for invalid index handling.",
        "Add coverage to test_todo.py and run python -m unittest test_todo.py.",
    ),
)
def test_test_writing_and_implementation_acs_keep_files_touched_required(
    ac_content: str,
) -> None:
    """ACs that mutate code/tests must still prove files_touched."""
    schema = _effective_evidence_schema_for_ac(load_profile("code"), ac_content)
    assert schema.required == ("files_touched", "commands_run", "tests_passed")


def test_build_governed_parent_summary_preserves_embedded_wrapper_headings() -> None:
    """Only orchestrator-owned wrappers are normalized for governed dispatch."""
    level_context = LevelContext(
        level_number=0,
        completed_acs=(
            ACContextSummary(
                ac_index=0,
                ac_content="Prepare helper",
                success=True,
                key_output=(
                    "Helper is ready\n"
                    "## User Heading\n"
                    "## Previous Work Context\n"
                    "## Coordinator Review (Level 12)\n"
                    "Prior result detail"
                ),
            ),
        ),
        coordinator_review=CoordinatorReview(
            level_number=12,
            review_summary=(
                "No conflicts remain\n## Previous Work Context\n## Coordinator Review (Level 12)"
            ),
        ),
    )

    normalized = _build_governed_parent_summary([level_context])

    assert normalized.splitlines() == [
        "Previous Work Context:",
        "The following ACs have already been completed. Use this context to inform your work.",
        "",
        "- AC 1: Prepare helper",
        "  Result: Helper is ready",
        "## User Heading",
        "## Previous Work Context",
        "## Coordinator Review (Level 12)",
        "Prior result detail",
        "",
        "Coordinator Review (Level 12):",
        "**Review**: No conflicts remain",
        "## Previous Work Context",
        "## Coordinator Review (Level 12)",
    ]


class _FinalMessageRuntime:
    """Minimal runtime that returns one successful final message with a handle."""

    _runtime_handle_backend = "opencode"
    _cwd = "/tmp/project"
    _permission_mode = "acceptEdits"

    def __init__(
        self,
        final_message: str,
        *,
        native_session_id: str,
        support_messages: tuple[AgentMessage, ...] = (),
        execute_support_commands: tuple[str, ...] = (),
        cwd: str = "/tmp/project",
        success: bool = True,
    ) -> None:
        self._final_message = final_message
        self._native_session_id = native_session_id
        self._support_messages = support_messages
        self._execute_support_commands = execute_support_commands
        self._cwd = cwd
        self._success = success
        self.call_count = 0
        self.last_prompt: str | None = None
        self.last_system_prompt: str | None = None

    @property
    def runtime_backend(self) -> str:
        return self._runtime_handle_backend

    @property
    def working_directory(self) -> str | None:
        return self._cwd

    @property
    def permission_mode(self) -> str | None:
        return self._permission_mode

    async def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
    ):
        del tools, resume_session_id
        self.call_count += 1
        self.last_prompt = prompt
        self.last_system_prompt = system_prompt
        for message in self._support_messages:
            if (
                message.tool_name in {"Edit", "Write", "NotebookEdit"}
                and "subtype" not in message.data
                and "runtime_event_type" not in message.data
                and "exit_code" not in message.data
            ):
                # These scripted support messages model already-completed
                # OpenCode/Codex file-change events, not bare dispatch starts.
                message = replace(
                    message,
                    data={
                        **message.data,
                        "subtype": "success",
                        "runtime_event_type": "tool.completed",
                    },
                )
            yield message
            tool_input = message.data.get("tool_input")
            command = tool_input.get("command") if isinstance(tool_input, dict) else None
            if command in self._execute_support_commands:
                completed = subprocess.run(  # noqa: S602
                    command,
                    cwd=self._cwd,
                    shell=True,
                    check=False,
                )
                if completed.returncode != 0:
                    raise RuntimeError(f"scripted support command failed: {command}")
        yield AgentMessage(
            type="result",
            content=self._final_message,
            data={"subtype": "success" if self._success else "error"},
            resume_handle=RuntimeHandle(
                backend=resume_handle.backend if resume_handle is not None else "opencode",
                kind=resume_handle.kind if resume_handle is not None else "implementation_session",
                native_session_id=self._native_session_id,
                cwd=resume_handle.cwd if resume_handle is not None else "/tmp/project",
                metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
            ),
        )


def _deep_macos_workspace() -> str:
    workspace = "/Users/developer/Library/Application Support/" + "/".join(
        f"workspace-{index:03d}" for index in range(61)
    )
    assert len(os.fsencode(workspace)) < 1_024
    return workspace


@pytest.mark.asyncio
async def test_atomic_admission_rejects_unmaterializable_contract_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _deep_macos_workspace()
    monkeypatch.setattr("ouroboros.core.seed.os.pathconf", lambda *_args: 1_024)
    runtime = _FinalMessageRuntime("[TASK_COMPLETE]", native_session_id="must-not-dispatch")
    executor = ParallelACExecutor(
        adapter=runtime,
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        task_cwd=workspace,
    )

    result = await executor._execute_atomic_ac(
        ac_index=0,
        ac_content="Materialize the declared artifact",
        session_id="sess_unmaterializable",
        execution_id="exec_unmaterializable",
        tools=["Read"],
        system_prompt="system",
        seed_goal="Ship the artifact",
        depth=0,
        start_time=datetime.now(UTC),
        ac_spec=AcceptanceCriterionSpec(
            description="Materialize the declared artifact",
            expected_artifacts=("nested/" + "a" * 200,),
        ),
    )

    assert result.success is False
    assert result.outcome is ACExecutionOutcome.INVALID
    assert result.error is not None
    assert result.error.startswith("unmaterializable_success_contract:")
    assert "workspace path exceeds POSIX capacity (1024 bytes)" in result.error
    assert runtime.call_count == 0


@pytest.mark.asyncio
async def test_atomic_windows_capacity_rejection_does_not_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ouroboros.core.seed import expected_artifact_workspace_path_error

    monkeypatch.setattr(
        "ouroboros.orchestrator.ac_execution_capsule.expected_artifact_workspace_path_error",
        lambda artifact, workspace: expected_artifact_workspace_path_error(
            artifact,
            workspace,
            platform="windows",
            path_capacity=260,
        ),
    )
    runtime = _FinalMessageRuntime("[TASK_COMPLETE]", native_session_id="must-not-dispatch")
    executor = ParallelACExecutor(
        adapter=runtime,
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        task_cwd=r"C:\Users\developer\source\ouroboros",
    )

    result = await executor._execute_atomic_ac(
        ac_index=0,
        ac_content="Materialize the declared artifact",
        session_id="sess_windows_unmaterializable",
        execution_id="exec_windows_unmaterializable",
        tools=["Read"],
        system_prompt="system",
        seed_goal="Ship the artifact",
        depth=0,
        start_time=datetime.now(UTC),
        ac_spec=AcceptanceCriterionSpec(
            description="Materialize the declared artifact",
            expected_artifacts=("nested/" + "a" * 200,),
        ),
    )

    assert result.outcome is ACExecutionOutcome.INVALID
    assert result.error is not None
    assert "workspace path exceeds Windows capacity (260 UTF-16 units)" in result.error
    assert runtime.call_count == 0


@pytest.mark.asyncio
async def test_unmaterializable_ac_is_judged_while_valid_sibling_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _deep_macos_workspace()
    monkeypatch.setattr("ouroboros.core.seed.os.pathconf", lambda *_args: 1_024)
    seed = _make_seed(
        AcceptanceCriterionSpec(
            description="Materialize the declared artifact",
            expected_artifacts=("nested/" + "a" * 200,),
        ),
        "Complete the independent sibling",
    )
    graph = DependencyGraph(
        nodes=(
            ACNode(index=0, content=seed.acceptance_criteria[0], depends_on=()),
            ACNode(index=1, content=seed.acceptance_criteria[1], depends_on=()),
        ),
        execution_levels=((0, 1),),
    )
    event_store, appended_events = _make_replaying_event_store()
    runtime = _FinalMessageRuntime("[TASK_COMPLETE]", native_session_id="valid-sibling")
    executor = ParallelACExecutor(
        adapter=runtime,
        event_store=event_store,
        console=MagicMock(),
        enable_decomposition=False,
        ac_retry_attempts=3,
        task_cwd=workspace,
    )
    executor._coordinator.detect_file_conflicts = MagicMock(return_value=[])
    executor._maybe_recover_with_bounce_decomposition = AsyncMock()  # type: ignore[method-assign]
    executor._maybe_redispatch_alt_harness = AsyncMock()  # type: ignore[method-assign]
    executor._maybe_redispatch_alt_harness_for_batch_ac = AsyncMock()  # type: ignore[method-assign]
    executor._build_ac_retry_prompt = MagicMock()  # type: ignore[method-assign]

    with patch("ouroboros.orchestrator.parallel_executor.log") as log_mock:
        result = await executor.execute_parallel(
            seed=seed,
            execution_plan=graph.to_execution_plan(),
            session_id="sess_sibling_admission",
            execution_id="exec_sibling_admission",
            tools=["Read"],
            system_prompt="system",
        )

    by_index = {item.ac_index: item for item in result.results}
    assert by_index[0].outcome is ACExecutionOutcome.INVALID
    assert by_index[0].retry_attempt == 0
    assert by_index[1].success is True
    assert result.invalid_count == 1
    assert result.success_count == 1
    assert len(result.stages) == 1
    assert result.stages[0].started is True
    assert runtime.call_count == 1
    judged = [event for event in appended_events if event.type == "execution.ac.attempt_judged"]
    invalid_judgments = [event for event in judged if event.data["ac_index"] == 0]
    assert len(invalid_judgments) == 1
    invalid_judgment = invalid_judgments[0]
    assert invalid_judgment.data["outcome"] == "invalid"
    assert invalid_judgment.data["error_code"] == "unmaterializable_success_contract"
    assert str(invalid_judgment.data["error"]).startswith("unmaterializable_success_contract:")
    admission_rejections = [
        call
        for call in log_mock.warning.call_args_list
        if call.args and call.args[0] == "parallel_executor.ac.admission_rejected"
    ]
    assert len(admission_rejections) == 1
    executor._maybe_recover_with_bounce_decomposition.assert_not_awaited()
    executor._maybe_redispatch_alt_harness.assert_not_awaited()
    executor._maybe_redispatch_alt_harness_for_batch_ac.assert_not_awaited()
    executor._build_ac_retry_prompt.assert_not_called()


@pytest.mark.parametrize(
    ("runtime_command", "claim"),
    (
        pytest.param(
            "/bin/zsh -lc \"rg --files -g 'AGENTS.md' -g '!**/.git/**'\"",
            "rg --files -g 'AGENTS.md' -g '!**/.git/**'",
            id="exact-structured-shell-body",
        ),
        pytest.param(
            "/bin/bash -lc 'cd /workspace && python scripts/generate.py'",
            "python scripts/generate.py",
            id="inner-command-after-safe-shell-preamble",
        ),
        pytest.param(
            "/bin/bash -lc 'set -o pipefail && ./gradlew test "
            '--tests "com.example.app.unit.SomeNewTest" -i 2>&1 | tail -100\'',
            "./gradlew test --tests com.example.app.unit.SomeNewTest -i",
            id="gradle-quoted-target-and-tail-pipe",
        ),
        pytest.param(
            "python -m ruff check src/poc/structure_extractor.py "
            "tests/test_structure_and_draft_substance.py 2>&1 | tail -20",
            "python -m ruff check src/poc/structure_extractor.py "
            "tests/test_structure_and_draft_substance.py",
            id="output-redirection-and-pager-pipe",
        ),
        pytest.param(
            "/bin/bash -lc 'cd /workspace && python -m ruff check "
            "src/foo.py tests/test_foo.py 2>&1 | tail -20'",
            "python -m ruff check src/foo.py tests/test_foo.py",
            id="safe-preamble-with-output-plumbing",
        ),
        pytest.param(
            "/bin/bash -lc 'set -o pipefail && cd /workspace && "
            "pytest tests/test_foo.py 2>&1 | tail -20'",
            "pytest tests/test_foo.py",
            id="test-invocation-pipefail-output-plumbing",
        ),
        pytest.param(
            '"C:\\\\Users\\\\runner\\\\pwsh.exe" -NoProfile -Command '
            "'uv run --isolated --with pytest pytest -q tests/test_foo.py'",
            "uv run --isolated --with pytest pytest -q tests/test_foo.py",
            id="windows-pwsh-command-wrapper",
        ),
    ),
)
def test_command_claim_supports_runtime_command_shape(runtime_command: str, claim: str) -> None:
    """A clean ``commands_run`` claim matches shell-wrapped runs and output-only plumbing.

    Regression for #978 broader observation: read-only command claims may be
    shell-wrapped, and agents routinely run ``<cmd> 2>&1 | tail -20`` while
    citing the clean ``<cmd>`` in evidence. Trailing redirection and
    output-only pager pipes must not block the match (which previously failed
    the whole AC as FABRICATION_SUSPECTED). Test proof strips pager plumbing
    only when ``pipefail`` preserves the pipeline status.
    """
    message = AgentMessage(
        type="tool",
        content="Bash command started",
        tool_name="Bash",
        data={"tool_input": {"command": runtime_command}},
    )

    assert _runtime_messages_support_command_claim(claim, (message,))


@pytest.mark.parametrize(
    ("runtime_command", "claim", "extra_data"),
    (
        pytest.param(
            "/bin/zsh -lc 'pwd && rg --files'",
            "rg --files",
            {},
            id="partial-shell-body",
        ),
        pytest.param(
            "/bin/zsh -lc 'python setup.py && python scripts/generate.py'",
            "python scripts/generate.py",
            {},
            id="inner-command-after-non-setup-preamble",
        ),
        pytest.param(
            "/bin/bash -lc 'cd /workspace && pytest tests/test_foo.py | grep PASSED'",
            "pytest tests/test_foo.py",
            {},
            id="safe-preamble-with-grep-filter",
        ),
        pytest.param(
            "/bin/bash -lc 'cd /workspace && pytest tests/test_foo.py | cat'",
            "pytest tests/test_foo.py",
            {"exit_code": 0},
            id="status-masking-output-pipe",
        ),
        pytest.param(
            "/bin/bash -lc 'cd /workspace && "
            "pytest tests/test_pipefail.py 2>&1 | cat # pipefail mentioned'",
            "pytest tests/test_pipefail.py",
            {"exit_code": 0},
            id="pipefail-text-without-shell-option",
        ),
        pytest.param(
            "/bin/bash -lc 'cd /workspace && "
            "pytest tests/test_foo.py 2>&1 | cat && set -o pipefail'",
            "pytest tests/test_foo.py",
            {"exit_code": 0},
            id="pipefail-set-after-output-pipe",
        ),
        pytest.param(
            "python gen.py | python process.py",
            "python gen.py",
            {},
            id="meaningful-pipeline-segment",
        ),
        pytest.param(
            "pytest tests/unit/test_foo.py | grep passed",
            "pytest tests/unit/test_foo.py",
            {},
            id="grep-filtered-clean-command",
        ),
        pytest.param(
            "pytest tests/unit/test_foo.py -x | grep PASSED",
            "pytest tests/unit/test_foo.py -x",
            {},
            id="grep-filtered-tests-passed-claim",
        ),
        pytest.param(
            "pytest tests/unit/test_foo.py | wc -l",
            "pytest tests/unit/test_foo.py",
            {},
            id="wc-collapsed-run",
        ),
        pytest.param(
            "pytest tests/unit/test_foo.py | tee pytest.log",
            "pytest tests/unit/test_foo.py",
            {},
            id="tee-redirected-run",
        ),
        pytest.param(
            "pwsh.exe -Command 'pytest tests/test_foo.py' trailing",
            "pytest tests/test_foo.py",
            {},
            id="powershell-command-with-trailing-argv",
        ),
        pytest.param(
            "pwsh.exe -EncodedCommand cAB5AHQAZQBzAHQA",
            "pytest tests/test_foo.py",
            {},
            id="powershell-encoded-command",
        ),
        pytest.param(
            "pwsh.exe -File bootstrap.ps1 -Command 'pytest tests/test_foo.py'",
            "pytest tests/test_foo.py",
            {},
            id="powershell-file-before-command",
        ),
    ),
)
def test_command_claim_rejects_evidence_transforming_runtime_command(
    runtime_command: str, claim: str, extra_data: dict[str, object]
) -> None:
    """Only output-filter plumbing is peeled; anything that can rewrite evidence is not proof.

    Generic ``commands_run`` aliases stay exact, so partial shell scripts and
    arbitrary shell-script tails are not proof. ``grep``/``wc``/``tee`` can
    hide failure output, collapse the stream to a count, or divert it to a
    file, so a filtered ``pytest tests/foo.py | grep passed`` run must not
    "prove" the clean ``pytest tests/foo.py`` claim even when the unfiltered
    run had failures. ``_normalized_command_claim_aliases`` is also consumed on
    the ``tests_passed`` path, so the same invariant holds there. The
    ``pipefail`` guard must prove a shell option — not arbitrary command text —
    and must be enabled before the pipeline it protects.
    """
    message = AgentMessage(
        type="tool",
        content="Bash command started",
        tool_name="Bash",
        data={"tool_input": {"command": runtime_command}, **extra_data},
    )

    assert not _runtime_messages_support_command_claim(claim, (message,))


def test_command_claim_supports_goose_cmd_and_list_shapes() -> None:
    """Goose Bash tool_input may use cmd and list argv forms instead of command."""
    cmd_message = AgentMessage(
        type="tool",
        content="Calling tool: Bash: pytest tests/test_a.py",
        tool_name="Bash",
        data={"tool_input": {"cmd": "pytest tests/test_a.py"}},
    )
    list_message = AgentMessage(
        type="tool",
        content="Calling tool: Bash: python -m unittest test_slugify.py",
        tool_name="Bash",
        data={"tool_input": {"cmd": ["python", "-m", "unittest", "test_slugify.py"]}},
    )

    assert _runtime_messages_support_command_claim("pytest tests/test_a.py", (cmd_message,))
    assert _runtime_messages_support_command_claim(
        "python -m unittest test_slugify.py",
        (list_message,),
    )


def test_gradle_tests_passed_claim_supports_class_target_and_case_result() -> None:
    """A Gradle case result can back a class-level tests_passed claim."""
    command_message = AgentMessage(
        type="tool",
        content="Bash command started",
        tool_name="Bash",
        data={
            "tool_input": {
                "command": (
                    "/bin/bash -lc 'set -o pipefail && ./gradlew test "
                    '--tests "com.example.app.unit.SomeNewTest" -i 2>&1 | tail -100\''
                )
            }
        },
    )
    output = "com.example.app.unit.SomeNewTest > createsApp PASSED\nBUILD SUCCESSFUL in 8s"
    result_message = AgentMessage(
        type="tool_result",
        content=output,
        tool_name=None,
        data={
            "subtype": "tool_result",
            "output": output,
        },
    )

    assert _runtime_messages_support_test_claim(
        value="com.example.app.unit.SomeNewTest",
        backed_commands=("./gradlew test --tests com.example.app.unit.SomeNewTest -i",),
        messages=(command_message, result_message),
        task_cwd=None,
    )


def test_unprotected_tail_pipe_is_form_mismatch_not_command_proof() -> None:
    """A bare output-filter pipe is visible but still not trusted as proof."""
    message = AgentMessage(
        type="tool",
        content="Bash command started",
        tool_name="Bash",
        data={
            "tool_input": {
                "command": (
                    './gradlew test --tests "com.example.app.unit.SomeNewTest" -i 2>&1 | tail -100'
                )
            }
        },
    )

    assert not _runtime_messages_support_command_claim(
        "./gradlew test --tests com.example.app.unit.SomeNewTest -i",
        (message,),
    )
    assert _runtime_messages_have_masked_test_command_form(
        "./gradlew test --tests com.example.app.unit.SomeNewTest -i",
        (message,),
    )


def test_atomic_verifier_classifies_masked_test_command_as_form_mismatch() -> None:
    """Masked test commands are contract mismatches, not fabricated work."""
    profile = load_profile("code").model_copy(
        update={"evidence_schema": EvidenceSchema(required=("commands_run",))}
    )
    executor = ParallelACExecutor(
        adapter=MagicMock(working_directory="/workspace"),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        execution_profile=profile,
        fat_harness_mode=True,
    )

    verdict = executor._verify_atomic_evidence_against_runtime_messages(
        messages=(
            AgentMessage(
                type="tool",
                content="Bash command started",
                tool_name="Bash",
                data={
                    "tool_input": {
                        "command": (
                            './gradlew test --tests "com.example.app.unit.SomeNewTest" '
                            "-i 2>&1 | tail -100"
                        )
                    }
                },
            ),
        ),
        typed_evidence=EvidenceRecord(
            data={"commands_run": ["./gradlew test --tests com.example.app.unit.SomeNewTest -i"]}
        ),
        ac_content="Run SomeNewTest.",
    )

    assert verdict.passed is False
    assert verdict.failure_class == "EVIDENCE_FORM_MISMATCH"
    assert "unprotected output-filter pipeline" in " ".join(verdict.reasons)


def test_atomic_verifier_classifies_dependent_masked_test_evidence_as_form_mismatch(
    tmp_path,
) -> None:
    """Full code evidence with masked test output is one evidence-form mismatch."""
    source_file = tmp_path / "src" / "app.py"
    executor = ParallelACExecutor(
        adapter=MagicMock(working_directory=str(tmp_path)),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        execution_profile=load_profile("code"),
        fat_harness_mode=True,
    )

    verdict = executor._verify_atomic_evidence_against_runtime_messages(
        messages=(
            AgentMessage(
                type="tool",
                content="Edit src/app.py",
                tool_name="Edit",
                data={
                    "tool_call_id": "edit_app",
                    "tool_input": {"file_path": str(source_file)},
                },
            ),
            AgentMessage(
                type="tool_result",
                content="updated",
                data={
                    "subtype": "tool_result",
                    "tool_call_id": "edit_app",
                    "is_error": False,
                },
            ),
            AgentMessage(
                type="tool",
                content="Bash command started",
                tool_name="Bash",
                data={
                    "tool_input": {
                        "command": (
                            './gradlew test --tests "com.example.app.unit.SomeNewTest" '
                            "-i 2>&1 | tail -100"
                        )
                    }
                },
            ),
            AgentMessage(
                type="tool_result",
                content=(
                    "com.example.app.unit.SomeNewTest > createsApp PASSED\nBUILD SUCCESSFUL in 8s"
                ),
                tool_name=None,
                data={
                    "subtype": "tool_result",
                    "output": (
                        "com.example.app.unit.SomeNewTest > createsApp PASSED\n"
                        "BUILD SUCCESSFUL in 8s"
                    ),
                },
            ),
        ),
        typed_evidence=EvidenceRecord(
            data={
                "files_touched": ["src/app.py"],
                "commands_run": ["./gradlew test --tests com.example.app.unit.SomeNewTest -i"],
                "tests_passed": ["com.example.app.unit.SomeNewTest"],
            }
        ),
        ac_content="Update app.py and run SomeNewTest.",
    )

    assert verdict.passed is False
    assert verdict.failure_class == "EVIDENCE_FORM_MISMATCH"
    assert (
        "commands_run: ./gradlew test --tests com.example.app.unit.SomeNewTest -i"
        in (verdict.reasons[0])
    )
    assert "tests_passed: com.example.app.unit.SomeNewTest" in verdict.reasons[0]


def _greeting_repro_messages(edit_path: str) -> tuple[AgentMessage, ...]:
    """Build the maintainer's live codex transcript for the greeting seed.

    The Edit event records an absolute disposable-repo path while the Bash test
    command is wrapped in ``/bin/zsh -lc "..."`` with the inner ``python3 -c``
    payload requoted, matching the exact shapes that broke fat-harness matching.
    """
    wrapped_command = (
        '/bin/zsh -lc "python3 -c \\"from hello import greet; '
        "assert greet('World') == 'Hello, World!'; print('OK')\\\"\""
    )
    return (
        AgentMessage(
            type="tool",
            content="Edit hello.py",
            tool_name="Edit",
            data={
                "tool_call_id": "edit_hello",
                "tool_input": {"file_path": edit_path},
            },
        ),
        AgentMessage(
            type="tool_result",
            content="updated hello.py",
            data={
                "subtype": "tool_result",
                "tool_call_id": "edit_hello",
                "is_error": False,
            },
        ),
        AgentMessage(
            type="tool",
            content="run verification",
            tool_name="Bash",
            data={"tool_input": {"command": wrapped_command}},
        ),
        AgentMessage(
            type="tool_result",
            content="OK",
            tool_name=None,
            data={"subtype": "tool_result", "output": "OK", "exit_code": 0},
        ),
        AgentMessage(
            type="tool",
            content="run tests",
            tool_name="Bash",
            data={"tool_input": {"command": _GREETING_TEST_COMMAND}},
        ),
        AgentMessage(
            type="tool_result",
            content="1 passed",
            data={"subtype": "tool_result", "output": "1 passed", "exit_code": 0},
        ),
        AgentMessage(type="result", content="done", data={}),
    )


_GREETING_AC = "hello.py defines greet(name) returning the string Hello, <name>"
_GREETING_INNER_COMMAND = (
    "python3 -c \"from hello import greet; assert greet('World') == 'Hello, World!'; print('OK')\""
)
_GREETING_TEST_COMMAND = "python -m pytest -q"


def test_atomic_verifier_rejects_absolute_transcript_path_when_cwd_is_unknown() -> None:
    """An arbitrary absolute Edit path cannot prove workspace ownership."""
    executor = ParallelACExecutor(
        adapter=MagicMock(working_directory=None),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        execution_profile=load_profile("code"),
        fat_harness_mode=True,
        task_cwd=None,
    )

    verdict = executor._verify_atomic_evidence_against_runtime_messages(
        messages=_greeting_repro_messages("/private/tmp/ooo-repro-blos/hello.py"),
        typed_evidence=EvidenceRecord(
            data={
                "files_touched": ["hello.py"],
                "commands_run": [_GREETING_INNER_COMMAND],
            }
        ),
        ac_content=_GREETING_AC,
        has_success_contract=True,
        verify_gate_active=True,
    )

    assert verdict.passed is False
    assert verdict.failure_class == "FABRICATION_SUSPECTED"
    assert "files_touched: hello.py" in verdict.reasons[0]


def test_atomic_verifier_accepts_symlinked_cwd_against_resolved_transcript_path(
    tmp_path,
) -> None:
    """The resolve tier matches a symlinked run cwd against a resolved transcript path.

    Portable stand-in for the macOS ``/tmp`` -> ``/private/tmp`` layout: create a
    real symlink inside ``tmp_path``, run with ``task_cwd`` set to the symlinked
    directory, and have the transcript record the Edit under the resolved real
    directory. ``Path.resolve`` on both sides must treat them as the same file.
    """
    real_dir = tmp_path / "real_workspace"
    real_dir.mkdir()
    (real_dir / "hello.py").write_text("def greet(name):\n    return f'Hello, {name}!'\n")
    link_dir = tmp_path / "linked_workspace"
    try:
        os.symlink(real_dir, link_dir, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - unprivileged/Windows
        pytest.skip("symlink creation not permitted in this environment")

    assert link_dir.resolve() == real_dir.resolve()
    assert str(link_dir) != str(real_dir)

    executor = ParallelACExecutor(
        adapter=MagicMock(working_directory=str(link_dir)),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        execution_profile=load_profile("code"),
        fat_harness_mode=True,
        # Run cwd is the symlink; the transcript path below is under the real dir.
        task_cwd=str(link_dir),
    )
    verdict = executor._verify_atomic_evidence_against_runtime_messages(
        messages=_greeting_repro_messages(str(real_dir / "hello.py")),
        typed_evidence=EvidenceRecord(
            data={
                "files_touched": ["hello.py"],
                "commands_run": [_GREETING_INNER_COMMAND, _GREETING_TEST_COMMAND],
                "tests_passed": [_GREETING_TEST_COMMAND],
            }
        ),
        ac_content=_GREETING_AC,
        has_success_contract=True,
        verify_gate_active=True,
    )
    assert verdict.passed is True, verdict.reasons


def test_atomic_verifier_rejects_fabricated_greeting_claims_without_transcript() -> None:
    """A near-miss filename and a near-miss command payload still fail (no real event)."""
    executor = ParallelACExecutor(
        adapter=MagicMock(working_directory=None),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        execution_profile=load_profile("code"),
        fat_harness_mode=True,
        task_cwd=None,
    )

    verdict = executor._verify_atomic_evidence_against_runtime_messages(
        messages=_greeting_repro_messages("/private/tmp/ooo-repro-blos/hello.py"),
        typed_evidence=EvidenceRecord(
            data={
                # Different filename than the transcript Edit event.
                "files_touched": ["goodbye.py"],
                # Different command payload than the transcript Bash event.
                "commands_run": ["python3 -c \"from hello import greet; print(greet('x'))\""],
                "tests_passed": [
                    "python3 -c \"from hello import greet; assert greet('World') == 'WRONG'\""
                ],
            }
        ),
        ac_content=_GREETING_AC,
    )

    assert verdict.passed is False
    assert verdict.failure_class == "FABRICATION_SUSPECTED"
    assert "files_touched: goodbye.py" in verdict.reasons[0]


def _file_scope_executor(task_cwd: str | None) -> ParallelACExecutor:
    return ParallelACExecutor(
        adapter=MagicMock(working_directory=task_cwd),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        execution_profile=load_profile("code"),
        fat_harness_mode=True,
        task_cwd=task_cwd,
    )


def test_files_touched_rejects_absolute_outside_workspace_claim_with_touch(tmp_path) -> None:
    """Scope guard: an outside-workspace absolute claim cannot be backed by ``touch``.

    The bot's repro: ``task_cwd`` is a subdir, ``files_touched`` names a sibling
    file outside it, and a ``touch <outside>`` command + exit 0 must NOT satisfy
    the claim. ``files_touched`` is contractually workspace-scoped.
    """
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "hello.py").write_text("x = 1\n")
    outside = tmp_path / "outside.py"
    executor = _file_scope_executor(str(workspace))

    verdict = executor._verify_atomic_evidence_against_runtime_messages(
        messages=(
            AgentMessage(
                type="tool",
                content="touch outside",
                tool_name="Bash",
                data={"tool_input": {"command": f"touch {outside}"}, "exit_code": 0},
            ),
            AgentMessage(
                type="tool",
                content="pytest",
                tool_name="Bash",
                data={"tool_input": {"command": "pytest"}, "exit_code": 0, "output": "1 passed"},
            ),
            AgentMessage(type="result", content="done", data={}),
        ),
        typed_evidence=EvidenceRecord(
            data={
                "files_touched": [str(outside)],
                "commands_run": [f"touch {outside}", "pytest"],
                "tests_passed": ["pytest"],
            }
        ),
        ac_content="Implement the module.",
    )

    assert verdict.passed is False
    assert "files_touched: " in verdict.reasons[0]


def test_files_touched_rejects_parent_traversal_claim_escaping_cwd(tmp_path) -> None:
    """Scope guard: a ``../`` claim escaping the workspace is rejected."""
    workspace = tmp_path / "work"
    workspace.mkdir()
    executor = _file_scope_executor(str(workspace))

    verdict = executor._verify_atomic_evidence_against_runtime_messages(
        messages=(
            AgentMessage(
                type="tool",
                content="touch escape",
                tool_name="Bash",
                data={"tool_input": {"command": "touch ../outside.py"}, "exit_code": 0},
            ),
            AgentMessage(type="result", content="done", data={}),
        ),
        typed_evidence=EvidenceRecord(
            data={
                "files_touched": ["../outside.py"],
                "commands_run": ["touch ../outside.py"],
                "tests_passed": ["pytest"],
            }
        ),
        ac_content="Implement the module.",
    )

    assert verdict.passed is False
    assert "files_touched: ../outside.py" in verdict.reasons[0]


def test_files_touched_accepts_in_workspace_relative_vs_absolute_edit(tmp_path) -> None:
    """In-workspace relative claim matches an absolute Edit path (form mismatch)."""
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "hello.py").write_text("def greet(name):\n    return f'Hello, {name}!'\n")
    edit = AgentMessage(
        type="tool",
        content="edit",
        tool_name="Edit",
        data={
            "tool_call_id": "edit_hello",
            "tool_input": {"file_path": str(workspace / "hello.py")},
        },
    )
    completed = AgentMessage(
        type="tool_result",
        content="updated",
        data={
            "subtype": "tool_result",
            "tool_call_id": "edit_hello",
            "is_error": False,
        },
    )

    assert _runtime_messages_support_file_claim(
        "hello.py",
        (edit, completed),
        task_cwd=str(workspace),
    )


@pytest.mark.parametrize(
    ("command", "claim"),
    (
        # A successful process exit before the write is not mutation evidence.
        pytest.param(
            'python -c "from pathlib import Path; raise SystemExit(0); '
            "Path('generated.py').write_text('x')\"",
            "generated.py",
            id="unexecuted-write",
        ),
        # Invalid literal paths fail closed instead of aborting verification.
        pytest.param(
            "python -c \"from pathlib import Path; Path('src/\\x00generated.py').write_text('x')\"",
            "src/generated.py",
            id="invalid-literal",
        ),
    ),
)
def test_files_touched_rejects_unprovable_python_c_pathlib_write(
    tmp_path, command: str, claim: str
) -> None:
    """Python ``-c`` pathlib text only proves a file when the write provably executed."""
    workspace = tmp_path / "work"
    workspace.mkdir()
    message = AgentMessage(
        type="tool",
        content="python write",
        tool_name="Bash",
        data={
            "tool_input": {"command": command},
            "exit_code": 0,
        },
    )

    assert not _runtime_messages_support_file_claim(
        claim,
        (message,),
        task_cwd=str(workspace),
    )


def test_files_touched_rejects_python_c_pathlib_command_text_only_with_command_cwd(
    tmp_path,
) -> None:
    """Command cwd cannot make Python command text prove file mutation."""
    workspace = tmp_path / "work"
    subdir = workspace / "subdir"
    subdir.mkdir(parents=True)
    message = AgentMessage(
        type="tool",
        content="python write",
        tool_name="Bash",
        data={
            "tool_input": {
                "command": _trusted_python_c(
                    "from pathlib import Path; Path('generated.py').write_text('x')"
                ),
                "cwd": str(subdir),
            },
            "exit_code": 0,
        },
    )

    assert not _runtime_messages_support_file_claim(
        "subdir/generated.py",
        (message,),
        task_cwd=str(workspace),
    )
    assert not _runtime_messages_support_file_claim(
        "generated.py",
        (message,),
        task_cwd=str(workspace),
    )


def test_files_touched_rejects_python_c_pathlib_command_cwd_outside_workspace(tmp_path) -> None:
    """An outside command cwd cannot prove a workspace files_touched claim."""
    workspace = tmp_path / "work"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    message = AgentMessage(
        type="tool",
        content="python write",
        tool_name="Bash",
        data={
            "tool_input": {
                "command": "python -c \"from pathlib import Path; Path('generated.py').write_text('x')\"",
                "cwd": str(outside),
            },
            "exit_code": 0,
        },
    )

    assert not _runtime_messages_support_file_claim(
        "generated.py",
        (message,),
        task_cwd=str(workspace),
    )


@pytest.mark.parametrize(
    "tool_input",
    (
        {
            "cmd": _trusted_python_c(
                "from pathlib import Path; Path('generated.py').write_text('x')"
            )
        },
        {
            "cmd": [
                str(Path(sys.executable).resolve()),
                "-I",
                "-S",
                "-c",
                "from pathlib import Path; Path('generated.py').write_text('x')",
            ]
        },
    ),
)
def test_files_touched_rejects_python_c_pathlib_goose_cmd_text_only(tmp_path, tool_input) -> None:
    """Goose cmd string and argv list shapes still need separate file proof."""
    workspace = tmp_path / "work"
    workspace.mkdir()
    message = AgentMessage(
        type="tool",
        content="python write",
        tool_name="Bash",
        data={"tool_input": tool_input, "exit_code": 0},
    )

    assert not _runtime_messages_support_file_claim(
        "generated.py",
        (message,),
        task_cwd=str(workspace),
    )


def test_files_touched_task_cwd_none_rejects_touch_command_text() -> None:
    """Workspace unknown: only structured Edit/Write proves files, not ``touch`` text."""
    touch = AgentMessage(
        type="tool",
        content="touch outside",
        tool_name="Bash",
        data={"tool_input": {"command": "touch /tmp/evil/outside.py"}, "exit_code": 0},
    )
    # Command-text mutation is not trusted when the workspace is unknown.
    assert not _runtime_messages_support_file_claim("outside.py", (touch,), task_cwd=None)
    # Absolute structured paths are also out-of-scope without a trusted cwd.
    edit = AgentMessage(
        type="tool",
        content="edit",
        tool_name="Edit",
        data={
            "tool_call_id": "edit_hello",
            "tool_input": {"file_path": "/private/tmp/ooo-run/hello.py"},
        },
    )
    completed = AgentMessage(
        type="tool_result",
        content="updated",
        data={
            "subtype": "tool_result",
            "tool_call_id": "edit_hello",
            "is_error": False,
        },
    )
    messages = (edit, completed)
    assert not _runtime_messages_support_file_claim("hello.py", messages, task_cwd=None)
    assert not _runtime_messages_support_file_claim("goodbye.py", messages, task_cwd=None)

    relative_edit = replace(
        edit,
        data={
            "tool_call_id": "edit_relative",
            "tool_input": {"file_path": "hello.py"},
        },
    )
    relative_completed = replace(
        completed,
        data={**completed.data, "tool_call_id": "edit_relative"},
    )
    assert _runtime_messages_support_file_claim(
        "hello.py",
        (relative_edit, relative_completed),
        task_cwd=None,
    )


@pytest.mark.parametrize("is_error", [True, None])
def test_files_touched_rejects_failed_or_missing_edit_completion(
    tmp_path,
    is_error: bool | None,
) -> None:
    workspace = tmp_path / "work"
    workspace.mkdir()
    edit = AgentMessage(
        type="tool",
        content="edit",
        tool_name="Edit",
        data={
            "tool_call_id": "edit_hello",
            "tool_input": {"file_path": str(workspace / "hello.py")},
        },
    )
    messages: tuple[AgentMessage, ...] = (edit,)
    if is_error is not None:
        messages += (
            AgentMessage(
                type="tool_result",
                content="edit failed",
                data={
                    "subtype": "tool_result",
                    "tool_call_id": "edit_hello",
                    "is_error": is_error,
                },
            ),
        )

    assert not _runtime_messages_support_file_claim(
        "hello.py",
        messages,
        task_cwd=str(workspace),
    )


def test_files_touched_rejects_duplicate_start_or_completion_correlation(tmp_path) -> None:
    workspace = tmp_path / "work"
    workspace.mkdir()
    start = AgentMessage(
        type="tool",
        content="edit",
        tool_name="Edit",
        data={
            "tool_call_id": "edit_hello",
            "tool_input": {"file_path": str(workspace / "hello.py")},
        },
    )
    success = AgentMessage(
        type="tool_result",
        content="updated",
        data={
            "subtype": "tool_result",
            "tool_call_id": "edit_hello",
            "is_error": False,
        },
    )
    failure = replace(success, content="failed", data={**success.data, "is_error": True})

    assert not _runtime_messages_support_file_claim(
        "hello.py",
        (start, replace(start), success),
        task_cwd=str(workspace),
    )
    assert not _runtime_messages_support_file_claim(
        "hello.py",
        (start, success, failure),
        task_cwd=str(workspace),
    )
    self_completed = replace(
        start,
        data={
            **start.data,
            "subtype": "success",
            "runtime_event_type": "tool.completed",
        },
    )
    assert not _runtime_messages_support_file_claim(
        "hello.py",
        (self_completed, replace(self_completed)),
        task_cwd=str(workspace),
    )


def test_files_touched_rejects_malformed_is_error_even_with_completed_status(tmp_path) -> None:
    workspace = tmp_path / "work"
    workspace.mkdir()
    start = AgentMessage(
        type="tool",
        content="edit",
        tool_name="Edit",
        data={
            "tool_call_id": "edit_hello",
            "tool_input": {"file_path": str(workspace / "hello.py")},
        },
    )
    malformed = AgentMessage(
        type="tool_result",
        content="unknown",
        data={
            "subtype": "tool_result",
            "tool_call_id": "edit_hello",
            "is_error": "true",
            "runtime_event_type": "tool.completed",
        },
    )

    assert not _runtime_messages_support_file_claim(
        "hello.py",
        (start, malformed),
        task_cwd=str(workspace),
    )


def test_correlated_tool_result_name_requires_one_exact_call_id_match() -> None:
    start = AgentMessage(
        type="tool",
        content="edit",
        tool_name="Edit",
        data={"tool_call_id": "edit_1"},
    )
    result = AgentMessage(
        type="tool_result",
        content="updated",
        data={"subtype": "tool_result", "tool_call_id": "edit_1", "is_error": False},
    )

    assert _correlated_tool_result_name([start, result], result) == "Edit"
    assert (
        _correlated_tool_result_name(
            [replace(start, data={"tool_call_id": "other"}), result],
            result,
        )
        is None
    )
    assert (
        _correlated_tool_result_name(
            [start, replace(start, tool_name="Write"), result],
            result,
        )
        is None
    )
    conflicting_result = replace(
        result,
        data={
            **result.data,
            "meta": {"tool_use_id": "different"},
        },
    )
    assert _correlated_tool_result_name([start, conflicting_result], conflicting_result) is None


def test_files_touched_rejects_conflicting_top_level_and_nested_call_ids(tmp_path) -> None:
    """A conflicting completion cannot fall through as id-less success/effect proof."""
    target = tmp_path / "claimed.py"
    target.write_text("changed\n", encoding="utf-8")
    call = AgentMessage(
        type="tool",
        content="Bash: touch claimed.py",
        tool_name="Bash",
        data={"tool_input": {"command": "touch claimed.py"}, "tool_call_id": "call-a"},
    )
    completion = AgentMessage(
        type="tool_result",
        content="command completed with exit code 0",
        data={
            "subtype": "tool_result",
            "exit_code": 0,
            "tool_call_id": "call-a",
            "tool_result": {"meta": {"tool_use_id": "call-b"}},
            "filesystem_effects": [_filesystem_effect(target, reported_path="claimed.py")],
        },
    )

    assert not _runtime_messages_support_file_claim(
        "claimed.py",
        (call, completion),
        task_cwd=str(tmp_path),
    )


def test_contract_keeps_transcript_evidence_requirements() -> None:
    """An active verify gate adds evidence without weakening the profile."""
    profile = load_profile("code")

    schema = _effective_evidence_schema_for_ac(
        profile,
        "Implement the module.",
        has_success_contract=True,
        verify_gate_active=True,
    )
    artifact_schema = _effective_evidence_schema_for_ac(
        profile,
        "Implement the module.",
        has_success_contract=True,
        has_expected_artifacts=True,
        verify_gate_active=True,
    )

    expected = ("files_touched", "commands_run", "tests_passed")
    assert schema.required == expected
    assert artifact_schema.required == expected


def test_legacy_ac_keeps_transcript_backed_evidence() -> None:
    """Legacy ACs retain every transcript-backed required evidence field."""
    profile = load_profile("code")

    schema = _effective_evidence_schema_for_ac(
        profile,
        "Implement the module.",
        has_success_contract=False,
        has_expected_artifacts=True,
    )

    assert schema.required == ("files_touched", "commands_run", "tests_passed")


def test_contract_ac_retains_transcript_backed_evidence_when_verify_gate_inactive() -> None:
    """A disabled contract gate cannot replace transcript-backed evidence."""
    profile = load_profile("code")

    schema = _effective_evidence_schema_for_ac(
        profile,
        "Implement the module.",
        has_success_contract=True,
        has_expected_artifacts=True,
        verify_gate_active=False,
    )

    assert schema.required == ("files_touched", "commands_run", "tests_passed")


def test_contract_with_artifacts_keeps_all_evidence_when_verify_gate_active() -> None:
    profile = load_profile("code")
    schema = _effective_evidence_schema_for_ac(
        profile,
        "Implement the module.",
        has_success_contract=True,
        has_expected_artifacts=True,
        verify_gate_active=True,
    )
    assert schema.required == ("files_touched", "commands_run", "tests_passed")


def test_contract_ac_verifier_still_checks_transcript_evidence() -> None:
    executor = _file_scope_executor("/private/tmp/ooo-repro-blos")
    verdict = executor._verify_atomic_evidence_against_runtime_messages(
        messages=_greeting_repro_messages("/private/tmp/ooo-repro-blos/hello.py"),
        typed_evidence=EvidenceRecord(
            data={
                "files_touched": ["hello.py"],
                "commands_run": [_GREETING_INNER_COMMAND, _GREETING_TEST_COMMAND],
                "tests_passed": [_GREETING_TEST_COMMAND],
            }
        ),
        ac_content=_GREETING_AC,
        has_success_contract=True,
        verify_gate_active=True,
    )
    assert verdict.passed is True, verdict.reasons


def test_contract_artifacts_do_not_excuse_unbacked_transcript_claims() -> None:
    executor = _file_scope_executor("/private/tmp/ooo-repro-blos")
    verdict = executor._verify_atomic_evidence_against_runtime_messages(
        messages=_greeting_repro_messages("/private/tmp/ooo-repro-blos/hello.py"),
        typed_evidence=EvidenceRecord(
            data={
                "files_touched": ["not-backed-by-transcript.py"],
                "commands_run": [_GREETING_INNER_COMMAND, _GREETING_TEST_COMMAND],
                "tests_passed": [_GREETING_TEST_COMMAND],
            }
        ),
        ac_content=_GREETING_AC,
        has_success_contract=True,
        has_expected_artifacts=True,
        verify_gate_active=True,
    )
    assert verdict.passed is False
    assert "files_touched" in verdict.reasons[0]


def test_legacy_ac_verifier_keeps_strict_formal_runner_tests_passed() -> None:
    """Legacy AC (no verify_command): tests_passed keeps strict formal-runner semantics."""
    executor = _file_scope_executor("/private/tmp/ooo-repro-blos")

    # Inline python3 -c is not a formal test runner -> tests_passed unsupported.
    rejected = executor._verify_atomic_evidence_against_runtime_messages(
        messages=_greeting_repro_messages("/private/tmp/ooo-repro-blos/hello.py"),
        typed_evidence=EvidenceRecord(
            data={
                "files_touched": ["hello.py"],
                "commands_run": [_GREETING_INNER_COMMAND],
                "tests_passed": [_GREETING_INNER_COMMAND],
            }
        ),
        ac_content=_GREETING_AC,
        has_success_contract=False,
    )
    assert rejected.passed is False
    assert "tests_passed:" in rejected.reasons[0]

    # A real pytest run with success output backs tests_passed for a legacy AC.
    messages = (
        AgentMessage(
            type="tool",
            content="edit",
            tool_name="Edit",
            data={
                "subtype": "success",
                "runtime_event_type": "tool.completed",
                "tool_input": {"file_path": "/private/tmp/ooo-repro-blos/src/mod.py"},
            },
        ),
        AgentMessage(
            type="tool_result",
            content="updated",
            tool_name="Edit",
            data={"subtype": "tool_result", "is_error": False},
        ),
        AgentMessage(
            type="tool",
            content="pytest",
            tool_name="Bash",
            data={
                "tool_input": {"command": "pytest tests/test_mod.py"},
                "exit_code": 0,
                "output": "1 passed in 0.01s",
            },
        ),
        AgentMessage(type="result", content="done", data={}),
    )
    accepted = executor._verify_atomic_evidence_against_runtime_messages(
        messages=messages,
        typed_evidence=EvidenceRecord(
            data={
                "files_touched": ["src/mod.py"],
                "commands_run": ["pytest tests/test_mod.py"],
                "tests_passed": ["pytest tests/test_mod.py"],
            }
        ),
        ac_content="Implement src/mod.py and run pytest.",
        has_success_contract=False,
    )
    assert accepted.passed is True, accepted.reasons


@pytest.mark.asyncio
async def test_verify_gate_flips_contract_ac_to_failed_when_declared_command_fails(
    tmp_path,
) -> None:
    """Delegation is ENFORCED: a broken contract AC fails via the orchestrator gate.

    A successful AC result whose declared verify_command exits non-zero is flipped
    to FAILED by ``_apply_verify_gate`` — the orchestrator runs the real command,
    so a fabricated tests_passed claim cannot pass a broken implementation.
    """
    executor = _file_scope_executor(str(tmp_path))
    passing_result = ACExecutionResult(
        ac_index=0,
        ac_content="greet works",
        success=True,
        outcome=ACExecutionOutcome.SUCCEEDED,
    )

    failing_seed = Seed(
        goal="greeting",
        constraints=(),
        acceptance_criteria=(
            AcceptanceCriterionSpec(
                description="greet works",
                verify_command='python3 -c "import sys; sys.exit(1)"',
            ),
        ),
        ontology_schema=OntologySchema(name="Greeting", description="d"),
        metadata=SeedMetadata(ambiguity_score=0.1),
    )
    gated = await executor._apply_verify_gate(
        seed=failing_seed,
        ac_index=0,
        result=passing_result,
        session_id="s",
        execution_id="e",
    )
    assert gated.success is False
    assert gated.outcome == ACExecutionOutcome.FAILED
    assert "Verify gate failed" in (gated.error or "")

    passing_seed = Seed(
        goal="greeting",
        constraints=(),
        acceptance_criteria=(
            AcceptanceCriterionSpec(
                description="greet works",
                verify_command="python3 -c \"print('OK')\"",
                output_assertion="OK",
            ),
        ),
        ontology_schema=OntologySchema(name="Greeting", description="d"),
        metadata=SeedMetadata(ambiguity_score=0.1),
    )
    kept = await executor._apply_verify_gate(
        seed=passing_seed,
        ac_index=0,
        result=passing_result,
        session_id="s",
        execution_id="e",
    )
    assert kept.success is True


@pytest.mark.parametrize(
    ("runtime_command", "claimed_command"),
    (
        ("./gradlew test -x test", "./gradlew test -x test"),
        ("./gradlew check -x test", "./gradlew check -x test"),
        ("./gradlew test --exclude-task test", "./gradlew test --exclude-task test"),
        ("./gradlew check --exclude-task :test", "./gradlew check --exclude-task :test"),
        ("mvn -DskipTests verify", "mvn -DskipTests verify"),
        ("mvn -D skipTests test", "mvn -D skipTests test"),
        ("mvn -Dmaven.test.skip=true test", "mvn -Dmaven.test.skip=true test"),
        ("mvn --define skipTests test", "mvn --define skipTests test"),
        ("mvn --define=skipTests=true test", "mvn --define=skipTests=true test"),
    ),
)
def test_gradle_maven_tests_passed_rejects_skip_test_invocations(
    runtime_command: str,
    claimed_command: str,
) -> None:
    """Build success cannot prove tests_passed when the command skipped tests."""
    command_message = AgentMessage(
        type="tool",
        content="Bash command started",
        tool_name="Bash",
        data={"tool_input": {"command": runtime_command}},
    )
    result_message = AgentMessage(
        type="tool_result",
        content="BUILD SUCCESSFUL in 8s",
        tool_name=None,
        data={"subtype": "tool_result", "output": "BUILD SUCCESSFUL in 8s"},
    )

    assert not _runtime_messages_support_test_claim(
        value=claimed_command,
        backed_commands=(claimed_command,),
        messages=(command_message, result_message),
        task_cwd=None,
    )


@pytest.mark.parametrize(
    "command",
    (
        "mvn -DskipTests=false test",
        "mvn -Dmaven.test.skip=false test",
    ),
)
def test_maven_tests_passed_supports_explicit_false_skip_properties(command: str) -> None:
    """Explicit false Maven skip properties still run tests and can prove success."""
    command_message = AgentMessage(
        type="tool",
        content="Bash command started",
        tool_name="Bash",
        data={"tool_input": {"command": command}},
    )
    result_message = AgentMessage(
        type="tool_result",
        content="[INFO] Tests run: 1, Failures: 0, Errors: 0\n[INFO] BUILD SUCCESS",
        tool_name=None,
        data={
            "subtype": "tool_result",
            "output": "[INFO] Tests run: 1, Failures: 0, Errors: 0\n[INFO] BUILD SUCCESS",
        },
    )

    assert _runtime_messages_support_test_claim(
        value=command,
        backed_commands=(command,),
        messages=(command_message, result_message),
        task_cwd=None,
    )


@pytest.mark.parametrize(
    "output",
    (
        "> Task :test\nBUILD SUCCESSFUL in 1s",
        "> Task :test NO-SOURCE\nBUILD SUCCESSFUL in 1s",
        "> Task :test SKIPPED\nBUILD SUCCESSFUL in 1s",
        "0 tests completed\nBUILD SUCCESSFUL",
    ),
)
def test_gradle_tests_passed_rejects_successful_build_with_no_tests(output: str) -> None:
    """Gradle build success without executed tests cannot prove tests_passed."""
    command_message = AgentMessage(
        type="tool",
        content="Bash command started",
        tool_name="Bash",
        data={"tool_input": {"command": "./gradlew test"}},
    )
    result_message = AgentMessage(
        type="tool_result",
        content=output,
        tool_name=None,
        data={"subtype": "tool_result", "output": output},
    )

    assert not _runtime_messages_support_test_claim(
        value="./gradlew test",
        backed_commands=("./gradlew test",),
        messages=(command_message, result_message),
        task_cwd=None,
    )


def test_gradle_tests_passed_accepts_individual_test_case_result() -> None:
    output = "com.example.AppTest > createsApp PASSED\nBUILD SUCCESSFUL in 1s"
    command_message = AgentMessage(
        type="tool",
        content="Bash command started",
        tool_name="Bash",
        data={"tool_input": {"command": "./gradlew test"}},
    )
    result_message = AgentMessage(
        type="tool_result",
        content=output,
        data={"subtype": "tool_result", "output": output},
    )

    assert _runtime_messages_support_test_claim(
        value="./gradlew test",
        backed_commands=("./gradlew test",),
        messages=(command_message, result_message),
        task_cwd=None,
    )


def test_maven_tests_passed_supports_surefire_zero_failure_summary() -> None:
    """Standard Surefire zero-failure fields plus build success prove Maven tests."""
    output = "[INFO] Tests run: 3, Failures: 0, Errors: 0, Skipped: 0\n[INFO] BUILD SUCCESS"
    command_message = AgentMessage(
        type="tool",
        content="Bash command started",
        tool_name="Bash",
        data={"tool_input": {"command": "mvn test"}},
    )
    result_message = AgentMessage(
        type="tool_result",
        content=output,
        tool_name=None,
        data={"subtype": "tool_result", "output": output},
    )

    assert _runtime_messages_support_test_claim(
        value="mvn test",
        backed_commands=("mvn test",),
        messages=(command_message, result_message),
        task_cwd=None,
    )


class TestProfileAwareDecompositionAudit:
    @pytest.mark.asyncio
    async def test_level_started_event_records_active_decomposition_profile(self) -> None:
        event_store = AsyncMock()
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            execution_profile=load_profile("code"),
        )

        await executor._emit_level_started(
            session_id="sess_profile",
            level=1,
            ac_indices=[0, 1],
            total_levels=2,
        )

        event = event_store.append.await_args.args[0]
        assert event.type == "execution.decomposition.level_started"
        assert event.data["decomposition_profile"] == {
            "profile": "code",
            "axis": "testable_unit",
            "min_unit": (
                "a cohesive change verified by one test-command run — typically a "
                "function or module plus its tests; never split below a single function"
            ),
            "cut_signal": "sub-AC produces an independently runnable test",
            "max_branching": 5,
        }

    @pytest.mark.asyncio
    async def test_level_started_event_records_legacy_decomposition_fallback(self) -> None:
        event_store = AsyncMock()
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
        )

        await executor._emit_level_started(
            session_id="sess_legacy",
            level=1,
            ac_indices=[0],
            total_levels=1,
        )

        event = event_store.append.await_args.args[0]
        assert event.data["decomposition_profile"] is None


class TestProfileAwareContextGovernance:
    @pytest.mark.asyncio
    async def test_profile_backed_atomic_dispatch_uses_context_governor(self) -> None:
        class _StubRuntime:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                self.calls.append(
                    {
                        "prompt": prompt,
                        "tools": tools,
                        "system_prompt": system_prompt,
                        "resume_handle": resume_handle,
                        "resume_session_id": resume_session_id,
                    }
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=resume_handle,
                )

        event_store, appended_events = _make_replaying_event_store()
        runtime = _StubRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
        )
        level_context = LevelContext(
            level_number=0,
            completed_acs=(
                ACContextSummary(
                    ac_index=0,
                    ac_content="Prepare helper",
                    success=True,
                    key_output=(
                        "Helper is ready\n"
                        "## User Heading\n"
                        "## Previous Work Context\n"
                        "## Coordinator Review (Level 1)\n"
                        "Prior result detail"
                    ),
                ),
            ),
            coordinator_review=CoordinatorReview(
                level_number=1,
                review_summary="No conflicts remain\n## Previous Work Context",
                warnings_for_next_level=("Keep edits localized",),
            ),
        )

        result = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Implement duplicate leaf",
            session_id="sess_context",
            tools=["Read"],
            system_prompt="system",
            seed_goal="Ship context governance",
            depth=0,
            start_time=datetime.now(UTC),
            execution_id="exec_context",
            level_contexts=[level_context],
            sibling_acs=[(1, "Implement duplicate leaf"), (2, "Implement duplicate leaf")],
        )

        assert result.success is True
        prompt = runtime.calls[0]["prompt"]
        assert "## Governed Dispatch Context (AC 2)" in prompt
        assert "## Parent context" in prompt
        assert "## Previous Work Context\nThe following ACs" not in prompt
        assert "## Coordinator Review (Level 1)\n**Review**" not in prompt
        assert "Previous Work Context:" in prompt
        assert "Coordinator Review (Level 1):" in prompt
        assert "Helper is ready" in prompt
        assert "## User Heading" in prompt
        assert "User Heading:" not in prompt
        assert "## Previous Work Context" in prompt
        assert "## Coordinator Review (Level 1)" in prompt
        assert "Prior result detail" in prompt
        assert "No conflicts remain" in prompt
        assert "Keep edits localized" in prompt
        assert "## Sibling status" in prompt
        assert "… sibling-1: Implement duplicate leaf" in prompt
        assert "## AC\nImplement duplicate leaf" in prompt
        assert "## Parallel Execution Notice" in prompt
        assert "Avoid modifying files that other agents are likely editing." in prompt
        assert "summarized in the governed sibling-status section above" in prompt

        context_events = [
            event for event in appended_events if event.type == "execution.ac.context_governed"
        ]
        assert len(context_events) == 1
        assert context_events[0].data["context_governed"] is True
        assert context_events[0].data["context_acceptance_enforced"] is False
        assert context_events[0].data["context_default_flipped"] is False
        assert context_events[0].data["profile"] == "code"
        assert context_events[0].data["context_sibling_status_count"] == 1

    @pytest.mark.asyncio
    async def test_legacy_atomic_dispatch_keeps_existing_context_prompt_shape(self) -> None:
        class _StubRuntime:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                self.calls.append(
                    {
                        "prompt": prompt,
                        "tools": tools,
                        "system_prompt": system_prompt,
                        "resume_handle": resume_handle,
                        "resume_session_id": resume_session_id,
                    }
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=resume_handle,
                )

        event_store, appended_events = _make_replaying_event_store()
        runtime = _StubRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        level_context = LevelContext(
            level_number=0,
            completed_acs=(
                ACContextSummary(
                    ac_index=0,
                    ac_content="Prepare helper",
                    success=True,
                    key_output="Helper is ready",
                ),
            ),
        )

        result = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Implement legacy leaf",
            session_id="sess_legacy_context",
            tools=["Read"],
            system_prompt="system",
            seed_goal="Ship legacy context",
            depth=0,
            start_time=datetime.now(UTC),
            execution_id="exec_legacy_context",
            level_contexts=[level_context],
            sibling_acs=[(1, "Implement legacy leaf"), (2, "Update sibling docs")],
        )

        assert result.success is True
        prompt = runtime.calls[0]["prompt"]
        assert "## Your Task (AC 2)\nImplement legacy leaf" in prompt
        assert "## Previous Work Context" in prompt
        assert "## Parallel Execution Notice" in prompt
        assert "## Governed Dispatch Context" not in prompt
        assert not any(event.type == "execution.ac.context_governed" for event in appended_events)

    @pytest.mark.asyncio
    async def test_profile_context_governor_budget_error_falls_back_without_failing_ac(
        self,
    ) -> None:
        class _StubRuntime:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                self.calls.append(
                    {
                        "prompt": prompt,
                        "tools": tools,
                        "system_prompt": system_prompt,
                        "resume_handle": resume_handle,
                        "resume_session_id": resume_session_id,
                    }
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=resume_handle,
                )

        event_store, appended_events = _make_replaying_event_store()
        runtime = _StubRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
        )
        oversized_ac = "x" * 13_000

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content=oversized_ac,
            session_id="sess_context_fallback",
            tools=["Read"],
            system_prompt="system",
            seed_goal="Ship context governance fallback",
            depth=0,
            start_time=datetime.now(UTC),
            execution_id="exec_context_fallback",
        )

        assert result.success is True
        prompt = runtime.calls[0]["prompt"]
        assert "## Your Task (AC 1)" in prompt
        assert "## Governed Dispatch Context" not in prompt
        context_events = [
            event for event in appended_events if event.type == "execution.ac.context_governed"
        ]
        assert len(context_events) == 1
        assert context_events[0].data["context_governed"] is False
        assert context_events[0].data["context_fallback"] == "legacy_prompt"
        assert (
            "AC alone exceeds context budget" in context_events[0].data["context_governance_error"]
        )


def _prepare_generated_workspace(tmp_path: Path) -> None:
    """Create the ``src/generated.py`` receiver shared by pathlib claim cases."""
    generated_file = tmp_path / "src" / "generated.py"
    generated_file.parent.mkdir()
    generated_file.write_text("VALUE = 1\n", encoding="utf-8")


def _generated_claim_case(
    command_factory: Callable[[Path], str],
) -> Callable[[Path], tuple[str, str]]:
    """Build a pathlib claim case that claims ``src/generated.py``."""

    def _prepare(tmp_path: Path) -> tuple[str, str]:
        _prepare_generated_workspace(tmp_path)
        return command_factory(tmp_path), "src/generated.py"

    return _prepare


def _claimed_file_case(
    command_factory: Callable[[Path], str],
) -> Callable[[Path], tuple[str, str]]:
    """Build a pathlib claim case whose workspace holds a single ``claimed.py``."""

    def _prepare(tmp_path: Path) -> tuple[str, str]:
        (tmp_path / "claimed.py").write_text("VALUE = 1\n", encoding="utf-8")
        return command_factory(tmp_path), "claimed.py"

    return _prepare


def _pathlib_case_literal_space_identity(tmp_path: Path) -> tuple[str, str]:
    """Literal spaces are part of the pathlib receiver's filesystem identity."""
    generated_file = tmp_path / "src" / "generated.py"
    spaced_file = tmp_path / " src" / "generated.py "
    generated_file.parent.mkdir()
    spaced_file.parent.mkdir()
    generated_file.write_text("VALUE = 1\n", encoding="utf-8")
    spaced_file.write_text("VALUE = 2\n", encoding="utf-8")
    command = (
        "python -c \"from pathlib import Path; Path(' src/generated.py ').write_text('VALUE = 3')\""
    )
    return command, "src/generated.py"


def _pathlib_case_write_to_different_file(tmp_path: Path) -> tuple[str, str]:
    """A pathlib write must bind to the receiver, not any mentioned file."""
    claimed_file = tmp_path / "src" / "preexisting.py"
    generated_file = tmp_path / "src" / "generated.py"
    claimed_file.parent.mkdir()
    claimed_file.write_text("VALUE = 1\n", encoding="utf-8")
    generated_file.write_text("VALUE = 2\n", encoding="utf-8")
    command = (
        'python -c "from pathlib import Path; '
        "Path('src/preexisting.py').read_text(); "
        "Path('src/generated.py').write_text('VALUE = 3')\""
    )
    return command, "src/preexisting.py"


def _pathlib_case_nonmatching_without_shell_fallback(tmp_path: Path) -> tuple[str, str]:
    """A Python comment cannot make a nonmatching pathlib write prove the claim."""
    claimed_file = tmp_path / "claimed.py"
    other_file = tmp_path / "other.py"
    claimed_file.write_text("VALUE = 1\n", encoding="utf-8")
    other_file.write_text("VALUE = 2\n", encoding="utf-8")
    command = (
        'python -c "from pathlib import Path; # touch claimed.py\n'
        "Path('other.py') . write_text('VALUE = 3')\""
    )
    return command, "claimed.py"


def _pathlib_case_shell_expanded_source(tmp_path: Path) -> tuple[str, str]:
    """Raw shell text with expansion is not proof of the executed Python source."""
    generated_file = tmp_path / "generated.py"
    generated_file.write_text("VALUE = 1\n", encoding="utf-8")
    command = (
        'PAYLOAD=x python -c "from pathlib import Path; '
        "Path('generated.py').write_text('$PAYLOAD')\""
    )
    return command, "generated.py"


def _pathlib_case_nonisolated_local_shadow(tmp_path: Path) -> tuple[str, str]:
    """Bare Python can import a workspace pathlib.py, so it is not static proof."""
    claimed_file = tmp_path / "claimed.py"
    claimed_file.write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "pathlib.py").write_text(
        "class Path:\n"
        "    def __init__(self, value):\n"
        "        self.value = value\n"
        "    def write_text(self, value):\n"
        "        return None\n",
        encoding="utf-8",
    )
    command = "python -c \"from pathlib import Path; Path('claimed.py').write_text('x')\""
    return command, "claimed.py"


def _pathlib_case_write_outside_workspace(tmp_path: Path) -> tuple[str, str]:
    """An out-of-workspace pathlib write must not prove a same-basename claim."""
    generated_file = tmp_path / "src" / "generated.py"
    outside = tmp_path.parent / f"{tmp_path.name}-outside" / "generated.py"
    generated_file.parent.mkdir()
    outside.parent.mkdir()
    generated_file.write_text("VALUE = 1\n", encoding="utf-8")
    outside.write_text("VALUE = 2\n", encoding="utf-8")
    command = f"python -c \"from pathlib import Path; Path('{outside}').write_text('VALUE = 3')\""
    return command, "src/generated.py"


# Each case prepares a workspace and returns the runtime command plus the
# ``files_touched`` path the agent claims. Every case must fail the fat-harness
# file verifier.
_PATHLIB_FILE_CLAIM_REJECTION_CASES = (
    # Command text alone is not execution-bound file proof.
    pytest.param(
        _generated_claim_case(
            lambda _tmp_path: _trusted_python_c(
                "from pathlib import Path; Path('src/generated.py').write_text('VALUE = 2')"
            )
        ),
        id="command-text-only",
    ),
    # Equivalent pathlib spellings still need execution-bound file proof.
    pytest.param(
        _generated_claim_case(
            lambda _tmp_path: _trusted_python_c(
                "from pathlib import Path; Path('./src/generated.py').write_text('VALUE = 2')"
            )
        ),
        id="equivalent-dot-relative",
    ),
    pytest.param(
        _generated_claim_case(
            lambda _tmp_path: _trusted_python_c(
                "from pathlib import Path; (Path('src') / 'generated.py').write_text('VALUE = 2')"
            )
        ),
        id="equivalent-joined-parts",
    ),
    pytest.param(
        _generated_claim_case(
            lambda _tmp_path: _trusted_python_c(
                "from pathlib import Path; Path('src', 'generated.py').write_text('VALUE = 2')"
            )
        ),
        id="equivalent-multi-argument",
    ),
    pytest.param(
        _generated_claim_case(
            lambda tmp_path: _trusted_python_c(
                f"from pathlib import Path; Path('{tmp_path / 'src' / 'generated.py'}').write_text('VALUE = 2')"
            )
        ),
        id="equivalent-absolute",
    ),
    # Pathlib syntax is not proof unless a top-level pathlib.Path write executed.
    pytest.param(
        _generated_claim_case(
            lambda _tmp_path: (
                'python -c "from pathlib import Path; '
                "unused = lambda: Path('src/generated.py').write_text('VALUE = 2')\""
            )
        ),
        id="unexecuted-lambda-body",
    ),
    pytest.param(
        _generated_claim_case(
            lambda _tmp_path: (
                'python -c "from pathlib import Path; '
                "\nif False:\n    Path('src/generated.py').write_text('VALUE = 2')\""
            )
        ),
        id="unexecuted-if-false",
    ),
    pytest.param(
        _generated_claim_case(
            lambda _tmp_path: (
                'python -c "from pathlib import Path; '
                "\nif True:\n    Path('src/generated.py').write_text('VALUE = 2')\""
            )
        ),
        id="nested-if-true",
    ),
    pytest.param(
        _generated_claim_case(
            lambda _tmp_path: (
                'python -c "from pathlib import Path; '
                "Path = lambda _value: type('Noop', (), {'write_text': lambda self, _text: None})(); "
                "Path('src/generated.py').write_text('VALUE = 2')\""
            )
        ),
        id="rebound-noop-path",
    ),
    pytest.param(
        _generated_claim_case(
            lambda _tmp_path: (
                'python -c "from pathlib import Path; '
                "Path = lambda _value: None; from pathlib import Path; "
                "Path('src/generated.py').write_text('VALUE = 2')\""
            )
        ),
        id="rebound-then-reimported",
    ),
    pytest.param(
        _generated_claim_case(lambda _tmp_path: 'python -c "from pathlib import Path; if"'),
        id="malformed-source",
    ),
    pytest.param(
        _generated_claim_case(
            lambda _tmp_path: (
                'python -c "from pathlib import Path; '
                "Path('src /generated.py').write_text('VALUE = 2')\""
            )
        ),
        id="literal-space-in-receiver",
    ),
    pytest.param(
        _generated_claim_case(
            lambda _tmp_path: "python -c \"from pathlib import Path; Path('src') / 'generated.py'\""
        ),
        id="no-write-call",
    ),
    pytest.param(_pathlib_case_literal_space_identity, id="literal-space-identity"),
    pytest.param(_pathlib_case_write_to_different_file, id="write-to-different-file"),
    pytest.param(
        _pathlib_case_nonmatching_without_shell_fallback,
        id="nonmatching-without-shell-fallback",
    ),
    pytest.param(_pathlib_case_shell_expanded_source, id="shell-expanded-source"),
    # A Python-looking executable path is not an authenticated interpreter.
    pytest.param(
        _claimed_file_case(
            lambda _tmp_path: (
                "/tmp/fake/python -c "
                "\"from pathlib import Path; Path('claimed.py').write_text('x')\""
            )
        ),
        id="untrusted-absolute-python",
    ),
    pytest.param(
        _claimed_file_case(
            lambda _tmp_path: (
                "./python3 -c \"from pathlib import Path; Path('claimed.py').write_text('x')\""
            )
        ),
        id="untrusted-relative-python",
    ),
    pytest.param(_pathlib_case_nonisolated_local_shadow, id="nonisolated-local-pathlib-shadow"),
    pytest.param(_pathlib_case_write_outside_workspace, id="write-outside-workspace"),
)


class _UnittestClaimCase(NamedTuple):
    """One accepted ``tests_passed`` unittest claim backed by real Bash output."""

    test_methods: str
    runtime_command: str
    commands_run: str
    tests_passed: str
    runtime_output: str
    extra_tool_data: Mapping[str, object]
    native_session_id: str


_SINGLE_SLUGIFY_TEST = (
    "    def test_slugify(self):\n"
    "        self.assertEqual(slugify('Hello World'), 'hello-world')\n\n"
)
_SINGLE_SLUGIFY_SPACES_TEST = (
    "    def test_slugify_spaces(self):\n"
    "        self.assertEqual(slugify('Hello World'), 'hello-world')\n\n"
)
_FOUR_SLUGIFY_TESTS = (
    "    def test_slugify_spaces(self):\n"
    "        self.assertEqual(slugify('Hello World'), 'hello-world')\n"
    "    def test_slugify_lowercase(self):\n"
    "        self.assertEqual(slugify('Already Lower'), 'already-lower')\n"
    "    def test_slugify_empty(self):\n"
    "        self.assertEqual(slugify(''), '')\n"
    "    def test_slugify_one_word(self):\n"
    "        self.assertEqual(slugify('Hello'), 'hello')\n\n"
)
_UNITTEST_INNER_COMMAND = "python -m unittest test_slugify.py"


def _unittest_claim_case_summary(_tmp_path: Path) -> _UnittestClaimCase:
    """Regression for #961: Codex may put unittest command + OK summary in tests_passed."""
    return _UnittestClaimCase(
        test_methods=_SINGLE_SLUGIFY_TEST,
        runtime_command=_UNITTEST_INNER_COMMAND,
        commands_run=_UNITTEST_INNER_COMMAND,
        tests_passed=f"{_UNITTEST_INNER_COMMAND}: Ran 4 tests in 0.000s OK",
        runtime_output="Ran 4 tests in 0.000s\n\nOK",
        extra_tool_data={},
        native_session_id="codex-session-unittest-summary-claim",
    )


def _unittest_claim_case_bare_ok(_tmp_path: Path) -> _UnittestClaimCase:
    """A backed unittest command plus bare OK can rely on real Bash unittest output."""
    return _UnittestClaimCase(
        test_methods=_FOUR_SLUGIFY_TESTS,
        runtime_command=_UNITTEST_INNER_COMMAND,
        commands_run=_UNITTEST_INNER_COMMAND,
        tests_passed=f"{_UNITTEST_INNER_COMMAND}: OK",
        runtime_output="Ran 4 tests in 0.000s\n\nOK",
        extra_tool_data={},
        native_session_id="codex-session-unittest-bare-ok-claim",
    )


def _unittest_claim_case_shell_wrapped_bare_ok(_tmp_path: Path) -> _UnittestClaimCase:
    """Shell-wrapped unittest commands can back concise unittest claims."""
    shell_command = "/bin/zsh -lc 'python -m unittest \"test_slugify.py\"'"
    return _UnittestClaimCase(
        test_methods=_FOUR_SLUGIFY_TESTS,
        runtime_command=shell_command,
        commands_run=shell_command,
        tests_passed=f"{_UNITTEST_INNER_COMMAND}: OK",
        runtime_output="Ran 4 tests in 0.000s\n\nOK",
        extra_tool_data={},
        native_session_id="codex-session-shell-wrapped-unittest-bare-ok",
    )


def _unittest_claim_case_shell_wrapped_cd(tmp_path: Path) -> _UnittestClaimCase:
    """Codex shell wrappers may run setup before the claimed inner unittest command."""
    shell_command = (
        f"/bin/bash --noprofile --norc -lc 'cd {tmp_path} && python -m unittest "
        '"test_slugify.py"\''
    )
    return _UnittestClaimCase(
        test_methods=_SINGLE_SLUGIFY_SPACES_TEST,
        runtime_command=shell_command,
        commands_run=_UNITTEST_INNER_COMMAND,
        tests_passed=f"{_UNITTEST_INNER_COMMAND}: OK",
        runtime_output="Ran 1 test in 0.000s\n\nOK",
        extra_tool_data={"exit_code": 0},
        native_session_id="codex-session-shell-wrapped-cd-unittest-inner-claim",
    )


def _unittest_claim_case_shell_wrapped_export(tmp_path: Path) -> _UnittestClaimCase:
    """Shell env setup preambles may precede the claimed inner unittest command."""
    shell_command = (
        f"/bin/zsh -lc 'export PYTHONPATH={tmp_path} && python -m unittest \"test_slugify.py\"'"
    )
    return _UnittestClaimCase(
        test_methods=_SINGLE_SLUGIFY_SPACES_TEST,
        runtime_command=shell_command,
        commands_run=_UNITTEST_INNER_COMMAND,
        tests_passed=f"{_UNITTEST_INNER_COMMAND}: OK",
        runtime_output="Ran 1 test in 0.000s\n\nOK",
        extra_tool_data={"exit_code": 0},
        native_session_id="codex-session-shell-wrapped-export-unittest-inner-claim",
    )


_ACCEPTED_UNITTEST_CLAIM_CASES = (
    pytest.param(_unittest_claim_case_summary, id="command-plus-ok-summary"),
    pytest.param(_unittest_claim_case_bare_ok, id="command-plus-bare-ok"),
    pytest.param(_unittest_claim_case_shell_wrapped_bare_ok, id="shell-wrapped-bare-ok"),
    pytest.param(_unittest_claim_case_shell_wrapped_cd, id="shell-wrapped-cd-inner-claim"),
    pytest.param(_unittest_claim_case_shell_wrapped_export, id="shell-wrapped-export-inner-claim"),
)


class _EchoedUnittestCase(NamedTuple):
    """One fabricated unittest claim whose runtime command never ran unittest."""

    runtime_command: str
    tests_passed: str
    runtime_output: str
    error_fragment: str
    native_session_id: str


_ECHOED_UNITTEST_CLAIM_CASES = (
    # Commands merely mentioning unittest must not back tests_passed.
    pytest.param(
        _EchoedUnittestCase(
            runtime_command="echo unittest docs",
            tests_passed="unittest docs",
            runtime_output="unittest docs\nsuccess",
            error_fragment="tests_passed: unittest docs",
            native_session_id="codex-session-bare-unittest-word",
        ),
        id="bare-unittest-word",
    ),
    # Echoing a unittest command string must not count as running unittest.
    pytest.param(
        _EchoedUnittestCase(
            runtime_command="echo python -m unittest test_slugify.py",
            tests_passed="python -m unittest test_slugify.py: OK",
            runtime_output=(
                "python -m unittest test_slugify.py\nsuccess\nRan 4 tests in 0.000s\n\nOK"
            ),
            error_fragment="tests_passed:",
            native_session_id="codex-session-echoed-unittest-command",
        ),
        id="echoed-unittest-command",
    ),
    # Echoing a shell-wrapped unittest command must not count as running it.
    pytest.param(
        _EchoedUnittestCase(
            runtime_command="echo /bin/zsh -lc 'python -m unittest \"test_slugify.py\"'",
            tests_passed="python -m unittest test_slugify.py: OK",
            runtime_output=(
                "/bin/zsh -lc 'python -m unittest \"test_slugify.py\"'"
                "\nsuccess\nRan 4 tests in 0.000s\n\nOK"
            ),
            error_fragment="tests_passed:",
            native_session_id="codex-session-echoed-shell-wrapped-unittest-command",
        ),
        id="echoed-shell-wrapped-unittest-command",
    ),
)


class TestParallelACExecutor:
    """Tests for staged hybrid result handling."""

    def test_verification_report_uses_task_completion_terms(self) -> None:
        parallel_result = ParallelExecutionResult(
            stages=(),
            results=(
                ACExecutionResult(
                    ac_index=0,
                    ac_content="Create tasks",
                    success=True,
                    is_decomposed=True,
                    sub_results=(
                        ACExecutionResult(
                            ac_index=100,
                            ac_content="Create task storage",
                            success=False,
                            final_message="Storage failed",
                        ),
                    ),
                ),
            ),
            success_count=0,
            failure_count=1,
        )

        report = render_parallel_verification_report(parallel_result, 1)
        completion = render_parallel_completion_message(parallel_result, 1)

        assert "## Task Results" in report
        assert "### Task 1: [COMPLETED] Create tasks" in report
        assert "#### Subtask 1.1: [FAILED] Create task storage" in report
        assert "## AC Results" not in report
        assert "[PASS]" not in report
        assert "[FAIL]" not in report
        assert "Task Status:" in completion
        assert "- Task 1: [COMPLETED] Create tasks (1 subtasks)" in completion

    @pytest.mark.asyncio
    async def test_emit_subtask_event_preserves_full_content_with_compact_label(self) -> None:
        """Sub-AC events should retain full replay content plus compact display text."""
        event_store = AsyncMock()
        appended_events: list[BaseEvent] = []

        async def _append(event: BaseEvent) -> None:
            appended_events.append(event)

        event_store.append.side_effect = _append
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
        )
        full_content = (
            "Define baseline_source_branch as a single authoritative baseline identity "
            "with repository URL, exact ref, commit SHA, capture timestamp, operator, "
            "and artifact bundle IDs."
        )

        await executor._emit_subtask_event(
            execution_id="exec_subtask_event",
            ac_index=0,
            sub_task_index=1,
            sub_task_content=full_content,
            status="executing",
        )

        assert len(appended_events) == 1
        data = appended_events[0].data
        assert data["content"] == full_content
        assert data["label"] == "Define baseline_source_branch as a single authorit"
        assert len(data["label"]) == 50
        assert data["sub_task_id"] == "ac_1_sub_1"
        assert data["status"] == "executing"

    @pytest.mark.asyncio
    async def test_emit_subtask_event_emits_node_identity_with_legacy_event(self) -> None:
        """New Sub-AC events should expose canonical node identity and legacy fields."""
        event_store = AsyncMock()
        appended_events: list[BaseEvent] = []

        async def _append(event: BaseEvent) -> None:
            appended_events.append(event)

        event_store.append.side_effect = _append
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
        )
        node_identity = ExecutionNodeIdentity.root(
            execution_context_id="exec_subtask_event",
            ac_index=0,
        ).child(1)

        await executor._emit_subtask_event(
            execution_id="exec_subtask_event",
            ac_index=0,
            sub_task_index=2,
            sub_task_content="Populate the baseline source branch evidence ledger.",
            status="pending",
            node_identity=node_identity,
        )

        assert [event.type for event in appended_events] == [
            "execution.node.created",
            "execution.subtask.updated",
        ]
        node_event, legacy_event = appended_events
        assert node_event.data["identity_model"] == "execution_node_v1"
        assert node_event.data["node_id"] == node_identity.node_id
        assert node_event.data["parent_node_id"] == node_identity.parent_node_id
        assert node_event.data["legacy_parent_node_id"] == "ac_0"
        assert node_event.data["display_path"] == "1.2"
        assert node_event.data["legacy_ac_index"] == 1
        assert node_event.data["legacy_sub_task_id"] == "ac_1_sub_2"
        assert legacy_event.data["node_id"] == node_identity.node_id
        assert legacy_event.data["parent_node_id"] == node_identity.parent_node_id
        assert legacy_event.data["legacy_parent_node_id"] == "ac_0"
        assert legacy_event.data["sub_task_id"] == "ac_1_sub_2"

    @pytest.mark.asyncio
    async def test_node_runtime_load_falls_back_to_legacy_scope_events(self) -> None:
        """Node-aware resume lookup should still find pre-node runtime events."""
        node_identity = ExecutionNodeIdentity.root(
            execution_context_id="orch_123",
            ac_index=1,
        )
        legacy_scope_id = "orch_123_ac_2"
        legacy_state_path = (
            "execution.workflows.orch_123.acceptance_criteria.ac_2.implementation_session"
        )
        persisted_handle = RuntimeHandle(
            backend="opencode",
            kind="implementation_session",
            native_session_id="opencode-session-legacy",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            metadata={
                "scope": "ac",
                "session_role": "implementation",
                "retry_attempt": 0,
                "ac_index": 1,
                "session_scope_id": legacy_scope_id,
                "session_state_path": legacy_state_path,
                "server_session_id": "server-legacy",
            },
        )
        replayed_scope_ids: list[str] = []

        async def _replay(_aggregate_type: str, aggregate_id: str) -> list[BaseEvent]:
            replayed_scope_ids.append(aggregate_id)
            if aggregate_id != legacy_scope_id:
                return []
            return [
                BaseEvent(
                    type="execution.session.started",
                    aggregate_type="execution",
                    aggregate_id=legacy_scope_id,
                    data={
                        "retry_attempt": 0,
                        "session_scope_id": legacy_scope_id,
                        "session_state_path": legacy_state_path,
                        "runtime": persisted_handle.to_dict(),
                    },
                )
            ]

        event_store = AsyncMock()
        event_store.replay.side_effect = _replay
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
        )

        resume_handle = await executor._load_persisted_ac_runtime_handle(
            1,
            execution_context_id="orch_123",
            node_identity=node_identity,
        )

        assert resume_handle is not None
        assert replayed_scope_ids[0] == f"orch_123_{node_identity.node_id}"
        assert legacy_scope_id in replayed_scope_ids
        assert resume_handle.native_session_id == "opencode-session-legacy"
        assert resume_handle.metadata["server_session_id"] == "server-legacy"
        assert resume_handle.metadata["node_id"] == node_identity.node_id
        assert resume_handle.metadata["legacy_node_id"] == "ac_1"
        assert resume_handle.metadata["session_scope_id"] == f"orch_123_{node_identity.node_id}"
        assert resume_handle.metadata["legacy_session_scope_id"] == legacy_scope_id

    @pytest.mark.asyncio
    async def test_deep_sub_ac_runtime_identity_does_not_require_legacy_indices(self) -> None:
        """Grandchild Sub-AC execution should not crash while building runtime identity."""

        class _StubRuntime:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                self.calls.append(
                    {
                        "prompt": prompt,
                        "tools": tools,
                        "system_prompt": system_prompt,
                        "resume_handle": resume_handle,
                        "resume_session_id": resume_session_id,
                    }
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=resume_handle,
                )

        grandchild_identity = (
            ExecutionNodeIdentity.root(
                execution_context_id="exec_deep_runtime",
                ac_index=0,
            )
            .child(0)
            .child(1)
        )
        event_store, _appended_events = _make_replaying_event_store()
        runtime = _StubRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=10000,
            ac_content="Implement deep recursive leaf",
            session_id="sess_deep_runtime",
            tools=["Read"],
            system_prompt="system",
            seed_goal="Support recursive decomposition",
            depth=2,
            start_time=datetime.now(UTC),
            execution_id="exec_deep_runtime",
            is_sub_ac=True,
            node_identity=grandchild_identity,
        )

        assert result.success is True
        resume_handle = runtime.calls[0]["resume_handle"]
        assert isinstance(resume_handle, RuntimeHandle)
        assert resume_handle.metadata["node_id"] == grandchild_identity.node_id
        assert resume_handle.metadata["parent_node_id"] == grandchild_identity.parent_node_id
        assert resume_handle.metadata["session_scope_id"] == (
            f"exec_deep_runtime_{grandchild_identity.node_id}"
        )
        assert "legacy_session_scope_id" not in resume_handle.metadata
        assert "legacy_session_scope_ids" not in resume_handle.metadata
        event_store.replay.assert_awaited_once_with(
            "execution",
            f"exec_deep_runtime_{grandchild_identity.node_id}",
        )

    @pytest.mark.asyncio
    async def test_batch_fans_out_in_parallel_regardless_of_tool_catalog(self) -> None:
        """Batch scheduling is tool-catalog-agnostic.

        The control plane exists as declarative audit/metadata, not as a
        batch-level scheduler.  Cross-AC safety is enforced by the
        file-conflict guard (static) and by the provider runtime at
        tool-invocation time (dynamic); the scheduler must not degrade
        a batch to serial execution based on session-level tool
        availability, because "tool is in the catalog" does not imply
        "every AC in this batch will invoke it".

        This test mixes read-only and write-capable tools in the same
        catalog to pin that mixed catalogs also fan out in parallel.
        """
        seed = _make_seed("AC alpha", "AC beta")
        executor = _make_executor()
        active_count = 0
        max_active_count = 0

        async def fake_execute_single_ac(**kwargs: Any) -> ACExecutionResult:
            nonlocal active_count, max_active_count
            ac_index = int(kwargs["ac_index"])
            active_count += 1
            max_active_count = max(max_active_count, active_count)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            active_count -= 1
            return ACExecutionResult(
                ac_index=ac_index,
                ac_content=str(kwargs["ac_content"]),
                success=True,
                final_message=f"AC {ac_index} complete",
            )

        with patch.object(executor, "_execute_single_ac", side_effect=fake_execute_single_ac):
            results = await executor._execute_ac_batch(
                seed=seed,
                batch_indices=[0, 1],
                session_id="sess_batch_parallel",
                execution_id="exec_batch_parallel",
                tools=["Read", "Edit", "Bash"],
                tool_catalog=(
                    MCPToolDefinition(name="Read", description="Read files"),
                    MCPToolDefinition(name="Edit", description="Edit files"),
                    MCPToolDefinition(name="Bash", description="Run shell"),
                ),
                system_prompt="test",
                level_contexts=[],
                ac_retry_attempts={0: 0, 1: 0},
            )

        assert [result.ac_index for result in results if isinstance(result, ACExecutionResult)] == [
            0,
            1,
        ]
        # Regression guard: even a catalog containing SERIALIZED (Edit)
        # and ISOLATED_SESSION_REQUIRED (Bash) tools must not collapse
        # a batch to serial execution.
        assert max_active_count == 2

    @pytest.mark.asyncio
    async def test_batch_adaptive_window_grows_after_sustained_provider_success(self) -> None:
        """The pre-flight value is an initial estimate, not a permanent cap."""

        seed = _make_seed(*(f"AC {index}" for index in range(6)))
        executor = ProcessLocalTestExecutor(
            adapter=MagicMock(),
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=False,
            max_concurrent=1,
            adaptive_max_concurrent=2,
        )
        active_count = 0
        max_active_count = 0

        async def fake_execute_single_ac(**kwargs: Any) -> ACExecutionResult:
            nonlocal active_count, max_active_count
            ac_index = int(kwargs["ac_index"])
            messages = (
                AgentMessage(
                    type="result",
                    content="done",
                    data={"subtype": "success"},
                ),
            )
            async with executor._adaptive_concurrency.slot() as permit_epoch:
                active_count += 1
                max_active_count = max(max_active_count, active_count)
                try:
                    await asyncio.sleep(0)
                    await asyncio.sleep(0)
                    await observe_provider_messages(
                        executor._adaptive_concurrency,
                        messages,
                        permit_epoch,
                        ("sess_adaptive_growth", "exec_adaptive_growth", ac_index),
                        provider_completed=True,
                    )
                finally:
                    active_count -= 1
            return ACExecutionResult(
                ac_index=ac_index,
                ac_content=str(kwargs["ac_content"]),
                success=True,
                messages=messages,
                final_message="done",
            )

        with patch.object(executor, "_execute_single_ac", side_effect=fake_execute_single_ac):
            results = await executor._execute_ac_batch(
                seed=seed,
                batch_indices=list(range(6)),
                session_id="sess_adaptive_growth",
                execution_id="exec_adaptive_growth",
                tools=["Read"],
                tool_catalog=None,
                system_prompt="test",
                level_contexts=[],
                ac_retry_attempts=dict.fromkeys(range(6), 0),
            )

        assert all(isinstance(result, ACExecutionResult) for result in results)
        assert executor._adaptive_concurrency.snapshot().current_limit == 2
        assert max_active_count == 2

    @pytest.mark.asyncio
    async def test_batch_preserves_completed_result_with_hostile_retry_after(self) -> None:
        """Provider metadata cannot replace completed work with float overflow."""

        seed = _make_seed("AC hostile cooldown")
        executor = ProcessLocalTestExecutor(
            adapter=MagicMock(),
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=False,
            max_concurrent=1,
        )
        completed = ACExecutionResult(
            ac_index=0,
            ac_content="AC hostile cooldown",
            success=False,
            messages=(
                AgentMessage(
                    type="result",
                    content="Interface request concurrency exceeded",
                    data={
                        "subtype": "error",
                        "kind": "concurrency_limit",
                        "retry_after_seconds": 10**1000,
                    },
                ),
            ),
            final_message="provider rejected concurrency",
        )

        async def fake_execute_single_ac(**_kwargs: Any) -> ACExecutionResult:
            async with executor._adaptive_concurrency.slot() as permit_epoch:
                await observe_provider_messages(
                    executor._adaptive_concurrency,
                    completed.messages,
                    permit_epoch,
                    ("sess_hostile_cooldown", "exec_hostile_cooldown", 0),
                    provider_completed=True,
                )
            return completed

        with patch.object(executor, "_execute_single_ac", side_effect=fake_execute_single_ac):
            results = await executor._execute_ac_batch(
                seed=seed,
                batch_indices=[0],
                session_id="sess_hostile_cooldown",
                execution_id="exec_hostile_cooldown",
                tools=["Read"],
                tool_catalog=None,
                system_prompt="test",
                level_contexts=[],
                ac_retry_attempts={0: 0},
            )

        assert results == [completed]
        cooldown = executor._adaptive_concurrency.snapshot().cooldown_remaining_seconds
        assert 0 < cooldown <= MAX_ADAPTIVE_CONCURRENCY_COOLDOWN_SECONDS

    @pytest.mark.asyncio
    async def test_decomposed_child_pressure_delays_next_provider_entrance(self) -> None:
        """A child 429 must reach AIMD before its sequential sibling dispatches."""

        clock = {"now": 100.0}
        sleeps: list[float] = []
        provider_entrances: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock["now"] += seconds

        class _SequencedRuntime:
            def __init__(self) -> None:
                self.calls = 0
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
                **_kwargs: Any,
            ):
                del prompt, tools, system_prompt, resume_session_id
                self.calls += 1
                provider_entrances.append(clock["now"])
                if self.calls == 1:
                    yield AgentMessage(
                        type="result",
                        content="Too many concurrent requests",
                        data={
                            "subtype": "error",
                            "http_status": 429,
                            "headers": {"Retry-After": "2"},
                        },
                        resume_handle=resume_handle,
                    )
                    return
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=resume_handle,
                )

        event_store, _appended_events = _make_replaying_event_store()
        executor = ProcessLocalTestExecutor(
            adapter=_SequencedRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=True,
            max_concurrent=1,
            cross_harness_redispatch=False,
        )
        # The policy is identical to the constructor-bound authority; only the
        # deterministic clock/sleep seams differ for this provider-boundary test.
        executor._adaptive_concurrency = AdaptiveConcurrencyController(
            initial_limit=1,
            clock=lambda: clock["now"],
            sleep=fake_sleep,
        )
        executor._emit_subtask_event = AsyncMock()
        executor._maybe_recover_with_bounce_decomposition = AsyncMock(return_value=(None, None))
        root = ExecutionNodeIdentity.root(
            execution_context_id="exec_child_pressure",
            ac_index=0,
        )
        executor._publish_event_owned_decomposition_decision(
            _trusted_preflight_split(root.node_id, "First child", "Second child")
        )

        result = await executor._execute_single_ac(
            ac_index=0,
            ac_content="Composite AC",
            session_id="sess_child_pressure",
            tools=["Read"],
            tool_catalog=None,
            system_prompt="system",
            seed_goal="Respect provider cooldowns",
            execution_id="exec_child_pressure",
        )

        assert len(result.sub_results) == 2
        assert provider_entrances == [100.0, 102.0]
        assert sleeps == [2.0]

    @pytest.mark.asyncio
    async def test_atomic_ac_uses_ac_scoped_runtime_handle(self) -> None:
        """Atomic AC execution should seed a fresh AC-scoped runtime handle."""

        class _StubImplementationRuntime:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                self.calls.append(
                    {
                        "prompt": prompt,
                        "tools": tools,
                        "system_prompt": system_prompt,
                        "resume_handle": resume_handle,
                        "resume_session_id": resume_session_id,
                    }
                )
                bound_handle = RuntimeHandle(
                    backend=resume_handle.backend if resume_handle is not None else "opencode",
                    kind=resume_handle.kind
                    if resume_handle is not None
                    else "implementation_session",
                    native_session_id="opencode-session-1",
                    cwd=resume_handle.cwd if resume_handle is not None else "/tmp/project",
                    approval_mode=(
                        resume_handle.approval_mode if resume_handle is not None else "acceptEdits"
                    ),
                    metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=bound_handle,
                )

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_StubImplementationRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=2,
            ac_content="Implement AC 3",
            session_id="orch_123",
            tools=["Read", "Edit"],
            tool_catalog=(
                MCPToolDefinition(name="Read", description="Read a file from the workspace."),
                MCPToolDefinition(
                    name="Edit", description="Edit an existing file in the workspace."
                ),
            ),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        runtime_call = executor._adapter.calls[0]
        resume_handle = runtime_call["resume_handle"]
        assert isinstance(resume_handle, RuntimeHandle)
        assert resume_handle.backend == "opencode"
        assert resume_handle.kind == "implementation_session"
        assert resume_handle.native_session_id is None
        assert resume_handle.cwd == "/tmp/project"
        assert resume_handle.approval_mode == "acceptEdits"
        assert resume_handle.metadata["ac_id"] == "orch_123_ac_3"
        assert resume_handle.metadata["scope"] == "ac"
        assert resume_handle.metadata["session_role"] == "implementation"
        assert resume_handle.metadata["retry_attempt"] == 0
        assert resume_handle.metadata["attempt_number"] == 1
        assert resume_handle.metadata["ac_index"] == 2
        assert [tool["name"] for tool in resume_handle.metadata["tool_catalog"]] == [
            "Read",
            "Edit",
        ]
        assert [tool["name"] for tool in resume_handle.metadata["capability_graph"]] == [
            "Read",
            "Edit",
        ]
        assert [hint["name"] for hint in resume_handle.metadata["control_plane"]] == [
            "Read",
            "Edit",
        ]
        assert resume_handle.metadata["session_scope_id"] == "orch_123_ac_3"
        assert resume_handle.metadata["session_attempt_id"] == "orch_123_ac_3_attempt_1"
        assert (
            resume_handle.metadata["session_state_path"]
            == "execution.workflows.orch_123.acceptance_criteria.ac_3.implementation_session"
        )
        started_event = next(
            event for event in appended_events if event.type == "execution.session.started"
        )
        assert [tool["name"] for tool in started_event.data["tool_catalog"]] == ["Read", "Edit"]
        assert [
            tool["name"] for tool in started_event.data["runtime"]["metadata"]["tool_catalog"]
        ] == ["Read", "Edit"]
        assert [
            tool["name"] for tool in started_event.data["runtime"]["metadata"]["capability_graph"]
        ] == [
            "Read",
            "Edit",
        ]
        assert started_event.data["session_attempt_id"] == "orch_123_ac_3_attempt_1"
        assert result.success is True
        assert result.session_id == "opencode-session-1"
        assert result.runtime_handle is not None
        assert result.runtime_handle.native_session_id == "opencode-session-1"

    @pytest.mark.asyncio
    async def test_atomic_ac_terminates_live_runtime_handle_after_completion(self) -> None:
        """Completed AC runs should best-effort terminate live runtime handles."""
        terminate_calls = 0

        async def _terminate(_handle: RuntimeHandle) -> bool:
            nonlocal terminate_calls
            terminate_calls += 1
            return True

        class _StubImplementationRuntime:
            def __init__(self) -> None:
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                del prompt, tools, system_prompt, resume_session_id
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=RuntimeHandle(
                        backend=resume_handle.backend if resume_handle is not None else "opencode",
                        kind=resume_handle.kind
                        if resume_handle is not None
                        else "implementation_session",
                        native_session_id="opencode-session-live",
                        cwd=resume_handle.cwd if resume_handle is not None else "/tmp/project",
                        approval_mode=(
                            resume_handle.approval_mode
                            if resume_handle is not None
                            else "acceptEdits"
                        ),
                        metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
                    ).bind_controls(terminate_callback=_terminate),
                )

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_StubImplementationRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(
                MCPToolDefinition(name="Read", description="Read a file from the workspace."),
            ),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert terminate_calls == 1

    @pytest.mark.asyncio
    async def test_atomic_ac_observes_profile_typed_evidence_without_changing_success(self) -> None:
        """Profile-backed atomic completion records typed evidence observe-only."""

        class _StubImplementationRuntime:
            _runtime_handle_backend = "opencode"
            _cwd = "/tmp/project"
            _permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                del prompt, tools, system_prompt, resume_session_id
                yield AgentMessage(
                    type="result",
                    content=(
                        "Done.\n"
                        "```json\n"
                        '{"files_touched":["src/app.py"],'
                        '"commands_run":["pytest"],'
                        '"tests_passed":["tests/test_app.py"]}\n'
                        "```"
                    ),
                    data={"subtype": "success"},
                    resume_handle=RuntimeHandle(
                        backend=resume_handle.backend if resume_handle is not None else "opencode",
                        kind=resume_handle.kind
                        if resume_handle is not None
                        else "implementation_session",
                        native_session_id="opencode-session-evidence",
                        cwd=resume_handle.cwd if resume_handle is not None else "/tmp/project",
                        metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
                    ),
                )

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_StubImplementationRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.typed_evidence is not None
        assert result.typed_evidence.data["files_touched"] == ["src/app.py"]
        assert result.typed_evidence_validation is not None
        assert result.typed_evidence_validation.ok is True
        assert result.typed_evidence_error is None

        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["observe_only"] is True
        assert evidence_event.data["enforced"] is False
        assert evidence_event.data["fat_harness_mode"] is False
        assert evidence_event.data["enforcement_error"] is None
        assert evidence_event.data["typed_evidence_present"] is True
        assert evidence_event.data["typed_evidence_valid"] is True
        assert evidence_event.data["verifier_ran"] is False
        assert evidence_event.data["verifier_passed"] is False
        assert evidence_event.data["required_fields"] == [
            "files_touched",
            "commands_run",
            "tests_passed",
        ]
        assert evidence_event.data["typed_evidence_fields"] == [
            "commands_run",
            "files_touched",
            "tests_passed",
        ]

    @pytest.mark.asyncio
    async def test_atomic_ac_records_typed_evidence_error_without_default_flip(self) -> None:
        """Malformed typed evidence is observed but does not change legacy success."""

        class _StubImplementationRuntime:
            _runtime_handle_backend = "opencode"
            _cwd = "/tmp/project"
            _permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                del prompt, tools, system_prompt, resume_session_id
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE] no JSON evidence yet",
                    data={"subtype": "success"},
                    resume_handle=RuntimeHandle(
                        backend=resume_handle.backend if resume_handle is not None else "opencode",
                        kind=resume_handle.kind
                        if resume_handle is not None
                        else "implementation_session",
                        native_session_id="opencode-session-no-evidence",
                        cwd=resume_handle.cwd if resume_handle is not None else "/tmp/project",
                        metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
                    ),
                )

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_StubImplementationRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.typed_evidence is None
        assert result.typed_evidence_validation is None
        assert result.typed_evidence_error is not None

        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["observe_only"] is True
        assert evidence_event.data["enforced"] is False
        assert evidence_event.data["typed_evidence_present"] is False
        assert evidence_event.data["typed_evidence_valid"] is False
        assert evidence_event.data["verifier_ran"] is False
        assert "Evidence is not valid JSON" in evidence_event.data["typed_evidence_error"]

    @pytest.mark.asyncio
    async def test_fat_harness_atomic_prompt_requests_json_evidence_without_task_complete(
        self,
    ) -> None:
        """Fat-harness atomic prompts must not ask for prose [TASK_COMPLETE]."""
        event_store, _ = _make_replaying_event_store()
        runtime = _FinalMessageRuntime(
            "```json\n"
            '{"files_touched":["src/app.py"],"commands_run":["pytest"],"tests_passed":["pytest"]}'
            "\n```",
            native_session_id="opencode-session-prompt",
            support_messages=(
                AgentMessage(
                    type="tool",
                    content="Edit src/app.py",
                    tool_name="Edit",
                    data={"input": {"file_path": "src/app.py"}},
                ),
                AgentMessage(
                    type="tool",
                    content="pytest passed\n1 passed in 0.01s",
                    tool_name="Bash",
                    data={
                        "input": {"command": "pytest"},
                        "output": "1 passed in 0.01s",
                    },
                ),
            ),
        )
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read", "Edit", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert runtime.last_prompt is not None
        assert "emit exactly ONE fenced JSON evidence record" in runtime.last_prompt
        assert "files_touched, commands_run, tests_passed" in runtime.last_prompt
        assert "do not emit a generic command_result wrapper" in runtime.last_prompt
        assert "Do not prefix it with [TASK_COMPLETE]" in runtime.last_prompt
        assert "You are responsible only for the current acceptance criterion" in (
            runtime.last_prompt
        )
        assert "Do not implement, test, document, or pre-create work" in runtime.last_prompt
        assert "sibling or future ACs" in runtime.last_prompt
        assert "current AC in this runtime session" in runtime.last_prompt
        assert "workspace-relative paths only" in runtime.last_prompt
        assert "never absolute paths" in runtime.last_prompt
        assert "omit exploratory" in runtime.last_prompt
        assert "rg, grep, sed, cat, ls, find, or pwd" in runtime.last_prompt
        assert "Auto Recursion Guard" in runtime.last_prompt
        assert "ouroboros_auto" in runtime.last_prompt
        assert "nested auto session" in runtime.last_prompt
        assert "explicitly state: [TASK_COMPLETE]" not in runtime.last_prompt

    @pytest.mark.asyncio
    async def test_fat_harness_docs_only_ac_uses_docs_evidence_contract(self, tmp_path) -> None:
        """Regression for #961: README-only ACs must not require prior test IDs."""
        readme = tmp_path / "README.md"
        readme.write_text("# String utils\n", encoding="utf-8")

        event_store, appended_events = _make_replaying_event_store()
        runtime = _FinalMessageRuntime(
            "```json\n"
            "{\n"
            '  "files_touched": ["README.md"],\n'
            '  "commands_run": ["grep -n slugify README.md"]\n'
            "}\n"
            "```",
            native_session_id="codex-session-docs-only-current-ac",
            support_messages=(
                AgentMessage(
                    type="assistant",
                    content=f"Calling tool: Edit: {readme}",
                    tool_name="Edit",
                    data={"tool_input": {"file_path": str(readme)}},
                ),
                AgentMessage(
                    type="assistant",
                    content="Calling tool: Bash: grep -n slugify README.md",
                    tool_name="Bash",
                    data={
                        "tool_input": {"command": "grep -n slugify README.md"},
                        "output": "12:slugify('Hello World') -> hello-world",
                        "exit_code": 0,
                    },
                ),
            ),
            cwd=str(tmp_path),
        )
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=2,
            ac_content="Document slugify and truncate usage in README.md.",
            session_id="orch_123",
            tools=["Read", "Edit", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship string utilities",
            depth=0,
            start_time=datetime.now(UTC),
            sibling_acs=[
                (0, "Create string_utils.py with slugify(text) and test_slugify.py."),
                (1, "Add truncate(text, max_length) and test_truncate.py."),
                (2, "Document slugify and truncate usage in README.md."),
            ],
        )

        assert result.success is True
        assert result.error is None
        assert runtime.last_prompt is not None
        assert "documentation-only current AC" in runtime.last_prompt
        assert "read/grep/diff command when that command is the validation" in runtime.last_prompt
        assert "Do not include tests_passed at all for documentation-only ACs" in (
            runtime.last_prompt
        )
        assert "do not list individual test names or prior test IDs" in runtime.last_prompt
        assert "files_touched, commands_run" in runtime.last_prompt
        assert "files_touched, commands_run, tests_passed" not in runtime.last_prompt
        assert result.typed_evidence is not None
        assert "tests_passed" not in result.typed_evidence.data
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["required_fields"] == ["files_touched", "commands_run"]
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.parametrize(
        ("ac_content", "doc_path"),
        [
            ("Document the API in docs/api.md.", "docs/api.md"),
            ("Write a CLI flag guide in README.md.", "README.md"),
            ("Update the changelog for the parser bug.", "CHANGELOG.md"),
            ("Document test setup in README.md.", "README.md"),
            ("Write a unit test guide in docs/testing.md.", "docs/testing.md"),
            (
                "Create README.md documenting how to run the CLI and the required test command.",
                "README.md",
            ),
            (
                "Document CLI usage and the required test command in README.md.",
                "README.md",
            ),
            (
                "Update README.md with usage and verification instructions for python -m unittest test_todo.py.",
                "README.md",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_fat_harness_docs_only_ac_allows_code_subject_documentation(
        self, tmp_path, ac_content: str, doc_path: str
    ) -> None:
        """Docs about code subjects are still docs-only when they do not mutate code."""
        doc_file = tmp_path / doc_path
        doc_file.parent.mkdir(parents=True, exist_ok=True)
        doc_file.write_text("Documentation\n", encoding="utf-8")

        event_store, appended_events = _make_replaying_event_store()
        runtime = _FinalMessageRuntime(
            "```json\n"
            "{\n"
            f'  "files_touched": ["{doc_path}"],\n'
            f'  "commands_run": ["grep -n Documentation {doc_path}"]\n'
            "}\n"
            "```",
            native_session_id="codex-session-docs-only-code-subject",
            support_messages=(
                AgentMessage(
                    type="assistant",
                    content=f"Calling tool: Edit: {doc_file}",
                    tool_name="Edit",
                    data={"tool_input": {"file_path": str(doc_file)}},
                ),
                AgentMessage(
                    type="assistant",
                    content=f"Calling tool: Bash: grep -n Documentation {doc_path}",
                    tool_name="Bash",
                    data={
                        "tool_input": {"command": f"grep -n Documentation {doc_path}"},
                        "output": "1:Documentation",
                        "exit_code": 0,
                    },
                ),
            ),
            cwd=str(tmp_path),
        )
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content=ac_content,
            session_id="orch_123",
            tools=["Read", "Edit", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship docs",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert runtime.last_prompt is not None
        assert "documentation-only current AC" in runtime.last_prompt
        assert "read/grep/diff command when that command is the validation" in runtime.last_prompt
        assert "Do not include tests_passed at all for documentation-only ACs" in (
            runtime.last_prompt
        )
        assert "do not list individual test names or prior test IDs" in runtime.last_prompt
        assert "files_touched, commands_run" in runtime.last_prompt
        assert "files_touched, commands_run, tests_passed" not in runtime.last_prompt
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["required_fields"] == ["files_touched", "commands_run"]
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_markdown_code_ac_keeps_test_evidence_required(
        self, tmp_path
    ) -> None:
        """A markdown-related implementation AC must not be misclassified as docs-only."""
        parser_file = tmp_path / "src" / "markdown_parser.py"
        parser_file.parent.mkdir()
        parser_file.write_text("def parse(text):\n    return text\n", encoding="utf-8")
        test_file = tmp_path / "tests" / "test_markdown_parser.py"
        test_file.parent.mkdir()
        test_file.write_text("def test_parse():\n    assert True\n", encoding="utf-8")

        event_store, appended_events = _make_replaying_event_store()
        runtime = _FinalMessageRuntime(
            "```json\n"
            "{\n"
            '  "files_touched": ["src/markdown_parser.py", "tests/test_markdown_parser.py"],\n'
            '  "commands_run": ["python -m pytest tests/test_markdown_parser.py"],\n'
            '  "tests_passed": ["tests/test_markdown_parser.py::test_parse"]\n'
            "}\n"
            "```",
            native_session_id="codex-session-markdown-code-ac",
            support_messages=(
                AgentMessage(
                    type="assistant",
                    content=f"Calling tool: Edit: {parser_file}",
                    tool_name="Edit",
                    data={"tool_input": {"file_path": str(parser_file)}},
                ),
                AgentMessage(
                    type="assistant",
                    content=f"Calling tool: Edit: {test_file}",
                    tool_name="Edit",
                    data={"tool_input": {"file_path": str(test_file)}},
                ),
                AgentMessage(
                    type="assistant",
                    content="Calling tool: Bash: python -m pytest tests/test_markdown_parser.py",
                    tool_name="Bash",
                    data={
                        "tool_input": {"command": "python -m pytest tests/test_markdown_parser.py"},
                        "output": "tests/test_markdown_parser.py::test_parse passed; 1 passed",
                        "exit_code": 0,
                    },
                ),
            ),
            cwd=str(tmp_path),
        )
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement a markdown parser and usage examples.",
            session_id="orch_123",
            tools=["Read", "Edit", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship markdown support",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert runtime.last_prompt is not None
        assert "documentation-only current AC" not in runtime.last_prompt
        assert "files_touched, commands_run, tests_passed" in runtime.last_prompt
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["required_fields"] == [
            "files_touched",
            "commands_run",
            "tests_passed",
        ]
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.parametrize(
        "ac_content",
        [
            "Add slugify() and update README.md.",
            "Fix parser bug and document it in docs/.",
            "Update README.md and fix parser bug.",
            "Write docs/api.md and add endpoint validation.",
            "Document README.md, then create parser.py.",
            "Run pytest and update README.md.",
            "Add docs command to CLI.",
            "Create docs endpoint.",
            "Fix docs parser bug.",
            "Update README.md while fixing parser bug.",
            "Update README.md plus fix parser bug.",
            "Fix documentation parser bug.",
        ],
    )
    @pytest.mark.asyncio
    async def test_fat_harness_mixed_code_and_docs_ac_keeps_test_evidence_required(
        self, tmp_path, ac_content: str
    ) -> None:
        """Mixed implementation/docs ACs must not drop tests_passed from code profile evidence."""
        source_file = tmp_path / "src" / "string_utils.py"
        source_file.parent.mkdir()
        source_file.write_text("def slugify(text):\n    return text.lower()\n", encoding="utf-8")
        test_file = tmp_path / "tests" / "test_string_utils.py"
        test_file.parent.mkdir()
        test_file.write_text("def test_slugify():\n    assert True\n", encoding="utf-8")
        readme = tmp_path / "README.md"
        readme.write_text("# String utils\n", encoding="utf-8")

        event_store, appended_events = _make_replaying_event_store()
        runtime = _FinalMessageRuntime(
            "```json\n"
            "{\n"
            '  "files_touched": ["src/string_utils.py", "tests/test_string_utils.py", "README.md"],\n'
            '  "commands_run": ["python -m pytest tests/test_string_utils.py"],\n'
            '  "tests_passed": ["tests/test_string_utils.py::test_slugify"]\n'
            "}\n"
            "```",
            native_session_id="codex-session-mixed-code-docs-ac",
            support_messages=(
                AgentMessage(
                    type="assistant",
                    content=f"Calling tool: Edit: {source_file}",
                    tool_name="Edit",
                    data={"tool_input": {"file_path": str(source_file)}},
                ),
                AgentMessage(
                    type="assistant",
                    content=f"Calling tool: Edit: {test_file}",
                    tool_name="Edit",
                    data={"tool_input": {"file_path": str(test_file)}},
                ),
                AgentMessage(
                    type="assistant",
                    content=f"Calling tool: Edit: {readme}",
                    tool_name="Edit",
                    data={"tool_input": {"file_path": str(readme)}},
                ),
                AgentMessage(
                    type="assistant",
                    content="Calling tool: Bash: python -m pytest tests/test_string_utils.py",
                    tool_name="Bash",
                    data={
                        "tool_input": {"command": "python -m pytest tests/test_string_utils.py"},
                        "output": "tests/test_string_utils.py::test_slugify passed; 1 passed",
                        "exit_code": 0,
                    },
                ),
            ),
            cwd=str(tmp_path),
        )
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content=ac_content,
            session_id="orch_123",
            tools=["Read", "Edit", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship string utilities",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert runtime.last_prompt is not None
        assert "documentation-only current AC" not in runtime.last_prompt
        assert "files_touched, commands_run, tests_passed" in runtime.last_prompt
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["required_fields"] == [
            "files_touched",
            "commands_run",
            "tests_passed",
        ]
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_docs_only_ac_ignores_out_of_scope_test_id_bleed(
        self, tmp_path
    ) -> None:
        """Docs-only ACs ignore extra tests_passed instead of failing required docs evidence."""
        readme = tmp_path / "README.md"
        readme.write_text("# String utils\n", encoding="utf-8")

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "```json\n"
                "{\n"
                '  "files_touched": ["README.md"],\n'
                '  "commands_run": ["grep -n slugify README.md"],\n'
                '  "tests_passed": ["test_slugify.py::test_slugify"]\n'
                "}\n"
                "```",
                native_session_id="codex-session-docs-only-prior-test-bleed",
                support_messages=(
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {readme}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(readme)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content="Calling tool: Bash: grep -n slugify README.md",
                        tool_name="Bash",
                        data={
                            "tool_input": {"command": "grep -n slugify README.md"},
                            "output": "12:slugify('Hello World') -> hello-world",
                            "exit_code": 0,
                        },
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=2,
            ac_content="Document slugify and truncate usage in README.md.",
            session_id="orch_123",
            tools=["Read", "Edit", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship string utilities",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.error is None
        assert result.typed_evidence is not None
        assert result.typed_evidence.data == {
            "files_touched": ["README.md"],
            "commands_run": ["grep -n slugify README.md"],
        }
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["required_fields"] == ["files_touched", "commands_run"]
        assert evidence_event.data["ignored_out_of_scope_evidence_fields"] == ["tests_passed"]
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_docs_only_ac_passes_consistent_profile_to_injected_verifier(
        self, tmp_path
    ) -> None:
        """Docs-only AC profile overrides must keep must_produce within required evidence."""
        readme = tmp_path / "README.md"
        readme.write_text("# String utils\n", encoding="utf-8")
        verifier_profiles: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
        verifier_records: list[dict[str, object]] = []

        def _recording_verifier(**kwargs: object) -> VerifierVerdict:
            profile = kwargs["profile"]
            record = kwargs["record"]
            verifier_profiles.append(
                (
                    tuple(profile.evidence_schema.required),  # type: ignore[attr-defined]
                    tuple(profile.must_produce),  # type: ignore[attr-defined]
                )
            )
            verifier_records.append(dict(record.data))  # type: ignore[attr-defined]
            return VerifierVerdict(passed=True)

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "```json\n"
                "{\n"
                '  "files_touched": ["README.md"],\n'
                '  "commands_run": ["grep -n slugify README.md"],\n'
                '  "tests_passed": ["test_slugify.py::test_slugify"]\n'
                "}\n"
                "```",
                native_session_id="codex-session-docs-only-injected-verifier",
                support_messages=(
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {readme}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(readme)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content="Calling tool: Bash: grep -n slugify README.md",
                        tool_name="Bash",
                        data={
                            "tool_input": {"command": "grep -n slugify README.md"},
                            "output": "12:slugify('Hello World') -> hello-world",
                            "exit_code": 0,
                        },
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            atomic_verifier=_recording_verifier,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=2,
            ac_content="Document slugify and truncate usage in README.md.",
            session_id="orch_123",
            tools=["Read", "Edit", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship string utilities",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert verifier_profiles == [(("files_touched", "commands_run"), ("files_touched",))]
        assert set(verifier_profiles[0][1]).issubset(verifier_profiles[0][0])
        assert verifier_records == [
            {
                "files_touched": ["README.md"],
                "commands_run": ["grep -n slugify README.md"],
            }
        ]
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["required_fields"] == ["files_touched", "commands_run"]
        assert evidence_event.data["ignored_out_of_scope_evidence_fields"] == ["tests_passed"]
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_sibling_context_marks_siblings_out_of_scope(self) -> None:
        """Fat-harness sibling context must be a boundary, not an invitation."""
        event_store, _ = _make_replaying_event_store()
        runtime = _FinalMessageRuntime(
            "```json\n"
            '{"files_touched":["string_utils.py","test_slugify.py"],'
            '"commands_run":["python -m pytest test_slugify.py"],'
            '"tests_passed":["python -m pytest test_slugify.py"]}'
            "\n```",
            native_session_id="opencode-session-scope-boundary",
            support_messages=(
                AgentMessage(
                    type="tool",
                    content="Write string_utils.py",
                    tool_name="Write",
                    data={"tool_input": {"file_path": "string_utils.py"}},
                ),
                AgentMessage(
                    type="tool",
                    content="Write test_slugify.py",
                    tool_name="Write",
                    data={"tool_input": {"file_path": "test_slugify.py"}},
                ),
                AgentMessage(
                    type="tool",
                    content="Bash: python -m pytest test_slugify.py",
                    tool_name="Bash",
                    data={"tool_input": {"command": "python -m pytest test_slugify.py"}},
                ),
                AgentMessage(
                    type="tool_result",
                    content="test_slugify.py passed\n1 passed in 0.01s",
                    data={"subtype": "tool_result"},
                ),
            ),
        )
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Create string_utils.py with slugify(text) and test_slugify.py.",
            session_id="orch_123",
            tools=["Read", "Write", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship string utilities",
            depth=0,
            start_time=datetime.now(UTC),
            sibling_acs=[
                (0, "Create string_utils.py with slugify(text) and test_slugify.py."),
                (1, "Add truncate(text, max_length) and test_truncate.py."),
                (2, "Document slugify and truncate usage in README.md."),
            ],
        )

        assert result.success is True
        assert runtime.last_prompt is not None
        assert "## Current AC Scope Boundary" in runtime.last_prompt
        assert "outside the current dispatch" in runtime.last_prompt
        assert "Do not satisfy those criteria now" in runtime.last_prompt
        assert "do not pre-create their files, tests, docs, or evidence" in runtime.last_prompt
        assert "Sibling/future ACs are summarized in the governed sibling-status" in (
            runtime.last_prompt
        )
        assert "as out-of-scope boundary context" in runtime.last_prompt
        assert "Sibling tasks in progress" not in runtime.last_prompt

    @pytest.mark.asyncio
    async def test_fat_harness_accepts_validation_evidence_after_code_fence(self) -> None:
        """Regression for #978 batch 2b: parser must skip earlier code fences."""
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "[AC_COMPLETE: 1]\n\n"
                "`hello.py` contains:\n\n"
                "```python\n"
                "def hello():\n"
                '    return "hello"\n'
                "```\n\n"
                "Validation evidence:\n\n"
                "```json\n"
                "{\n"
                '  "files_touched": ["hello.py", "test_hello.py"],\n'
                '  "commands_run": ["pytest test_hello.py"],\n'
                '  "tests_passed": ["test_hello.py::test_hello"]\n'
                "}\n"
                "```",
                native_session_id="opencode-session-evidence-code-fence",
                support_messages=(
                    AgentMessage(
                        type="tool",
                        content="Write: hello.py created",
                        tool_name="Write",
                        data={"tool_input": {"file_path": "hello.py"}},
                    ),
                    AgentMessage(
                        type="tool",
                        content="Write: test_hello.py created",
                        tool_name="Write",
                        data={"tool_input": {"file_path": "test_hello.py"}},
                    ),
                    AgentMessage(
                        type="tool",
                        content="Bash: pytest test_hello.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "pytest test_hello.py"}},
                    ),
                    AgentMessage(
                        type="tool_result",
                        content="test_hello.py::test_hello passed\n1 passed in 0.01s",
                        data={"subtype": "tool_result"},
                    ),
                ),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content='Create hello.py with hello() returning "hello".',
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.error is None
        assert result.typed_evidence is not None
        assert result.typed_evidence.data["files_touched"] == ["hello.py", "test_hello.py"]
        assert result.typed_evidence_validation is not None
        assert result.typed_evidence_validation.ok is True
        assert result.atomic_verifier_verdict is not None
        assert result.atomic_verifier_verdict.passed is True
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["typed_evidence_present"] is True
        assert evidence_event.data["typed_evidence_valid"] is True
        assert evidence_event.data["verifier_ran"] is True
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_accepts_codex_runtime_evidence_shape(
        self, tmp_path
    ) -> None:
        """Regression for #978 post-#1025: Codex emits abs paths and same-message output."""
        hello_file = tmp_path / "hello.py"
        test_file = tmp_path / "test_hello.py"
        hello_file.write_text('def hello():\n    return "hello"\n', encoding="utf-8")
        test_file.write_text(
            "from hello import hello\n\n"
            "def test_hello_returns_hello():\n"
            "    assert hello() == 'hello'\n",
            encoding="utf-8",
        )

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "```json\n"
                "{\n"
                '  "files_touched": ["hello.py", "test_hello.py"],\n'
                '  "commands_run": ["pytest"],\n'
                '  "tests_passed": ["test_hello.py::test_hello_returns_hello"]\n'
                "}\n"
                "```",
                native_session_id="codex-session-post-1025-observation",
                support_messages=(
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {hello_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(hello_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {test_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(test_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content="Calling tool: Bash: pytest",
                        tool_name="Bash",
                        data={
                            "tool_input": {"command": "pytest"},
                            "output": "1 passed in 0.01s",
                            "exit_code": 0,
                        },
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content='Create hello.py with hello() returning "hello".',
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.error is None
        assert result.atomic_verifier_verdict is not None
        assert result.atomic_verifier_verdict.passed is True
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["typed_evidence_present"] is True
        assert evidence_event.data["typed_evidence_valid"] is True
        assert evidence_event.data["verifier_ran"] is True
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_accepts_notebookedit_notebook_path(self, tmp_path) -> None:
        """NotebookEdit reports its target as notebook_path, not file_path."""
        notebook_file = tmp_path / "analysis.ipynb"
        notebook_file.write_text("{}\n", encoding="utf-8")
        test_file = tmp_path / "tests" / "test_analysis.py"
        test_file.parent.mkdir()
        test_file.write_text("def test_analysis():\n    assert True\n", encoding="utf-8")

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "```json\n"
                "{\n"
                '  "files_touched": ["analysis.ipynb"],\n'
                '  "commands_run": ["pytest tests/test_analysis.py"],\n'
                '  "tests_passed": ["tests/test_analysis.py"]\n'
                "}\n"
                "```",
                native_session_id="codex-session-notebook-path",
                support_messages=(
                    AgentMessage(
                        type="assistant",
                        content="Calling tool: NotebookEdit",
                        tool_name="NotebookEdit",
                        data={"tool_input": {"notebook_path": str(notebook_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content="Calling tool: Bash: pytest tests/test_analysis.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "pytest tests/test_analysis.py"}},
                    ),
                    AgentMessage(
                        type="tool_result",
                        content="tests/test_analysis.py passed; 1 passed",
                        data={"subtype": "tool_result"},
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Update notebook and tests.",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.error is None
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_rejects_bare_pytest_for_unmentioned_stale_test(
        self, tmp_path
    ) -> None:
        """A bare pytest success must not prove arbitrary existing test files."""
        generated_file = tmp_path / "src" / "generated.py"
        generated_file.parent.mkdir()
        generated_file.write_text("VALUE = 1\n", encoding="utf-8")
        stale_test = tmp_path / "tests" / "test_other.py"
        stale_test.parent.mkdir()
        stale_test.write_text("def test_other():\n    assert True\n", encoding="utf-8")

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "```json\n"
                "{\n"
                '  "files_touched": ["src/generated.py"],\n'
                '  "commands_run": ["pytest"],\n'
                '  "tests_passed": ["tests/test_other.py::test_other"]\n'
                "}\n"
                "```",
                native_session_id="codex-session-bare-pytest-unrelated-test",
                support_messages=(
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {generated_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(generated_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content="Calling tool: Bash: pytest",
                        tool_name="Bash",
                        data={
                            "tool_input": {"command": "pytest"},
                            "output": "1 passed in 0.01s",
                            "exit_code": 0,
                        },
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement generated module.",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert "tests_passed: tests/test_other.py::test_other" in result.error
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_passed"] is False

    @pytest.mark.asyncio
    async def test_fat_harness_rejects_command_result_wrapper_after_parsing_json_fence(
        self,
    ) -> None:
        """Actual #978 failing shape parses, then fails schema without verifier."""
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "[AC_COMPLETE: 1]\n\n"
                "```python\n"
                "def hello():\n"
                '    return "hello"\n'
                "```\n\n"
                "Validation evidence:\n\n"
                "```json\n"
                "{\n"
                '  "type": "command_result",\n'
                '  "command": "pytest test_hello.py",\n'
                '  "cwd": "/Users/jh0927/character-chat",\n'
                '  "exit_code": 0,\n'
                '  "result": "1 passed in 0.01s"\n'
                "}\n"
                "```",
                native_session_id="opencode-session-command-result-wrapper",
                support_messages=(
                    AgentMessage(
                        type="tool",
                        content="Bash: pytest test_hello.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "pytest test_hello.py"}},
                    ),
                    AgentMessage(
                        type="result",
                        content="1 passed in 0.01s",
                        data={"subtype": "success"},
                    ),
                ),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content='Create hello.py with hello() returning "hello".',
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.typed_evidence is not None
        assert result.typed_evidence.data["type"] == "command_result"
        assert result.typed_evidence_validation is not None
        assert result.typed_evidence_validation.ok is False
        assert result.typed_evidence_error is None
        assert "Fat-harness typed evidence validation failed" in (result.error or "")
        assert result.atomic_verifier_verdict is None
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["typed_evidence_present"] is True
        assert evidence_event.data["typed_evidence_valid"] is False
        assert evidence_event.data["typed_evidence_error"] is None
        assert evidence_event.data["missing_fields"] == [
            "files_touched",
            "commands_run",
            "tests_passed",
        ]
        assert evidence_event.data["verifier_ran"] is False

    @pytest.mark.asyncio
    async def test_contract_ac_with_artifacts_keeps_transcript_obligations(
        self,
        tmp_path,
    ) -> None:
        """A passing gate does not excuse malformed transcript evidence."""
        command = f"{sys.executable} -c \"print('OK')\""
        (tmp_path / "hello.py").write_text(
            "def greet(name):\n    return f'Hello, {name}'\n",
            encoding="utf-8",
        )
        event_store, appended_events = _make_replaying_event_store()
        runtime = _FinalMessageRuntime(
            "Done.\n"
            "```json\n"
            "{"
            '"files_touched":["not-backed-by-transcript.py"],'
            '"commands_run":0'
            "}\n"
            "```",
            native_session_id="codex-session-contract-artifact-delegation",
            support_messages=(
                AgentMessage(
                    type="assistant",
                    content=f"Calling tool: Bash: {command}",
                    tool_name="Bash",
                    data={"tool_input": {"command": command}},
                ),
            ),
            cwd=str(tmp_path),
        )
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content=_GREETING_AC,
            session_id="orch_123",
            tools=["Read", "Edit", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            ac_spec=AcceptanceCriterionSpec(
                description=_GREETING_AC,
                verify_command=command,
                expected_artifacts=("hello.py",),
            ),
        )

        assert result.success is False
        assert result.error is not None
        assert "missing fields: tests_passed" in result.error
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["required_fields"] == [
            "files_touched",
            "commands_run",
            "tests_passed",
        ]
        assert evidence_event.data["typed_evidence_valid"] is False

    @pytest.mark.asyncio
    async def test_contract_ac_missing_artifact_rejected_when_verify_gate_disabled(
        self,
        tmp_path,
    ) -> None:
        """Reproduced blocker: with the verify gate off, delegation must not fire.

        ``run_verify_commands=False`` makes ``_apply_verify_gate`` return early, so
        neither the filesystem oracle nor command exit status verifies the contract.
        If the schema still dropped ``commands_run``/``tests_passed``/``files_touched``,
        a contract AC could complete without transcript-backed evidence or an
        artifact on disk. The verify-gate-active guard keeps those fields required,
        so the worker's self-reported evidence fails and the AC is not accepted.
        """
        command = "python3 -c \"print('OK')\""
        command_json = command.replace("\\", "\\\\").replace('"', '\\"')
        # Deliberately do NOT write hello.py: the artifact is missing on disk.
        event_store, appended_events = _make_replaying_event_store()
        runtime = _FinalMessageRuntime(
            f'Done.\n```json\n{{"commands_run":["{command_json}"]}}\n```',
            native_session_id="codex-session-verify-gate-off-missing-artifact",
            support_messages=(
                AgentMessage(
                    type="assistant",
                    content=f"Calling tool: Bash: {command}",
                    tool_name="Bash",
                    data={"tool_input": {"command": command}},
                ),
            ),
            cwd=str(tmp_path),
        )
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            run_verify_commands=False,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content=_GREETING_AC,
            session_id="orch_123",
            tools=["Read", "Edit", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            ac_spec=AcceptanceCriterionSpec(
                description=_GREETING_AC,
                verify_command=command,
                expected_artifacts=("hello.py",),
            ),
        )

        assert result.success is False
        assert result.error is not None
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        # files_touched, commands_run, and tests_passed remain required because the gate is off.
        assert "files_touched" in evidence_event.data["required_fields"]
        assert "commands_run" in evidence_event.data["required_fields"]
        assert "tests_passed" in evidence_event.data["required_fields"]
        assert evidence_event.data["typed_evidence_valid"] is False

    @pytest.mark.asyncio
    async def test_fat_harness_preserves_work_when_transcript_is_missing(self) -> None:
        """Missing transcript infrastructure produces unverified success."""
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "[TASK_COMPLETE] no JSON evidence yet",
                native_session_id="opencode-session-no-evidence",
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.error is None
        assert result.atomic_verifier_verdict is not None
        assert result.atomic_verifier_verdict.status is VerifierStatus.UNAVAILABLE

        report = render_parallel_verification_report(
            ParallelExecutionResult(
                results=(result,),
                success_count=1,
                failure_count=0,
                total_messages=len(result.messages),
            ),
            total_acceptance_criteria=1,
        )
        assert "[COMPLETED]" in report
        assert "Succeeded without machine verification" in report

        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["observe_only"] is False
        assert evidence_event.data["enforced"] is False
        assert evidence_event.data["fat_harness_mode"] is True
        assert evidence_event.data["enforcement_error"] is None
        assert evidence_event.data["verifier_ran"] is True
        assert evidence_event.data["verifier_status"] == "UNAVAILABLE"

        terminal_event = next(
            event for event in appended_events if event.type == "execution.session.completed"
        )
        assert terminal_event.data["success"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_mode_accepts_valid_typed_evidence(self) -> None:
        """Valid profile evidence keeps the opt-in fat-harness leaf accepted."""
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                '{"files_touched":["src/app.py"],'
                '"commands_run":["pytest"],'
                '"tests_passed":["tests/test_app.py"]}\n'
                "```",
                native_session_id="opencode-session-evidence",
                support_messages=(
                    AgentMessage(
                        type="tool",
                        content="Edit: src/app.py",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": "src/app.py"}},
                    ),
                    AgentMessage(
                        type="tool",
                        content="Bash: pytest tests/test_app.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "pytest tests/test_app.py"}},
                    ),
                    AgentMessage(
                        type="tool_result",
                        content="tests/test_app.py passed\n1 passed in 0.01s",
                        data={"subtype": "tool_result"},
                    ),
                ),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.error is None
        assert result.atomic_verifier_verdict is not None
        assert result.atomic_verifier_verdict.passed is True
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["observe_only"] is False
        assert evidence_event.data["enforced"] is True
        assert evidence_event.data["fat_harness_mode"] is True
        assert evidence_event.data["enforcement_error"] is None
        assert evidence_event.data["typed_evidence_valid"] is True
        assert evidence_event.data["verifier_ran"] is True
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_validation_only_ac_accepts_no_files_touched(
        self,
    ) -> None:
        """Validation-only ACs can pass with command and test evidence only."""
        event_store, appended_events = _make_replaying_event_store()
        runtime = _FinalMessageRuntime(
            "Done.\n"
            "```json\n"
            '{"commands_run":["python -m unittest test_todo.py"],'
            '"tests_passed":["python -m unittest test_todo.py"]}\n'
            "```",
            native_session_id="opencode-session-validation-only",
            support_messages=(
                AgentMessage(
                    type="tool",
                    content="Bash: python -m unittest test_todo.py",
                    tool_name="Bash",
                    data={"tool_input": {"command": "python -m unittest test_todo.py"}},
                ),
                AgentMessage(
                    type="tool_result",
                    content="Ran 6 tests in 0.002s\n\nOK",
                    data={"subtype": "tool_result", "exit_code": 0},
                ),
            ),
        )
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Run python -m unittest test_todo.py successfully.",
            session_id="orch_123",
            tools=["Read", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.error is None
        assert runtime.last_prompt is not None
        assert "validation-only current AC" in runtime.last_prompt
        assert "Do not include files_touched unless you actually edited" in runtime.last_prompt
        assert "Read-only inspection or running tests does not count as files_touched" in (
            runtime.last_prompt
        )
        assert "commands_run, tests_passed" in runtime.last_prompt
        assert "files_touched, commands_run, tests_passed" not in runtime.last_prompt
        assert result.typed_evidence is not None
        assert result.typed_evidence.data == {
            "commands_run": ["python -m unittest test_todo.py"],
            "tests_passed": ["python -m unittest test_todo.py"],
        }
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["required_fields"] == ["commands_run", "tests_passed"]
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_validation_only_ac_ignores_files_touched_overclaim(
        self,
    ) -> None:
        """Validation-only ACs record but ignore extra files_touched overclaims."""
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                '{"files_touched":["test_todo.py"],'
                '"commands_run":["python -m unittest test_todo.py"],'
                '"tests_passed":["python -m unittest test_todo.py"]}\n'
                "```",
                native_session_id="opencode-session-validation-only-overclaim",
                support_messages=(
                    AgentMessage(
                        type="tool",
                        content="Bash: sed -n '1,240p' test_todo.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "sed -n '1,240p' test_todo.py"}},
                    ),
                    AgentMessage(
                        type="tool",
                        content="Bash: python -m unittest test_todo.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "python -m unittest test_todo.py"}},
                    ),
                    AgentMessage(
                        type="tool_result",
                        content="Ran 6 tests in 0.002s\n\nOK",
                        data={"subtype": "tool_result", "exit_code": 0},
                    ),
                ),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Run python -m unittest test_todo.py successfully.",
            session_id="orch_123",
            tools=["Read", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.error is None
        assert result.typed_evidence is not None
        assert result.typed_evidence.data == {
            "commands_run": ["python -m unittest test_todo.py"],
            "tests_passed": ["python -m unittest test_todo.py"],
        }
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["required_fields"] == ["commands_run", "tests_passed"]
        assert evidence_event.data["ignored_out_of_scope_evidence_fields"] == ["files_touched"]
        assert evidence_event.data["ignored_out_of_scope_evidence"] == {
            "files_touched": ["test_todo.py"]
        }
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_test_writing_ac_still_requires_files_touched(
        self,
    ) -> None:
        """Test-writing ACs must still prove file mutation."""
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                '{"commands_run":["python -m unittest test_todo.py"],'
                '"tests_passed":["python -m unittest test_todo.py"]}\n'
                "```",
                native_session_id="opencode-session-test-writing-missing-file",
                support_messages=(
                    AgentMessage(
                        type="tool",
                        content="Bash: python -m unittest test_todo.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "python -m unittest test_todo.py"}},
                    ),
                    AgentMessage(
                        type="tool_result",
                        content="Ran 6 tests in 0.002s\n\nOK",
                        data={"subtype": "tool_result", "exit_code": 0},
                    ),
                ),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Create test_todo.py with unittest coverage.",
            session_id="orch_123",
            tools=["Read", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert "missing fields: files_touched" in result.error
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["required_fields"] == [
            "files_touched",
            "commands_run",
            "tests_passed",
        ]
        assert "files_touched" in evidence_event.data["missing_fields"]

    @pytest.mark.asyncio
    async def test_fat_harness_marks_final_only_evidence_unverified(self) -> None:
        """Final-only evidence is preserved as success but remains unverified."""
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                '{"files_touched":["src/app.py"],'
                '"commands_run":["pytest"],'
                '"tests_passed":["tests/test_app.py"]}\n'
                "```",
                native_session_id="opencode-session-evidence",
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.error is None
        assert result.atomic_verifier_verdict is not None
        assert result.atomic_verifier_verdict.status is VerifierStatus.UNAVAILABLE
        assert (
            result.atomic_verifier_verdict.failure_class
            == FailureClass.TRANSCRIPT_MISSING_INFRASTRUCTURE.value
        )

        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["typed_evidence_valid"] is True
        assert evidence_event.data["verifier_ran"] is True
        assert evidence_event.data["verifier_passed"] is False
        assert (
            evidence_event.data["verifier_failure_class"]
            == FailureClass.TRANSCRIPT_MISSING_INFRASTRUCTURE.value
        )

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_allows_bash_generated_file_and_whole_suite_test(
        self, tmp_path
    ) -> None:
        """Bash-backed generation plus whole-suite pytest can support evidence."""
        generated_file = tmp_path / "src" / "generated.py"
        generated_file.parent.mkdir()
        generated_file.write_text("VALUE = 1\n", encoding="utf-8")
        generated_test = tmp_path / "tests" / "test_generated.py"
        generated_test.parent.mkdir()
        generated_test.write_text("def test_generated():\n    assert True\n", encoding="utf-8")

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                '{"files_touched":["src/generated.py"],'
                '"commands_run":["python scripts/generate.py","pytest"],'
                '"tests_passed":["tests/test_generated.py"]}\n'
                "```",
                native_session_id="opencode-session-evidence",
                support_messages=(
                    AgentMessage(
                        type="tool",
                        content="Bash: python scripts/generate.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "python scripts/generate.py"}},
                    ),
                    AgentMessage(
                        type="tool",
                        content="Bash: pytest",
                        tool_name="Bash",
                        data={"tool_input": {"command": "pytest"}},
                    ),
                    AgentMessage(
                        type="tool_result",
                        content=(
                            "generated.py updated; tests/test_generated.py passed; "
                            "0 failed, 0 errors, 1 passed"
                        ),
                        data={"subtype": "tool_result"},
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.error is None
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_ran"] is True
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_normalizes_workspace_absolute_file_claim(
        self, tmp_path
    ) -> None:
        """Absolute files_touched claims under task_cwd are normalized before matching."""
        touched_file = tmp_path / "test_todo.py"
        touched_file.write_text("import unittest\n", encoding="utf-8")

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                f'{{"files_touched":["{touched_file}"],'
                '"commands_run":["python -m unittest test_todo.py"],'
                '"tests_passed":["python -m unittest test_todo.py"]}\n'
                "```",
                native_session_id="opencode-session-evidence",
                support_messages=(
                    AgentMessage(
                        type="tool",
                        content=f"Edit {touched_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(touched_file)}},
                    ),
                    AgentMessage(
                        type="tool",
                        content="Bash: python -m unittest test_todo.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "python -m unittest test_todo.py"}},
                    ),
                    AgentMessage(
                        type="tool_result",
                        content="Ran 1 test in 0.001s\n\nOK",
                        data={"subtype": "tool_result", "exit_code": 0},
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.error is None
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_ran"] is True
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_rejects_unscoped_file_and_failed_test_command(
        self, tmp_path
    ) -> None:
        """Workspace path scope and test success are required for verifier support."""
        outside_file = tmp_path.parent / "outside.py"
        outside_file.write_text("VALUE = 1\n", encoding="utf-8")
        test_file = tmp_path / "tests" / "test_generated.py"
        test_file.parent.mkdir()
        test_file.write_text("def test_generated():\n    assert False\n", encoding="utf-8")

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                f'{{"files_touched":["{outside_file}"],'
                '"commands_run":["pytest"],'
                '"tests_passed":["tests/test_generated.py"]}}\n'
                "```",
                native_session_id="opencode-session-evidence",
                support_messages=(
                    AgentMessage(
                        type="tool",
                        content="Bash: pytest",
                        tool_name="Bash",
                        data={"tool_input": {"command": "pytest"}},
                    ),
                    AgentMessage(
                        type="result",
                        content="1 failed, 3 passed",
                        data={"subtype": "success"},
                    ),
                ),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert "files_touched:" in result.error
        assert "tests_passed: tests/test_generated.py" in result.error
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_ran"] is True
        assert evidence_event.data["verifier_passed"] is False

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_rejects_preexisting_file_without_transcript_support(
        self, tmp_path
    ) -> None:
        """A stale workspace file must not prove this run touched that file."""
        preexisting_file = tmp_path / "src" / "preexisting.py"
        preexisting_file.parent.mkdir()
        preexisting_file.write_text("VALUE = 1\n", encoding="utf-8")
        test_file = tmp_path / "tests" / "test_preexisting.py"
        test_file.parent.mkdir()
        test_file.write_text("def test_preexisting():\n    assert True\n", encoding="utf-8")

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                '{"files_touched":["src/preexisting.py"],'
                '"commands_run":["pytest tests/test_preexisting.py"],'
                '"tests_passed":["tests/test_preexisting.py"]}\n'
                "```",
                native_session_id="opencode-session-evidence",
                support_messages=(
                    AgentMessage(
                        type="result",
                        content="Read src/preexisting.py for context only.",
                        data={"subtype": "success"},
                    ),
                    AgentMessage(
                        type="tool",
                        content="Bash: pytest tests/test_preexisting.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "pytest tests/test_preexisting.py"}},
                    ),
                    AgentMessage(
                        type="result",
                        content="tests/test_preexisting.py passed",
                        data={"subtype": "success"},
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert "files_touched: src/preexisting.py" in result.error
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_ran"] is True
        assert evidence_event.data["verifier_passed"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("read_only_command", "claimed_command"),
        (
            pytest.param(
                "cat src/preexisting.py",
                "pytest tests/test_preexisting.py",
                id="read-only-file-reference",
            ),
            pytest.param(
                "grep updated src/preexisting.py",
                "grep updated src/preexisting.py",
                id="read-only-command-with-write-word",
            ),
        ),
    )
    async def test_fat_harness_verifier_rejects_read_only_file_reference(
        self, tmp_path, read_only_command: str, claimed_command: str
    ) -> None:
        """Read-only command text is not files_touched proof, mutation words included."""
        preexisting_file = tmp_path / "src" / "preexisting.py"
        preexisting_file.parent.mkdir()
        preexisting_file.write_text("VALUE = 1\n", encoding="utf-8")

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                '{"files_touched":["src/preexisting.py"],'
                f'"commands_run":["{claimed_command}"],'
                '"tests_passed":["tests/test_preexisting.py"]}\n'
                "```",
                native_session_id="opencode-session-evidence",
                support_messages=(
                    AgentMessage(
                        type="tool",
                        content=f"Bash: {read_only_command}",
                        tool_name="Bash",
                        data={"tool_input": {"command": read_only_command}},
                    ),
                    AgentMessage(
                        type="tool",
                        content="Bash: pytest tests/test_preexisting.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "pytest tests/test_preexisting.py"}},
                    ),
                    AgentMessage(
                        type="result",
                        content="tests/test_preexisting.py passed",
                        data={"subtype": "success"},
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert "files_touched: src/preexisting.py" in result.error
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_passed"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command",
        (
            "touch src/generated.py",
            "printf 'VALUE = 1' > src/generated.py",
            "truncate -s 0 src/generated.py",
        ),
    )
    async def test_fat_harness_verifier_allows_explicit_bash_file_mutation_without_output(
        self, tmp_path, command
    ) -> None:
        """Explicit shell writes can prove files_touched even without path-specific output."""
        generated_file = tmp_path / "src" / "generated.py"
        generated_file.parent.mkdir()
        generated_file.write_text("VALUE = 1\n", encoding="utf-8")
        os.utime(generated_file, ns=(1_000_000_000, 1_000_000_000))

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                f'{{"files_touched":["src/generated.py"],'
                f'"commands_run":["{command}","pytest tests/test_generated.py"],'
                '"tests_passed":["tests/test_generated.py"]}\n'
                "```",
                native_session_id="opencode-session-evidence",
                support_messages=(
                    AgentMessage(
                        type="tool",
                        content=f"Bash: {command}",
                        tool_name="Bash",
                        data={"tool_input": {"command": command}},
                    ),
                    AgentMessage(
                        type="tool_result",
                        content="command completed with exit code 0",
                        data={
                            "subtype": "tool_result",
                            "exit_code": 0,
                        },
                    ),
                    AgentMessage(
                        type="tool",
                        content="Bash: pytest tests/test_generated.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "pytest tests/test_generated.py"}},
                    ),
                    AgentMessage(
                        type="tool_result",
                        content="tests/test_generated.py passed\n1 passed in 0.01s",
                        data={"subtype": "tool_result", "is_error": False},
                    ),
                ),
                execute_support_commands=(command,),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.error is None
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_rejects_bash_mutation_of_different_file(
        self, tmp_path
    ) -> None:
        """A mutating Bash pipeline must not prove a separately read file was touched."""
        preexisting_file = tmp_path / "src" / "preexisting.py"
        generated_file = tmp_path / "src" / "generated.py"
        preexisting_file.parent.mkdir()
        preexisting_file.write_text("VALUE = 1\n", encoding="utf-8")
        generated_file.write_text("VALUE = 2\n", encoding="utf-8")

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                '{"files_touched":["src/preexisting.py"],'
                '"commands_run":["cat src/preexisting.py | tee src/generated.py",'
                '"pytest tests/test_generated.py"],'
                '"tests_passed":["tests/test_generated.py"]}\n'
                "```",
                native_session_id="opencode-session-evidence",
                support_messages=(
                    AgentMessage(
                        type="tool",
                        content="Bash: cat src/preexisting.py | tee src/generated.py",
                        tool_name="Bash",
                        data={
                            "tool_input": {
                                "command": "cat src/preexisting.py | tee src/generated.py"
                            }
                        },
                    ),
                    AgentMessage(
                        type="tool",
                        content="Bash: pytest tests/test_generated.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "pytest tests/test_generated.py"}},
                    ),
                    AgentMessage(
                        type="result",
                        content="tests/test_generated.py passed\n1 passed in 0.01s",
                        data={"subtype": "success"},
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert "files_touched: src/preexisting.py" in result.error
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_passed"] is False

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_rejects_failed_explicit_bash_file_mutation(
        self, tmp_path
    ) -> None:
        """An explicit shell write command must also have a successful result."""
        generated_file = tmp_path / "src" / "generated.py"
        generated_file.parent.mkdir()

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                '{"files_touched":["src/generated.py"],'
                '"commands_run":["touch src/generated.py","pytest tests/test_generated.py"],'
                '"tests_passed":["tests/test_generated.py"]}\n'
                "```",
                native_session_id="opencode-session-evidence",
                support_messages=(
                    AgentMessage(
                        type="tool",
                        content="Bash: touch src/generated.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "touch src/generated.py"}},
                    ),
                    AgentMessage(
                        type="result",
                        content="touch: src/generated.py: permission denied",
                        data={"subtype": "error", "exit_code": 1},
                    ),
                    AgentMessage(
                        type="tool",
                        content="Bash: pytest tests/test_generated.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "pytest tests/test_generated.py"}},
                    ),
                    AgentMessage(
                        type="result",
                        content="tests/test_generated.py passed\n1 passed in 0.01s",
                        data={"subtype": "success"},
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert "files_touched: src/generated.py" in result.error
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_passed"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("prepare_case", _PATHLIB_FILE_CLAIM_REJECTION_CASES)
    async def test_fat_harness_verifier_rejects_unproven_pathlib_file_claim(
        self, tmp_path, prepare_case
    ) -> None:
        """Only an execution-bound, receiver-matching pathlib write proves a file claim.

        Command text alone, equivalent spellings, unexecuted or unbound syntax,
        literal-space identity, writes to a different file, shell-expanded
        sources, untrusted interpreters, a workspace ``pathlib.py`` shadow, and
        out-of-workspace receivers all fail closed.
        """
        command, claimed_path = prepare_case(tmp_path)
        evidence_json = json.dumps(
            {
                "files_touched": [claimed_path],
                "commands_run": [command, "pytest tests/test_generated.py"],
                "tests_passed": ["tests/test_generated.py"],
            }
        )

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                f"Done.\n```json\n{evidence_json}\n```",
                native_session_id="opencode-session-evidence",
                support_messages=(
                    AgentMessage(
                        type="tool",
                        content=f"Bash: {command}",
                        tool_name="Bash",
                        data={"tool_input": {"command": command}},
                    ),
                    AgentMessage(
                        type="tool_result",
                        content="command completed with exit code 0",
                        data={"subtype": "tool_result", "exit_code": 0},
                    ),
                    AgentMessage(
                        type="tool",
                        content="Bash: pytest tests/test_generated.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "pytest tests/test_generated.py"}},
                    ),
                    AgentMessage(
                        type="tool_result",
                        content="tests/test_generated.py passed\n1 passed in 0.01s",
                        data={"subtype": "tool_result", "is_error": False},
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert f"files_touched: {claimed_path}" in result.error
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_passed"] is False

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_rejects_bash_command_basename_fallback(
        self, tmp_path
    ) -> None:
        """Bash command-text proof must not use basename fallback for another path."""
        generated_file = tmp_path / "src" / "generated.py"
        generated_file.parent.mkdir()
        generated_file.write_text("VALUE = 1\n", encoding="utf-8")

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                '{"files_touched":["src/generated.py"],'
                '"commands_run":["touch generated.py","pytest tests/test_generated.py"],'
                '"tests_passed":["tests/test_generated.py"]}\n'
                "```",
                native_session_id="opencode-session-evidence",
                support_messages=(
                    AgentMessage(
                        type="tool",
                        content="Bash: touch generated.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "touch generated.py"}},
                    ),
                    AgentMessage(
                        type="result",
                        content="command completed with exit code 0",
                        data={"subtype": "success", "exit_code": 0},
                    ),
                    AgentMessage(
                        type="tool",
                        content="Bash: pytest tests/test_generated.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "pytest tests/test_generated.py"}},
                    ),
                    AgentMessage(
                        type="result",
                        content="tests/test_generated.py passed\n1 passed in 0.01s",
                        data={"subtype": "success"},
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert "files_touched: src/generated.py" in result.error
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_passed"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tests_pass", [True, False])
    async def test_fat_harness_exit_code_only_test_output_is_settled_by_harness_reexecution(
        self, tmp_path, tests_pass: bool
    ) -> None:
        """A zero exit without execution output cannot prove that tests ran.

        The transcript alone is still insufficient; the harness re-runs the
        claimed test command in the workspace and the verdict follows what
        that run actually produced.
        """
        hello_file = tmp_path / "hello.py"
        test_file = tmp_path / "test_hello.py"
        hello_file.write_text('def hello():\n    return "hello"\n', encoding="utf-8")
        expected = "'hello'" if tests_pass else "'goodbye'"
        test_file.write_text(
            "from hello import hello\n\n"
            "def test_hello_returns_hello():\n"
            f"    assert hello() == {expected}\n",
            encoding="utf-8",
        )

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "```json\n"
                "{\n"
                '  "files_touched": ["hello.py", "test_hello.py"],\n'
                '  "commands_run": ["python -m pytest test_hello.py"],\n'
                '  "tests_passed": ["test_hello.py::test_hello_returns_hello"]\n'
                "}\n"
                "```",
                native_session_id="codex-session-exit-code-only-pytest",
                support_messages=(
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {hello_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(hello_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {test_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(test_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content="Calling tool: Bash: /bin/zsh -lc 'python -m pytest test_hello.py'",
                        tool_name="Bash",
                        data={
                            "tool_input": {
                                "command": "/bin/zsh -lc 'python -m pytest test_hello.py'"
                            },
                            "exit_code": 0,
                        },
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content='Create hello.py with hello() returning "hello".',
            session_id="orch_123",
            tools=["Read", "Bash"],
            tool_catalog=(
                MCPToolDefinition(name="Read", description="Read a file."),
                MCPToolDefinition(name="Bash", description="Run a shell command."),
            ),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is tests_pass
        assert result.atomic_verifier_verdict is not None
        assert result.atomic_verifier_verdict.passed is tests_pass
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_passed"] is tests_pass

    @pytest.mark.parametrize(
        "command",
        (
            "uv run --python 3.12 --with pytest pytest -q",
            "uv run --fork-strategy fewest --with pytest pytest -q",
            "uv run --no-sources-package foo pytest -q",
            "uv run --upgrade-group foo pytest -q",
            "uv run -qn pytest -q",
            "uv run -p3.12 pytest -q",
            "uv run -m pytest -q",
            "uv run --python 3.12 --with-requirements requirements.txt "
            "--with pytest python -m pytest -q",
            "uvx --python 3.12 --from pytest --with-requirements requirements.txt pytest -q",
        ),
    )
    def test_option_bearing_uv_pytest_is_candidate_evidence(self, command: str) -> None:
        assert _looks_like_test_command(command) is True

    @pytest.mark.parametrize(
        "command",
        (
            "uv run --no-project -s pytest",
            "uv run --script pytest",
            "uv run --gui-script pytest",
        ),
    )
    def test_uv_script_modes_are_not_test_evidence(self, command: str) -> None:
        assert _looks_like_test_command(command) is False

    def test_uv_dependency_named_pytest_does_not_become_program_identity(self) -> None:
        command = "uv run --with pytest python -c \"print('2 passed in 0.01s')\""
        assert _looks_like_test_command(command) is False

    @pytest.mark.parametrize("command", ("uv run pytest -s -q", "uv run -- pytest -s -q"))
    def test_pytest_s_argument_is_not_uv_script_mode(self, command: str) -> None:
        assert _looks_like_test_command(command) is True

    @pytest.mark.parametrize(
        "command",
        (
            "uv run --with --isolated pytest -q",
            "uv run --color -- pytest -q",
        ),
    )
    def test_uv_missing_option_values_fail_closed(self, command: str) -> None:
        assert _looks_like_test_command(command) is False

    @pytest.mark.parametrize(
        "command",
        (
            "uv run -p --isolated pytest -q",
            "uv run -w --isolated pytest -q",
        ),
    )
    def test_uv_missing_short_option_values_fail_closed(self, command: str) -> None:
        assert _looks_like_test_command(command) is False

    @pytest.mark.parametrize(
        "command",
        (
            "uv run --python 3.12 pytest --collect-only",
            "uv run -m pytest --setup-only",
        ),
    )
    def test_uv_non_executing_pytest_modes_are_not_evidence(self, command: str) -> None:
        assert _looks_like_test_command(command) is False

    def test_uv_preview_features_value_is_test_evidence(self) -> None:
        command = "uv run --preview-features target-workspace-discovery pytest -q"
        assert _looks_like_test_command(command) is True

    @pytest.mark.parametrize("option", ("--color=", "--directory="))
    def test_uv_empty_attached_option_values_fail_closed(self, option: str) -> None:
        assert _looks_like_test_command(f"uv run {option} pytest -q") is False

    @pytest.mark.parametrize(
        ("command", "expected"),
        (
            ("uv run --preview-features target-workspace-discovery pytest -q", True),
            ("uv run --color= pytest -q", False),
            ("uv run --directory= pytest -q", False),
        ),
    )
    def test_uv_long_option_operands_gate_end_to_end_claims(
        self,
        command: str,
        expected: bool,
    ) -> None:
        message = AgentMessage(
            type="assistant",
            content=f"Calling tool: Bash: {command}",
            tool_name="Bash",
            data={
                "tool_input": {"command": command},
                "output": "2 passed in 0.01s",
                "exit_code": 0,
            },
        )
        assert (
            _runtime_messages_support_test_claim(
                value=command,
                backed_commands=(command,),
                messages=(message,),
                task_cwd="/tmp",
            )
            is expected
        )

    def test_compound_uv_command_cannot_back_standalone_pytest_claim(self) -> None:
        command = "uv run --with pytest pytest -q && python scripts/postprocess.py"
        claim = "uv run --with pytest pytest -q"
        message = AgentMessage(
            type="assistant",
            content=f"Calling tool: Bash: {command}",
            tool_name="Bash",
            data={
                "tool_input": {"command": command},
                "output": "2 passed in 0.01s",
                "exit_code": 0,
            },
        )
        assert (
            _runtime_messages_support_test_claim(
                value=claim,
                backed_commands=(command,),
                messages=(message,),
                task_cwd="/tmp",
            )
            is False
        )

    @pytest.mark.parametrize("shell", ("bash", "/bin/zsh"))
    def test_wrapped_compound_uv_command_cannot_back_standalone_claim(self, shell: str) -> None:
        inner = "uv run pytest -q && python scripts/postprocess.py"
        command = f"{shell} -lc '{inner}'"
        claim = "uv run pytest -q"
        message = AgentMessage(
            type="assistant",
            content=f"Calling tool: Bash: {command}",
            tool_name="Bash",
            data={
                "tool_input": {"command": command},
                "output": "2 passed in 0.01s",
                "exit_code": 0,
            },
        )
        assert (
            _runtime_messages_support_test_claim(
                value=claim,
                backed_commands=(command,),
                messages=(message,),
                task_cwd="/tmp",
            )
            is False
        )

    @pytest.mark.parametrize(
        ("command", "expected"),
        (
            ("uv run -V pytest", False),
            ("uv run -C foo=bar pytest -q", True),
            ("uv run -U pytest -q", True),
        ),
    )
    def test_uv_short_options_preserve_case(self, command: str, expected: bool) -> None:
        assert _looks_like_test_command(command) is expected

    def test_uv_version_mode_cannot_back_tests_passed_claim(self) -> None:
        command = "uv run -V pytest"
        message = AgentMessage(
            type="assistant",
            content=f"Calling tool: Bash: {command}",
            tool_name="Bash",
            data={
                "tool_input": {"command": command},
                "output": "2 passed in 0.01s",
                "exit_code": 0,
            },
        )
        assert (
            _runtime_messages_support_test_claim(
                value=command,
                backed_commands=(command,),
                messages=(message,),
                task_cwd="/tmp",
            )
            is False
        )

    @pytest.mark.parametrize(
        "command",
        (
            "uv run --quiet pytest -q",
            "uv run --verbose pytest -q",
            "uv run --system-certs pytest -q",
        ),
    )
    def test_uv_current_global_flags_remain_test_evidence(self, command: str) -> None:
        assert _looks_like_test_command(command) is True

    @pytest.mark.parametrize(
        "command",
        (
            "uv run --python 3.12 --with pytest pytest -q",
            "uv run --python 3.12 --with-requirements requirements.txt "
            "--with pytest python -m pytest -q",
            "uvx --python 3.12 --from pytest --with-requirements requirements.txt pytest -q",
        ),
    )
    def test_option_bearing_uv_command_backs_its_exact_tests_passed_claim(
        self, command: str
    ) -> None:
        message = AgentMessage(
            type="assistant",
            content=f"Calling tool: Bash: {command}",
            tool_name="Bash",
            data={
                "tool_input": {"command": command},
                "output": "2 passed in 0.01s",
                "exit_code": 0,
            },
        )

        assert (
            _runtime_messages_support_test_claim(
                value=command,
                backed_commands=(command,),
                messages=(message,),
                task_cwd="/tmp",
            )
            is True
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("build_case", _ACCEPTED_UNITTEST_CLAIM_CASES)
    async def test_fat_harness_verifier_accepts_backed_unittest_claim(
        self, tmp_path, build_case
    ) -> None:
        """Real Bash unittest output backs concise unittest ``tests_passed`` claims.

        Regression for #961: Codex may put the unittest command plus an ``OK``
        summary (or a bare ``OK``) in ``tests_passed``, and may run the command
        inside a shell wrapper whose preamble does setup — ``cd`` or ``export``
        — before the claimed inner command.
        """
        case = build_case(tmp_path)
        source_file = tmp_path / "string_utils.py"
        test_file = tmp_path / "test_slugify.py"
        source_file.write_text(
            "def slugify(text):\n    return text.lower().replace(' ', '-')\n",
            encoding="utf-8",
        )
        test_file.write_text(
            "import unittest\n\n"
            "from string_utils import slugify\n\n"
            "class SlugifyTest(unittest.TestCase):\n"
            f"{case.test_methods}"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n",
            encoding="utf-8",
        )

        escaped_commands_run = case.commands_run.replace('"', '\\"')
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "```json\n"
                "{\n"
                '  "files_touched": ["string_utils.py", "test_slugify.py"],\n'
                f'  "commands_run": ["{escaped_commands_run}"],\n'
                f'  "tests_passed": ["{case.tests_passed}"]\n'
                "}\n"
                "```",
                native_session_id=case.native_session_id,
                support_messages=(
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {source_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(source_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {test_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(test_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Bash: {case.runtime_command}",
                        tool_name="Bash",
                        data={
                            "tool_input": {"command": case.runtime_command},
                            "output": case.runtime_output,
                            **case.extra_tool_data,
                        },
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Create slugify and unittest coverage.",
            session_id="orch_123",
            tools=["Read", "Edit", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship string utilities",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.error is None
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_accepts_pytest_node_id_claim_backed_by_transcript_command(
        self, tmp_path
    ) -> None:
        """A transcript ``pytest <file>`` run backs node-id ``tests_passed`` claims.

        Regression: candidate test commands were sourced only from
        ``commands_run`` evidence. When the agent listed lint in
        ``commands_run`` but ran ``pytest`` (recorded in the transcript) without
        echoing it into ``commands_run``, every node-id ``tests_passed`` claim
        was rejected as FABRICATION_SUSPECTED even though the run is real and
        green. The Bash message's own command is now also a candidate, so a
        transcript-proven test run supports the claim.
        """
        source_file = tmp_path / "string_utils.py"
        test_file = tmp_path / "test_slugify.py"
        source_file.write_text(
            "def slugify(text):\n    return text.lower().replace(' ', '-')\n",
            encoding="utf-8",
        )
        test_file.write_text(
            "from string_utils import slugify\n\n"
            "def test_spaces():\n"
            "    assert slugify('Hello World') == 'hello-world'\n",
            encoding="utf-8",
        )

        lint_command = "python -m ruff check string_utils.py test_slugify.py"
        pytest_command = "python -m pytest test_slugify.py -q"
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "```json\n"
                "{\n"
                '  "files_touched": ["string_utils.py", "test_slugify.py"],\n'
                f'  "commands_run": ["{lint_command}"],\n'
                '  "tests_passed": ["test_slugify.py::test_spaces"]\n'
                "}\n"
                "```",
                native_session_id="session-pytest-node-id-transcript-only",
                support_messages=(
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {source_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(source_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {test_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(test_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Bash: {lint_command}",
                        tool_name="Bash",
                        data={
                            "tool_input": {"command": lint_command},
                            "output": "All checks passed!",
                            "exit_code": 0,
                        },
                    ),
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Bash: {pytest_command}",
                        tool_name="Bash",
                        data={
                            "tool_input": {"command": pytest_command},
                            "output": "1 passed in 0.01s",
                            "exit_code": 0,
                        },
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Create slugify and pytest coverage.",
            session_id="orch_123",
            tools=["Read", "Edit", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship string utilities",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.error is None
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_rejects_node_id_claim_backed_only_by_non_test_command(
        self, tmp_path
    ) -> None:
        """The new candidate source must not let a non-test command back a test claim.

        Guards the message-command candidate path added for node-id ``tests_passed``
        support: a Bash message whose command merely prints a fake success line
        (``cat fake_results.txt`` whose output is ``test_x.py::test_y passed``) is
        not a test command, so ``_looks_like_test_command`` must exclude it and the
        node-id claim stays unsupported (FABRICATION_SUSPECTED) — even though the
        recorded output literally contains the node-id-plus-"passed" marker.
        """
        source_file = tmp_path / "string_utils.py"
        source_file.write_text("def slugify(text):\n    return text\n", encoding="utf-8")

        fake_command = "cat fake_results.txt"
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "```json\n"
                "{\n"
                '  "files_touched": ["string_utils.py"],\n'
                f'  "commands_run": ["{fake_command}"],\n'
                '  "tests_passed": ["test_slugify.py::test_spaces"]\n'
                "}\n"
                "```",
                native_session_id="session-node-id-non-test-command-only",
                support_messages=(
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {source_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(source_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Bash: {fake_command}",
                        tool_name="Bash",
                        data={
                            "tool_input": {"command": fake_command},
                            "output": "test_slugify.py::test_spaces passed",
                            "exit_code": 0,
                        },
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Create slugify and pytest coverage.",
            session_id="orch_123",
            tools=["Read", "Edit", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship string utilities",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert "tests_passed:" in result.error
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_failure_class"] == "FABRICATION_SUSPECTED"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("runtime_command", "native_session_id"),
        (
            pytest.param(
                "python -m unittest test_slugify.py",
                "codex-session-unittest-invented-summary",
                id="bare-unittest-command",
            ),
            pytest.param(
                "/bin/zsh -lc 'python -m unittest \"test_slugify.py\"'",
                "codex-session-shell-wrapped-unittest-invented-summary",
                id="shell-wrapped-unittest-command",
            ),
        ),
    )
    async def test_fat_harness_verifier_rejects_unittest_summary_missing_from_runtime(
        self, tmp_path, runtime_command: str, native_session_id: str
    ) -> None:
        """A tests_passed summary must be backed by runtime output, not assistant prose."""
        source_file = tmp_path / "string_utils.py"
        test_file = tmp_path / "test_slugify.py"
        source_file.write_text("def slugify(text):\n    return text\n", encoding="utf-8")
        test_file.write_text("import unittest\n", encoding="utf-8")

        escaped_command = runtime_command.replace('"', '\\"')
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "```json\n"
                "{\n"
                '  "files_touched": ["string_utils.py", "test_slugify.py"],\n'
                f'  "commands_run": ["{escaped_command}"],\n'
                '  "tests_passed": ['
                '"python -m unittest test_slugify.py: Ran 4 tests in 0.000s OK"'
                "]\n"
                "}\n"
                "```",
                native_session_id=native_session_id,
                support_messages=(
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {source_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(source_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {test_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(test_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Bash: {runtime_command}",
                        tool_name="Bash",
                        data={"tool_input": {"command": runtime_command}, "exit_code": 0},
                    ),
                    AgentMessage(
                        type="assistant",
                        content=(
                            "Tests passed: python -m unittest test_slugify.py: "
                            "Ran 4 tests in 0.000s OK"
                        ),
                        data={},
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Create slugify and unittest coverage.",
            session_id="orch_123",
            tools=["Read", "Edit", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship string utilities",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert "tests_passed:" in result.error
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_failure_class"] == "FABRICATION_SUSPECTED"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", _ECHOED_UNITTEST_CLAIM_CASES)
    async def test_fat_harness_verifier_rejects_echoed_unittest_test_command(
        self, tmp_path, case
    ) -> None:
        """Mentioning or echoing a unittest command is not running it."""
        source_file = tmp_path / "string_utils.py"
        source_file.write_text("def slugify(text):\n    return text\n", encoding="utf-8")

        escaped_command = case.runtime_command.replace('"', '\\"')
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "```json\n"
                "{\n"
                '  "files_touched": ["string_utils.py"],\n'
                f'  "commands_run": ["{escaped_command}"],\n'
                f'  "tests_passed": ["{case.tests_passed}"]\n'
                "}\n"
                "```",
                native_session_id=case.native_session_id,
                support_messages=(
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {source_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(source_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Bash: {case.runtime_command}",
                        tool_name="Bash",
                        data={
                            "tool_input": {"command": case.runtime_command},
                            "output": case.runtime_output,
                            "exit_code": 0,
                        },
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Create slugify and unittest coverage.",
            session_id="orch_123",
            tools=["Read", "Edit", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship string utilities",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert case.error_fragment in result.error
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_failure_class"] == "FABRICATION_SUSPECTED"

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_accepts_wrapped_broad_pytest_for_current_test_file(
        self, tmp_path
    ) -> None:
        """Wrapped bare pytest should behave like unwrapped broad pytest for current files."""
        source_file = tmp_path / "src" / "generated.py"
        source_file.parent.mkdir()
        source_file.write_text("VALUE = 1\n", encoding="utf-8")
        test_file = tmp_path / "tests" / "test_generated.py"
        test_file.parent.mkdir()
        test_file.write_text("def test_generated():\n    assert True\n", encoding="utf-8")

        shell_command = f"/bin/zsh -lc 'cd {tmp_path} && pytest'"
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                '{"files_touched":["src/generated.py", "tests/test_generated.py"],'
                f'"commands_run":["{shell_command}"],'
                '"tests_passed":["tests/test_generated.py"]}\n'
                "```",
                native_session_id="codex-session-wrapped-broad-pytest",
                support_messages=(
                    AgentMessage(
                        type="tool",
                        content="Edit: src/generated.py",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": "src/generated.py"}},
                    ),
                    AgentMessage(
                        type="tool",
                        content="Edit: tests/test_generated.py",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": "tests/test_generated.py"}},
                    ),
                    AgentMessage(
                        type="tool",
                        content=f"Bash: {shell_command}",
                        tool_name="Bash",
                        data={
                            "tool_input": {"command": shell_command},
                            "output": "tests/test_generated.py passed\n1 passed in 0.01s",
                            "exit_code": 0,
                        },
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read", "Edit", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.error is None
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_rejects_test_not_covered_by_success_chunk(
        self, tmp_path
    ) -> None:
        """A successful test command must cover the claimed tests_passed entry."""
        touched_file = tmp_path / "src" / "generated.py"
        touched_file.parent.mkdir()
        touched_file.write_text("VALUE = 1\n", encoding="utf-8")
        test_a = tmp_path / "tests" / "test_a.py"
        test_b = tmp_path / "tests" / "test_b.py"
        test_a.parent.mkdir()
        test_a.write_text("def test_a():\n    assert True\n", encoding="utf-8")
        test_b.write_text("def test_b():\n    assert True\n", encoding="utf-8")

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                '{"files_touched":["src/generated.py"],'
                '"commands_run":["pytest tests/test_a.py"],'
                '"tests_passed":["tests/test_b.py"]}\n'
                "```",
                native_session_id="opencode-session-evidence",
                support_messages=(
                    AgentMessage(
                        type="tool",
                        content="Bash: pytest tests/test_a.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "pytest tests/test_a.py"}},
                    ),
                    AgentMessage(
                        type="result",
                        content="tests/test_a.py passed",
                        data={"subtype": "success"},
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert "tests_passed: tests/test_b.py" in result.error
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_passed"] is False

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_rejects_zero_passed_test_output(self, tmp_path) -> None:
        """A zero-passed test run is not proof for a claimed passing test."""
        touched_file = tmp_path / "src" / "generated.py"
        touched_file.parent.mkdir()
        touched_file.write_text("VALUE = 1\n", encoding="utf-8")

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                '{"files_touched":["src/generated.py"],'
                '"commands_run":["pytest tests/test_generated.py"],'
                '"tests_passed":["tests/test_generated.py"]}\n'
                "```",
                native_session_id="opencode-session-evidence",
                support_messages=(
                    AgentMessage(
                        type="tool",
                        content="Edit: src/generated.py",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": "src/generated.py"}},
                    ),
                    AgentMessage(
                        type="tool",
                        content="Bash: pytest tests/test_generated.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "pytest tests/test_generated.py"}},
                    ),
                    AgentMessage(
                        type="result",
                        content="tests/test_generated.py collected, 0 passed, 0 failed",
                        data={"subtype": "success"},
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert "tests_passed: tests/test_generated.py" in result.error
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_passed"] is False

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_rejects_targeted_failed_test_command(
        self, tmp_path
    ) -> None:
        """A targeted test command mentioning the claim is not proof without success."""
        touched_file = tmp_path / "src" / "generated.py"
        touched_file.parent.mkdir()
        touched_file.write_text("VALUE = 1\n", encoding="utf-8")
        test_file = tmp_path / "tests" / "test_generated.py"
        test_file.parent.mkdir()
        test_file.write_text("def test_generated():\n    assert False\n", encoding="utf-8")

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                '{"files_touched":["src/generated.py"],'
                '"commands_run":["pytest tests/test_generated.py"],'
                '"tests_passed":["tests/test_generated.py"]}\n'
                "```",
                native_session_id="opencode-session-evidence",
                support_messages=(
                    AgentMessage(
                        type="tool",
                        content="Bash: pytest tests/test_generated.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "pytest tests/test_generated.py"}},
                    ),
                    AgentMessage(
                        type="result",
                        content="tests/test_generated.py failed",
                        data={"subtype": "success"},
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert "tests_passed: tests/test_generated.py" in result.error
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_passed"] is False

    @pytest.mark.asyncio
    async def test_fat_harness_mode_rejects_verifier_fail(self) -> None:
        """Fat harness requires a separate verifier PASS after typed evidence."""

        def _rejecting_verifier(**kwargs: object) -> VerifierVerdict:
            del kwargs
            return VerifierVerdict(
                passed=False,
                reasons=("claimed test command did not support the AC",),
                failure_class="FABRICATION_SUSPECTED",
            )

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                '{"files_touched":["src/app.py"],'
                '"commands_run":["pytest"],'
                '"tests_passed":["tests/test_app.py"]}\n'
                "```",
                native_session_id="opencode-session-evidence",
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            atomic_verifier=_rejecting_verifier,
        )

        with patch("ouroboros.orchestrator.parallel_executor.log") as log_mock:
            result = await executor._execute_atomic_ac(
                ac_index=0,
                ac_content="Implement AC 1",
                session_id="orch_123",
                tools=["Read"],
                tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
                system_prompt="system",
                seed_goal="Ship the feature",
                depth=0,
                start_time=datetime.now(UTC),
            )

        log_mock.warning.assert_any_call(
            "parallel_executor.ac.verifier_rejected",
            session_id="orch_123",
            execution_id="",
            ac_index=0,
            depth=0,
            reason="Fat-harness verifier failed (claimed test command did not support the AC).",
            typed_evidence_present=True,
            typed_evidence_valid=True,
            verifier_ran=True,
            verifier_passed=False,
            verifier_reasons=["claimed test command did not support the AC"],
            verifier_failure_class="FABRICATION_SUSPECTED",
            verifier_status="FAIL",
            retry_admission="ESCALATE_MODEL",
            verifier_evidence_used=[],
        )

        assert result.success is False
        assert result.error is not None
        assert "Fat-harness verifier failed" in result.error
        assert "claimed test command did not support the AC" in result.error
        assert result.atomic_verifier_verdict is not None
        assert result.atomic_verifier_verdict.passed is False

        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["typed_evidence_valid"] is True
        assert evidence_event.data["verifier_ran"] is True
        assert evidence_event.data["verifier_passed"] is False
        assert evidence_event.data["verifier_failure_class"] == "FABRICATION_SUSPECTED"
        assert evidence_event.data["verifier_status"] == "FAIL"
        assert evidence_event.data["retry_admission"] == "ESCALATE_MODEL"
        assert evidence_event.data["verifier_evidence_used"] == []
        assert evidence_event.data["verifier_reasons"] == [
            "claimed test command did not support the AC"
        ]

    @pytest.mark.asyncio
    async def test_fat_harness_marks_incomplete_artifact_evidence_unverified(
        self, tmp_path: Any
    ) -> None:
        """A passing artifact command cannot erase missing transcript evidence."""
        (tmp_path / "output.txt").write_text("OZO_RUN_SMOKE_OK\n", encoding="utf-8")
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                'Done.\n```json\n{"files_touched":["output.txt"]}\n```',
                native_session_id="opencode-session-artifact-contract",
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("artifact"),
            fat_harness_mode=True,
        )

        with patch("ouroboros.orchestrator.parallel_executor.log") as log_mock:
            result = await executor._execute_atomic_ac(
                ac_index=0,
                ac_content="Create output.txt with the smoke marker.",
                session_id="orch_123",
                tools=["Read"],
                tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
                system_prompt="system",
                seed_goal="Ship the artifact",
                depth=0,
                start_time=datetime.now(UTC),
                ac_spec=AcceptanceCriterionSpec(
                    description="Create output.txt with the smoke marker.",
                    expected_artifacts=("output.txt",),
                    verify_command="test -f output.txt",
                ),
            )

        assert result.success is True
        assert result.error is None
        assert result.typed_evidence is not None
        assert result.typed_evidence_validation is not None
        assert result.typed_evidence_validation.ok is False
        assert result.atomic_verifier_verdict is not None
        assert result.atomic_verifier_verdict.status is VerifierStatus.UNAVAILABLE
        assert not any(
            call.args and call.args[0] == "parallel_executor.ac.verifier_rejected"
            for call in log_mock.warning.call_args_list
        )

        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["typed_evidence_present"] is True
        assert evidence_event.data["typed_evidence_valid"] is False
        assert evidence_event.data["enforcement_error"] is None
        assert evidence_event.data["has_success_contract"] is True
        assert evidence_event.data["has_expected_artifacts"] is True
        assert evidence_event.data["verify_gate_active"] is True
        assert evidence_event.data["verifier_status"] == "UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_fat_harness_preserves_artifact_without_transcript(self, tmp_path: Any) -> None:
        """A missing transcript yields unverified success, not a rerun."""
        (tmp_path / "output.txt").write_text("OZO_RUN_SMOKE_OK\n", encoding="utf-8")
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "[TASK_COMPLETE] artifact created without a JSON evidence block",
                native_session_id="opencode-session-artifact-no-evidence",
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("artifact"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Create output.txt with the smoke marker.",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the artifact",
            depth=0,
            start_time=datetime.now(UTC),
            ac_spec=AcceptanceCriterionSpec(
                description="Create output.txt with the smoke marker.",
                expected_artifacts=("output.txt",),
                verify_command="test -f output.txt",
            ),
        )

        assert result.success is True
        assert result.error is None
        assert result.typed_evidence is None
        assert result.atomic_verifier_verdict is not None
        assert result.atomic_verifier_verdict.status is VerifierStatus.UNAVAILABLE
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["typed_evidence_present"] is False
        assert evidence_event.data["enforcement_error"] is None

    @pytest.mark.asyncio
    async def test_verify_only_code_contract_still_requires_files_touched_evidence(
        self, tmp_path: Any
    ) -> None:
        """A command gate cannot replace code-profile filesystem evidence."""
        (tmp_path / "hello.py").write_text("VALUE = 1\n", encoding="utf-8")
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "[TASK_COMPLETE] command passed without a JSON evidence block",
                native_session_id="opencode-session-code-verify-only-no-evidence",
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement hello.py.",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the code",
            depth=0,
            start_time=datetime.now(UTC),
            ac_spec=AcceptanceCriterionSpec(
                description="Implement hello.py.",
                verify_command="test -f hello.py",
            ),
        )

        assert result.success is True
        assert result.error is None
        assert result.atomic_verifier_verdict is not None
        assert result.atomic_verifier_verdict.status is VerifierStatus.UNAVAILABLE
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["required_fields"] == [
            "files_touched",
            "commands_run",
            "tests_passed",
        ]

    @pytest.mark.asyncio
    async def test_contract_verify_gate_is_single_shot_across_atomic_and_final_gate(
        self, tmp_path: Any
    ) -> None:
        """A cached atomic verify outcome prevents duplicate command execution."""
        (tmp_path / "output.txt").write_text("OZO_RUN_SMOKE_OK\n", encoding="utf-8")
        command = "test -f output.txt"
        seed = Seed.from_dict(
            {
                "goal": "Create output.txt",
                "task_type": "artifact",
                "acceptance_criteria": [
                    {
                        "description": "Create output.txt with the smoke marker.",
                        "expected_artifacts": ["output.txt"],
                        "verify_command": command,
                    }
                ],
                "ontology_schema": {
                    "name": "SmokeArtifact",
                    "description": "Smoke artifact contract.",
                    "fields": [],
                },
                "metadata": {
                    "ambiguity_score": 0.0,
                    "generation_mode": "test",
                },
            }
        )
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                'Done.\n```json\n{"files_touched":["output.txt"]}\n```',
                native_session_id="opencode-session-single-shot-contract",
                cwd=str(tmp_path),
            ),
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("artifact"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Create output.txt with the smoke marker.",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the artifact",
            depth=0,
            start_time=datetime.now(UTC),
            ac_spec=seed.acceptance_criteria[0],
        )
        assert result.success is True
        assert result.verify_gate_outcome is not None

        finalized = await executor._apply_verify_gate(
            seed=seed,
            ac_index=0,
            result=result,
            session_id="orch_123",
            execution_id="exec_123",
        )

        assert finalized.success is True

    @pytest.mark.asyncio
    async def test_verify_gate_never_recovers_failed_artifact_result(self, tmp_path: Any) -> None:
        """Verification does not rewrite a failed worker execution."""
        (tmp_path / "output.txt").write_text("OZO_RUN_SMOKE_OK\n", encoding="utf-8")
        seed = Seed.from_dict(
            {
                "goal": "Create output.txt",
                "task_type": "artifact",
                "acceptance_criteria": [
                    {
                        "description": "Create output.txt with the smoke marker.",
                        "expected_artifacts": ["output.txt"],
                        "verify_command": "test -f output.txt",
                    }
                ],
                "ontology_schema": {
                    "name": "SmokeArtifact",
                    "description": "Smoke artifact contract.",
                    "fields": [],
                },
                "metadata": {
                    "ambiguity_score": 0.0,
                    "generation_mode": "test",
                },
            }
        )
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "memory pressure timeout after artifact write",
                native_session_id="codex-session-artifact-false-negative",
                cwd=str(tmp_path),
                success=False,
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("artifact"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )
        failed = ACExecutionResult(
            ac_index=0,
            ac_content="Create output.txt with the smoke marker.",
            success=False,
            error="memory pressure timeout after artifact write",
            final_message="memory pressure timeout after artifact write",
            outcome=ACExecutionOutcome.FAILED,
        )

        gated = await executor._apply_verify_gate(
            seed=seed,
            ac_index=0,
            result=failed,
            session_id="orch_123",
            execution_id="exec_123",
        )

        assert gated is failed
        assert gated.success is False
        assert not any(event.type == "execution.verify.recovered" for event in appended_events)

    @pytest.mark.asyncio
    async def test_fat_harness_mode_surfaces_operational_verifier_error(self) -> None:
        """Operational verifier failures remain typed verifier rejections."""

        def _timeout_verifier(**kwargs: object) -> VerifierVerdict:
            del kwargs
            raise TimeoutError("verifier timed out")

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                '{"files_touched":["src/app.py"],'
                '"commands_run":["pytest"],'
                '"tests_passed":["tests/test_app.py"]}\n'
                "```",
                native_session_id="opencode-session-evidence",
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            atomic_verifier=_timeout_verifier,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert "verifier raised TimeoutError: verifier timed out" in result.error
        assert result.atomic_verifier_verdict is not None
        assert result.atomic_verifier_verdict.failure_class == "STALL"

        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_ran"] is True
        assert evidence_event.data["verifier_passed"] is False
        assert evidence_event.data["verifier_failure_class"] == "STALL"
        assert evidence_event.data["verifier_reasons"] == [
            "verifier raised TimeoutError: verifier timed out"
        ]

    @pytest.mark.asyncio
    async def test_observe_only_mode_does_not_run_injected_verifier(self) -> None:
        """Non-enforced profile evidence telemetry must stay observe-only."""

        def _raising_verifier(**kwargs: object) -> VerifierVerdict:
            del kwargs
            raise AssertionError("observe-only mode must not invoke the verifier")

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                '{"files_touched":["src/app.py"],'
                '"commands_run":["pytest"],'
                '"tests_passed":["tests/test_app.py"]}\n'
                "```",
                native_session_id="opencode-session-evidence",
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            atomic_verifier=_raising_verifier,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.atomic_verifier_verdict is None
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_ran"] is False

    @pytest.mark.asyncio
    async def test_atomic_ac_typed_evidence_event_failure_does_not_fail_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Observe-only typed-evidence telemetry must not change AC success."""
        # The append retry ladder backs off for real seconds; keep the retries,
        # drop the waiting.
        monkeypatch.setattr("ouroboros.orchestrator.parallel_executor.anyio.sleep", AsyncMock())

        class _StubImplementationRuntime:
            _runtime_handle_backend = "opencode"
            _cwd = "/tmp/project"
            _permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                del prompt, tools, system_prompt, resume_session_id
                yield AgentMessage(
                    type="result",
                    content=(
                        "Done.\n"
                        "```json\n"
                        '{"files_touched":["src/app.py"],'
                        '"commands_run":["pytest"],'
                        '"tests_passed":["tests/test_app.py"]}\n'
                        "```"
                    ),
                    data={"subtype": "success"},
                    resume_handle=RuntimeHandle(
                        backend=resume_handle.backend if resume_handle is not None else "opencode",
                        kind=resume_handle.kind
                        if resume_handle is not None
                        else "implementation_session",
                        native_session_id="opencode-session-evidence",
                        cwd=resume_handle.cwd if resume_handle is not None else "/tmp/project",
                        metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
                    ),
                )

        event_store, appended_events = _make_replaying_event_store()
        original_append = event_store.append

        async def _append(event: BaseEvent) -> None:
            if event.type == "execution.ac.typed_evidence.observed":
                raise RuntimeError("typed evidence telemetry failed")
            await original_append(event)

        event_store.append = AsyncMock(side_effect=_append)
        executor = ParallelACExecutor(
            adapter=_StubImplementationRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.typed_evidence is not None
        assert all(
            event.type != "execution.ac.typed_evidence.observed" for event in appended_events
        )

    @pytest.mark.asyncio
    async def test_atomic_ac_profile_evidence_config_error_remains_loud(self) -> None:
        """Profile-authored evidence-schema bugs must not be downgraded to telemetry."""

        class _StubImplementationRuntime:
            _runtime_handle_backend = "opencode"
            _cwd = "/tmp/project"
            _permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                del prompt, tools, system_prompt, resume_session_id
                yield AgentMessage(
                    type="result",
                    content=(
                        "Done.\n"
                        "```json\n"
                        '{"files_touched":["src/app.py"],'
                        '"commands_run":["pytest"],'
                        '"tests_passed":["tests/test_app.py"]}\n'
                        "```"
                    ),
                    data={"subtype": "success"},
                    resume_handle=RuntimeHandle(
                        backend=resume_handle.backend if resume_handle is not None else "opencode",
                        kind=resume_handle.kind
                        if resume_handle is not None
                        else "implementation_session",
                        native_session_id="opencode-session-evidence",
                        cwd=resume_handle.cwd if resume_handle is not None else "/tmp/project",
                        metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
                    ),
                )

        profile = load_profile("code").model_copy(
            update={
                "evidence_schema": EvidenceSchema(
                    required=("files_touched", "commands_run", "tests_passed"),
                    rejected_if=("tests_passed != []",),
                )
            }
        )
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_StubImplementationRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=profile,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert "Unsupported rejected_if expression" in result.error
        assert "execution.session.completed" not in {event.type for event in appended_events}
        assert "execution.session.failed" in {event.type for event in appended_events}

    @pytest.mark.asyncio
    async def test_remembered_runtime_handle_preserves_live_controls(self) -> None:
        """AC-scope rebinding should preserve live observe/terminate callbacks."""
        executor = _make_executor()
        control_calls = {"observe": 0, "terminate": 0}

        async def _observe(handle: RuntimeHandle) -> dict[str, object]:
            control_calls["observe"] += 1
            snapshot = handle.snapshot()
            snapshot["observed"] = True
            return snapshot

        async def _terminate(_handle: RuntimeHandle) -> bool:
            control_calls["terminate"] += 1
            return True

        rebound = executor._remember_ac_runtime_handle(
            0,
            RuntimeHandle(
                backend="opencode",
                kind="implementation_session",
                native_session_id="oc-session-1",
                metadata={"server_session_id": "server-1"},
            ).bind_controls(
                observe_callback=_observe,
                terminate_callback=_terminate,
            ),
            execution_context_id="orch_ctrl",
        )

        assert rebound is not None
        assert rebound.metadata["session_scope_id"] == "orch_ctrl_ac_1"
        assert rebound.can_terminate is True

        observed = await rebound.observe()
        assert observed["observed"] is True
        assert observed["control_session_id"] == "server-1"
        assert await rebound.terminate() is True
        assert control_calls == {"observe": 1, "terminate": 1}

    @pytest.mark.asyncio
    async def test_completed_ac_attempt_does_not_reuse_cached_runtime_handle(self) -> None:
        """Terminal AC attempts should drop the cached session before the next invocation."""

        class _StubResumeRuntime:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                self.calls.append(
                    {
                        "prompt": prompt,
                        "tools": tools,
                        "system_prompt": system_prompt,
                        "resume_handle": resume_handle,
                        "resume_session_id": resume_session_id,
                    }
                )
                native_session_id = f"opencode-session-{len(self.calls)}"
                bound_handle = RuntimeHandle(
                    backend=resume_handle.backend if resume_handle is not None else "opencode",
                    kind=resume_handle.kind
                    if resume_handle is not None
                    else "implementation_session",
                    native_session_id=native_session_id,
                    cwd=resume_handle.cwd if resume_handle is not None else "/tmp/project",
                    approval_mode=(
                        resume_handle.approval_mode if resume_handle is not None else "acceptEdits"
                    ),
                    metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=bound_handle,
                )

        runtime = _StubResumeRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=False,
        )

        first_attempt = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Implement AC 2",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            retry_attempt=0,
        )
        resumed_attempt = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Implement AC 2",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            retry_attempt=0,
        )

        first_handle = runtime.calls[0]["resume_handle"]
        second_handle = runtime.calls[1]["resume_handle"]
        assert isinstance(first_handle, RuntimeHandle)
        assert isinstance(second_handle, RuntimeHandle)
        assert first_handle.native_session_id is None
        assert second_handle.native_session_id is None
        assert second_handle.metadata["session_scope_id"] == "orch_123_ac_2"
        assert second_handle.metadata["retry_attempt"] == 0
        assert second_handle.metadata["session_attempt_id"] == "orch_123_ac_2_attempt_1"
        assert first_attempt.runtime_handle is not None
        assert resumed_attempt.runtime_handle is not None
        assert first_attempt.runtime_handle.native_session_id == "opencode-session-1"
        assert resumed_attempt.runtime_handle.native_session_id == "opencode-session-2"
        assert executor._ac_runtime_handles == {}

    @pytest.mark.asyncio
    async def test_atomic_ac_skips_memory_gate_for_mocked_backend_runtime(self) -> None:
        """Mocked runtimes should not block on low-memory gating without explicit opt-in."""

        class _StubRuntime:
            def __init__(self) -> None:
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                del prompt, tools, system_prompt, resume_session_id
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=RuntimeHandle(
                        backend="opencode",
                        kind="implementation_session",
                        native_session_id="opencode-session-1",
                        cwd=resume_handle.cwd if resume_handle is not None else "/tmp/project",
                        approval_mode=(
                            resume_handle.approval_mode
                            if resume_handle is not None
                            else "acceptEdits"
                        ),
                        metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
                    ),
                )

        executor = ParallelACExecutor(
            adapter=_StubRuntime(),
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=False,
        )

        with (
            patch(
                "ouroboros.orchestrator.parallel_executor._get_available_memory_gb",
                return_value=0.5,
            ),
            patch(
                "ouroboros.orchestrator.parallel_executor.asyncio.sleep",
                new_callable=AsyncMock,
            ) as sleep_mock,
        ):
            result = await executor._execute_atomic_ac(
                ac_index=0,
                ac_content="Implement AC 1",
                session_id="orch_123",
                tools=["Read", "Edit"],
                system_prompt="system",
                seed_goal="Ship the feature",
                depth=0,
                start_time=datetime.now(UTC),
            )

        assert result.success is True
        sleep_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_try_decompose_ac_times_out_and_falls_back_to_atomic(self) -> None:
        """A hung decomposition child should time out and fall back to atomic execution."""

        class _HangingRuntime:
            def __init__(self) -> None:
                self.cancelled = False

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                del prompt, tools, system_prompt, resume_handle, resume_session_id
                try:
                    await asyncio.Future()
                    if False:  # pragma: no cover
                        yield AgentMessage(type="assistant", content="")
                finally:
                    self.cancelled = True

        runtime = _HangingRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=True,
        )

        with patch(
            "ouroboros.orchestrator.parallel_executor.DECOMPOSITION_TIMEOUT_SECONDS",
            0.01,
        ):
            result = await executor._try_decompose_ac(
                ac_content="Implement the full OpenCode runtime adapter.",
                ac_index=0,
                seed_goal="Ship OpenCode support",
                tools=["Read", "Edit"],
                system_prompt="system",
                source=DecompositionSource.BOUNCE,
                cause=BounceCause.TOO_BIG,
            )

        assert result.disposition is DecompositionDisposition.UNKNOWN
        assert result.reasons == ("decomposition_timeout",)
        assert result.trustworthy is False
        assert runtime.cancelled is True

    @pytest.mark.asyncio
    async def test_decomposition_policy_pressure_delays_next_policy_call(self) -> None:
        """Policy streams must not discard pressure before another policy entrance."""

        clock = {"now": 50.0}
        sleeps: list[float] = []
        provider_entrances: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock["now"] += seconds

        class _PolicyRuntime:
            runtime_backend = "opencode"
            working_directory = "/tmp/project"
            permission_mode = "acceptEdits"

            def __init__(self) -> None:
                self.calls = 0

            async def execute_task(self, **_kwargs: Any):
                self.calls += 1
                provider_entrances.append(clock["now"])
                if self.calls == 1:
                    yield AgentMessage(
                        type="result",
                        content="Too many concurrent requests",
                        data={
                            "subtype": "error",
                            "http_status": 429,
                            "headers": {"Retry-After": "3"},
                        },
                    )
                    return
                yield AgentMessage(
                    type="result",
                    content="ATOMIC",
                    data={"subtype": "success"},
                )

        executor = ParallelACExecutor(
            adapter=_PolicyRuntime(),
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=True,
            max_concurrent=1,
        )
        executor._adaptive_concurrency = AdaptiveConcurrencyController(
            initial_limit=1,
            clock=lambda: clock["now"],
            sleep=fake_sleep,
        )

        first = await executor._dispatch_decomposition_prompt(
            prompt="classify first",
            system_prompt="system",
        )
        second = await executor._dispatch_decomposition_prompt(
            prompt="classify second",
            system_prompt="system",
        )

        assert first == "Too many concurrent requests"
        assert second == "ATOMIC"
        assert provider_entrances == [50.0, 53.0]
        assert sleeps == [3.0]

    @pytest.mark.asyncio
    async def test_leaf_revalidates_runtime_capabilities_after_cooldown_wait(self) -> None:
        """A queued leaf must not dispatch with capabilities stale after admission."""

        clock = {"now": 100.0}
        runtime = _CooldownDriftRuntime()

        async def drift_during_sleep(seconds: float) -> None:
            runtime.capabilities = replace(FULL_CAPABILITIES, structured_output=False)
            clock["now"] += seconds

        controller = AdaptiveConcurrencyController(
            initial_limit=1,
            max_limit=1,
            clock=lambda: clock["now"],
            sleep=drift_during_sleep,
        )
        executor = _make_cooldown_drift_executor(runtime, controller)
        await controller.observe(
            ConcurrencyObservation(
                BackendPressureKind.CONCURRENCY_REJECTION,
                retry_after_seconds=2,
            ),
            permit_epoch=0,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Do not cross a stale runtime boundary",
            session_id="session-leaf-cooldown-drift",
            tools=["Read"],
            system_prompt="test",
            seed_goal="Revalidate provider authority",
            depth=0,
            start_time=datetime.now(UTC),
            execution_id="execution-leaf-cooldown-drift",
        )

        assert runtime.calls == 0
        assert result.outcome is ACExecutionOutcome.BLOCKED
        assert result.error == (
            "route admission blocked: live route state changed before provider entry"
        )

    @pytest.mark.asyncio
    async def test_decomposition_policy_revalidates_authority_after_cooldown_wait(self) -> None:
        """A queued policy request must re-check authority after its slot wait."""

        clock = {"now": 200.0}
        runtime = _CooldownDriftRuntime()
        executor_holder: dict[str, ProcessLocalTestExecutor] = {}
        drifted_dispatcher = AsyncMock()

        async def drift_during_sleep(seconds: float) -> None:
            executor_holder["executor"]._authority_leaf_dispatcher_stream = drifted_dispatcher
            clock["now"] += seconds

        controller = AdaptiveConcurrencyController(
            initial_limit=1,
            max_limit=1,
            clock=lambda: clock["now"],
            sleep=drift_during_sleep,
        )
        executor = _make_cooldown_drift_executor(
            runtime,
            controller,
            enable_decomposition=True,
        )
        executor_holder["executor"] = executor
        await controller.observe(
            ConcurrencyObservation(
                BackendPressureKind.CONCURRENCY_REJECTION,
                retry_after_seconds=2,
            ),
            permit_epoch=0,
        )

        with pytest.raises(ValueError, match="execution authority drifted before effect"):
            await executor._dispatch_decomposition_prompt(
                prompt="Classify without stale authority",
                system_prompt="test",
            )

        assert runtime.calls == 0
        drifted_dispatcher.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_coordinator_revalidates_authority_after_cooldown_wait(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A queued coordinator must re-check authority after its slot wait."""

        clock = {"now": 300.0}
        runtime = _CooldownDriftRuntime()
        executor_holder: dict[str, ProcessLocalTestExecutor] = {}
        drifted_review_provider = AsyncMock()

        async def drift_during_sleep(seconds: float) -> None:
            executor_holder["executor"]._authority_coordinator_review = drifted_review_provider
            clock["now"] += seconds

        controller = AdaptiveConcurrencyController(
            initial_limit=1,
            max_limit=1,
            clock=lambda: clock["now"],
            sleep=drift_during_sleep,
        )
        conflict = FileConflict(file_path="src/shared.py", ac_indices=(0, 1))
        review_provider = AsyncMock(
            return_value=CoordinatorReview(
                level_number=1,
                conflicts_detected=(conflict,),
                review_summary="should not execute",
            )
        )
        monkeypatch.setattr(LevelCoordinator, "run_review", review_provider)
        executor = _make_cooldown_drift_executor(runtime, controller)
        executor_holder["executor"] = executor
        executor._coordinator.detect_file_conflicts = MagicMock(return_value=[conflict])

        async def execute_ac(**kwargs: Any) -> ACExecutionResult:
            return ACExecutionResult(
                ac_index=int(kwargs["ac_index"]),
                ac_content=str(kwargs["ac_content"]),
                success=True,
                conflict_files=("src/shared.py",),
                final_message="done",
            )

        executor._execute_single_ac = execute_ac  # type: ignore[method-assign]
        await controller.observe(
            ConcurrencyObservation(
                BackendPressureKind.CONCURRENCY_REJECTION,
                retry_after_seconds=2,
            ),
            permit_epoch=0,
        )

        with pytest.RaisesGroup(ValueError):
            await executor.execute_parallel(
                seed=_make_seed("Write shared A", "Write shared B"),
                execution_plan=DependencyGraph(
                    nodes=(
                        ACNode(index=0, content="Write shared A", depends_on=()),
                        ACNode(index=1, content="Write shared B", depends_on=()),
                    ),
                    execution_levels=((0, 1),),
                ).to_execution_plan(),
                session_id="session-coordinator-cooldown-drift",
                execution_id="execution-coordinator-cooldown-drift",
                tools=["Read", "Edit"],
                system_prompt="test",
            )

        assert runtime.calls == 0
        review_provider.assert_not_awaited()
        drifted_review_provider.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_decomposed_ac_inlines_sub_ac_dispatch_into_single_ac(self) -> None:
        """Decomposed execution should recurse through _execute_single_ac without a helper path."""
        executor = ProcessLocalTestExecutor(
            adapter=MagicMock(),
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=True,
        )
        executor._emit_subtask_event = AsyncMock()
        root = ExecutionNodeIdentity.root(execution_context_id="exec_decompose", ac_index=1)
        executor._publish_event_owned_decomposition_decision(
            _trusted_preflight_split(
                root.node_id,
                "Extract parser",
                "Wire parser",
            )
        )
        executor._try_decompose_ac = AsyncMock()

        async def fake_execute_atomic_ac(**kwargs: Any) -> ACExecutionResult:
            return ACExecutionResult(
                ac_index=int(kwargs["ac_index"]),
                ac_content=str(kwargs["ac_content"]),
                success=True,
                final_message=f"{kwargs['ac_content']} complete",
                depth=int(kwargs["depth"]),
            )

        execute_atomic_ac = AsyncMock(side_effect=fake_execute_atomic_ac)
        executor._execute_atomic_ac = execute_atomic_ac

        executor._test_single_ac_calls = []
        result = await executor._execute_single_ac(
            ac_index=1,
            ac_content="Implement parser workflow",
            session_id="sess_decompose",
            tools=["Read", "Edit"],
            tool_catalog=None,
            system_prompt="system",
            seed_goal="Ship parser workflow",
            depth=0,
            execution_id="exec_decompose",
        )

        assert hasattr(executor, "_execute_sub_acs") is False
        assert result.success is True
        assert result.is_decomposed is True
        assert [sub_result.ac_content for sub_result in result.sub_results] == [
            "Extract parser",
            "Wire parser",
        ]
        assert [sub_result.depth for sub_result in result.sub_results] == [1, 1]
        assert [
            (
                int(call["ac_index"]),
                str(call["ac_content"]),
                int(call["depth"]),
            )
            for call in executor._test_single_ac_calls
        ] == [
            (1, "Implement parser workflow", 0),
            (100, "Extract parser", 1),
            (101, "Wire parser", 1),
        ]
        executor._try_decompose_ac.assert_not_awaited()
        assert execute_atomic_ac.await_count == 2

    @pytest.mark.asyncio
    async def test_top_level_decomposition_preserves_sub_ac_runtime_identity(self) -> None:
        """First-level decomposed children should still execute with sub-AC runtime metadata."""
        executor = ProcessLocalTestExecutor(
            adapter=MagicMock(),
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=True,
        )
        executor._emit_subtask_event = AsyncMock()
        root = ExecutionNodeIdentity.root(execution_context_id="exec_sub_ac_runtime", ac_index=1)
        executor._publish_event_owned_decomposition_decision(
            _trusted_preflight_split(
                root.node_id,
                "Extract parser",
                "Wire parser",
            )
        )
        executor._try_decompose_ac = AsyncMock()

        async def fake_execute_atomic_ac(**kwargs: Any) -> ACExecutionResult:
            return ACExecutionResult(
                ac_index=int(kwargs["ac_index"]),
                ac_content=str(kwargs["ac_content"]),
                success=True,
                final_message=f"{kwargs['ac_content']} complete",
                depth=int(kwargs["depth"]),
            )

        execute_atomic_ac = AsyncMock(side_effect=fake_execute_atomic_ac)
        executor._execute_atomic_ac = execute_atomic_ac

        await executor._execute_single_ac(
            ac_index=1,
            ac_content="Implement parser workflow",
            session_id="sess_sub_ac_runtime",
            tools=["Read", "Edit"],
            tool_catalog=None,
            system_prompt="system",
            seed_goal="Ship parser workflow",
            depth=0,
            execution_id="exec_sub_ac_runtime",
        )

        assert [
            (
                int(call.kwargs["ac_index"]),
                bool(call.kwargs["is_sub_ac"]),
                int(call.kwargs["parent_ac_index"]),
                int(call.kwargs["sub_ac_index"]),
            )
            for call in execute_atomic_ac.await_args_list
        ] == [
            (100, True, 1, 0),
            (101, True, 1, 1),
        ]

    def test_depth_above_durable_max_uses_compatible_legacy_executor(self) -> None:
        """Historical larger depths remain live without Routing D replay claims."""
        adapter = MagicMock()

        executor = ProcessLocalTestExecutor(
            adapter=adapter,
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=True,
            max_decomposition_depth=MAX_DECOMPOSITION_DEPTH + 1,
        )

        assert executor._max_decomposition_depth == 5
        assert executor._durable_decomposition_replay_enabled is False
        assert executor._bounded_route_escalation_enabled is False
        adapter.execute_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_parallel_skips_externally_satisfied_acs(self) -> None:
        """Top-level ACs flagged by --skip-completed should not be re-executed."""
        seed = _make_seed("AC 1", "AC 2")
        dependency_graph = DependencyGraph(
            nodes=(
                ACNode(index=0, content="AC 1", depends_on=()),
                ACNode(index=1, content="AC 2", depends_on=()),
            ),
            execution_levels=((0, 1),),
        )
        executor = _make_executor()
        executor._execute_ac_batch = AsyncMock(
            return_value=[
                ACExecutionResult(
                    ac_index=1,
                    ac_content="AC 2",
                    success=True,
                    final_message="Implemented AC 2",
                )
            ]
        )

        result = await executor.execute_parallel(
            seed=seed,
            execution_plan=dependency_graph.to_execution_plan(),
            session_id="orch_skip_completed",
            execution_id="exec_skip_completed",
            tools=["Read"],
            tool_catalog=None,
            system_prompt="system",
            externally_satisfied_acs={
                0: {"reason": "Implemented manually", "commit": "abc1234"},
            },
        )

        assert result.success_count == 1
        assert result.externally_satisfied_count == 1
        assert result.failure_count == 0
        assert result.results[0].outcome == ACExecutionOutcome.SATISFIED_EXTERNALLY
        assert "Implemented manually" in result.results[0].final_message
        assert "abc1234" in result.results[0].final_message
        executor._execute_ac_batch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_execute_parallel_logs_dependency_edges(self) -> None:
        """The inferred dependency graph should be visible before cascaded skips."""
        seed = _make_seed("AC 0 foundation", "AC 1 dependent flow")
        dependency_graph = DependencyGraph(
            nodes=(
                ACNode(index=0, content=seed.acceptance_criteria[0], depends_on=()),
                ACNode(index=1, content=seed.acceptance_criteria[1], depends_on=(0,)),
            ),
            execution_levels=((0,), (1,)),
        )
        executor = _make_executor()
        executor._execute_ac_batch = AsyncMock(
            return_value=[
                ACExecutionResult(
                    ac_index=0,
                    ac_content=seed.acceptance_criteria[0],
                    success=False,
                    error="Foundation failed",
                    outcome=ACExecutionOutcome.FAILED,
                )
            ]
        )

        with patch("ouroboros.orchestrator.parallel_executor.log") as log_mock:
            await executor.execute_parallel(
                seed=seed,
                execution_plan=dependency_graph.to_execution_plan(),
                session_id="orch_dependency_log",
                execution_id="exec_dependency_log",
                tools=["Read"],
                tool_catalog=None,
                system_prompt="system",
            )

        log_mock.info.assert_any_call(
            "parallel_executor.dependency_graph",
            session_id="orch_dependency_log",
            execution_id="exec_dependency_log",
            total_acs=2,
            dependency_edges=[{"ac_index": 1, "depends_on": (0,)}],
        )

    @pytest.mark.asyncio
    async def test_externally_satisfied_ac_blocked_when_dependency_failed(self) -> None:
        """Externally satisfied ACs must be BLOCKED when an upstream dep failed.

        Regression guard for #401: a stale --skip-completed marker must never
        bypass dependency validation. If AC0 fails and AC1 (which depends on
        AC0) is flagged externally_satisfied, AC1 must be BLOCKED — not
        SATISFIED_EXTERNALLY — because the supposed satisfied state is stale
        relative to the current failed run.
        """
        seed = _make_seed("AC 0 foundation", "AC 1 dependent flow")
        dependency_graph = DependencyGraph(
            nodes=(
                ACNode(index=0, content=seed.acceptance_criteria[0], depends_on=()),
                ACNode(index=1, content=seed.acceptance_criteria[1], depends_on=(0,)),
            ),
            execution_levels=((0,), (1,)),
        )
        executor = _make_executor()
        executed_batches: list[list[int]] = []

        async def fake_execute_ac_batch(**kwargs: Any) -> list[ACExecutionResult]:
            batch_indices = list(kwargs["batch_indices"])
            executed_batches.append(batch_indices)
            return [
                ACExecutionResult(
                    ac_index=ac_index,
                    ac_content=seed.acceptance_criteria[ac_index],
                    success=False,
                    error="Foundation failed",
                    outcome=ACExecutionOutcome.FAILED,
                )
                for ac_index in batch_indices
            ]

        executor._execute_ac_batch = fake_execute_ac_batch  # type: ignore[method-assign]

        result = await executor.execute_parallel(
            seed=seed,
            execution_plan=dependency_graph.to_execution_plan(),
            session_id="orch_stale_external_satisfied",
            execution_id="exec_stale_external_satisfied",
            tools=["Read"],
            tool_catalog=None,
            system_prompt="system",
            externally_satisfied_acs={
                1: {"reason": "Previously satisfied", "commit": "deadbeef"},
            },
        )

        # Only AC0 should be executed (and fails). AC1 must NOT run even
        # though it was flagged externally satisfied — its upstream dep failed.
        assert executed_batches == [[0]]

        ac1_result = next(r for r in result.results if r.ac_index == 1)
        assert ac1_result.outcome == ACExecutionOutcome.BLOCKED
        assert ac1_result.success is False
        assert ac1_result.error == "Skipped: dependency failed"

        assert result.externally_satisfied_count == 0
        assert result.blocked_count == 1
        assert result.failure_count == 1

    def test_verification_report_emits_depth_warning_feedback_metadata(self) -> None:
        """Verification report should expose depth warnings as structured metadata."""
        parallel_result = ParallelExecutionResult(
            results=(
                ACExecutionResult(
                    ac_index=0,
                    ac_content="Root AC",
                    success=True,
                    is_decomposed=True,
                    sub_results=(
                        ACExecutionResult(
                            ac_index=100,
                            ac_content="Depth-limited leaf",
                            success=True,
                            final_message="Leaf complete",
                            depth=3,
                            decomposition_depth_warning=True,
                        ),
                    ),
                ),
            ),
            success_count=1,
            failure_count=0,
        )

        report = render_parallel_verification_report(
            parallel_result,
            1,
            max_decomposition_depth=3,
        )

        assert "## Feedback Metadata" in report
        assert '"code": "decomposition_depth_warning"' in report
        assert '"affected_ac_paths": ["1.1"]' in report
        assert '"max_depth": 3' in report

    @pytest.mark.asyncio
    async def test_stall_retry_is_scoped_to_atomic_leaf_execution(self) -> None:
        """Leaf retries should not re-run composite decomposition or sibling dispatch."""
        event_store = AsyncMock()
        event_store.append = AsyncMock()
        executor = ProcessLocalTestExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=True,
        )
        executor._emit_subtask_event = AsyncMock()
        root = ExecutionNodeIdentity.root(
            execution_context_id="exec_atomic_retry_scope", ac_index=1
        )
        executor._publish_event_owned_decomposition_decision(
            _trusted_preflight_split(
                root.node_id,
                "Retry leaf",
                "Stable leaf",
            )
        )
        executor._try_decompose_ac = AsyncMock()

        async def fake_execute_atomic_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            retry_attempt = int(kwargs["retry_attempt"])
            if ac_index == 100 and retry_attempt == 0:
                return ACExecutionResult(
                    ac_index=ac_index,
                    ac_content=str(kwargs["ac_content"]),
                    success=False,
                    error="__STALL_DETECTED__",
                    retry_attempt=retry_attempt,
                    depth=int(kwargs["depth"]),
                )

            return ACExecutionResult(
                ac_index=ac_index,
                ac_content=str(kwargs["ac_content"]),
                success=True,
                final_message="retry leaf complete",
                retry_attempt=retry_attempt,
                depth=int(kwargs["depth"]),
            )

        execute_atomic_ac = AsyncMock(side_effect=fake_execute_atomic_ac)
        executor._execute_atomic_ac = execute_atomic_ac

        result = await executor._execute_single_ac(
            ac_index=1,
            ac_content="Composite AC",
            session_id="sess_atomic_retry_scope",
            tools=["Read"],
            tool_catalog=None,
            system_prompt="system",
            seed_goal="Retry only stalled leaves",
            depth=0,
            execution_id="exec_atomic_retry_scope",
        )

        assert result.success is True
        assert result.is_decomposed is True
        assert [sub_result.retry_attempt for sub_result in result.sub_results] == [1, 0]
        executor._try_decompose_ac.assert_not_awaited()
        assert [
            (
                int(call.kwargs["ac_index"]),
                int(call.kwargs["depth"]),
                int(call.kwargs["retry_attempt"]),
            )
            for call in execute_atomic_ac.await_args_list
        ] == [
            (100, 1, 0),
            (100, 1, 1),
            (101, 1, 0),
        ]

        stall_events = [
            call.args[0]
            for call in event_store.append.await_args_list
            if call.args and call.args[0].type == "execution.ac.stall_detected"
        ]
        assert len(stall_events) == 1
        first_leaf_identity = execute_atomic_ac.await_args_list[0].kwargs["node_identity"]
        assert (
            stall_events[0].aggregate_id == f"exec_atomic_retry_scope_{first_leaf_identity.node_id}"
        )
        assert stall_events[0].data["node_id"] == first_leaf_identity.node_id
        assert stall_events[0].data["parent_node_id"] == first_leaf_identity.parent_node_id
        assert stall_events[0].data["legacy_parent_node_id"] == "ac_1"
        assert stall_events[0].data["display_path"] == "2.1"
        assert stall_events[0].data["attempt"] == 1
        assert stall_events[0].data["max_attempts"] == MAX_STALL_RETRIES + 1
        assert stall_events[0].data["action"] == "restart"

    @pytest.mark.asyncio
    async def test_stall_retry_exhaustion_returns_terminal_failure_from_single_ac(self) -> None:
        """Single-AC execution should convert an unrecoverable stall into a normal failure."""
        event_store = AsyncMock()
        event_store.append = AsyncMock()
        executor = ProcessLocalTestExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            # Isolate the stall→failure conversion: cross-harness redispatch is a
            # separate recovery path (default on) that would otherwise intercept
            # the abandoned stall and surface an alternate backend's own failure.
            cross_harness_redispatch=False,
        )

        async def always_stall(**kwargs: Any) -> ACExecutionResult:
            return ACExecutionResult(
                ac_index=int(kwargs["ac_index"]),
                ac_content=str(kwargs["ac_content"]),
                success=False,
                error="__STALL_DETECTED__",
                retry_attempt=int(kwargs["retry_attempt"]),
                depth=int(kwargs["depth"]),
            )

        execute_atomic_ac = AsyncMock(side_effect=always_stall)
        executor._execute_atomic_ac = execute_atomic_ac

        result = await executor._execute_single_ac(
            ac_index=2,
            ac_content="Leaf AC",
            session_id="sess_atomic_retry_exhausted",
            tools=["Read"],
            tool_catalog=None,
            system_prompt="system",
            seed_goal="Normalize terminal stall failures",
            depth=0,
            execution_id="exec_atomic_retry_exhausted",
        )

        assert result.success is False
        assert result.error == f"Stalled (no activity for {STALL_TIMEOUT_SECONDS:.0f}s)"
        assert result.retry_attempt == MAX_STALL_RETRIES
        assert execute_atomic_ac.await_count == MAX_STALL_RETRIES + 1
        assert [
            int(call.kwargs["retry_attempt"]) for call in execute_atomic_ac.await_args_list
        ] == list(range(MAX_STALL_RETRIES + 1))

        stall_events = [
            call.args[0]
            for call in event_store.append.await_args_list
            if call.args and call.args[0].type == "execution.ac.stall_detected"
        ]
        assert [event.data["attempt"] for event in stall_events] == [1, 2, 3]
        assert [event.data["action"] for event in stall_events] == [
            "restart",
            "restart",
            "abandon",
        ]
        assert all(event.data["max_attempts"] == MAX_STALL_RETRIES + 1 for event in stall_events)

    @pytest.mark.asyncio
    async def test_alt_harness_defers_until_same_runtime_retry_budget_spent(self) -> None:
        """Cross-harness redispatch must not fire until same-runtime retries are spent.

        Regression for the ordering blocker: with ``ac_retry_attempts > 0`` the
        batch retry loop owns the same-runtime recovery budget. Each worker's
        alt-harness hook is gated on ``same_runtime_budget_exhausted``, which the
        batch layer sets ``True`` only on the AC's final attempt. So across the
        initial dispatch plus each configured retry, the flag stays ``False``
        until the last attempt — the alternate harness never pre-empts the
        configured same-runtime retries.
        """
        seed = _make_seed("AC 0 flow")
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=False,
            ac_retry_attempts=2,
            cross_harness_redispatch=True,
        )

        exhausted_flags: list[bool] = []

        async def fake_batch(**kwargs: Any) -> list[ACExecutionResult]:
            exhausted_flags.append(bool(kwargs["same_runtime_budget_exhausted"]))
            return [
                ACExecutionResult(
                    ac_index=idx,
                    ac_content=seed.acceptance_criteria[idx],
                    success=False,
                    error="non-stall failure",
                    outcome=ACExecutionOutcome.FAILED,
                )
                for idx in kwargs["batch_indices"]
            ]

        executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]

        results = await executor._run_batch_with_verify_and_retry(
            seed=seed,
            batch_executable=[0],
            session_id="sess",
            execution_id="exec",
            tools=["Read"],
            tool_catalog=None,
            system_prompt="system",
            level_contexts=[],
            ac_retry_attempts={0: 0},
            execution_counters=None,
        )

        # Initial dispatch + 2 configured retries = 3 batch calls. The
        # same-runtime budget is only 'exhausted' (so alt-harness may run) on
        # the final attempt.
        assert exhausted_flags == [False, False, True]
        assert isinstance(results[0], ACExecutionResult)
        assert results[0].success is False

    @pytest.mark.asyncio
    async def test_alt_harness_opens_on_retry_early_stop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Repeated-failure-class early-stop must still reach cross-harness recovery.

        Regression for the narrowed ordering blocker: with ``ac_retry_attempts=2``
        and the SAME alt-harness-eligible class (FABRICATION_SUSPECTED) on the
        initial attempt and retry 1, the retry loop early-stops before the counter
        cap. The same-runtime path has given up, so its budget is spent — the
        alt-harness hook must open on that early-stopped attempt instead of never
        firing, and the failed alternate must be surfaced as authoritative.
        """
        from ouroboros.orchestrator import cross_harness_redispatch as chr

        investment = InvestmentSpec(
            difficulty="high",
            stakes="high",
            provenance="declared",
            confidence="high",
        )
        ac_spec = AcceptanceCriterionSpec(
            description="AC 0 flow",
            investment=investment,
        )
        seed = _make_seed(ac_spec)
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=False,
            ac_retry_attempts=2,
            cross_harness_redispatch=True,
        )
        executor._adapter.runtime_backend = "claude"
        monkeypatch.setattr(chr, "pick_alternative_runtime", lambda *_a, **_k: "codex")

        fab_verdict = VerifierVerdict(
            passed=False,
            reasons=("claimed a file that does not exist",),
            failure_class="FABRICATION_SUSPECTED",
        )

        async def fake_batch(**kwargs: Any) -> list[ACExecutionResult]:
            return [
                ACExecutionResult(
                    ac_index=idx,
                    ac_content="AC 0 flow",
                    success=False,
                    error="fabricated claim",
                    outcome=ACExecutionOutcome.FAILED,
                    atomic_verifier_verdict=fab_verdict,
                )
                for idx in kwargs["batch_indices"]
            ]

        executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]

        alt_backends: list[str] = []
        alternate_rerun_kwargs: list[dict[str, Any]] = []

        async def fake_run_single(backend: str, **kwargs: Any) -> ACExecutionResult:
            alt_backends.append(backend)
            alternate_rerun_kwargs.append(kwargs["rerun_kwargs"])
            return ACExecutionResult(
                ac_index=0,
                ac_content="AC 0 flow",
                success=False,
                error="codex also failed verification",
                session_id="alt-sess",
            )

        executor._run_single_ac_on_backend = fake_run_single  # type: ignore[method-assign]

        results = await executor._run_batch_with_verify_and_retry(
            seed=seed,
            batch_executable=[0],
            session_id="sess",
            execution_id="exec",
            tools=["Read"],
            tool_catalog=None,
            system_prompt="system",
            level_contexts=[],
            ac_retry_attempts={0: 0},
            execution_counters=None,
        )

        # Early-stop fired after retry 1 (initial FAB + retry-1 FAB), before the
        # counter cap — yet the alternate harness was still consulted exactly once.
        assert alt_backends == ["codex"]
        assert alternate_rerun_kwargs[0]["ac_spec"] == seed.acceptance_criteria[0]
        assert alternate_rerun_kwargs[0]["ac_spec"].semantic_ac_key is not None
        assert alternate_rerun_kwargs[0]["investment_spec"] is investment
        assert alternate_rerun_kwargs[0]["decomposition_trustworthy"] is False
        # The failed alternate is surfaced as the authoritative result.
        assert isinstance(results[0], ACExecutionResult)
        assert results[0].success is False
        assert "alt-harness" in (results[0].error or "")
        assert "codex" in (results[0].error or "")

    @pytest.mark.asyncio
    async def test_runtime_handle_cache_isolated_between_acceptance_criteria(self) -> None:
        """Completing one AC must not seed a different AC with its prior runtime session."""

        class _StubCrossACRuntime:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                self.calls.append(
                    {
                        "prompt": prompt,
                        "tools": tools,
                        "system_prompt": system_prompt,
                        "resume_handle": resume_handle,
                        "resume_session_id": resume_session_id,
                    }
                )
                bound_handle = RuntimeHandle(
                    backend=resume_handle.backend if resume_handle is not None else "opencode",
                    kind=resume_handle.kind
                    if resume_handle is not None
                    else "implementation_session",
                    native_session_id=f"opencode-session-{len(self.calls)}",
                    cwd=resume_handle.cwd if resume_handle is not None else "/tmp/project",
                    approval_mode=(
                        resume_handle.approval_mode if resume_handle is not None else "acceptEdits"
                    ),
                    metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=bound_handle,
                )

        runtime = _StubCrossACRuntime()
        event_store, _ = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        first_result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )
        second_result = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Implement AC 2",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        first_handle = runtime.calls[0]["resume_handle"]
        second_handle = runtime.calls[1]["resume_handle"]
        assert isinstance(first_handle, RuntimeHandle)
        assert isinstance(second_handle, RuntimeHandle)
        assert first_handle.native_session_id is None
        assert second_handle.native_session_id is None
        assert first_handle.metadata["session_scope_id"] == "orch_123_ac_1"
        assert second_handle.metadata["session_scope_id"] == "orch_123_ac_2"
        assert first_handle.metadata["session_attempt_id"] == "orch_123_ac_1_attempt_1"
        assert second_handle.metadata["session_attempt_id"] == "orch_123_ac_2_attempt_1"
        assert second_handle.metadata["ac_index"] == 1
        assert first_result.runtime_handle is not None
        assert second_result.runtime_handle is not None
        assert first_result.runtime_handle.native_session_id == "opencode-session-1"
        assert second_result.runtime_handle.native_session_id == "opencode-session-2"
        assert executor._ac_runtime_handles == {}

    @pytest.mark.asyncio
    async def test_restarted_executor_rejects_persisted_runtime_handle_from_another_ac(
        self,
    ) -> None:
        """A persisted runtime handle must not resume when its metadata belongs to another AC."""

        class _StubFreshRuntime:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                self.calls.append(
                    {
                        "prompt": prompt,
                        "tools": tools,
                        "system_prompt": system_prompt,
                        "resume_handle": resume_handle,
                        "resume_session_id": resume_session_id,
                    }
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=RuntimeHandle(
                        backend="opencode",
                        kind="implementation_session",
                        native_session_id="opencode-session-fresh",
                        cwd="/tmp/project",
                        approval_mode="acceptEdits",
                        metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
                    ),
                )

        current_state_path = (
            "execution.workflows.orch_123.acceptance_criteria.ac_2.implementation_session"
        )
        current_attempt_id = "orch_123_ac_2_attempt_1"
        foreign_handle = RuntimeHandle(
            backend="opencode",
            kind="implementation_session",
            native_session_id="opencode-session-foreign",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            metadata={
                "ac_id": "orch_123_ac_1",
                "scope": "ac",
                "session_role": "implementation",
                "retry_attempt": 0,
                "attempt_number": 1,
                "ac_index": 0,
                "session_scope_id": "orch_123_ac_1",
                "session_attempt_id": "orch_123_ac_1_attempt_1",
                "session_state_path": (
                    "execution.workflows.orch_123.acceptance_criteria.ac_1.implementation_session"
                ),
                "server_session_id": "server-foreign",
            },
        )
        event_store = AsyncMock()
        event_store.replay = AsyncMock(
            return_value=[
                BaseEvent(
                    type="execution.session.started",
                    aggregate_type="execution",
                    aggregate_id="orch_123_ac_2",
                    data={
                        "retry_attempt": 0,
                        "attempt_number": 1,
                        "session_scope_id": "orch_123_ac_2",
                        "session_attempt_id": current_attempt_id,
                        "session_state_path": current_state_path,
                        "runtime": foreign_handle.to_dict(),
                    },
                )
            ]
        )
        event_store.append = AsyncMock()
        runtime = _StubFreshRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Keep AC sessions isolated",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            retry_attempt=0,
        )

        resume_handle = runtime.calls[0]["resume_handle"]
        assert isinstance(resume_handle, RuntimeHandle)
        assert resume_handle.native_session_id is None
        assert resume_handle.metadata["ac_index"] == 1
        assert resume_handle.metadata["session_scope_id"] == "orch_123_ac_2"
        assert resume_handle.metadata["session_attempt_id"] == current_attempt_id
        assert "server_session_id" not in resume_handle.metadata
        assert result.runtime_handle is not None
        assert result.runtime_handle.native_session_id == "opencode-session-fresh"

    @pytest.mark.asyncio
    async def test_cached_runtime_handle_from_another_ac_is_not_reused(self) -> None:
        """An in-memory runtime-handle cache entry must not leak a foreign AC session."""

        class _StubFreshRuntime:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                self.calls.append(
                    {
                        "prompt": prompt,
                        "tools": tools,
                        "system_prompt": system_prompt,
                        "resume_handle": resume_handle,
                        "resume_session_id": resume_session_id,
                    }
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=RuntimeHandle(
                        backend="opencode",
                        kind="implementation_session",
                        native_session_id="opencode-session-current",
                        cwd="/tmp/project",
                        approval_mode="acceptEdits",
                        metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
                    ),
                )

        runtime = _StubFreshRuntime()
        event_store = AsyncMock()
        event_store.replay = AsyncMock(return_value=[])
        event_store.append = AsyncMock()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        runtime_identity = executor._resolve_ac_runtime_identity(
            1,
            execution_context_id="orch_123",
            retry_attempt=0,
        )
        executor._ac_runtime_handles[runtime_identity.cache_key] = RuntimeHandle(
            backend="opencode",
            kind="implementation_session",
            native_session_id="opencode-session-foreign",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            metadata={
                "ac_id": "orch_123_ac_1",
                "scope": "ac",
                "session_role": "implementation",
                "retry_attempt": 0,
                "attempt_number": 1,
                "ac_index": 0,
                "session_scope_id": "orch_123_ac_1",
                "session_attempt_id": "orch_123_ac_1_attempt_1",
                "session_state_path": (
                    "execution.workflows.orch_123.acceptance_criteria.ac_1.implementation_session"
                ),
                "server_session_id": "server-foreign",
            },
        )

        result = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Keep AC sessions isolated",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            retry_attempt=0,
        )

        resume_handle = runtime.calls[0]["resume_handle"]
        assert isinstance(resume_handle, RuntimeHandle)
        assert resume_handle.native_session_id is None
        assert resume_handle.metadata["ac_index"] == 1
        assert resume_handle.metadata["session_scope_id"] == "orch_123_ac_2"
        assert resume_handle.metadata["session_attempt_id"] == "orch_123_ac_2_attempt_1"
        assert "server_session_id" not in resume_handle.metadata
        assert result.runtime_handle is not None
        assert result.runtime_handle.native_session_id == "opencode-session-current"

    @pytest.mark.asyncio
    async def test_atomic_ac_persists_reconnectable_handle_before_native_session_id(self) -> None:
        """OpenCode AC lifecycle should persist once the runtime exposes a resumable handle."""

        class _StubReconnectableRuntime:
            def __init__(self) -> None:
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                assert isinstance(resume_handle, RuntimeHandle)
                reconnectable_handle = RuntimeHandle(
                    backend=resume_handle.backend,
                    kind=resume_handle.kind,
                    conversation_id="conversation-9",
                    previous_response_id="response-9",
                    transcript_path="/tmp/opencode-runtime.jsonl",
                    cwd=resume_handle.cwd,
                    approval_mode=resume_handle.approval_mode,
                    updated_at="2026-03-13T09:00:00+00:00",
                    metadata={
                        **dict(resume_handle.metadata),
                        "server_session_id": "server-42",
                        "runtime_event_type": "session.ready",
                    },
                )
                yield AgentMessage(
                    type="system",
                    content="OpenCode session ready for reconnect.",
                    data={"server_session_id": "server-42"},
                    resume_handle=reconnectable_handle,
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=reconnectable_handle,
                )

        event_store = AsyncMock()
        event_store.replay = AsyncMock(return_value=[])
        event_store.append = AsyncMock()
        executor = ParallelACExecutor(
            adapter=_StubReconnectableRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Persist reconnectable OpenCode implementation handles",
            session_id="orch_123",
            tools=["Read"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            execution_id="exec_ac_progress",
        )

        appended_events = [call.args[0] for call in event_store.append.await_args_list]
        started_event = next(
            event for event in appended_events if event.type == "execution.session.started"
        )
        completed_event = next(
            event for event in appended_events if event.type == "execution.session.completed"
        )
        execution_completed_event = next(
            event for event in appended_events if event.type == "execution.ac.completed"
        )

        assert result.success is True
        assert result.session_id is None
        assert result.runtime_handle is not None
        assert result.runtime_handle.native_session_id is None
        assert result.runtime_handle.conversation_id == "conversation-9"
        assert result.runtime_handle.previous_response_id == "response-9"
        assert result.runtime_handle.transcript_path == "/tmp/opencode-runtime.jsonl"
        assert result.runtime_handle.metadata["server_session_id"] == "server-42"
        assert started_event.data["session_id"] == "server-42"
        assert started_event.data["server_session_id"] == "server-42"
        assert started_event.data["runtime"]["native_session_id"] is None
        assert started_event.data["runtime"]["metadata"]["server_session_id"] == "server-42"
        assert "conversation_id" not in started_event.data["runtime"]
        assert "previous_response_id" not in started_event.data["runtime"]
        assert "transcript_path" not in started_event.data["runtime"]
        assert "updated_at" not in started_event.data["runtime"]
        assert completed_event.data["session_id"] == "server-42"
        assert execution_completed_event.aggregate_id == "exec_ac_progress"
        assert execution_completed_event.data["success"] is True
        assert execution_completed_event.data["acceptance_criterion"] == (
            "Persist reconnectable OpenCode implementation handles"
        )

    @pytest.mark.asyncio
    async def test_execution_scoped_ac_completion_append_is_best_effort(self) -> None:
        """Root AC evidence must not corrupt an already persisted successful AC lifecycle."""

        event_store = AsyncMock()
        event_store.replay = AsyncMock(return_value=[])
        appended_events: list[BaseEvent] = []

        async def _append(event: BaseEvent) -> None:
            if event.type == "execution.ac.completed":
                raise RuntimeError("root aggregate temporarily unavailable")
            appended_events.append(event)

        event_store.append = AsyncMock(side_effect=_append)
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        runtime_identity = executor._resolve_ac_runtime_identity(
            0,
            execution_context_id="exec_ac_progress",
            retry_attempt=0,
        )

        await executor._emit_ac_runtime_event(
            event_type="execution.session.completed",
            runtime_identity=runtime_identity,
            ac_content="Persist AC completion evidence",
            runtime_handle=None,
            execution_id="exec_ac_progress",
            session_id="server-42",
            result_summary="[TASK_COMPLETE]",
            success=True,
        )

        assert [event.type for event in appended_events] == ["execution.session.completed"]
        assert event_store.append.await_count == 2

    @pytest.mark.asyncio
    async def test_restarted_executor_loads_persisted_runtime_handle_for_same_attempt(self) -> None:
        """A fresh executor should rehydrate the same-attempt runtime handle from events."""

        class _StubPersistedResumeRuntime:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "bypassPermissions"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                self.calls.append(
                    {
                        "prompt": prompt,
                        "tools": tools,
                        "system_prompt": system_prompt,
                        "resume_handle": resume_handle,
                        "resume_session_id": resume_session_id,
                    }
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=resume_handle,
                )

        persisted_handle = RuntimeHandle(
            backend="opencode",
            kind="implementation_session",
            native_session_id="opencode-session-9",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            metadata={
                "scope": "ac",
                "session_role": "implementation",
                "retry_attempt": 0,
                "ac_index": 1,
                "session_scope_id": "orch_123_ac_2",
                "session_state_path": (
                    "execution.workflows.orch_123.acceptance_criteria.ac_2.implementation_session"
                ),
                "server_session_id": "server-99",
            },
        )
        event_store = AsyncMock()
        event_store.replay = AsyncMock(
            return_value=[
                BaseEvent(
                    type="execution.session.started",
                    aggregate_type="execution",
                    aggregate_id="orch_123_ac_2",
                    data={
                        "retry_attempt": 0,
                        "session_state_path": (
                            "execution.workflows.orch_123.acceptance_criteria."
                            "ac_2.implementation_session"
                        ),
                        "runtime": persisted_handle.to_dict(),
                    },
                )
            ]
        )
        event_store.append = AsyncMock()
        runtime = _StubPersistedResumeRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Resume the interrupted AC implementation session",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            retry_attempt=0,
        )

        resume_handle = runtime.calls[0]["resume_handle"]
        assert isinstance(resume_handle, RuntimeHandle)
        # Foundation C rejects historical persisted handles without a durable
        # capsule/dispatch authority; the fresh attempt must not inherit the
        # provider session across an executor boundary.
        assert resume_handle.native_session_id is None
        assert resume_handle.approval_mode == "bypassPermissions"
        assert "server_session_id" not in resume_handle.metadata
        event_store.replay.assert_awaited_once_with("execution", "orch_123_ac_2")
        assert result.runtime_handle is not None
        assert result.runtime_handle.native_session_id == resume_handle.native_session_id
        assert result.runtime_handle.metadata == resume_handle.metadata

    @pytest.mark.asyncio
    async def test_restarted_executor_ignores_invalid_persisted_runtime_handle_for_same_attempt(
        self,
    ) -> None:
        """Malformed persisted runtime payloads should be skipped in favor of a fresh handle."""

        class _StubInvalidPersistedHandleRuntime:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                self.calls.append(
                    {
                        "prompt": prompt,
                        "tools": tools,
                        "system_prompt": system_prompt,
                        "resume_handle": resume_handle,
                        "resume_session_id": resume_session_id,
                    }
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=resume_handle,
                )

        event_store = AsyncMock()
        event_store.replay = AsyncMock(
            return_value=[
                BaseEvent(
                    type="execution.session.started",
                    aggregate_type="execution",
                    aggregate_id="orch_123_ac_2",
                    data={
                        "retry_attempt": 0,
                        "session_state_path": (
                            "execution.workflows.orch_123.acceptance_criteria."
                            "ac_2.implementation_session"
                        ),
                        "runtime": {
                            "kind": "implementation_session",
                            "cwd": "/tmp/project",
                            "approval_mode": "acceptEdits",
                            "metadata": {
                                "scope": "ac",
                                "session_role": "implementation",
                                "retry_attempt": 0,
                                "ac_index": 1,
                                "session_scope_id": "orch_123_ac_2",
                                "session_state_path": (
                                    "execution.workflows.orch_123.acceptance_criteria."
                                    "ac_2.implementation_session"
                                ),
                                "server_session_id": "server-invalid",
                            },
                        },
                    },
                )
            ]
        )
        event_store.append = AsyncMock()
        runtime = _StubInvalidPersistedHandleRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Recover from malformed persisted runtime state",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            retry_attempt=0,
        )

        resume_handle = runtime.calls[0]["resume_handle"]
        assert isinstance(resume_handle, RuntimeHandle)
        assert resume_handle.backend == "opencode"
        assert resume_handle.native_session_id is None
        assert resume_handle.metadata["session_scope_id"] == "orch_123_ac_2"
        assert resume_handle.metadata["session_role"] == "implementation"
        assert "server_session_id" not in resume_handle.metadata
        event_store.replay.assert_awaited_once_with("execution", "orch_123_ac_2")
        # Compare handles ignoring updated_at (timestamp set at creation time
        # may differ by microseconds from the one stored in the result).
        result_handle = replace(result.runtime_handle, updated_at=None)  # type: ignore[type-var]
        expected_handle = replace(resume_handle, updated_at=None)  # type: ignore[type-var]
        assert result_handle == expected_handle

    @pytest.mark.asyncio
    async def test_restarted_executor_prefers_latest_resumed_runtime_handle_for_same_attempt(
        self,
    ) -> None:
        """Resume should hydrate from the newest active lifecycle event for the same attempt."""

        class _StubResumedHandleRuntime:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                self.calls.append(
                    {
                        "prompt": prompt,
                        "tools": tools,
                        "system_prompt": system_prompt,
                        "resume_handle": resume_handle,
                        "resume_session_id": resume_session_id,
                    }
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=resume_handle,
                )

        started_handle = RuntimeHandle(
            backend="opencode",
            kind="implementation_session",
            native_session_id="opencode-session-started",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            metadata={
                "scope": "ac",
                "session_role": "implementation",
                "retry_attempt": 0,
                "ac_index": 1,
                "session_scope_id": "orch_123_ac_2",
                "session_state_path": (
                    "execution.workflows.orch_123.acceptance_criteria.ac_2.implementation_session"
                ),
                "server_session_id": "server-started",
            },
        )
        resumed_handle = RuntimeHandle(
            backend="opencode",
            kind="implementation_session",
            native_session_id="opencode-session-resumed",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            metadata={
                "scope": "ac",
                "session_role": "implementation",
                "retry_attempt": 0,
                "ac_index": 1,
                "session_scope_id": "orch_123_ac_2",
                "session_state_path": (
                    "execution.workflows.orch_123.acceptance_criteria.ac_2.implementation_session"
                ),
                "server_session_id": "server-resumed",
            },
        )
        event_store = AsyncMock()
        event_store.replay = AsyncMock(
            return_value=[
                BaseEvent(
                    type="execution.session.started",
                    aggregate_type="execution",
                    aggregate_id="orch_123_ac_2",
                    data={
                        "retry_attempt": 0,
                        "session_state_path": (
                            "execution.workflows.orch_123.acceptance_criteria."
                            "ac_2.implementation_session"
                        ),
                        "runtime": started_handle.to_dict(),
                    },
                ),
                BaseEvent(
                    type="execution.session.resumed",
                    aggregate_type="execution",
                    aggregate_id="orch_123_ac_2",
                    data={
                        "retry_attempt": 0,
                        "session_state_path": (
                            "execution.workflows.orch_123.acceptance_criteria."
                            "ac_2.implementation_session"
                        ),
                        "runtime": resumed_handle.to_dict(),
                    },
                ),
            ]
        )
        event_store.append = AsyncMock()
        runtime = _StubResumedHandleRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Resume the latest persisted implementation session",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            retry_attempt=0,
        )

        resume_handle = runtime.calls[0]["resume_handle"]
        assert isinstance(resume_handle, RuntimeHandle)
        assert resume_handle.native_session_id is None
        assert "server_session_id" not in resume_handle.metadata
        event_store.replay.assert_awaited_once_with("execution", "orch_123_ac_2")
        assert result.runtime_handle is not None
        assert result.runtime_handle.native_session_id == resume_handle.native_session_id
        assert result.runtime_handle.metadata == resume_handle.metadata

    @pytest.mark.asyncio
    async def test_restarted_executor_does_not_cross_resume_into_another_execution_context(
        self,
    ) -> None:
        """Persisted AC handles must stay bound to the parent execution/session context."""

        class _StubFreshRuntime:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                self.calls.append(
                    {
                        "prompt": prompt,
                        "tools": tools,
                        "system_prompt": system_prompt,
                        "resume_handle": resume_handle,
                        "resume_session_id": resume_session_id,
                    }
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=RuntimeHandle(
                        backend="opencode",
                        kind="implementation_session",
                        native_session_id="opencode-session-fresh",
                        cwd="/tmp/project",
                        approval_mode="acceptEdits",
                        metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
                    ),
                )

        event_store = AsyncMock()
        event_store.replay = AsyncMock(return_value=[])
        event_store.append = AsyncMock()
        runtime = _StubFreshRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Start a new implementation session in a different execution context",
            session_id="orch_new",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        resume_handle = runtime.calls[0]["resume_handle"]
        assert isinstance(resume_handle, RuntimeHandle)
        assert resume_handle.native_session_id is None
        assert resume_handle.metadata["session_scope_id"] == "orch_new_ac_2"
        assert (
            resume_handle.metadata["session_state_path"]
            == "execution.workflows.orch_new.acceptance_criteria.ac_2.implementation_session"
        )
        event_store.replay.assert_awaited_once_with("execution", "orch_new_ac_2")
        assert result.runtime_handle is not None
        assert result.runtime_handle.native_session_id == "opencode-session-fresh"

    @pytest.mark.asyncio
    async def test_restarted_executor_ignores_terminal_runtime_handle_for_same_attempt(
        self,
    ) -> None:
        """Persisted terminal events should not revive a completed AC attempt."""

        class _StubTerminalAwareRuntime:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                self.calls.append(
                    {
                        "prompt": prompt,
                        "tools": tools,
                        "system_prompt": system_prompt,
                        "resume_handle": resume_handle,
                        "resume_session_id": resume_session_id,
                    }
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=RuntimeHandle(
                        backend="opencode",
                        kind="implementation_session",
                        native_session_id="opencode-session-fresh",
                        cwd="/tmp/project",
                        approval_mode="acceptEdits",
                        metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
                    ),
                )

        persisted_handle = RuntimeHandle(
            backend="opencode",
            kind="implementation_session",
            native_session_id="opencode-session-terminal",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            metadata={
                "scope": "ac",
                "session_role": "implementation",
                "retry_attempt": 0,
                "ac_index": 1,
                "session_scope_id": "orch_123_ac_2",
                "session_state_path": (
                    "execution.workflows.orch_123.acceptance_criteria.ac_2.implementation_session"
                ),
            },
        )
        event_store = AsyncMock()
        event_store.replay = AsyncMock(
            return_value=[
                BaseEvent(
                    type="execution.session.started",
                    aggregate_type="execution",
                    aggregate_id="orch_123_ac_2",
                    data={
                        "retry_attempt": 0,
                        "session_state_path": (
                            "execution.workflows.orch_123.acceptance_criteria."
                            "ac_2.implementation_session"
                        ),
                        "runtime": persisted_handle.to_dict(),
                    },
                ),
                BaseEvent(
                    type="execution.session.completed",
                    aggregate_type="execution",
                    aggregate_id="orch_123_ac_2",
                    data={
                        "retry_attempt": 0,
                        "session_state_path": (
                            "execution.workflows.orch_123.acceptance_criteria."
                            "ac_2.implementation_session"
                        ),
                        "runtime": persisted_handle.to_dict(),
                        "success": True,
                    },
                ),
            ]
        )
        event_store.append = AsyncMock()
        runtime = _StubTerminalAwareRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Start a fresh session after terminal completion",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            retry_attempt=0,
        )

        resume_handle = runtime.calls[0]["resume_handle"]
        assert isinstance(resume_handle, RuntimeHandle)
        assert resume_handle.native_session_id is None
        assert resume_handle.metadata["session_scope_id"] == "orch_123_ac_2"
        assert result.runtime_handle is not None
        assert result.runtime_handle.native_session_id == "opencode-session-fresh"
        assert executor._ac_runtime_handles == {}

    @pytest.mark.asyncio
    async def test_retry_reopens_failed_ac_with_same_scope_and_new_attempt_audit(self) -> None:
        """Retry attempts should start a fresh session while emitting a new attempt identity."""

        class _StubRetryRuntime:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"
                self._attempt = 0

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                self.calls.append(
                    {
                        "prompt": prompt,
                        "tools": tools,
                        "system_prompt": system_prompt,
                        "resume_handle": resume_handle,
                        "resume_session_id": resume_session_id,
                    }
                )
                native_session_id = f"opencode-session-{self._attempt}"
                is_error = self._attempt == 0
                self._attempt += 1
                bound_handle = RuntimeHandle(
                    backend=resume_handle.backend if resume_handle is not None else "opencode",
                    kind=resume_handle.kind
                    if resume_handle is not None
                    else "implementation_session",
                    native_session_id=native_session_id,
                    cwd=resume_handle.cwd if resume_handle is not None else "/tmp/project",
                    approval_mode=(
                        resume_handle.approval_mode if resume_handle is not None else "acceptEdits"
                    ),
                    metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
                )
                yield AgentMessage(
                    type="result",
                    content="retry me" if is_error else "[TASK_COMPLETE]",
                    data={"subtype": "error" if is_error else "success"},
                    resume_handle=bound_handle,
                )

        runtime = _StubRetryRuntime()
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        first_attempt = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            retry_attempt=0,
        )
        retry_attempt = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            retry_attempt=1,
        )

        first_handle = runtime.calls[0]["resume_handle"]
        second_handle = runtime.calls[1]["resume_handle"]
        assert isinstance(first_handle, RuntimeHandle)
        assert isinstance(second_handle, RuntimeHandle)
        assert first_handle.native_session_id is None
        assert second_handle.native_session_id is None
        assert first_handle.metadata["session_scope_id"] == "orch_123_ac_1"
        assert second_handle.metadata["session_scope_id"] == "orch_123_ac_1"
        assert first_handle.metadata["session_attempt_id"] == "orch_123_ac_1_attempt_1"
        assert second_handle.metadata["session_attempt_id"] == "orch_123_ac_1_attempt_2"
        assert (
            first_handle.metadata["session_state_path"]
            == second_handle.metadata["session_state_path"]
            == "execution.workflows.orch_123.acceptance_criteria.ac_1.implementation_session"
        )
        assert first_handle.metadata["retry_attempt"] == 0
        assert second_handle.metadata["retry_attempt"] == 1
        assert first_attempt.ac_index == retry_attempt.ac_index == 0
        assert first_attempt.success is False
        assert retry_attempt.success is True
        assert first_attempt.session_id == "opencode-session-0"
        assert retry_attempt.session_id == "opencode-session-1"
        assert first_attempt.retry_attempt == 0
        assert retry_attempt.retry_attempt == 1
        assert first_attempt.runtime_handle is not None
        assert retry_attempt.runtime_handle is not None
        assert first_attempt.runtime_handle.native_session_id == "opencode-session-0"
        assert retry_attempt.runtime_handle.native_session_id == "opencode-session-1"
        lifecycle_events = [
            event
            for event in appended_events
            if event.type
            in {
                "execution.session.started",
                "execution.session.failed",
                "execution.session.completed",
            }
        ]
        assert [event.type for event in lifecycle_events] == [
            "execution.session.started",
            "execution.session.failed",
            "execution.session.started",
            "execution.session.completed",
        ]
        assert [event.data["session_attempt_id"] for event in lifecycle_events] == [
            "orch_123_ac_1_attempt_1",
            "orch_123_ac_1_attempt_1",
            "orch_123_ac_1_attempt_2",
            "orch_123_ac_1_attempt_2",
        ]
        assert executor._ac_runtime_handles == {}

    @pytest.mark.asyncio
    async def test_retry_executes_on_reconciled_workspace_context(self) -> None:
        """Retry prompts should include prior reconciled workspace context."""

        class _StubContextRuntime:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                self.calls.append(
                    {
                        "prompt": prompt,
                        "tools": tools,
                        "system_prompt": system_prompt,
                        "resume_handle": resume_handle,
                        "resume_session_id": resume_session_id,
                    }
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=RuntimeHandle(
                        backend="opencode",
                        kind="implementation_session",
                        native_session_id="opencode-session-retry",
                        cwd="/tmp/project",
                        approval_mode="acceptEdits",
                        metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
                    ),
                )

        runtime = _StubContextRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=False,
        )
        reconciled_context = LevelContext(
            level_number=1,
            completed_acs=(
                ACContextSummary(
                    ac_index=1,
                    ac_content="Reconcile the shared auth helpers",
                    success=True,
                    files_modified=("src/auth.py",),
                    key_output="Shared auth helpers are reconciled",
                ),
            ),
            coordinator_review=CoordinatorReview(
                level_number=1,
                review_summary="Merged the auth helper edits into the shared workspace",
                fixes_applied=("Merged src/auth.py conflict",),
                warnings_for_next_level=("Continue from the reconciled src/auth.py state",),
            ),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Finish wiring the auth retry flow",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            level_contexts=[reconciled_context],
            retry_attempt=1,
        )

        prompt = runtime.calls[0]["prompt"]
        assert isinstance(prompt, str)
        assert "## Previous Work Context" in prompt
        assert "Shared auth helpers are reconciled" in prompt
        assert "## Coordinator Review (Level 1)" in prompt
        assert "Merged the auth helper edits into the shared workspace" in prompt
        assert "Continue from the reconciled src/auth.py state" in prompt
        assert "## Retry Context" in prompt
        assert "retry attempt 1" in prompt
        assert "current shared workspace state" in prompt
        assert result.success is True
        assert result.retry_attempt == 1
        assert result.session_id == "opencode-session-retry"

    @pytest.mark.asyncio
    async def test_atomic_ac_prompt_uses_adapter_working_directory(self) -> None:
        """Prompt workspace context should come from the runtime adapter, not the server cwd."""

        class _StubPromptRuntime:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/requested-workspace"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                self.calls.append(
                    {
                        "prompt": prompt,
                        "tools": tools,
                        "system_prompt": system_prompt,
                        "resume_handle": resume_handle,
                        "resume_session_id": resume_session_id,
                    }
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=RuntimeHandle(
                        backend="opencode",
                        kind="implementation_session",
                        native_session_id="opencode-session-prompt",
                        cwd=self._cwd,
                        approval_mode="acceptEdits",
                        metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
                    ),
                )

        runtime = _StubPromptRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=False,
        )
        listed_paths: list[str] = []

        def _listdir(path: str) -> list[str]:
            listed_paths.append(path)
            return [".git", "README.md", "src"]

        with (
            patch("os.getcwd", return_value="/tmp/server-cwd"),
            patch("os.listdir", side_effect=_listdir),
        ):
            result = await executor._execute_atomic_ac(
                ac_index=0,
                ac_content="Implement the requested feature",
                session_id="orch_prompt",
                tools=["Read"],
                system_prompt="system",
                seed_goal="Ship the feature",
                depth=0,
                start_time=datetime.now(UTC),
            )

        assert listed_paths == ["/tmp/requested-workspace"]
        prompt = runtime.calls[0]["prompt"]
        assert isinstance(prompt, str)
        assert "## Working Directory" in prompt
        assert "`/tmp/requested-workspace`" in prompt
        assert "- README.md" in prompt
        assert "- src" in prompt
        assert "/tmp/server-cwd" not in prompt
        assert result.success is True
        assert result.session_id == "opencode-session-prompt"

    @pytest.mark.asyncio
    async def test_aggregates_mixed_stage_outcomes(self) -> None:
        """A later stage may be partially executable while blocked dependents are withheld."""
        seed = _make_seed(
            "Build the shared model",
            "Implement the fragile integration",
            "Add endpoint on top of the model",
            "Wire reporting to the fragile integration",
        )
        graph = DependencyGraph(
            nodes=(
                ACNode(index=0, content=seed.acceptance_criteria[0], depends_on=()),
                ACNode(index=1, content=seed.acceptance_criteria[1], depends_on=()),
                ACNode(index=2, content=seed.acceptance_criteria[2], depends_on=(0,)),
                ACNode(index=3, content=seed.acceptance_criteria[3], depends_on=(1,)),
            ),
            execution_levels=((0, 1), (2, 3)),
        )
        executor = _make_executor()

        async def fake_execute_single_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = kwargs["ac_index"]
            ac_content = kwargs["ac_content"]
            if ac_index == 0:
                return ACExecutionResult(
                    ac_index=0,
                    ac_content=str(ac_content),
                    success=True,
                    final_message="Shared model complete",
                )
            if ac_index == 1:
                return ACExecutionResult(
                    ac_index=1,
                    ac_content=str(ac_content),
                    success=False,
                    error="Integration step failed",
                )
            if ac_index == 2:
                return ACExecutionResult(
                    ac_index=2,
                    ac_content=str(ac_content),
                    success=True,
                    final_message="Endpoint complete",
                )
            msg = f"AC {ac_index} should have been blocked before execution"
            raise AssertionError(msg)

        executor._execute_single_ac = fake_execute_single_ac  # type: ignore[method-assign]

        result = await executor.execute_parallel(
            seed=seed,
            execution_plan=graph.to_execution_plan(),
            session_id="sess_stage_mixed",
            execution_id="exec_stage_mixed",
            tools=["Read", "Edit"],
            system_prompt="test",
        )

        assert result.success_count == 2
        assert result.failure_count == 1
        assert result.blocked_count == 1
        assert result.invalid_count == 0
        assert result.skipped_count == 1
        assert [r.outcome for r in result.results] == [
            ACExecutionOutcome.SUCCEEDED,
            ACExecutionOutcome.FAILED,
            ACExecutionOutcome.SUCCEEDED,
            ACExecutionOutcome.BLOCKED,
        ]

        assert len(result.stages) == 2
        assert result.stages[0].outcome == StageExecutionOutcome.PARTIAL
        assert result.stages[0].started is True
        assert result.stages[1].outcome == StageExecutionOutcome.PARTIAL
        assert result.stages[1].success_count == 1
        assert result.stages[1].blocked_count == 1
        executor._emit_level_started.assert_awaited()

    @pytest.mark.asyncio
    async def test_fully_blocked_stage_does_not_start(self) -> None:
        """If all ACs in a later stage depend on a failed AC, that stage is blocked but recorded."""
        seed = _make_seed(
            "Create the foundational abstraction",
            "Build the first dependent flow",
            "Build the second dependent flow",
        )
        graph = DependencyGraph(
            nodes=(
                ACNode(index=0, content=seed.acceptance_criteria[0], depends_on=()),
                ACNode(index=1, content=seed.acceptance_criteria[1], depends_on=(0,)),
                ACNode(index=2, content=seed.acceptance_criteria[2], depends_on=(0,)),
            ),
            execution_levels=((0,), (1, 2)),
        )
        executor = _make_executor()
        executed_indices: list[int] = []

        async def fake_execute_single_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            executed_indices.append(ac_index)
            return ACExecutionResult(
                ac_index=ac_index,
                ac_content=str(kwargs["ac_content"]),
                success=False,
                error="Foundation failed",
            )

        executor._execute_single_ac = fake_execute_single_ac  # type: ignore[method-assign]

        result = await executor.execute_parallel(
            seed=seed,
            execution_plan=graph.to_execution_plan(),
            session_id="sess_stage_blocked",
            execution_id="exec_stage_blocked",
            tools=["Read", "Edit"],
            system_prompt="test",
        )

        assert executed_indices == [0]
        assert result.success_count == 0
        assert result.failure_count == 1
        assert result.blocked_count == 2
        assert result.skipped_count == 2
        assert len(result.stages) == 2
        assert result.stages[0].outcome == StageExecutionOutcome.FAILED
        assert result.stages[1].started is False
        assert result.stages[1].outcome == StageExecutionOutcome.BLOCKED
        assert result.stages[1].blocked_count == 2

        assert executor._emit_level_started.await_count == 1
        assert executor._emit_level_completed.await_count == 2
        blocked_completion = executor._emit_level_completed.await_args_list[1].kwargs
        assert blocked_completion["started"] is False
        assert blocked_completion["blocked_count"] == 2
        assert blocked_completion["outcome"] == StageExecutionOutcome.BLOCKED.value

    @pytest.mark.asyncio
    async def test_runs_serial_stages_in_order(self) -> None:
        """The executor should not dispatch the next stage until the current one finishes."""
        seed = _make_seed("Implement parser", "Implement formatter", "Wire runner")
        graph = DependencyGraph(
            nodes=(
                ACNode(index=0, content=seed.acceptance_criteria[0], depends_on=()),
                ACNode(index=1, content=seed.acceptance_criteria[1], depends_on=()),
                ACNode(index=2, content=seed.acceptance_criteria[2], depends_on=(0, 1)),
            ),
            execution_levels=((0, 1), (2,)),
        )
        executor = _make_executor()

        stage_one_started: set[int] = set()
        stage_one_completed: list[int] = []
        release_stage_one = asyncio.Event()
        all_stage_one_started = asyncio.Event()
        stage_two_started = asyncio.Event()
        stage_two_started_after: frozenset[int] | None = None

        async def fake_execute_single_ac(**kwargs: Any) -> ACExecutionResult:
            nonlocal stage_two_started_after
            ac_index = int(kwargs["ac_index"])
            ac_content = str(kwargs["ac_content"])

            if ac_index in (0, 1):
                stage_one_started.add(ac_index)
                if stage_one_started == {0, 1}:
                    all_stage_one_started.set()
                await release_stage_one.wait()
                stage_one_completed.append(ac_index)
            elif ac_index == 2:
                stage_two_started_after = frozenset(stage_one_completed)
                stage_two_started.set()

            return ACExecutionResult(
                ac_index=ac_index,
                ac_content=ac_content,
                success=True,
                final_message=f"AC {ac_index} complete",
            )

        with patch.object(executor, "_execute_single_ac", side_effect=fake_execute_single_ac):
            execution_task = asyncio.create_task(
                executor.execute_parallel(
                    seed=seed,
                    execution_plan=graph.to_execution_plan(),
                    session_id="sess_stage_order",
                    execution_id="exec_stage_order",
                    tools=["Read"],
                    system_prompt="test",
                )
            )

            await asyncio.wait_for(all_stage_one_started.wait(), timeout=30)
            assert stage_two_started.is_set() is False

            release_stage_one.set()
            result = await asyncio.wait_for(execution_task, timeout=30)

        assert result.all_succeeded is True
        assert result.success_count == 3
        assert stage_two_started.is_set() is True
        assert stage_two_started_after == frozenset({0, 1})

    @pytest.mark.asyncio
    async def test_consumes_stage_batches_sequentially_within_stage_boundaries(self) -> None:
        """Batch-aware stages should run batch-by-batch without crossing stage boundaries."""
        seed = _make_seed(
            "Build parser core",
            "Build formatter core",
            "Assemble shared CLI",
            "Wire end-to-end runner",
        )
        executor = _make_executor()

        execution_plan = SimpleNamespace(
            stages=(
                SimpleNamespace(
                    index=0,
                    ac_indices=(),
                    batches=(
                        SimpleNamespace(ac_indices=(0, 1)),
                        SimpleNamespace(ac_indices=(2,)),
                    ),
                ),
                SimpleNamespace(
                    index=1,
                    ac_indices=(),
                    batches=(SimpleNamespace(ac_indices=(3,)),),
                ),
            ),
            total_stages=2,
            execution_levels=((0, 1, 2), (3,)),
            get_dependencies=lambda ac_index: {3: (2,)}.get(ac_index, ()),
        )

        first_batch_started: set[int] = set()
        release_first_batch = asyncio.Event()
        all_first_batch_started = asyncio.Event()
        second_batch_started = asyncio.Event()
        stage_two_started = asyncio.Event()

        async def fake_execute_single_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            ac_content = str(kwargs["ac_content"])

            if ac_index in (0, 1):
                first_batch_started.add(ac_index)
                if first_batch_started == {0, 1}:
                    all_first_batch_started.set()
                await release_first_batch.wait()
            elif ac_index == 2:
                second_batch_started.set()
            elif ac_index == 3:
                stage_two_started.set()

            return ACExecutionResult(
                ac_index=ac_index,
                ac_content=ac_content,
                success=True,
                final_message=f"AC {ac_index} complete",
            )

        with patch.object(executor, "_execute_single_ac", side_effect=fake_execute_single_ac):
            execution_task = asyncio.create_task(
                executor.execute_parallel(
                    seed=seed,
                    execution_plan=execution_plan,
                    session_id="sess_stage_batches",
                    execution_id="exec_stage_batches",
                    tools=["Read"],
                    system_prompt="test",
                )
            )

            await asyncio.wait_for(all_first_batch_started.wait(), timeout=30)
            assert second_batch_started.is_set() is False
            assert stage_two_started.is_set() is False

            release_first_batch.set()
            result = await asyncio.wait_for(execution_task, timeout=30)

        assert result.all_succeeded is True
        assert result.success_count == 4
        assert second_batch_started.is_set() is True
        assert stage_two_started.is_set() is True

    @pytest.mark.asyncio
    async def test_aggregates_stage_batch_results_with_failures_and_blocked_dependents(
        self,
    ) -> None:
        """Stage aggregation should include all batch outcomes before moving to the next stage."""
        seed = _make_seed(
            "Build parser core",
            "Build formatter core",
            "Wire parser command",
            "Wire formatter command",
        )
        executor = _make_executor()

        execution_plan = SimpleNamespace(
            stages=(
                SimpleNamespace(
                    index=0,
                    ac_indices=(),
                    batches=(
                        SimpleNamespace(ac_indices=(0,)),
                        SimpleNamespace(ac_indices=(1,)),
                    ),
                ),
                SimpleNamespace(
                    index=1,
                    ac_indices=(),
                    batches=(SimpleNamespace(ac_indices=(2, 3)),),
                ),
            ),
            total_stages=2,
            execution_levels=((0, 1), (2, 3)),
            get_dependencies=lambda ac_index: {2: (0,), 3: (1,)}.get(ac_index, ()),
        )

        async def fake_execute_single_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            ac_content = str(kwargs["ac_content"])
            if ac_index == 0:
                return ACExecutionResult(
                    ac_index=ac_index,
                    ac_content=ac_content,
                    success=False,
                    error="Parser core failed",
                )

            return ACExecutionResult(
                ac_index=ac_index,
                ac_content=ac_content,
                success=True,
                final_message=f"AC {ac_index} complete",
            )

        executor._execute_single_ac = fake_execute_single_ac  # type: ignore[method-assign]

        result = await executor.execute_parallel(
            seed=seed,
            execution_plan=execution_plan,
            session_id="sess_stage_batch_outcomes",
            execution_id="exec_stage_batch_outcomes",
            tools=["Read"],
            system_prompt="test",
        )

        assert [r.outcome for r in result.results] == [
            ACExecutionOutcome.FAILED,
            ACExecutionOutcome.SUCCEEDED,
            ACExecutionOutcome.BLOCKED,
            ACExecutionOutcome.SUCCEEDED,
        ]
        assert result.success_count == 2
        assert result.failure_count == 1
        assert result.blocked_count == 1
        assert result.invalid_count == 0
        assert len(result.stages) == 2
        assert result.stages[0].ac_indices == (0, 1)
        assert result.stages[0].outcome == StageExecutionOutcome.PARTIAL
        assert result.stages[1].ac_indices == (2, 3)
        assert result.stages[1].outcome == StageExecutionOutcome.PARTIAL

    @pytest.mark.asyncio
    async def test_coordinator_pressure_delays_next_stage_provider_entrance(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Coordinator Retry-After must close the next stage's provider entrance."""

        clock = {"now": 100.0}
        sleeps: list[float] = []
        provider_entrances: list[tuple[int, float]] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock["now"] += seconds

        pressure = AgentMessage(
            type="result",
            content="Too many concurrent requests",
            data={
                "subtype": "error",
                "http_status": 429,
                "headers": {"Retry-After": "2"},
            },
        )
        conflict = FileConflict(file_path="src/shared.py", ac_indices=(0, 1))
        monkeypatch.setattr(
            LevelCoordinator,
            "run_review",
            AsyncMock(
                return_value=CoordinatorReview(
                    level_number=1,
                    conflicts_detected=(conflict,),
                    review_summary=pressure.content,
                    final_output=pressure.content,
                    messages=(pressure,),
                )
            ),
        )
        seed = _make_seed("Write shared A", "Write shared B", "Consume shared output")
        graph = DependencyGraph(
            nodes=(
                ACNode(index=0, content=seed.acceptance_criteria[0], depends_on=()),
                ACNode(index=1, content=seed.acceptance_criteria[1], depends_on=()),
                ACNode(index=2, content=seed.acceptance_criteria[2], depends_on=(0, 1)),
            ),
            execution_levels=((0, 1), (2,)),
        )
        controller = AdaptiveConcurrencyController(
            initial_limit=2,
            max_limit=2,
            clock=lambda: clock["now"],
            sleep=fake_sleep,
        )
        monkeypatch.setattr(
            "ouroboros.orchestrator.parallel_executor.adaptive_concurrency."
            "AdaptiveConcurrencyController",
            lambda **_kwargs: controller,
        )
        executor = _make_executor()
        executor._coordinator.detect_file_conflicts = MagicMock(side_effect=([conflict], []))

        async def execute_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            messages = (AgentMessage(type="result", content="done", data={"subtype": "success"}),)
            async with executor._adaptive_concurrency.slot() as permit_epoch:
                provider_entrances.append((ac_index, clock["now"]))
                await observe_provider_messages(
                    executor._adaptive_concurrency,
                    messages,
                    permit_epoch,
                    ("session", "execution", ac_index),
                    provider_completed=True,
                )
            return ACExecutionResult(
                ac_index=ac_index,
                ac_content=str(kwargs["ac_content"]),
                success=True,
                messages=messages,
                final_message="done",
            )

        executor._execute_single_ac = execute_ac  # type: ignore[method-assign]
        result = await executor.execute_parallel(
            seed=seed,
            execution_plan=graph.to_execution_plan(),
            session_id="session-coordinator-pressure",
            execution_id="execution-coordinator-pressure",
            tools=["Read", "Edit"],
            system_prompt="test",
        )

        assert result.all_succeeded is True
        assert next(at for index, at in provider_entrances if index == 2) == 102.0
        assert sleeps == [2.0]

    @pytest.mark.asyncio
    async def test_coordinator_quota_stops_before_next_stage_provider_effect(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Coordinator quota must become a durable pause input before stage two."""

        quota = AgentMessage(
            type="result",
            content="Usage limit reached. Please try again in 5 hours.",
            data={"subtype": "error", "error_type": "CodexCliError"},
        )
        conflict = FileConflict(file_path="src/shared.py", ac_indices=(0, 1))
        monkeypatch.setattr(
            LevelCoordinator,
            "run_review",
            AsyncMock(
                return_value=CoordinatorReview(
                    level_number=1,
                    conflicts_detected=(conflict,),
                    review_summary=quota.content,
                    final_output=quota.content,
                    messages=(quota,),
                )
            ),
        )
        seed = _make_seed("Write shared A", "Write shared B", "Consume shared output")
        graph = DependencyGraph(
            nodes=(
                ACNode(index=0, content=seed.acceptance_criteria[0], depends_on=()),
                ACNode(index=1, content=seed.acceptance_criteria[1], depends_on=()),
                ACNode(index=2, content=seed.acceptance_criteria[2], depends_on=(0, 1)),
            ),
            execution_levels=((0, 1), (2,)),
        )
        executor = _make_executor()
        executor._coordinator.detect_file_conflicts = MagicMock(return_value=[conflict])
        provider_effects: list[int] = []

        async def execute_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            provider_effects.append(ac_index)
            return ACExecutionResult(
                ac_index=ac_index,
                ac_content=str(kwargs["ac_content"]),
                success=True,
                final_message="done",
            )

        executor._execute_single_ac = execute_ac  # type: ignore[method-assign]
        result = await executor.execute_parallel(
            seed=seed,
            execution_plan=graph.to_execution_plan(),
            session_id="session-coordinator-quota",
            execution_id="execution-coordinator-quota",
            tools=["Read", "Edit"],
            system_prompt="test",
        )

        assert provider_effects == [0, 1]
        assert isinstance(result.recoverable_coordinator_pause, CoordinatorQuotaPause)
        assert result.recoverable_coordinator_pause.consequence.pause_seconds == 18_000
        assert result.all_succeeded is False
        assert len(result.stages) == 1
        assert result.stages[0].coordinator_review is not None
        coordinator_event_types = [
            call.args[0].type
            for call in executor._event_store.append.await_args_list
            if call.args[0].type.startswith("execution.coordinator.")
        ]
        assert coordinator_event_types == [
            "execution.coordinator.started",
            "execution.coordinator.completed",
        ]

    @pytest.mark.asyncio
    async def test_restored_coordinator_quota_stops_before_next_stage_provider_effect(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Checkpoint replay consumes the exact pause before opening stage two."""

        quota = AgentMessage(
            type="result",
            content="Usage limit reached. Please try again in 5 hours.",
            data={"subtype": "error", "error_type": "CodexCliError"},
        )
        conflict = FileConflict(file_path="src/shared.py", ac_indices=(0, 1))
        execution_id = "execution-restored-coordinator-quota"
        session_id = "session-restored-coordinator-quota"
        runtime_scope = build_level_coordinator_runtime_scope(execution_id, 1)
        review_provider = AsyncMock(
            return_value=CoordinatorReview(
                level_number=1,
                conflicts_detected=(conflict,),
                review_summary=quota.content,
                final_output=quota.content,
                messages=(quota,),
                session_scope_id=runtime_scope.aggregate_id,
                session_state_path=runtime_scope.state_path,
            )
        )
        monkeypatch.setattr(LevelCoordinator, "run_review", review_provider)
        seed = _make_seed("Write shared A", "Write shared B", "Consume shared output")
        graph = DependencyGraph(
            nodes=(
                ACNode(index=0, content=seed.acceptance_criteria[0], depends_on=()),
                ACNode(index=1, content=seed.acceptance_criteria[1], depends_on=()),
                ACNode(index=2, content=seed.acceptance_criteria[2], depends_on=(0, 1)),
            ),
            execution_levels=((0, 1), (2,)),
        )
        checkpoint_store = CheckpointStore(tmp_path)
        checkpoint_store.initialize()

        async def run_once(
            executor: ParallelACExecutor,
            provider_effects: list[int],
            *,
            published_owner: Mapping[str, object] | None = None,
        ) -> ParallelExecutionResult:
            async def execute_ac(**kwargs: Any) -> ACExecutionResult:
                ac_index = int(kwargs["ac_index"])
                provider_effects.append(ac_index)
                return ACExecutionResult(
                    ac_index=ac_index,
                    ac_content=str(kwargs["ac_content"]),
                    success=True,
                    conflict_files=("src/shared.py",) if ac_index < 2 else (),
                    final_message="done",
                )

            executor._coordinator.detect_file_conflicts = MagicMock(
                side_effect=lambda results: (
                    [conflict] if {result.ac_index for result in results} == {0, 1} else []
                )
            )
            executor._execute_single_ac = execute_ac  # type: ignore[method-assign]
            return await executor.execute_parallel(
                seed=seed,
                execution_plan=graph.to_execution_plan(),
                session_id=session_id,
                execution_id=execution_id,
                tools=["Read", "Edit"],
                system_prompt="test",
                published_coordinator_pause_owner=published_owner,
            )

        first = _make_executor(run_verify_commands=False)
        first._checkpoint_store = checkpoint_store
        first._event_store.query_events = AsyncMock(return_value=[])
        first_effects: list[int] = []
        first_result = await run_once(first, first_effects)
        first_events = [call.args[0] for call in first._event_store.append.await_args_list]
        expected_aggregate_id = first._coordinator_aggregate_id(execution_id, 1)
        coordinator_events = [
            event
            for event in first_events
            if isinstance(event, BaseEvent)
            and event.type in {"execution.coordinator.started", "execution.coordinator.completed"}
        ]
        assert [
            (event.type, event.aggregate_type, event.aggregate_id, isinstance(event.data, dict))
            for event in coordinator_events
        ] == [
            ("execution.coordinator.started", "execution", expected_aggregate_id, True),
            ("execution.coordinator.completed", "execution", expected_aggregate_id, True),
        ]
        first_checkpoint = checkpoint_store.load(session_id)
        assert first_checkpoint.is_ok
        assert first_checkpoint.value is not None
        assert first_checkpoint.value.state["completed_levels"] == 1

        async def replay_query(*_args: Any, **kwargs: Any) -> list[BaseEvent]:
            event_type = kwargs.get("event_type")
            if kwargs.get("aggregate_id") != expected_aggregate_id:
                return []
            matched = [
                event
                for event in first_events
                if isinstance(event, BaseEvent) and event.type == event_type
            ]
            if event_type == "execution.coordinator.quota_pause_consumed":
                assert matched == []
                return []
            assert len(matched) == 1
            assert matched[0].type == event_type
            assert matched[0].aggregate_type == "execution"
            assert matched[0].aggregate_id == expected_aggregate_id
            assert isinstance(matched[0].data, dict)
            return matched

        resumed = _make_executor(run_verify_commands=False)
        resumed._checkpoint_store = checkpoint_store
        resumed._event_store.query_events = AsyncMock(side_effect=replay_query)
        resumed_effects: list[int] = []
        resumed_result = await run_once(resumed, resumed_effects)

        assert first_effects == [0, 1]
        assert isinstance(first_result.recoverable_coordinator_pause, CoordinatorQuotaPause)
        assert resumed_effects == []
        assert isinstance(resumed_result.recoverable_coordinator_pause, CoordinatorQuotaPause)
        assert len(resumed_result.stages) == 0
        assert review_provider.await_count == 1

        published_owner = resumed_result.recoverable_coordinator_pause.owner_payload()
        published = _make_executor(run_verify_commands=False)
        published._checkpoint_store = checkpoint_store
        published._event_store.query_events = AsyncMock(side_effect=replay_query)
        published_effects: list[int] = []
        published_result = await run_once(
            published,
            published_effects,
            published_owner=published_owner,
        )

        assert published_effects == [2]
        assert published_result.recoverable_coordinator_pause is None
        assert len(published_result.stages) == 1
        consumed_events = [
            call.args[0]
            for call in published._event_store.append.await_args_list
            if call.args[0].type == "execution.coordinator.quota_pause_consumed"
        ]
        assert len(consumed_events) == 1
        assert consumed_events[0].data == published_owner

        replay_events = [*first_events, consumed_events[0]]

        async def consumed_query(*_args: Any, **kwargs: Any) -> list[BaseEvent]:
            event_type = kwargs.get("event_type")
            return [event for event in replay_events if event.type == event_type]

        after_consumption_crash = _make_executor(run_verify_commands=False)
        after_consumption_crash._checkpoint_store = checkpoint_store
        after_consumption_crash._event_store.query_events = AsyncMock(side_effect=consumed_query)
        crash_effects: list[int] = []
        crash_result = await run_once(after_consumption_crash, crash_effects)

        assert crash_effects == []
        assert crash_result.recoverable_coordinator_pause is None
        assert len(crash_result.stages) == 0
        assert review_provider.await_count == 1

    @pytest.mark.asyncio
    async def test_coordinator_pause_consumption_rejects_mismatched_owner(self) -> None:
        """A different PAUSED owner cannot consume the restored coordinator effect."""

        executor = _make_executor(run_verify_commands=False)
        executor._event_store.query_events = AsyncMock(return_value=[])
        pause = CoordinatorQuotaPause(
            execution_id="execution-owner",
            session_id="session-owner",
            level_number=1,
            coordinator_aggregate_id="execution-owner:l0:coord",
            consequence=UsageLimitPauseConsequence(
                reason="Usage limit reached",
                resume_hint="Resume after the provider window reopens.",
                pause_seconds=60,
                resume_after=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
            ),
        )
        mismatched_owner = {
            **pause.owner_payload(),
            "coordinator_aggregate_id": "execution-owner:l1:coord",
        }

        consumed = await consume_published_coordinator_pause(
            event_store=executor._event_store,
            pause=pause,
            published_owner=mismatched_owner,
        )

        assert consumed is False
        executor._event_store.append.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_coordinator_pause_consumption_fails_closed_when_ambiguous(self) -> None:
        """Duplicate consumption records cannot authorize another provider stage."""

        executor = _make_executor(run_verify_commands=False)
        pause = CoordinatorQuotaPause(
            execution_id="execution-owner",
            session_id="session-owner",
            level_number=1,
            coordinator_aggregate_id="execution-owner:l0:coord",
            consequence=UsageLimitPauseConsequence(
                reason="Usage limit reached",
                resume_hint="Resume after the provider window reopens.",
                pause_seconds=60,
                resume_after=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
            ),
        )
        consumed_event = BaseEvent(
            type="execution.coordinator.quota_pause_consumed",
            aggregate_type="execution",
            aggregate_id=pause.coordinator_aggregate_id,
            data=pause.owner_payload(),
        )
        executor._event_store.query_events = AsyncMock(
            return_value=[consumed_event, consumed_event]
        )

        with pytest.raises(
            RuntimeError,
            match="coordinator pause consumption state is ambiguous",
        ):
            await consume_published_coordinator_pause(
                event_store=executor._event_store,
                pause=pause,
                published_owner=pause.owner_payload(),
            )

        executor._event_store.append.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_records_coordinator_results_at_level_scope_without_ac_attribution(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Coordinator reconciliation should persist level-scoped events and artifacts only."""
        seed = _make_seed(
            "Update the shared module imports",
            "Wire the shared module into the runtime",
        )
        graph = DependencyGraph(
            nodes=(
                ACNode(index=0, content=seed.acceptance_criteria[0], depends_on=()),
                ACNode(index=1, content=seed.acceptance_criteria[1], depends_on=()),
            ),
            execution_levels=((0, 1),),
        )
        coordinator_review = CoordinatorReview(
            level_number=1,
            conflicts_detected=(
                FileConflict(
                    file_path="src/shared.py",
                    ac_indices=(0, 1),
                    resolved=True,
                    resolution_description="Merged by coordinator",
                ),
            ),
            review_summary="Resolved shared.py conflict",
            fixes_applied=("Merged overlapping import edits",),
            warnings_for_next_level=("Verify shared.py integration paths",),
            duration_seconds=1.5,
            session_id="coord-session-1",
            session_scope_id="level_1_coordinator",
            session_state_path=".ouroboros/execution_runtime/level_1_coordinator/session.json",
            final_output=(
                '{"review_summary":"Resolved shared.py conflict",'
                '"fixes_applied":["Merged overlapping import edits"],'
                '"warnings_for_next_level":["Verify shared.py integration paths"],'
                '"conflicts_resolved":["src/shared.py"]}'
            ),
            messages=(
                AgentMessage(
                    type="assistant",
                    content="Inspecting shared file",
                    tool_name="Read",
                    data={"tool_input": {"file_path": "src/shared.py"}},
                ),
                AgentMessage(
                    type="assistant",
                    content="Reconciling overlap",
                    data={"thinking": "Merge the import changes without changing behavior."},
                ),
            ),
        )
        monkeypatch.setattr(
            LevelCoordinator,
            "run_review",
            AsyncMock(return_value=coordinator_review),
        )
        executor = _make_executor()
        executor._coordinator.detect_file_conflicts = MagicMock(
            return_value=[FileConflict(file_path="src/shared.py", ac_indices=(0, 1))]
        )

        async def fake_execute_single_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            return ACExecutionResult(
                ac_index=ac_index,
                ac_content=str(kwargs["ac_content"]),
                success=True,
                messages=(
                    AgentMessage(
                        type="assistant",
                        content="Editing shared module",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": "src/shared.py"}},
                    ),
                ),
                final_message=f"AC {ac_index + 1} complete",
            )

        executor._execute_single_ac = fake_execute_single_ac  # type: ignore[method-assign]

        result = await executor.execute_parallel(
            seed=seed,
            execution_plan=graph.to_execution_plan(),
            session_id="sess_coord_scope",
            execution_id="exec_coord_scope",
            tools=["Read", "Edit"],
            system_prompt="test",
        )

        appended_events = [call.args[0] for call in executor._event_store.append.await_args_list]
        outcome_events = [
            event for event in appended_events if event.type == "execution.ac.attempt_judged"
        ]
        coordinator_events = [
            event for event in appended_events if event.type.startswith("execution.coordinator.")
        ]

        assert result.success_count == 2
        assert len(result.stages) == 1
        assert result.stages[0].coordinator_review is not None
        assert result.stages[0].coordinator_review.review_summary == "Resolved shared.py conflict"
        assert result.stages[0].coordinator_review.artifact_scope == "level"
        assert result.stages[0].coordinator_review.artifact_owner == "coordinator"
        assert result.stages[0].coordinator_review.artifact_owner_id == "level_1_coordinator"

        assert len(outcome_events) == 2
        assert all(event.data["success"] is True for event in outcome_events)
        assert [event.type for event in coordinator_events] == [
            "execution.coordinator.started",
            "execution.coordinator.tool.started",
            "execution.coordinator.thinking",
            "execution.coordinator.completed",
        ]
        for event in coordinator_events:
            assert event.aggregate_id == "exec_coord_scope:l0:coord"
            assert event.data["scope"] == "level"
            assert event.data["session_role"] == "coordinator"
            assert event.data["level_number"] == 1
            assert event.data["stage_index"] == 0
            assert "ac_id" not in event.data
            assert "ac_index" not in event.data
            assert "acceptance_criterion" not in event.data

        assert coordinator_events[-1].data["artifact_type"] == "coordinator_review"
        assert coordinator_events[-1].data["artifact_scope"] == "level"
        assert coordinator_events[-1].data["artifact_owner"] == "coordinator"
        assert coordinator_events[-1].data["artifact_owner_id"] == "level_1_coordinator"
        assert (
            coordinator_events[-1].data["artifact"]
            == '{"review_summary":"Resolved shared.py conflict","fixes_applied":["Merged overlapping import edits"],"warnings_for_next_level":["Verify shared.py integration paths"],"conflicts_resolved":["src/shared.py"]}'
        )

    @pytest.mark.asyncio
    async def test_coordinator_persists_all_post_effect_conflicts_without_fixed_cap(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A valid stage result population stays serializable after provider effects."""

        conflict_paths = tuple(f"src/generated_{index}.py" for index in range(4_097))

        async def run_review(
            _coordinator: LevelCoordinator,
            execution_id: str,
            conflicts: list[FileConflict],
            level_context: LevelContext,
            level_number: int,
            **_kwargs: Any,
        ) -> CoordinatorReview:
            del level_context
            runtime_scope = build_level_coordinator_runtime_scope(execution_id, level_number)
            return CoordinatorReview(
                level_number=level_number,
                conflicts_detected=tuple(conflicts),
                review_summary="Reviewed complete conflict population",
                duration_seconds=1.0,
                session_scope_id=runtime_scope.aggregate_id,
                session_state_path=runtime_scope.state_path,
                final_output="coordinator final output",
            )

        monkeypatch.setattr(LevelCoordinator, "run_review", run_review)
        seed = _make_seed("Produce shared files", "Integrate shared files")
        graph = DependencyGraph(
            nodes=(
                ACNode(index=0, content=seed.acceptance_criteria[0], depends_on=()),
                ACNode(index=1, content=seed.acceptance_criteria[1], depends_on=()),
            ),
            execution_levels=((0, 1),),
        )
        executor = _make_executor()
        executor._coordinator.detect_file_conflicts = LevelCoordinator.detect_file_conflicts

        async def execute_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            return ACExecutionResult(
                ac_index=ac_index,
                ac_content=str(kwargs["ac_content"]),
                success=True,
                final_message=f"AC {ac_index} complete",
                conflict_files=conflict_paths,
            )

        executor._execute_single_ac = execute_ac  # type: ignore[method-assign]
        result = await executor.execute_parallel(
            seed=seed,
            execution_plan=graph.to_execution_plan(),
            session_id="session-conflict-population",
            execution_id="execution-conflict-population",
            tools=["Read", "Edit"],
            system_prompt="test",
        )

        coordinator_events = [
            call.args[0]
            for call in executor._event_store.append.await_args_list
            if call.args[0].type
            in {
                "execution.coordinator.started",
                "execution.coordinator.completed",
            }
        ]
        assert result.success_count == 2
        assert len(coordinator_events) == 2
        assert coordinator_events[0].data["conflict_count"] == len(conflict_paths)
        assert len(coordinator_events[0].data["conflicts"]) == len(conflict_paths)
        assert len(coordinator_events[1].data["conflicts_detected"]) == len(conflict_paths)

    @pytest.mark.asyncio
    async def test_completed_coordinator_event_restores_without_repeating_provider_effect(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The full started/completed population is the no-checkpoint replay owner."""

        from ouroboros.orchestrator.execution_runtime_scope import (
            build_level_coordinator_runtime_scope,
        )

        seed = _make_seed("Update shared.py", "Integrate shared.py")
        graph = DependencyGraph(
            nodes=(
                ACNode(index=0, content=seed.acceptance_criteria[0], depends_on=()),
                ACNode(index=1, content=seed.acceptance_criteria[1], depends_on=()),
            ),
            execution_levels=((0, 1),),
        )
        execution_id = "exec_coord_replay"
        session_id = "sess_coord_replay"
        runtime_scope = build_level_coordinator_runtime_scope(execution_id, 1)
        conflict = FileConflict(file_path="src/shared.py", ac_indices=(0, 1))
        completed_review = CoordinatorReview(
            level_number=1,
            conflicts_detected=(
                replace(
                    conflict,
                    resolved=True,
                    resolution_description="Resolved by Coordinator",
                ),
            ),
            review_summary="Reconciled shared.py once",
            fixes_applied=("Merged overlapping edits",),
            warnings_for_next_level=("Keep the merged interface",),
            duration_seconds=1.25,
            session_id="coordinator-native-session",
            session_scope_id=runtime_scope.aggregate_id,
            session_state_path=runtime_scope.state_path,
            final_output="coordinator final output",
        )
        review_provider = AsyncMock(return_value=completed_review)
        monkeypatch.setattr(LevelCoordinator, "run_review", review_provider)

        async def execute_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            return ACExecutionResult(
                ac_index=ac_index,
                ac_content=str(kwargs["ac_content"]),
                success=True,
                conflict_files=("src/shared.py",),
                final_message=f"AC {ac_index} complete",
            )

        first = _make_executor(reasoning_effort="low")
        first._event_store.query_events = AsyncMock(return_value=[])
        first._coordinator.detect_file_conflicts = MagicMock(return_value=[conflict])
        first._execute_single_ac = execute_ac  # type: ignore[method-assign]
        await first.execute_parallel(
            seed=seed,
            execution_plan=graph.to_execution_plan(),
            session_id=session_id,
            execution_id=execution_id,
            tools=["Read", "Edit"],
            system_prompt="test",
        )
        first_events = [call.args[0] for call in first._event_store.append.await_args_list]

        async def replay_query(*_args: Any, **kwargs: Any) -> list[BaseEvent]:
            event_type = kwargs.get("event_type")
            return [event for event in first_events if event.type == event_type]

        resumed = _make_executor(reasoning_effort="low")
        resumed._event_store.query_events = AsyncMock(side_effect=replay_query)
        resumed._coordinator.detect_file_conflicts = MagicMock(return_value=[conflict])
        resumed._execute_single_ac = execute_ac  # type: ignore[method-assign]
        replayed = await resumed.execute_parallel(
            seed=seed,
            execution_plan=graph.to_execution_plan(),
            session_id=session_id,
            execution_id=execution_id,
            tools=["Read", "Edit"],
            system_prompt="test",
        )

        assert review_provider.await_count == 1
        assert resumed._coordinator._reasoning_effort == "low"
        assert replayed.stages[0].coordinator_review is not None
        assert replayed.stages[0].coordinator_review.review_summary == "Reconciled shared.py once"
        replayed_coordinator_writes = [
            call.args[0]
            for call in resumed._event_store.append.await_args_list
            if getattr(call.args[0], "type", "").startswith("execution.coordinator.")
        ]
        assert replayed_coordinator_writes == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("started_count", "completed_count"),
        ((1, 0), (0, 1), (2, 1), (1, 2)),
    )
    async def test_incomplete_or_ambiguous_coordinator_population_fails_closed(
        self,
        started_count: int,
        completed_count: int,
    ) -> None:
        executor = _make_executor()
        conflict = FileConflict(file_path="src/shared.py", ac_indices=(0, 1))

        async def query(*_args: Any, **kwargs: Any) -> list[BaseEvent]:
            event_type = kwargs["event_type"]
            count = (
                started_count if event_type == "execution.coordinator.started" else completed_count
            )
            return [
                BaseEvent(
                    type=event_type,
                    aggregate_type="execution",
                    aggregate_id="execution-1:l0:coord",
                    data={},
                )
                for _ in range(count)
            ]

        executor._event_store.query_events = AsyncMock(side_effect=query)
        with pytest.raises(RuntimeError, match="incomplete or ambiguous"):
            await executor._restore_completed_coordinator_review(
                execution_id="execution-1",
                session_id="session-1",
                level=1,
                conflicts=[conflict],
            )

    @pytest.mark.asyncio
    async def test_returns_reconciled_level_contexts_for_retry_handoff(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Completed stage contexts should be returned for retry workspace handoff."""
        seed = _make_seed(
            "Land the shared runtime update",
            "Repair the follow-up integration",
        )
        graph = DependencyGraph(
            nodes=(
                ACNode(index=0, content=seed.acceptance_criteria[0], depends_on=()),
                ACNode(index=1, content=seed.acceptance_criteria[1], depends_on=()),
            ),
            execution_levels=((0, 1),),
        )
        coordinator_review = CoordinatorReview(
            level_number=1,
            review_summary="Reconciled shared workspace",
            fixes_applied=("Merged shared.py edits",),
            warnings_for_next_level=("Retry AC 2 against the merged shared.py state",),
        )
        monkeypatch.setattr(
            LevelCoordinator,
            "run_review",
            AsyncMock(return_value=coordinator_review),
        )
        executor = _make_executor()
        executor._coordinator.detect_file_conflicts = MagicMock(
            return_value=[FileConflict(file_path="src/shared.py", ac_indices=(0, 1))]
        )

        async def fake_execute_single_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            ac_content = str(kwargs["ac_content"])
            if ac_index == 0:
                return ACExecutionResult(
                    ac_index=ac_index,
                    ac_content=ac_content,
                    success=True,
                    messages=(
                        AgentMessage(
                            type="assistant",
                            content="Updated shared module",
                            tool_name="Edit",
                            data={"tool_input": {"file_path": "src/shared.py"}},
                        ),
                    ),
                    final_message="Shared runtime landed",
                )
            return ACExecutionResult(
                ac_index=ac_index,
                ac_content=ac_content,
                success=False,
                messages=(
                    AgentMessage(
                        type="assistant",
                        content="Need to revisit integration",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": "src/shared.py"}},
                    ),
                ),
                error="Integration failed",
            )

        executor._execute_single_ac = fake_execute_single_ac  # type: ignore[method-assign]

        result = await executor.execute_parallel(
            seed=seed,
            execution_plan=graph.to_execution_plan(),
            session_id="sess_retry_handoff",
            execution_id="exec_retry_handoff",
            tools=["Read", "Edit"],
            system_prompt="test",
        )

        assert len(result.reconciled_level_contexts) == 1
        handoff = result.reconciled_level_contexts[0]
        assert handoff.level_number == 1
        assert handoff.coordinator_review is not None
        assert handoff.coordinator_review.review_summary == "Reconciled shared workspace"
        assert handoff.completed_acs[0].success is True

    @pytest.mark.asyncio
    async def test_reopened_execution_uses_reconciled_workspace_handoff(self) -> None:
        """Retries should seed reopened ACs with the latest reconciled workspace context."""
        seed = _make_seed("Retry the failed shared runtime integration")
        graph = DependencyGraph(
            nodes=(ACNode(index=0, content=seed.acceptance_criteria[0], depends_on=()),),
            execution_levels=((0,),),
        )
        executor = _make_executor()
        handoff = LevelContext(
            level_number=1,
            completed_acs=(),
            coordinator_review=CoordinatorReview(
                level_number=1,
                review_summary="Workspace was reconciled after the previous failure",
                fixes_applied=("Merged shared.py before retry",),
                warnings_for_next_level=(
                    "Build on the reconciled shared.py, not the earlier draft",
                ),
            ),
        )
        captured_contexts: list[LevelContext] = []

        async def fake_execute_single_ac(**kwargs: Any) -> ACExecutionResult:
            captured_contexts.extend(kwargs["level_contexts"])
            return ACExecutionResult(
                ac_index=int(kwargs["ac_index"]),
                ac_content=str(kwargs["ac_content"]),
                success=True,
                final_message="Retried successfully",
            )

        executor._execute_single_ac = fake_execute_single_ac  # type: ignore[method-assign]

        result = await executor.execute_parallel(
            seed=seed,
            execution_plan=graph.to_execution_plan(),
            session_id="sess_retry_reopen",
            execution_id="exec_retry_reopen",
            tools=["Read", "Edit"],
            system_prompt="test",
            reconciled_level_contexts=[handoff],
        )

        assert result.success_count == 1
        assert captured_contexts == [handoff]

    @pytest.mark.asyncio
    async def test_atomic_ac_events_include_retry_attempt_metadata(self) -> None:
        """AC-scoped runtime events should preserve AC id while recording retry attempts."""

        class StubRuntime:
            _runtime_handle_backend = "opencode"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return "/tmp/project"

            @property
            def permission_mode(self) -> str | None:
                return "acceptEdits"

            async def execute_task(self, **kwargs: Any):
                resume_handle = kwargs["resume_handle"]
                assert isinstance(resume_handle, RuntimeHandle)
                assert resume_handle.metadata["retry_attempt"] == 2
                yield AgentMessage(
                    type="assistant",
                    content="Retrying the implementation",
                    tool_name="Edit",
                    data={
                        "tool_input": {"file_path": "src/app.py"},
                        "thinking": "Reopen the same AC with a fresh runtime session.",
                    },
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                )

        event_store = AsyncMock()
        executor = ParallelACExecutor(
            adapter=StubRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=3,
            ac_content="Fix the failing AC",
            session_id="sess_retry",
            tools=["Edit"],
            system_prompt="test",
            seed_goal="Ship the fix",
            depth=0,
            start_time=datetime.now(UTC),
            retry_attempt=2,
        )

        appended_events = [call.args[0] for call in event_store.append.await_args_list]

        assert result.success is True
        assert result.retry_attempt == 2
        assert result.attempt_number == 3
        tool_event = next(
            event for event in appended_events if event.type == "execution.tool.started"
        )
        thinking_event = next(
            event for event in appended_events if event.type == "execution.agent.thinking"
        )
        completed_event = next(
            event for event in appended_events if event.type == "execution.session.completed"
        )

        assert tool_event.aggregate_id == "sess_retry_ac_4"
        assert tool_event.data["ac_id"] == "sess_retry_ac_4"
        assert tool_event.data["retry_attempt"] == 2
        assert tool_event.data["attempt_number"] == 3
        assert tool_event.data["session_attempt_id"] == "sess_retry_ac_4_attempt_3"
        assert thinking_event.aggregate_id == "sess_retry_ac_4"
        assert thinking_event.data["ac_id"] == "sess_retry_ac_4"
        assert thinking_event.data["retry_attempt"] == 2
        assert thinking_event.data["attempt_number"] == 3
        assert thinking_event.data["session_attempt_id"] == "sess_retry_ac_4_attempt_3"
        assert completed_event.aggregate_id == "sess_retry_ac_4"
        assert completed_event.data["ac_id"] == "sess_retry_ac_4"
        assert completed_event.data["retry_attempt"] == 2
        assert completed_event.data["attempt_number"] == 3
        assert completed_event.data["session_attempt_id"] == "sess_retry_ac_4_attempt_3"
        assert completed_event.data["success"] is True

    @pytest.mark.asyncio
    async def test_atomic_ac_events_capture_opencode_tool_metadata_and_results(self) -> None:
        """OpenCode AC sessions should emit normalized tool start/completion metadata."""
        from ouroboros.orchestrator.mcp_tools import (
            normalize_runtime_tool_definition,
            normalize_runtime_tool_result,
        )

        class StubRuntime:
            _runtime_handle_backend = "opencode"
            _cwd = "/tmp/project"
            _permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(self, **kwargs: Any):
                resume_handle = kwargs["resume_handle"]
                assert isinstance(resume_handle, RuntimeHandle)
                runtime_handle = RuntimeHandle(
                    backend="opencode",
                    native_session_id="oc-session-7",
                    cwd="/tmp/project",
                    approval_mode="acceptEdits",
                    metadata={"runtime_event_type": "tool.started"},
                )
                yield AgentMessage(
                    type="assistant",
                    content="Calling tool: Edit: src/app.py",
                    tool_name="Edit",
                    data={
                        "tool_input": {"file_path": "src/app.py"},
                        "tool_definition": normalize_runtime_tool_definition(
                            "Edit",
                            {"file_path": "src/app.py"},
                        ),
                    },
                    resume_handle=runtime_handle,
                )
                yield AgentMessage(
                    type="assistant",
                    content="Updated src/app.py",
                    data={
                        "subtype": "tool_result",
                        "tool_name": "Edit",
                        "tool_result": normalize_runtime_tool_result("Updated src/app.py"),
                    },
                    resume_handle=RuntimeHandle(
                        backend="opencode",
                        native_session_id="oc-session-7",
                        cwd="/tmp/project",
                        approval_mode="acceptEdits",
                        metadata={"runtime_event_type": "tool.completed"},
                    ),
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                )

        event_store = AsyncMock()
        executor = ParallelACExecutor(
            adapter=StubRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Wire OpenCode runtime events",
            session_id="sess_opencode",
            tools=["Edit"],
            system_prompt="test",
            seed_goal="Ship the adapter",
            depth=0,
            start_time=datetime.now(UTC),
        )

        appended_events = [call.args[0] for call in event_store.append.await_args_list]
        tool_started = next(
            event for event in appended_events if event.type == "execution.tool.started"
        )
        tool_completed = next(
            event for event in appended_events if event.type == "execution.tool.completed"
        )

        assert result.success is True
        assert tool_started.data["tool_definition"]["name"] == "Edit"
        assert tool_started.data["runtime_backend"] == "opencode"
        assert tool_started.data["runtime"]["native_session_id"] == "oc-session-7"
        assert tool_completed.data["tool_name"] == "Edit"
        assert tool_completed.data["tool_result"]["text_content"] == "Updated src/app.py"
        assert tool_completed.data["runtime_event_type"] == "tool.completed"

    @pytest.mark.asyncio
    async def test_atomic_ac_projects_codex_completion_receipt_to_journal(self) -> None:
        from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime

        class StubRuntime:
            _runtime_handle_backend = "codex_cli"
            _cwd = "/tmp/project"
            _permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(self, **kwargs: Any):
                runtime = CodexCliRuntime(cli_path="codex", cwd=self._cwd)
                for message in runtime._convert_event(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "cmd-42",
                            "type": "command_execution",
                            "command": "pytest -q tests/test_example.py",
                            "stdout": "1 passed in 0.01s",
                            "exit_code": 0,
                        },
                    },
                    current_handle=kwargs["resume_handle"],
                ):
                    yield message
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                )

        event_store = AsyncMock()
        executor = ParallelACExecutor(
            adapter=StubRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Run the focused test",
            session_id="sess_codex",
            tools=["Bash"],
            system_prompt="test",
            seed_goal="Ship the fix",
            depth=0,
            start_time=datetime.now(UTC),
        )

        appended_events = [call.args[0] for call in event_store.append.await_args_list]
        tool_started = next(
            event for event in appended_events if event.type == "execution.tool.started"
        )
        tool_completed = next(
            event for event in appended_events if event.type == "execution.tool.completed"
        )

        assert result.success is True
        assert tool_started.data["tool_call_id"] == "cmd-42"
        assert tool_started.data["tool_input"] == {"command": "pytest -q tests/test_example.py"}
        assert tool_completed.data["tool_call_id"] == "cmd-42"
        assert tool_completed.data["runtime_event_type"] == "tool.result"
        assert tool_completed.data["tool_result"]["is_error"] is False
        assert tool_completed.data["tool_result"]["meta"]["exit_status"] == 0

    @pytest.mark.asyncio
    async def test_atomic_ac_projects_gemini_tool_result_to_completed_journal(self) -> None:
        from ouroboros.orchestrator.gemini_cli_runtime import GeminiCLIRuntime

        class StubRuntime:
            _runtime_handle_backend = "gemini_cli"
            _cwd = "/tmp/project"
            _permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(self, **kwargs: Any):
                runtime = GeminiCLIRuntime(cli_path="gemini", cwd=self._cwd)
                raw_events = (
                    {
                        "type": "tool_use",
                        "name": "run_shell",
                        "input": {"command": "pytest -q tests/test_example.py"},
                    },
                    {
                        "type": "tool_result",
                        "name": "run_shell",
                        "output": "1 passed in 0.01s",
                        "is_error": False,
                    },
                )
                for raw_event in raw_events:
                    event = runtime._parse_json_event(json.dumps(raw_event))
                    assert event is not None
                    for message in runtime._convert_event(
                        event,
                        current_handle=kwargs["resume_handle"],
                    ):
                        yield message
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                )

        event_store = AsyncMock()
        executor = ParallelACExecutor(
            adapter=StubRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Run the focused test with Gemini",
            session_id="sess_gemini",
            tools=["Bash"],
            system_prompt="test",
            seed_goal="Ship the fix",
            depth=0,
            start_time=datetime.now(UTC),
        )

        appended_events = [call.args[0] for call in event_store.append.await_args_list]
        tool_started = next(
            event for event in appended_events if event.type == "execution.tool.started"
        )
        tool_completed = next(
            event for event in appended_events if event.type == "execution.tool.completed"
        )

        assert result.success is True
        assert tool_started.data["tool_input"] == {"command": "pytest -q tests/test_example.py"}
        assert tool_completed.data["tool_name"] == "Bash"
        assert tool_completed.data["tool_result"]["is_error"] is False
        assert tool_completed.data["tool_result"]["text_content"] == "1 passed in 0.01s"

    def test_message_level_tool_completion_event_type_is_not_terminal(self) -> None:
        """A finished tool must not classify the runtime handle as terminated.

        Message-level `runtime_event_type` now takes priority over stale handle
        metadata, so the adapter's completion vocabulary reaches lifecycle
        classification directly. `_runtime_handle_lifecycle_state` maps any value
        containing "completed" (outside the `message.`/`result.`/`turn.` prefixes)
        onto the terminal `completed` state, so naming a per-tool completion
        `tool.completed` would end the runtime after its first tool.
        """
        from ouroboros.orchestrator.adapter import (
            _RUNTIME_TERMINAL_STATES,
            _runtime_handle_lifecycle_state,
        )
        from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime

        runtime = CodexCliRuntime()
        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "id": "cmd-99",
                    "type": "command_execution",
                    "command": "pytest -q",
                    "exit_code": 0,
                    "status": "completed",
                },
            },
            None,
        )

        event_types = [
            message.data.get("runtime_event_type")
            for message in messages
            if message.data.get("runtime_event_type")
        ]
        assert event_types, "completion conversion must stamp a runtime_event_type"

        for event_type in event_types:
            lifecycle = _runtime_handle_lifecycle_state(event_type, has_session_id=True)
            assert lifecycle not in _RUNTIME_TERMINAL_STATES, (
                f"per-tool event type {event_type!r} classifies the runtime as "
                f"{lifecycle!r}, which is terminal"
            )

    def test_codex_id_bearing_exit_only_test_completion_does_not_prove_file_claim(self) -> None:
        from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime

        command = "pytest -q tests/test_example.py"
        messages = tuple(
            CodexCliRuntime(cli_path="codex", cwd="/tmp/project")._convert_event(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "cmd-exit-only",
                        "type": "command_execution",
                        "command": command,
                        "exit_code": 0,
                    },
                },
                current_handle=None,
            )
        )

        assert not _runtime_messages_support_test_claim(
            value="tests/test_example.py",
            backed_commands=(command,),
            messages=messages,
            task_cwd=None,
        )

    @pytest.mark.asyncio
    async def test_atomic_ac_projects_empty_tool_result_content_into_completion_events(
        self,
    ) -> None:
        """Tool-result projection should preserve completion text even when message content is empty."""
        from ouroboros.orchestrator.mcp_tools import normalize_runtime_tool_result

        class StubRuntime:
            _runtime_handle_backend = "opencode"
            _cwd = "/tmp/project"
            _permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(self, **kwargs: Any):
                resume_handle = kwargs["resume_handle"]
                assert isinstance(resume_handle, RuntimeHandle)
                yield AgentMessage(
                    type="assistant",
                    content="",
                    data={
                        "subtype": "tool_result",
                        "tool_name": "Edit",
                        "tool_result": normalize_runtime_tool_result("[AC_COMPLETE: 1] Done!"),
                    },
                    resume_handle=RuntimeHandle(
                        backend="opencode",
                        native_session_id="oc-session-8",
                        cwd="/tmp/project",
                        approval_mode="acceptEdits",
                        metadata={"runtime_event_type": "tool.completed"},
                    ),
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                )

        event_store = AsyncMock()
        executor = ParallelACExecutor(
            adapter=StubRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Project OpenCode completion markers",
            session_id="sess_projection",
            tools=["Edit"],
            system_prompt="test",
            seed_goal="Ship the projection wiring",
            depth=0,
            start_time=datetime.now(UTC),
        )

        appended_events = [call.args[0] for call in event_store.append.await_args_list]
        tool_completed = next(
            event for event in appended_events if event.type == "execution.tool.completed"
        )

        assert result.success is True
        assert tool_completed.data["tool_result_text"] == "[AC_COMPLETE: 1] Done!"
        assert tool_completed.data["tool_result"]["text_content"] == "[AC_COMPLETE: 1] Done!"

    @pytest.mark.asyncio
    async def test_restarted_executor_skips_invalid_event_and_resumes_from_valid_one(
        self,
    ) -> None:
        """When an invalid persisted event precedes a valid one, resume from the valid event."""

        class _StubResumeAfterInvalidRuntime:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                self.calls.append(
                    {
                        "prompt": prompt,
                        "tools": tools,
                        "system_prompt": system_prompt,
                        "resume_handle": resume_handle,
                        "resume_session_id": resume_session_id,
                    }
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=resume_handle,
                )

        valid_handle = RuntimeHandle(
            backend="opencode",
            kind="implementation_session",
            native_session_id="opencode-session-valid",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            metadata={
                "scope": "ac",
                "session_role": "implementation",
                "retry_attempt": 0,
                "ac_index": 1,
                "session_scope_id": "orch_123_ac_2",
                "session_state_path": (
                    "execution.workflows.orch_123.acceptance_criteria.ac_2.implementation_session"
                ),
                "server_session_id": "server-valid",
            },
        )
        event_store = AsyncMock()
        event_store.replay = AsyncMock(
            return_value=[
                # First event: valid handle
                BaseEvent(
                    type="execution.session.started",
                    aggregate_type="execution",
                    aggregate_id="orch_123_ac_2",
                    data={
                        "retry_attempt": 0,
                        "session_state_path": (
                            "execution.workflows.orch_123.acceptance_criteria."
                            "ac_2.implementation_session"
                        ),
                        "runtime": valid_handle.to_dict(),
                    },
                ),
                # Second event: invalid handle (no backend/provider)
                BaseEvent(
                    type="execution.session.resumed",
                    aggregate_type="execution",
                    aggregate_id="orch_123_ac_2",
                    data={
                        "retry_attempt": 0,
                        "session_state_path": (
                            "execution.workflows.orch_123.acceptance_criteria."
                            "ac_2.implementation_session"
                        ),
                        "runtime": {
                            "kind": "implementation_session",
                            "cwd": "/tmp/project",
                            "metadata": {},
                        },
                    },
                ),
            ]
        )
        event_store.append = AsyncMock()
        runtime = _StubResumeAfterInvalidRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Resume after skipping invalid persisted event",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            retry_attempt=0,
        )

        resume_handle = runtime.calls[0]["resume_handle"]
        assert isinstance(resume_handle, RuntimeHandle)
        # Historical events without capsule authority are not eligible for
        # cross-process continuation, even when one contains a valid handle.
        assert resume_handle.native_session_id is None
        assert "server_session_id" not in resume_handle.metadata
        assert result.runtime_handle is not None
        assert result.runtime_handle.native_session_id == resume_handle.native_session_id
        assert result.runtime_handle.metadata == resume_handle.metadata


@pytest.mark.asyncio
async def test_try_decompose_ac_replaces_goose_chunks_with_final_result() -> None:
    """Goose can emit deltas plus a final full answer; decomposition should not duplicate."""

    class _GooseChunkAndFinalRuntime:
        runtime_backend = "goose"

        async def execute_task(
            self,
            prompt: str,
            tools: list[str] | None = None,
            system_prompt: str | None = None,
            resume_handle: RuntimeHandle | None = None,
            resume_session_id: str | None = None,
        ):
            del prompt, tools, system_prompt, resume_handle, resume_session_id
            yield AgentMessage(type="assistant", content='["Sub-AC 1: inspect", ')
            yield AgentMessage(type="assistant", content='"Sub-AC 2: test"]')
            yield AgentMessage(
                type="result",
                content='["Sub-AC 1: inspect", "Sub-AC 2: test"]',
            )

    executor = ParallelACExecutor(
        adapter=_GooseChunkAndFinalRuntime(),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=True,
    )

    response = await executor._dispatch_decomposition_prompt(
        prompt="Investigate and test sub-AC behavior.",
        system_prompt="system",
    )

    assert response == '["Sub-AC 1: inspect", "Sub-AC 2: test"]'


@pytest.mark.asyncio
async def test_decomposition_policy_usage_is_included_in_generation_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ouroboros.evolution import provider_usage as provider_usage_module
    from ouroboros.evolution.provider_usage import capture_generation_provider_usage

    class _MeasuredRuntime:
        runtime_backend = "goose"
        llm_backend = "test-provider"
        _model = "test-model"

        def frugality_runtime_attestation(self) -> dict[str, object]:
            implementation = f"{type(self).__module__}.{type(self).__qualname__}"
            return {
                "schema_version": 1,
                "schema_id": "test.decomposition_runtime.v1",
                "implementation": implementation,
                "runtime_backend": "goose",
                "runtime_handle_backend": "goose",
                "settings": {"fixture": "decomposition"},
            }

        async def execute_task(self, **kwargs):  # noqa: ANN201, ARG002
            yield AgentMessage(
                type="result",
                content='["Sub-AC 1: inspect", "Sub-AC 2: test"]',
                data={
                    "model_observation": {"effective_model": "test-model"},
                    "usage": {"total_tokens": 150},
                },
            )

    runtime_class = f"{_MeasuredRuntime.__module__}.{_MeasuredRuntime.__qualname__}"
    monkeypatch.setitem(
        provider_usage_module._AGENT_RUNTIME_SCHEMAS,
        runtime_class,
        provider_usage_module._AgentRuntimeSchema(
            schema_id="test.decomposition_runtime.v1",
            runtime_backend="goose",
            runtime_handle_backend="goose",
            setting_kinds={"fixture": "text"},
        ),
    )

    executor = ParallelACExecutor(
        adapter=_MeasuredRuntime(),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=True,
    )

    with capture_generation_provider_usage() as capture:
        response = await executor._dispatch_decomposition_prompt(
            prompt="Investigate and test sub-AC behavior.",
            system_prompt="system",
        )

    assert response == '["Sub-AC 1: inspect", "Sub-AC 2: test"]'
    summary = capture.summary()
    assert summary.complete is True
    assert summary.call_count == 1
    assert summary.token_spend == 150


@pytest.mark.asyncio
async def test_try_decompose_ac_accumulates_goose_stream_chunks() -> None:
    """Goose stream-json emits token chunks; decomposition must parse accumulated output."""

    class _GooseChunkRuntime:
        runtime_backend = "goose"

        async def execute_task(
            self,
            prompt: str,
            tools: list[str] | None = None,
            system_prompt: str | None = None,
            resume_handle: RuntimeHandle | None = None,
            resume_session_id: str | None = None,
        ):
            del prompt, tools, system_prompt, resume_handle, resume_session_id
            yield AgentMessage(type="system", content="Session initialized: sess-1")
            for chunk in (
                '["Sub-AC 1: inspect the implementation", ',
                '"Sub-AC 2: write a focused regression test", ',
                '"Sub-AC 3: document the result"]',
            ):
                yield AgentMessage(type="assistant", content=chunk)

    executor = ParallelACExecutor(
        adapter=_GooseChunkRuntime(),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=True,
    )

    response = await executor._dispatch_decomposition_prompt(
        prompt="Investigate, test, and document sub-AC behavior.",
        system_prompt="system",
    )

    assert response == (
        '["Sub-AC 1: inspect the implementation", '
        '"Sub-AC 2: write a focused regression test", '
        '"Sub-AC 3: document the result"]'
    )


@pytest.mark.asyncio
async def test_try_decompose_ac_announces_same_empty_tools_allowlist_it_dispatches() -> None:
    class _CapturingRuntime:
        runtime_backend = "codex_cli"
        capabilities = RuntimeCapabilities(
            skill_dispatch=True,
            targeted_resume=True,
            structured_output=True,
            tool_restriction_support=ParamSupport.TRANSLATED,
            empty_tool_restriction_support=ParamSupport.IGNORED,
        )

        def __init__(self) -> None:
            self.dispatched_tools: list[str] | None = None

        async def execute_task(
            self,
            prompt: str,
            tools: list[str] | None = None,
            system_prompt: str | None = None,
            resume_handle: RuntimeHandle | None = None,
            resume_session_id: str | None = None,
        ):
            del prompt, system_prompt, resume_handle, resume_session_id
            self.dispatched_tools = tools
            yield AgentMessage(type="assistant", content='["Sub-AC 1: inspect", "Sub-AC 2: test"]')

    runtime = _CapturingRuntime()
    console = MagicMock()
    executor = ParallelACExecutor(
        adapter=runtime,
        event_store=AsyncMock(),
        console=console,
        enable_decomposition=True,
    )

    response = await executor._dispatch_decomposition_prompt(
        prompt="Investigate and test sub-AC behavior.",
        system_prompt="system",
    )

    assert response == '["Sub-AC 1: inspect", "Sub-AC 2: test"]'
    assert runtime.dispatched_tools == []
    console.print.assert_called_once()
    notice = console.print.call_args.args[0]
    assert "tools" in notice
    assert "ignored" in notice


class _ParamCapsStubAdapter:
    """Minimal adapter exposing the attributes the param-degradation hook reads."""

    def __init__(self, capabilities: RuntimeCapabilities) -> None:
        self.capabilities = capabilities
        self.runtime_backend = "hermes_cli"
        self.permission_mode = "acceptEdits"
        self.working_directory = "/workspace"


def _make_param_executor(capabilities: RuntimeCapabilities) -> ParallelACExecutor:
    return ParallelACExecutor(
        adapter=_ParamCapsStubAdapter(capabilities),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
    )


class TestParamDegradationNotice:
    """The executor surfaces non-native param handling once per run."""

    def test_translated_system_prompt_surfaces_one_notice(self) -> None:
        caps = RuntimeCapabilities(
            skill_dispatch=True,
            targeted_resume=True,
            structured_output=True,
            system_prompt_support=ParamSupport.TRANSLATED,
        )
        executor = _make_param_executor(caps)

        # Two dispatches with the same degraded param → surfaced once (deduped).
        executor._announce_param_degradations(system_prompt="be terse", tools=None)
        executor._announce_param_degradations(system_prompt="be terse", tools=None)

        assert executor._console.print.call_count == 1
        notice = executor._console.print.call_args.args[0]
        assert "system_prompt" in notice
        assert "hermes_cli" in notice

    def test_all_native_adapter_is_silent(self) -> None:
        executor = _make_param_executor(FULL_CAPABILITIES)

        executor._announce_param_degradations(system_prompt="be terse", tools=["Read"])

        executor._console.print.assert_not_called()

    def test_absent_system_prompt_produces_no_notice(self) -> None:
        caps = RuntimeCapabilities(
            skill_dispatch=True,
            targeted_resume=True,
            structured_output=True,
            system_prompt_support=ParamSupport.TRANSLATED,
        )
        executor = _make_param_executor(caps)

        executor._announce_param_degradations(system_prompt=None, tools=None)

        executor._console.print.assert_not_called()

    def test_empty_tools_allowlist_surfaces_one_notice(self) -> None:
        caps = RuntimeCapabilities(
            skill_dispatch=True,
            targeted_resume=True,
            structured_output=True,
            tool_restriction_support=ParamSupport.TRANSLATED,
            empty_tool_restriction_support=ParamSupport.IGNORED,
        )
        executor = _make_param_executor(caps)

        executor._announce_param_degradations(system_prompt=None, tools=[])
        executor._announce_param_degradations(system_prompt=None, tools=[])

        assert executor._console.print.call_count == 1
        notice = executor._console.print.call_args.args[0]
        assert "tools" in notice
        assert "ignored" in notice
