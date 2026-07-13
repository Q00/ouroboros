"""Evidence-only paired quality experiment for GitHub issue #1450.

The default test path is hermetic. The costly product experiment runs only when
``OUROBOROS_RUN_AUTO_FILL_QUALITY=1`` is set.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, TypedDict

import pytest

from ouroboros.auto.answerer import AutoAnswerContext, AutoAnswerer
from ouroboros.auto.auto_fill import AutoFillProposal, auto_fill_remaining
from ouroboros.auto.interview_driver import (
    AutoInterviewDriver,
    FunctionInterviewBackend,
    InterviewTurn,
)
from ouroboros.auto.ledger import LedgerStatus, SeedDraftLedger
from ouroboros.auto.ledger_seed import synthesize_seed_from_ledger
from ouroboros.auto.safe_defaults import finalize_safe_defaultable_gaps
from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore
from ouroboros.config.loader import get_llm_backend_for_role, get_llm_model_for_role

from .conftest import CanonicalScenario

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE_PATH = Path(__file__).resolve().parent / "cli-todo" / "issue-1450-base-ledger.json"
_LIVE_ENV = "OUROBOROS_RUN_AUTO_FILL_QUALITY"
_EVIDENCE_ENV = "OUROBOROS_AUTO_FILL_QUALITY_EVIDENCE_DIR"
_BASELINE_ARM = "x"
_TREATMENT_ARM = "y"
_PAIR_ORDERS: tuple[tuple[str, str], ...] = (
    (_BASELINE_ARM, _TREATMENT_ARM),
    (_TREATMENT_ARM, _BASELINE_ARM),
    (_BASELINE_ARM, _TREATMENT_ARM),
    (_TREATMENT_ARM, _BASELINE_ARM),
    (_BASELINE_ARM, _TREATMENT_ARM),
)
_GAP_QUESTIONS = {
    "actors": "Who are the actors, inputs, and outputs for this task?",
    "inputs": "Who are the actors, inputs, and outputs for this task?",
    "outputs": "Who are the actors, inputs, and outputs for this task?",
    "constraints": "What conservative constraints and failure modes should bound this MVP?",
    "failure_modes": "What conservative constraints and failure modes should bound this MVP?",
    "non_goals": "What non-goals should explicitly remain out of scope?",
    "acceptance_criteria": "Which command output verifies the acceptance criteria?",
    "verification_plan": "Which command output verifies the acceptance criteria?",
    "runtime_context": "Which runtime stack, repo, and project patterns should be used?",
}
_DETERMINISTIC_PROPOSALS = {
    "actors": "A single local CLI user manages their own habits.",
    "inputs": ("Commands are add <habit>, list, and done <habit>; habit names are non-empty text."),
    "outputs": (
        "add and done print the affected habit name; list prints every habit with completion "
        "state; state is stored in habits.json."
    ),
    "non_goals": "No accounts, cloud sync, reminders, billing, or production deployment.",
    "acceptance_criteria": (
        'python habit_tracker.py add "drink water" exits 0 and prints "drink water"; list '
        "prints it; done exits 0 and prints it; an unknown command exits 2."
    ),
    "verification_plan": (
        "Run the canonical add, list, done, and unknown-command smoke commands and verify "
        "habits.json exists."
    ),
    "runtime_context": (
        "Run with the repository Python interpreter from a fresh local working directory."
    ),
}
_CONFIG_ENV_KEYS = (
    "OUROBOROS_AGENT_RUNTIME_BACKEND",
    "OUROBOROS_CLARIFICATION_MODEL",
    "OUROBOROS_EXECUTION_MODEL",
    "OUROBOROS_QA_MODEL",
    "OUROBOROS_REFLECT_MODEL",
)
_INFRASTRUCTURE_MARKERS = (
    "api key",
    "authentication",
    "configuration",
    "connection",
    "credential",
    "model not found",
    "network",
    "provider",
    "rate limit",
    "timed out",
    "timeout",
)


class _SmokeCommand(TypedDict):
    argv: tuple[str, ...]
    expect_exit_code: int
    stdout_contains: tuple[str, ...]
    stderr_contains: tuple[str, ...]


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _load_fixture() -> dict[str, Any]:
    raw = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def _fixture_ledger(raw: Mapping[str, Any]) -> SeedDraftLedger:
    ledger_raw = raw.get("ledger")
    assert isinstance(ledger_raw, dict)
    return SeedDraftLedger.from_dict(deepcopy(ledger_raw))


def _json_write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _smoke_commands(scenario: CanonicalScenario) -> tuple[_SmokeCommand, ...]:
    commands: list[_SmokeCommand] = []
    for raw in scenario.product_smoke_commands:
        argv_raw = raw.get("argv")
        assert isinstance(argv_raw, list) and all(isinstance(item, str) for item in argv_raw)
        expected_exit_raw = raw.get("expect_exit_code", 0)
        assert isinstance(expected_exit_raw, int) and not isinstance(expected_exit_raw, bool)
        stdout_raw = raw.get("stdout_contains", ())
        stderr_raw = raw.get("stderr_contains", ())
        assert isinstance(stdout_raw, (list, tuple)) and all(
            isinstance(item, str) for item in stdout_raw
        )
        assert isinstance(stderr_raw, (list, tuple)) and all(
            isinstance(item, str) for item in stderr_raw
        )
        commands.append(
            {
                "argv": tuple(argv_raw),
                "expect_exit_code": expected_exit_raw,
                "stdout_contains": tuple(stdout_raw),
                "stderr_contains": tuple(stderr_raw),
            }
        )
    return tuple(commands)


def _latest_goal(ledger: SeedDraftLedger) -> str:
    entries = ledger.sections["goal"].entries
    active = [entry.value for entry in entries if entry.status is not LedgerStatus.WEAK]
    assert active
    return active[-1]


def _assert_fillable_fixture(ledger: SeedDraftLedger) -> None:
    statuses = ledger.section_statuses()
    gaps = ledger.open_gaps()
    assert gaps, "issue #1450 fixture must retain non-converged gaps"
    assert "goal" not in gaps
    assert all(
        statuses[section] not in {LedgerStatus.BLOCKED, LedgerStatus.CONFLICTING}
        for section in gaps
    )


async def _reproduce_fixture(
    goal: str, workdir: Path
) -> tuple[AutoPipelineState, SeedDraftLedger, str]:
    ambiguity = 0.42

    async def start(
        _goal: str,
        _cwd: str,
        *,
        interview_id: str | None = None,
    ) -> InterviewTurn:
        return InterviewTurn(
            "What else should we know?",
            interview_id or "interview_issue1450",
            ambiguity_score=ambiguity,
        )

    async def answer(
        session_id: str,
        text: str,
        *,
        last_question: str | None = None,  # noqa: ARG001
    ) -> InterviewTurn:
        if "[safe-default-synthesis]" in text:
            return InterviewTurn(
                "done",
                session_id,
                seed_ready=True,
                completed=True,
                ambiguity_score=ambiguity,
            )
        return InterviewTurn(
            "What else should we know?",
            session_id,
            seed_ready=False,
            completed=False,
            ambiguity_score=ambiguity,
        )

    state = AutoPipelineState(goal=goal, cwd=str(workdir))
    ledger = SeedDraftLedger.from_goal(goal)
    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(workdir / "store"),
        max_rounds=1,
        timeout_seconds=1,
        context_provider=lambda _cwd: AutoAnswerContext(),
    )
    result = await driver.run(state, ledger)
    return state, ledger, result.status


def _apply_baseline(base: SeedDraftLedger) -> SeedDraftLedger:
    ledger = SeedDraftLedger.from_dict(deepcopy(base.to_dict()))
    result = finalize_safe_defaultable_gaps(
        ledger,
        goal=_latest_goal(ledger),
        provenance="issue-1450 paired quality experiment",
    )
    assert result.completed, f"baseline could not close fixture: {result.unsafe_gaps}"
    return ledger


def _apply_treatment(
    base: SeedDraftLedger,
    proposals: Mapping[str, AutoFillProposal],
) -> SeedDraftLedger:
    ledger = SeedDraftLedger.from_dict(deepcopy(base.to_dict()))
    filled = auto_fill_remaining(ledger, fill_slot=lambda section, _ledger: proposals.get(section))
    assert set(filled) == set(base.open_gaps())
    return ledger


def _generic_value(answerer: AutoAnswerer, section: str, ledger: SeedDraftLedger) -> str:
    answer = answerer.answer_gap(section, ledger, AutoAnswerContext())
    if answer.blocker is not None:
        raise RuntimeError(answer.blocker.reason)
    matches = [entry.value for target, entry in answer.ledger_updates if target == section]
    if not matches:
        raise RuntimeError(f"answer_gap produced no value for {section}")
    return matches[-1]


def _configured_answer_refiner() -> Any:
    from ouroboros.auto.adapters import build_answer_refiner

    return build_answer_refiner()


async def _build_treatment_proposals(
    base: SeedDraftLedger,
    *,
    progress_path: Path | None = None,
) -> tuple[dict[str, AutoFillProposal], dict[str, Any]]:
    refiner = _configured_answer_refiner()
    if refiner is None:
        raise RuntimeError("configured interview LLM could not build LLMAnswerRefiner")

    answerer = AutoAnswerer()
    working = SeedDraftLedger.from_dict(deepcopy(base.to_dict()))
    proposals: dict[str, AutoFillProposal] = {}
    records: list[dict[str, Any]] = []
    resolved_backend = get_llm_backend_for_role("interview")
    resolved_model = get_llm_model_for_role("interview", backend=resolved_backend)
    manifest: dict[str, Any] = {
        "status": "running",
        "generator": type(refiner).__name__,
        "adapter": type(refiner.llm_adapter).__name__,
        "configured_backend": resolved_backend,
        "configured_model": resolved_model,
        "completion_model_argument": getattr(refiner, "model", None) or "",
        "records": records,
    }
    for section in tuple(working.open_gaps()):
        generic = _generic_value(answerer, section, working)
        record: dict[str, Any] = {
            "section": section,
            "question": _GAP_QUESTIONS[section],
            "generic_value": generic,
            "status": "running",
        }
        records.append(record)
        if progress_path is not None:
            manifest["sha256"] = _sha256(
                {key: value for key, value in manifest.items() if key != "sha256"}
            )
            _json_write(progress_path, manifest)
        try:
            concrete = await refiner(
                _latest_goal(working),
                _GAP_QUESTIONS[section],
                section,
                generic,
                tuple(working.committed_decisions()),
            )
        except Exception as exc:
            record.update({"status": "failed", "error": repr(exc)})
            manifest["status"] = "failed"
            manifest["sha256"] = _sha256(
                {key: value for key, value in manifest.items() if key != "sha256"}
            )
            if progress_path is not None:
                _json_write(progress_path, manifest)
            raise
        if concrete is None or not concrete.strip():
            record.update({"status": "failed", "error": "empty refiner response"})
            manifest["status"] = "failed"
            manifest["sha256"] = _sha256(
                {key: value for key, value in manifest.items() if key != "sha256"}
            )
            if progress_path is not None:
                _json_write(progress_path, manifest)
            raise RuntimeError(f"LLMAnswerRefiner returned no proposal for {section}")
        proposal = AutoFillProposal(value=concrete.strip(), confidence=0.5)
        proposals[section] = proposal
        auto_fill_remaining(
            working,
            fill_slot=lambda candidate, _ledger, *, target=section, value=proposal: (
                value if candidate == target else None
            ),
        )
        record.update(
            {
                "status": "complete",
                "proposal": proposal.value,
                "confidence": proposal.confidence,
            }
        )
        if progress_path is not None:
            manifest["sha256"] = _sha256(
                {key: value for key, value in manifest.items() if key != "sha256"}
            )
            _json_write(progress_path, manifest)

    manifest["status"] = "complete"
    manifest["sha256"] = _sha256({key: value for key, value in manifest.items() if key != "sha256"})
    if progress_path is not None:
        _json_write(progress_path, manifest)
    return proposals, manifest


def _git_output(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=_REPO_ROOT,
        capture_output=True,
        check=True,
        text=True,
    )
    return completed.stdout.strip()


def _live_preflight(raw_fixture: Mapping[str, Any], scenario: CanonicalScenario) -> dict[str, Any]:
    errors: list[str] = []
    source_tree_hash = _git_output("rev-parse", "HEAD:src/ouroboros")
    if source_tree_hash != raw_fixture["source_tree_hash"]:
        errors.append("src/ouroboros changed since fixture capture; recapture the base ledger")
    if _git_output("status", "--porcelain", "--", "src/ouroboros"):
        errors.append("src/ouroboros has uncommitted changes")

    expected_hash = _git_output("hash-object", str(scenario.directory / "expected.yaml"))
    goal_hash = _git_output("hash-object", str(scenario.directory / "goal.txt"))
    if expected_hash != raw_fixture["expected_blob_hash"]:
        errors.append("cli-todo expected.yaml changed since fixture capture")
    if goal_hash != raw_fixture["goal_blob_hash"]:
        errors.append("cli-todo goal.txt changed since fixture capture")

    executables = sorted({command["argv"][0] for command in _smoke_commands(scenario)})
    resolved = {executable: shutil.which(executable) for executable in executables}
    missing = [executable for executable, path in resolved.items() if path is None]
    if missing:
        errors.append(f"missing smoke command executables on PATH: {missing}")

    resolved_backend = get_llm_backend_for_role("interview")
    resolved_model = get_llm_model_for_role("interview", backend=resolved_backend)
    config_snapshot = {
        "resolved_interview_backend": resolved_backend,
        "resolved_interview_model": resolved_model,
        "runtime_env": {key: os.environ.get(key) for key in _CONFIG_ENV_KEYS},
    }
    snapshot = {
        "source_tree_hash": source_tree_hash,
        "expected_blob_hash": expected_hash,
        "goal_blob_hash": goal_hash,
        "python_executable": sys.executable,
        "smoke_executables": resolved,
        **config_snapshot,
        "config_fingerprint_sha256": _sha256(config_snapshot),
        "errors": errors,
    }
    snapshot["sha256"] = _sha256(snapshot)
    return snapshot


def _prepare_state(
    scenario: CanonicalScenario,
    ledger: SeedDraftLedger,
    workdir: Path,
    store: AutoStore,
) -> AutoPipelineState:
    state = AutoPipelineState(goal=scenario.goal, cwd=str(workdir))
    state.pipeline_timeout_seconds = float(scenario.wall_clock_budget_seconds)
    state.complete_product = True
    state.arm_deadline()
    state.transition(AutoPhase.INTERVIEW, "issue-1450 frozen ledger replay")
    state.interview_completed = True
    state.ledger = ledger.to_dict()
    state.transition(AutoPhase.SEED_GENERATION, "issue-1450 paired quality arm")
    store.save(state)
    return state


def _result_envelope(result: Any) -> dict[str, Any]:
    if not getattr(result, "is_ok", False):
        return {
            "is_ok": False,
            "error": str(result.error if getattr(result, "is_err", False) else "unknown"),
        }
    tool_result = result.unwrap()
    return {
        "is_ok": True,
        "is_error": bool(tool_result.is_error),
        "meta": dict(tool_result.meta or {}),
        "content": [
            {"type": str(getattr(item, "type", "")), "text": getattr(item, "text", None)}
            for item in (tool_result.content or ())
        ],
    }


def _resolve_workdir_path(workdir: Path, relative_path: str) -> Path:
    candidate = (workdir / relative_path).resolve()
    candidate.relative_to(workdir.resolve())
    return candidate


def _evaluate_smoke(scenario: CanonicalScenario, workdir: Path) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    artifact: Path | None = None
    if scenario.product_artifact_path:
        artifact = _resolve_workdir_path(workdir, scenario.product_artifact_path)
        if not artifact.is_file():
            return {
                "passed": False,
                "failure_class": "product",
                "error": f"missing product artifact: {artifact}",
                "commands": records,
            }

    for index, command in enumerate(_smoke_commands(scenario)):
        argv = list(command["argv"])
        if artifact is not None:
            argv = [part.format(artifact=str(artifact)) for part in argv]
        try:
            completed = subprocess.run(
                argv,
                cwd=workdir,
                capture_output=True,
                check=False,
                text=True,
                timeout=min(float(scenario.wall_clock_budget_seconds), 60.0),
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            records.append({"index": index, "argv": argv, "error": repr(exc)})
            return {
                "passed": False,
                "failure_class": "infrastructure",
                "error": repr(exc),
                "commands": records,
            }
        expected_exit = command["expect_exit_code"]
        stdout_missing = [
            text for text in command["stdout_contains"] if text not in completed.stdout
        ]
        stderr_missing = [
            text for text in command["stderr_contains"] if text not in completed.stderr
        ]
        passed = completed.returncode == expected_exit and not stdout_missing and not stderr_missing
        records.append(
            {
                "index": index,
                "argv": argv,
                "exit_code": completed.returncode,
                "expected_exit_code": expected_exit,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "stdout_missing": stdout_missing,
                "stderr_missing": stderr_missing,
                "passed": passed,
            }
        )

    missing_outputs = [
        path
        for path in scenario.declared_output_paths
        if not _resolve_workdir_path(workdir, path).exists()
    ]
    return {
        "passed": all(record.get("passed", False) for record in records) and not missing_outputs,
        "failure_class": "product",
        "commands": records,
        "missing_declared_outputs": missing_outputs,
    }


def _failure_class(envelope: Mapping[str, Any], smoke: Mapping[str, Any]) -> str | None:
    meta = envelope.get("meta") if isinstance(envelope.get("meta"), dict) else {}
    if meta.get("partial_product") or meta.get("partial_unresolved_slots"):
        return "degraded"
    if smoke.get("passed"):
        return None
    if smoke.get("failure_class") == "infrastructure":
        return "infrastructure"
    if not envelope.get("is_ok") or envelope.get("is_error"):
        text = json.dumps(envelope, ensure_ascii=False).lower()
        if any(marker in text for marker in _INFRASTRUCTURE_MARKERS):
            return "infrastructure"
        return "pipeline"
    if meta.get("status") != "complete" or meta.get("product_status") == "not_verified_complete":
        return "pipeline"
    return "product"


async def _run_arm(
    scenario: CanonicalScenario,
    ledger: SeedDraftLedger,
    *,
    arm: str,
    pair_number: int,
    pair_root: Path,
    handler_factory: Callable[[AutoStore], Any] | None = None,
) -> dict[str, Any]:
    arm_root = pair_root / f"arm-{arm}"
    workdir = arm_root / "workdir"
    workdir.mkdir(parents=True, exist_ok=False)
    store = AutoStore(arm_root / "store")
    state = _prepare_state(scenario, ledger, workdir, store)
    initial_seed = synthesize_seed_from_ledger(ledger).to_dict()
    _json_write(
        arm_root / "input.json",
        {
            "arm": arm,
            "pair": pair_number,
            "auto_session_id": state.auto_session_id,
            "ledger": ledger.to_dict(),
            "ledger_sha256": _sha256(ledger.to_dict()),
            "initial_seed_before_repair": initial_seed,
        },
    )

    try:
        if handler_factory is None:
            from ouroboros.mcp.tools.auto_handler import AutoHandler

            handler = AutoHandler(store=store)
        else:
            handler = handler_factory(store)
        result = await handler.handle({"resume": state.auto_session_id})
        envelope = _result_envelope(result)
    except Exception as exc:  # noqa: BLE001 - experiment must persist every failure
        envelope = {"is_ok": False, "exception": repr(exc)}

    try:
        final_state = store.load(state.auto_session_id)
        final_state_payload: dict[str, Any] | None = final_state.to_dict()
    except Exception as exc:  # noqa: BLE001 - evidence records corrupt/missing state
        final_state_payload = None
        envelope["state_load_error"] = repr(exc)

    trace_dir = workdir / ".ouroboros" / "traces" / state.auto_session_id
    trace_files = {
        "decisions": trace_dir / "decisions.jsonl",
        "outcome": trace_dir / "outcome.json",
    }
    meta = envelope.get("meta") if isinstance(envelope.get("meta"), dict) else {}
    final_seed_artifact = (
        final_state_payload.get("seed_artifact") if isinstance(final_state_payload, dict) else None
    )
    final_seed_meta = (
        final_seed_artifact.get("metadata") if isinstance(final_seed_artifact, dict) else None
    )
    state_consistent = bool(
        isinstance(final_state_payload, dict)
        and final_state_payload.get("auto_session_id") == state.auto_session_id
        and final_state_payload.get("phase") == AutoPhase.COMPLETE.value
        and final_state_payload.get("complete_product") is True
        and isinstance(final_seed_artifact, dict)
        and bool(final_seed_artifact)
        and isinstance(final_seed_meta, dict)
        and final_seed_meta.get("degraded") is False
        and meta.get("status") == AutoPhase.COMPLETE.value
        and meta.get("phase") == AutoPhase.COMPLETE.value
        and meta.get("auto_session_id") in {None, state.auto_session_id}
    )
    can_smoke = (
        envelope.get("is_ok") is True
        and envelope.get("is_error") is False
        and state_consistent
        and not meta.get("partial_product")
        and meta.get("product_status") != "not_verified_complete"
    )
    smoke = (
        _evaluate_smoke(scenario, workdir)
        if can_smoke
        else {"passed": False, "failure_class": "pipeline", "commands": []}
    )
    failure_class = _failure_class(envelope, smoke)
    evidence_complete = state_consistent and all(path.is_file() for path in trace_files.values())
    if not evidence_complete:
        failure_class = "missing_evidence"
    record = {
        "arm": arm,
        "pair": pair_number,
        "oracle_pass": bool(can_smoke and smoke.get("passed") and evidence_complete),
        "failure_class": failure_class,
        "evidence_complete": evidence_complete,
        "state_consistent": state_consistent,
        "envelope": envelope,
        "smoke": smoke,
        "final_state": final_state_payload,
        "trace_dir": str(trace_dir),
        "trace_files": {name: str(path) for name, path in trace_files.items()},
    }
    _json_write(arm_root / "result.json", record)
    return record


def _pair_outcome(records: Sequence[Mapping[str, Any]]) -> str:
    by_arm = {str(record["arm"]): record for record in records}
    if set(by_arm) != {_BASELINE_ARM, _TREATMENT_ARM}:
        return "invalid"
    if any(
        record.get("failure_class") in {"infrastructure", "degraded", "missing_evidence"}
        for record in by_arm.values()
    ):
        return "invalid"
    baseline_pass = bool(by_arm[_BASELINE_ARM].get("oracle_pass"))
    treatment_pass = bool(by_arm[_TREATMENT_ARM].get("oracle_pass"))
    if baseline_pass and treatment_pass:
        return "both_pass"
    if baseline_pass:
        return "baseline_only"
    if treatment_pass:
        return "treatment_only"
    return "both_fail"


def _decide(pair_outcomes: Sequence[str]) -> str:
    valid = [outcome for outcome in pair_outcomes if outcome != "invalid"]
    if len(valid) < 3:
        return "inconclusive"
    if all(outcome == "both_pass" for outcome in valid):
        return "no_observed_gap"
    if valid.count("both_fail") >= 2:
        return "inconclusive"
    treatment_only = valid.count("treatment_only")
    baseline_only = valid.count("baseline_only")
    if treatment_only >= 2 and baseline_only == 0:
        return "treatment_improvement_candidate"
    if baseline_only >= 2 and treatment_only == 0:
        return "treatment_regression"
    return "inconclusive"


def _select_scenario(canonical_scenarios: Sequence[CanonicalScenario]) -> CanonicalScenario:
    selected = [scenario for scenario in canonical_scenarios if scenario.slug == "cli-todo"]
    assert len(selected) == 1
    return selected[0]


def test_issue_1450_fixture_round_trips_and_hashes() -> None:
    raw = _load_fixture()
    ledger = _fixture_ledger(raw)

    assert raw["capture_kind"] == "current_main_deterministic_backend_reproduction"
    assert raw["reproduction"]["backend_ambiguity_score"] > 0.20
    assert _sha256(ledger.to_dict()) == raw["ledger_sha256"]
    assert SeedDraftLedger.from_dict(ledger.to_dict()).to_dict() == ledger.to_dict()
    _assert_fillable_fixture(ledger)


@pytest.mark.asyncio
async def test_issue_1450_fixture_matches_current_driver_reproduction(tmp_path: Path) -> None:
    raw = _load_fixture()
    goal = (Path(__file__).resolve().parent / "cli-todo" / "goal.txt").read_text().strip()

    state, reproduced, status = await _reproduce_fixture(goal, tmp_path)

    assert status == raw["reproduction"]["terminal_status"]
    assert state.last_error_code == raw["reproduction"]["stop_reason_code"]
    assert "ambiguity_score=0.42" in (state.last_error or "")
    assert _sha256(reproduced.to_dict()) == raw["ledger_sha256"]


def test_issue_1450_arm_transforms_preserve_fixture_invariants() -> None:
    base = _fixture_ledger(_load_fixture())
    base_hash = _sha256(base.to_dict())
    proposals = {
        section: AutoFillProposal(value=value, confidence=0.5)
        for section, value in _DETERMINISTIC_PROPOSALS.items()
    }

    baseline = _apply_baseline(base)
    treatment = _apply_treatment(base, proposals)

    assert _sha256(base.to_dict()) == base_hash
    assert baseline.is_seed_ready()
    assert treatment.is_seed_ready()
    assert _latest_goal(baseline) == _latest_goal(treatment) == _latest_goal(base)
    assert baseline.provenance_histogram().get("timeout_default") == len(base.open_gaps())
    assert treatment.provenance_histogram().get("timeout_default") == len(base.open_gaps())
    for section in base.open_gaps():
        assert all(
            entry.status not in {LedgerStatus.BLOCKED, LedgerStatus.CONFLICTING}
            for entry in baseline.sections[section].entries
        )
        assert all(
            entry.status not in {LedgerStatus.BLOCKED, LedgerStatus.CONFLICTING}
            for entry in treatment.sections[section].entries
        )


@pytest.mark.asyncio
async def test_issue_1450_proposal_failure_persists_partial_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _FailingRefiner:
        llm_adapter = object()
        model = "fake-model"

        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, *_args: Any) -> str | None:
            self.calls += 1
            return "first concrete proposal" if self.calls == 1 else None

    refiner = _FailingRefiner()
    monkeypatch.setattr(
        "tests.canonical.test_issue_1450_quality._configured_answer_refiner",
        lambda: refiner,
    )
    monkeypatch.setattr(
        "tests.canonical.test_issue_1450_quality.get_llm_backend_for_role",
        lambda _role: "fake-backend",
    )
    monkeypatch.setattr(
        "tests.canonical.test_issue_1450_quality.get_llm_model_for_role",
        lambda _role, *, backend: f"{backend}-model",
    )
    manifest_path = tmp_path / "proposal-manifest.json"

    with pytest.raises(RuntimeError, match="returned no proposal"):
        await _build_treatment_proposals(
            _fixture_ledger(_load_fixture()),
            progress_path=manifest_path,
        )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["configured_backend"] == "fake-backend"
    assert manifest["configured_model"] == "fake-backend-model"
    assert manifest["records"][0]["status"] == "complete"
    assert manifest["records"][1]["status"] == "failed"
    assert manifest["sha256"]


@pytest.mark.parametrize(
    ("outcomes", "expected"),
    [
        (["both_pass"] * 3, "no_observed_gap"),
        (
            ["treatment_only", "both_pass", "treatment_only"],
            "treatment_improvement_candidate",
        ),
        (["baseline_only", "both_pass", "baseline_only"], "treatment_regression"),
        (["treatment_only", "baseline_only", "both_pass"], "inconclusive"),
        (["treatment_only", "both_fail", "treatment_only", "both_fail"], "inconclusive"),
        (["invalid", "both_pass", "both_pass"], "inconclusive"),
        (["invalid", "both_pass", "both_pass", "both_pass"], "no_observed_gap"),
    ],
)
def test_issue_1450_verdict_classifier(outcomes: list[str], expected: str) -> None:
    assert _decide(outcomes) == expected


@pytest.mark.asyncio
async def test_issue_1450_arm_runner_requires_product_and_trace_evidence(
    canonical_scenarios: tuple[CanonicalScenario, ...],
    tmp_path: Path,
) -> None:
    from ouroboros.core.types import Result

    class _FakeHandler:
        def __init__(self, store: AutoStore) -> None:
            self.store = store

        async def handle(self, arguments: dict[str, Any]) -> Any:
            state = self.store.load(str(arguments["resume"]))
            workdir = Path(state.cwd)
            seed = synthesize_seed_from_ledger(SeedDraftLedger.from_dict(state.ledger))
            state.seed_id = seed.metadata.seed_id
            state.seed_artifact = seed.to_dict()
            state.transition(AutoPhase.REVIEW, "fake review complete")
            state.transition(AutoPhase.COMPLETE, "fake product verified")
            self.store.save(state)
            (workdir / "habit_tracker.py").write_text(
                """
