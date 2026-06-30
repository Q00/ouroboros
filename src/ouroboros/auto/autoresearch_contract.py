"""Autoresearch contract extraction and Seed normalization."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from ouroboros.core.seed import Seed

_JSON_FENCE_RE = re.compile(r"```json\s*(?P<body>.*?)```", re.IGNORECASE | re.DOTALL)

_GAME_TEMPLATE_MARKERS = (
    "headless simulation",
    "quit signal",
    "missing-asset",
)

_QA_RECOVERY_MARKERS = (
    "use this bounded implementation approach to resolve seed qa",
    "adopt this concrete implementation decision before execution",
    "## persona:",
    "evaluate failed:",
    "current approach (not working)",
)

_AUTORESEARCH_FORMAT_DRIFT_MARKERS = (
    "top-level `actors`",
    "top-level actors",
    "`actors` must",
    "actors must",
    "top-level `inputs`",
    "top-level inputs",
    "`inputs`",
    "top-level `outputs`",
    "top-level outputs",
    "`outputs`",
    "seed_verification_ok",
    "seed_verification_failed",
    "seed.yaml",
    "acceptance inspector",
)

_REQUIRED_KEYS = (
    "repository",
    "program_path",
    "handoff_brief_path",
    "editable_files",
    "fixed_files",
    "primary_metric",
    "metric_direction",
    "experiment_budget",
    "timeout_seconds",
    "verification_command",
    "candidate_sequence",
    "non_goals",
    "runtime_context",
    "metric_fallback",
    "ledger",
    "validity_rules",
    "verification_plan",
    "conflict_resolution",
)


def extract_autoresearch_contract(text: str) -> dict[str, Any] | None:
    """Return the first authoritative autoresearch contract embedded in text."""
    if "autoresearch_contract" not in text and "candidate_sequence" not in text:
        return None
    for match in _JSON_FENCE_RE.finditer(text):
        try:
            payload = json.loads(match.group("body"))
        except json.JSONDecodeError:
            continue
        contract = _coerce_contract(payload)
        if contract is not None:
            return contract
    return None


def apply_autoresearch_contract(seed: Seed) -> Seed:
    """Promote an embedded autoresearch contract into concrete Seed fields."""
    contract = _coerce_contract(seed.model_extra or {}) or extract_autoresearch_contract(seed.goal)
    if contract is None:
        return seed

    acceptance = _autoresearch_acceptance(contract)
    constraints = _autoresearch_constraints(seed.constraints, contract)
    update = {
        **contract,
        "task_type": "research",
        "constraints": constraints,
        "acceptance_criteria": acceptance,
    }
    return seed.model_copy(update=update)


def has_autoresearch_contract(seed: Seed) -> bool:
    """Return True when the Seed already carries or embeds an autoresearch contract."""
    return _coerce_contract(seed.model_extra or {}) is not None or extract_autoresearch_contract(seed.goal) is not None


def _coerce_contract(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = payload.get("autoresearch_contract")
    if isinstance(raw, Mapping):
        candidate = dict(raw)
    else:
        candidate = dict(payload)

    if not _looks_like_autoresearch_contract(candidate):
        return None
    return {key: candidate[key] for key in _REQUIRED_KEYS if key in candidate}


def _looks_like_autoresearch_contract(payload: Mapping[str, Any]) -> bool:
    sequence = payload.get("candidate_sequence")
    if not isinstance(sequence, list) or not sequence:
        return False
    return all(key in payload for key in ("repository", "primary_metric", "verification_command"))


def _autoresearch_acceptance(contract: Mapping[str, Any]) -> tuple[str, ...]:
    metric = str(contract.get("primary_metric", "primary_metric"))
    budget = int(contract.get("experiment_budget") or len(contract.get("candidate_sequence", ())))
    candidates = contract.get("candidate_sequence", [])
    candidate_names = ", ".join(
        str(item.get("name"))
        for item in candidates
        if isinstance(item, Mapping) and item.get("name")
    )
    editable = ", ".join(str(item) for item in contract.get("editable_files", ()) or ())
    fixed = ", ".join(str(item) for item in contract.get("fixed_files", ()) or ())
    ledger = contract.get("ledger", {})
    ledger_path = (
        str(ledger.get("path"))
        if isinstance(ledger, Mapping) and ledger.get("path")
        else ".ouroboros/autoresearch/experiment-log.md"
    )
    return (
        "Seed artifact exposes the authoritative autoresearch contract as top-level "
        "values: repository, program_path, editable_files, fixed_files, primary_metric, "
        "experiment_budget, timeout_seconds, verification_command, candidate_sequence, "
        "non_goals, runtime_context, metric_fallback, ledger, validity_rules, and "
        "conflict_resolution.",
        f"Candidate sequence contains exactly {budget} experiments in order: {candidate_names}.",
        "Execution runs experiment 1 as the unmodified baseline and then attempts experiments 2 through the configured budget as concrete train.py candidate changes.",
        f"Execution may edit only {editable}; fixed files must remain unchanged: {fixed}.",
        f"Every candidate experiment records command, changed files, observed {metric}, and conclusion in {ledger_path}. A baseline-only rerun or policy-inspection report is not sufficient.",
        f"Final verification exits 0, contains no traceback, emits parseable {metric} or the configured metric fallback, and reports baseline {metric}, final best {metric}, and whether the best result improved.",
        "If no candidate improves the baseline, execution must still show all planned candidates were attempted or explicitly rejected with measured evidence and must report no improvement.",
        "Invalid runs are recorded as failed and are never treated as metric improvements.",
        "The Seed includes a concrete top-level verification_plan covering seed artifact checks, experiment execution checks, metric parsing, ledger persistence, and invalid-run behavior.",
    )


def _autoresearch_constraints(
    existing_constraints: tuple[str, ...],
    contract: Mapping[str, Any],
) -> tuple[str, ...]:
    filtered = [
        item
        for item in existing_constraints
        if not any(marker in item.casefold() for marker in _GAME_TEMPLATE_MARKERS)
        and not any(marker in item.casefold() for marker in _QA_RECOVERY_MARKERS)
        and not any(marker in item.casefold() for marker in _AUTORESEARCH_FORMAT_DRIFT_MARKERS)
    ]
    non_goals = tuple(str(item) for item in contract.get("non_goals", ()) or ())
    runtime_context = contract.get("runtime_context", {})
    runtime_items = []
    if isinstance(runtime_context, Mapping):
        runtime_items = [
            f"runtime_context.{key}: {value}"
            for key, value in runtime_context.items()
        ]
    added = [
        "Autoresearch contract is authoritative over inferred task-class defaults.",
        *[f"Non-goal: {item}" for item in non_goals],
        *runtime_items,
        f"Verification command: {contract.get('verification_command')}",
        "Execution deliverable must be experimental evidence and the final train.py state, not only validation prose.",
        "Run or explicitly reject each configured candidate; baseline-only output is insufficient for autoresearch completion.",
        f"Conflict resolution: {contract.get('conflict_resolution')}",
    ]
    return tuple(dict.fromkeys([*filtered, *added]))


__all__ = [
    "apply_autoresearch_contract",
    "extract_autoresearch_contract",
    "has_autoresearch_contract",
]
