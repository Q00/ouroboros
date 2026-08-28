"""Opaque parent-owned Seed handoff for passive plugin execution."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import yaml

from ouroboros.core.seed_provenance import SeedGenerationReceipt
from ouroboros.orchestrator.contract_redaction import redact_hidden_contract_values

_HIDDEN_WORKER_KEYS = frozenset({"verify_command", "output_assertion"})


@dataclass
class SeedGenerationReceiptRegistry:
    """Bounded process-local authority for Seed generation provenance."""

    max_entries: int = 256
    _entries: OrderedDict[str, SeedGenerationReceipt] = field(
        default_factory=OrderedDict,
        init=False,
        repr=False,
    )

    def register(self, *, seed_id: str, gate_forced: bool | None) -> SeedGenerationReceipt:
        receipt = SeedGenerationReceipt(seed_id=seed_id, gate_forced=gate_forced)
        self._entries[seed_id] = receipt
        self._entries.move_to_end(seed_id)
        while len(self._entries) > max(1, self.max_entries):
            self._entries.popitem(last=False)
        return receipt

    def resolve(self, seed_id: str) -> SeedGenerationReceipt | None:
        receipt = self._entries.get(seed_id)
        if receipt is not None:
            self._entries.move_to_end(seed_id)
        return receipt


@dataclass(frozen=True, slots=True)
class WorkerSafeSeedProjection:
    """One Seed projection and the values hidden from every sibling field."""

    seed_content: str
    hidden_values: tuple[str, ...] = ()
    valid_mapping: bool = True

    def redact(self, text: str | None) -> str | None:
        if not self.valid_mapping:
            return None
        return (
            redact_hidden_contract_values(text, self.hidden_values)
            if isinstance(text, str)
            else None
        )

    def redact_value(self, value: Any) -> Any:
        return _redact_worker_value(value, self.hidden_values)


def _hidden_scalar_values(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, Mapping):
        return tuple(hidden for item in value.values() for hidden in _hidden_scalar_values(item))
    if isinstance(value, (list, tuple)):
        return tuple(hidden for item in value for hidden in _hidden_scalar_values(item))
    return ()


def _worker_safe_value(value: Any, hidden_values: list[str]) -> Any:
    if isinstance(value, Mapping):
        projected: dict[str, Any] = {}
        for key, item in value.items():
            rendered_key = str(key)
            if rendered_key in _HIDDEN_WORKER_KEYS:
                hidden_values.extend(_hidden_scalar_values(item))
                continue
            projected[rendered_key] = _worker_safe_value(item, hidden_values)
        return projected
    if isinstance(value, list):
        return [_worker_safe_value(item, hidden_values) for item in value]
    if isinstance(value, tuple):
        return [_worker_safe_value(item, hidden_values) for item in value]
    return value


def _redact_worker_value(value: Any, hidden_values: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        return redact_hidden_contract_values(value, hidden_values)
    if isinstance(value, Mapping):
        return {
            redact_hidden_contract_values(str(key), hidden_values): _redact_worker_value(
                item, hidden_values
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_worker_value(item, hidden_values) for item in value]
    if isinstance(value, tuple):
        return [_redact_worker_value(item, hidden_values) for item in value]
    return value


def project_worker_safe_seed(seed_content: str) -> WorkerSafeSeedProjection:
    """Project a Seed and retain its hidden values for sibling-field redaction.

    Malformed input fails closed: the raw text is never forwarded to a worker.
    """

    try:
        parsed = yaml.safe_load(seed_content)
    except yaml.YAMLError:
        return WorkerSafeSeedProjection(
            "# Seed omitted: invalid YAML; ask the parent to retry with a valid Seed.\n",
            valid_mapping=False,
        )
    if not isinstance(parsed, Mapping):
        return WorkerSafeSeedProjection(
            "# Seed omitted: expected a YAML mapping.\n", valid_mapping=False
        )
    hidden_values: list[str] = []
    projected = _worker_safe_value(parsed, hidden_values)
    hidden = tuple(hidden_values)
    projected = _redact_worker_value(projected, hidden)
    if projected == parsed:
        return WorkerSafeSeedProjection(seed_content, hidden)
    return WorkerSafeSeedProjection(
        yaml.safe_dump(
            projected,
            sort_keys=False,
            allow_unicode=True,
        ),
        hidden,
    )


def render_worker_safe_seed(seed_content: str) -> str:
    """Render a Seed without harness-owned verifier keys or values."""

    return project_worker_safe_seed(seed_content).seed_content


def plugin_evaluation_instruction(
    *,
    auto_evaluate: bool,
    auto_evolve: bool,
    seed_handoff_id: str | None,
) -> str:
    """Build the plugin child's explicit formal-evaluation successor contract."""

    if not auto_evaluate:
        return (
            "Formal evaluation auto-chain is disabled for this run; preserve the "
            "legacy manual next step `ooo evaluate <session_id>`."
        )
    seed_argument = (
        f"seed_handoff_id `{seed_handoff_id}`"
        if seed_handoff_id
        else "the worker-safe seed_content"
    )
    if not auto_evolve:
        return (
            "After every terminal evaluable execution result, including unsuccessful AC "
            "execution, run formal 3-stage evaluation without host involvement: call "
            "ouroboros_start_evaluate with the session_id, execution "
            f"artifact, {seed_argument}, working directory, and auto_evolve: false. "
            "This opt-out uses plugin delegation: when the call returns "
            "status `delegated_to_plugin` with no job_id, do not poll job tools. Treat "
            "the handoff as terminal and rely on the bridge-rendered evaluation child "
            "in the Task pane to deliver the final APPROVED/not-approved verdict. If "
            "evaluation fails, keep run success intact and report "
            "`ooo evaluate <session_id>` as the manual retry."
        )
    return (
        "After every terminal evaluable execution result, including unsuccessful AC "
        "execution, run formal 3-stage evaluation without host involvement: call "
        "ouroboros_start_evaluate with the session_id, execution "
        f"artifact, {seed_argument}, working directory, and auto_evolve: "
        f"{str(auto_evolve).lower()}; poll the returned job with "
        "ouroboros_job_wait/status. If its terminal result contains "
        "chained_ralph_job_id, poll that Ralph job to terminal too and report its "
        "convergence stop reason; otherwise include the final APPROVED/not-approved "
        "verdict in your report. If evaluation fails or times out, keep run success "
        "intact and report `ooo evaluate <session_id>` as the manual retry."
    )