import json
import sys
from pathlib import Path

store = Path("habits.json")
habits = json.loads(store.read_text()) if store.exists() else []
command = sys.argv[1] if len(sys.argv) > 1 else ""
name = " ".join(sys.argv[2:])
if command == "add" and name:
    habits.append({"name": name, "done": False})
    store.write_text(json.dumps(habits))
    print(name)
elif command == "list":
    for item in habits:
        print(item["name"])
elif command == "done" and name:
    print(name)
else:
    raise SystemExit(2)
""".strip(),
                encoding="utf-8",
            )
            trace_dir = workdir / ".ouroboros" / "traces" / state.auto_session_id
            trace_dir.mkdir(parents=True)
            (trace_dir / "decisions.jsonl").write_text('{"type":"decision"}\n', encoding="utf-8")
            (trace_dir / "outcome.json").write_text('{"status":"complete"}\n', encoding="utf-8")

            class _ToolResult:
                is_error = False
                content: tuple[object, ...] = ()
                meta = {
                    "auto_session_id": state.auto_session_id,
                    "status": "complete",
                    "phase": AutoPhase.COMPLETE.value,
                    "product_status": "verified_complete",
                }

            return Result.ok(_ToolResult())

    scenario = _select_scenario(canonical_scenarios)
    baseline = _apply_baseline(_fixture_ledger(_load_fixture()))

    record = await _run_arm(
        scenario,
        baseline,
        arm=_BASELINE_ARM,
        pair_number=1,
        pair_root=tmp_path / "pair-1",
        handler_factory=_FakeHandler,
    )

    assert record["oracle_pass"] is True
    assert record["failure_class"] is None
    assert record["evidence_complete"] is True
    assert record["smoke"]["passed"] is True


@pytest.mark.asyncio
async def test_issue_1450_live_paired_quality_experiment(
    canonical_scenarios: tuple[CanonicalScenario, ...],
) -> None:
    if os.environ.get(_LIVE_ENV, "").strip().lower() not in {"1", "true", "yes"}:
        pytest.skip(f"set {_LIVE_ENV}=1 to run the costly issue #1450 paired experiment")

    evidence_value = os.environ.get(_EVIDENCE_ENV, "").strip()
    if not evidence_value:
        pytest.fail(f"set {_EVIDENCE_ENV} to an absolute durable evidence directory")
    evidence_base = Path(evidence_value).expanduser()
    if not evidence_base.is_absolute():
        pytest.fail(f"{_EVIDENCE_ENV} must be an absolute path: {evidence_value!r}")
    evidence_base = evidence_base.resolve()

    raw_fixture = _load_fixture()
    scenario = _select_scenario(canonical_scenarios)
    run_id = datetime.now(tz=UTC).strftime("issue-1450-%Y%m%d-%H%M%S-%f")
    evidence_root = evidence_base / run_id
    evidence_root.mkdir(parents=True, exist_ok=False)

    preflight = _live_preflight(raw_fixture, scenario)
    _json_write(evidence_root / "preflight.json", preflight)
    if preflight["errors"]:
        pytest.fail(
            f"issue #1450 preflight failed: {preflight['errors']}; evidence={evidence_root}"
        )

    base = _fixture_ledger(raw_fixture)
    proposal_path = evidence_root / "proposal-manifest.json"
    try:
        proposals, proposal_manifest = await _build_treatment_proposals(
            base,
            progress_path=proposal_path,
        )
    except Exception as exc:  # noqa: BLE001 - preserve costly partial proposal evidence
        _json_write(
            evidence_root / "experiment-result.json",
            {
                "issue": 1450,
                "run_id": run_id,
                "verdict": "inconclusive",
                "stage": "proposal_generation",
                "error": repr(exc),
                "proposal_manifest": str(proposal_path),
            },
        )
        pytest.fail(f"issue #1450 proposal generation failed: {exc}; evidence={evidence_root}")
    baseline = _apply_baseline(base)
    treatment = _apply_treatment(base, proposals)
    for ledger in (baseline, treatment):
        assert ledger.is_seed_ready()

    _json_write(
        evidence_root / "experiment-input.json",
        {
            "issue": 1450,
            "scenario": scenario.slug,
            "fixture": raw_fixture,
            "fixture_sha256": _sha256(raw_fixture),
            "preflight": preflight,
            "proposal_manifest": proposal_manifest,
            "arm_mapping": {
                _BASELINE_ARM: "finalize_safe_defaultable_gaps",
                _TREATMENT_ARM: "LLMAnswerRefiner proposals + auto_fill_remaining",
            },
            "baseline_ledger": baseline.to_dict(),
            "treatment_ledger": treatment.to_dict(),
        },
    )

    arm_ledgers = {_BASELINE_ARM: baseline, _TREATMENT_ARM: treatment}
    pair_records: list[dict[str, Any]] = []
    for pair_index, order in enumerate(_PAIR_ORDERS[:3], start=1):
        pair_root = evidence_root / f"pair-{pair_index}"
        records = []
        for arm in order:
            records.append(
                await _run_arm(
                    scenario,
                    arm_ledgers[arm],
                    arm=arm,
                    pair_number=pair_index,
                    pair_root=pair_root,
                )
            )
        pair_records.append({"pair": pair_index, "order": order, "records": records})

    first_infrastructure_failures = sum(
        record.get("failure_class") == "infrastructure"
        for pair in pair_records
        for record in pair["records"]
    )
    if first_infrastructure_failures == 1:
        for pair_index, order in enumerate(_PAIR_ORDERS[3:], start=4):
            pair_root = evidence_root / f"pair-{pair_index}"
            records = []
            for arm in order:
                records.append(
                    await _run_arm(
                        scenario,
                        arm_ledgers[arm],
                        arm=arm,
                        pair_number=pair_index,
                        pair_root=pair_root,
                    )
                )
            pair_records.append({"pair": pair_index, "order": order, "records": records})

    outcomes = [_pair_outcome(pair["records"]) for pair in pair_records]
    verdict = _decide(outcomes)
    summary = {
        "issue": 1450,
        "run_id": run_id,
        "verdict": verdict,
        "pair_outcomes": outcomes,
        "pairs": pair_records,
        "interpretation_limit": (
            "This result applies only to the frozen canonical cli-todo fixture and compares "
            "proposal content strategies; it does not prove general equivalence or metadata causality."
        ),
    }
    _json_write(evidence_root / "experiment-result.json", summary)
    print(f"ISSUE 1450 verdict={verdict} evidence={evidence_root}")

    if verdict == "inconclusive":
        pytest.fail(f"issue #1450 experiment was inconclusive; evidence={evidence_root}")