@dataclass
class SeedHandoffRegistry:
    """Bounded process-local vault keyed by opaque plugin handoff IDs."""

    max_entries: int = 256
    generation_receipts: SeedGenerationReceiptRegistry = field(
        default_factory=SeedGenerationReceiptRegistry,
        repr=False,
    )
    _entries: OrderedDict[str, tuple[str, str]] = field(
        default_factory=OrderedDict,
        init=False,
        repr=False,
    )

    def register(self, *, session_id: str, seed_content: str) -> str:
        handoff_id = f"seed_handoff_{uuid4().hex}"
        self._entries[handoff_id] = (session_id, seed_content)
        self._entries.move_to_end(handoff_id)
        while len(self._entries) > max(1, self.max_entries):
            self._entries.popitem(last=False)
        return handoff_id

    def resolve(self, handoff_id: str, *, session_id: str) -> str | None:
        entry = self._entries.get(handoff_id)
        if entry is None or entry[0] != session_id:
            return None
        self._entries.move_to_end(handoff_id)
        return entry[1]

    def consume(self, handoff_id: str, *, session_id: str) -> str | None:
        """Redeem a session-bound handoff exactly once."""
        entry = self._entries.get(handoff_id)
        if entry is None or entry[0] != session_id:
            return None
        del self._entries[handoff_id]
        return entry[1]

    def restore(self, handoff_id: str, *, session_id: str, seed_content: str) -> bool:
        """Compensate a failed redemption without replacing another claim."""
        if handoff_id in self._entries:
            return False
        self._entries[handoff_id] = (session_id, seed_content)
        self._entries.move_to_end(handoff_id)
        while len(self._entries) > max(1, self.max_entries):
            self._entries.popitem(last=False)
        return True
