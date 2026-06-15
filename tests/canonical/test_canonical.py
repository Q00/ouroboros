"""Canonical acceptance tests.

L0-a — minimal manual harness. Each test in this file runs once per
discovered scenario in ``tests/canonical/<slug>/`` thanks to the
``pytest_generate_tests`` hook in ``conftest.py``.

Two cost regimes:

- Hermetic (default): shape-checks + L1 catalog cross-validation
  only. Always runs in CI without LLM cost. Catches fixture rot
  and L0 ↔ L1 contract drift.
- Live (``OUROBOROS_RUN_CANONICAL=1``): the maintainer-only path that
  actually invokes the ``ouroboros_auto`` MCP tool against the
  scenario and asserts the documented terminal state. Costs real
  LLM tokens.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any

import pytest

from ouroboros.core.types import Result

from .conftest import CanonicalScenario


def test_scenario_has_nonempty_goal(scenario: CanonicalScenario) -> None:
    """``goal.txt`` is the canonical input to ``ooo auto``; it must
    exist and have meaningful content beyond whitespace."""
    assert scenario.goal, f"{scenario.slug}: goal.txt is empty after strip"
    assert len(scenario.goal) >= 10, (
        f"{scenario.slug}: goal.txt content is suspiciously short "
        f"({len(scenario.goal)} chars); did you forget the real goal?"
    )


def test_scenario_domain_class_is_lowercase_snake(scenario: CanonicalScenario) -> None:
    """``domain_class`` matches the lowercase snake_case shape that the
    L1 catalog (#1173) emits. Pin the surface so a typo in
    ``expected.yaml`` fails here rather than at runtime when the
    inference hook is wired.

    Cross-validation against the actual L1 ``TaskClass`` enum is pinned
    below.
    """
    value = scenario.domain_class
    assert value == value.lower(), f"{scenario.slug}: domain_class {value!r} must be lowercase"
    assert value.replace("_", "").isalnum(), (
        f"{scenario.slug}: domain_class {value!r} must be snake_case alphanumerics only"
    )


def test_scenario_completion_mode_is_canonical(scenario: CanonicalScenario) -> None:
    """``completion_mode`` matches the L1 ``CompletionMode`` StrEnum
    surface. Pinned as a string set here so the harness validates
    without importing the catalog module."""
    valid = {"code_complete", "product_complete"}
    assert scenario.completion_mode in valid, (
        f"{scenario.slug}: completion_mode {scenario.completion_mode!r} must be "
        f"one of {sorted(valid)}"
    )


def test_scenario_runtime_probe_kinds_are_strings(
    scenario: CanonicalScenario,
) -> None:
    """``runtime_probe_kinds`` is a tuple of plain strings. Cross-
    validation against the L1 catalog's per-class probe whitelist is
    pinned below; this test pins the surface shape."""
    kinds = scenario.runtime_probe_kinds
    assert isinstance(kinds, tuple)
    for kind in kinds:
        assert isinstance(kind, str), (
            f"{scenario.slug}: runtime_probe_kinds entry {kind!r} must be a string"
        )
        assert kind == kind.lower(), (
            f"{scenario.slug}: runtime_probe_kinds entry {kind!r} must be lowercase"
        )


def test_scenario_wall_clock_budget_is_positive(
    scenario: CanonicalScenario,
) -> None:
    """The optional ``wall_clock_budget_seconds`` must be a positive
    integer when present (or take its default). Zero / negative
    budgets would cause the future L2 watchdog (#1172) to fire instantly."""
    assert scenario.wall_clock_budget_seconds > 0, (
        f"{scenario.slug}: wall_clock_budget_seconds must be positive; "
        f"got {scenario.wall_clock_budget_seconds}"
    )


def test_canonical_matrix_is_nonempty(
    canonical_scenarios: tuple[CanonicalScenario, ...],
) -> None:
    """The matrix must contain at least one scenario. Pins so a
    fixture-file rename does not silently disable the harness."""
    assert canonical_scenarios, (
        "no canonical scenarios discovered under tests/canonical/; "
        "either add a scenario directory or fix the discovery glob"
    )


# ---------------------------------------------------------------------------
# L1 catalog cross-validation (folded back in from #1170 L0-a deferred list)
# ---------------------------------------------------------------------------


def test_scenario_domain_class_resolves_in_l1_catalog(
    scenario: CanonicalScenario,
) -> None:
    """``expected.yaml``'s ``domain_class`` must resolve to a real
    :class:`TaskClass` value — otherwise the L1 inference output
    cannot match it.

    This is the L0 ↔ L1 contract pin: adding a scenario whose
    ``domain_class`` is not in the catalog fails here, prompting
    either a typo fix or an L1 catalog extension PR before the
    scenario lands."""
    from ouroboros.auto.task_classes import TaskClass

    valid = {tc.value for tc in TaskClass}
    assert scenario.domain_class in valid, (
        f"{scenario.slug}: domain_class {scenario.domain_class!r} is not a "
        f"known TaskClass; valid values are {sorted(valid)}"
    )


def test_scenario_completion_mode_matches_l1_catalog_default(
    scenario: CanonicalScenario,
) -> None:
    """The scenario's declared ``completion_mode`` must match the L1
    catalog default for its ``domain_class``. A mismatch means the
    scenario is asserting a regime the catalog does not endorse."""
    from ouroboros.auto.task_classes import (
        TASK_CLASS_CATALOG,
        CompletionMode,
        TaskClass,
    )

    task_class = TaskClass(scenario.domain_class)
    expected_mode = TASK_CLASS_CATALOG[task_class].default_completion_mode
    actual_mode = CompletionMode(scenario.completion_mode)
    assert actual_mode == expected_mode, (
        f"{scenario.slug}: completion_mode {scenario.completion_mode!r} "
        f"disagrees with TaskClass.{task_class.name} catalog default "
        f"{expected_mode.value!r}"
    )


def test_scenario_runtime_probe_kinds_subset_of_l1_catalog(
    scenario: CanonicalScenario,
) -> None:
    """Every ``runtime_probe_kinds`` entry in the scenario must be
    declared in the L1 catalog for that ``domain_class``. Pins the
    L0 ↔ L1 ↔ L3 contract: scenarios cannot invent ad-hoc probe
    kinds sideways."""
    from ouroboros.auto.task_classes import TASK_CLASS_CATALOG, TaskClass

    task_class = TaskClass(scenario.domain_class)
    allowed = set(TASK_CLASS_CATALOG[task_class].runtime_probe_kinds)
    declared = set(scenario.runtime_probe_kinds)
    unknown = declared - allowed
    assert not unknown, (
        f"{scenario.slug}: runtime_probe_kinds includes {sorted(unknown)} "
        f"which are not declared for TaskClass.{task_class.name} in the "
        f"catalog (allowed: {sorted(allowed)})"
    )



# ---------------------------------------------------------------------------
# Live product-reality smoke checks (#1452)
# ---------------------------------------------------------------------------


def _resolve_canonical_path(workdir: Path, relative_path: str) -> Path:
    candidate = (workdir / relative_path).resolve()
    root = workdir.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise AssertionError(f"canonical path escapes workdir: {relative_path!r}") from exc
    return candidate


def _run_product_reality_smoke_checks(
    scenario: CanonicalScenario,
    workdir: Path,
    meta: dict[str, Any],
) -> list[dict[str, Any]]:
    """Execute scenario-declared product smoke commands after live completion.

    The harness intentionally reads commands from ``expected.yaml`` so adding
    future canonical scenarios does not require new Python code. Commands run
    relative to the generated product workdir and may reference the generated
    artifact through the ``{artifact}`` placeholder.
    """
    if not scenario.product_artifact_path and not scenario.product_smoke_commands:
        return []

    artifact_path: Path | None = None
    if scenario.product_artifact_path:
        artifact_path = _resolve_canonical_path(workdir, scenario.product_artifact_path)
        assert artifact_path.is_file(), (
            f"{scenario.slug}: expected generated artifact at {artifact_path}; "
            f"auto meta was {meta}"
        )

    smoke_results: list[dict[str, Any]] = []
    timeout_seconds = min(float(scenario.wall_clock_budget_seconds), 30.0)
    for index, command in enumerate(scenario.product_smoke_commands):
        argv = list(command["argv"])
        if artifact_path is not None:
            argv = [part.format(artifact=str(artifact_path)) for part in argv]
        completed = subprocess.run(  # noqa: S603 - canonical smoke command
            argv,
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        expected_exit = int(command.get("expect_exit_code", 0))
        smoke_record = {
            "argv": argv,
            "exit_code": completed.returncode,
            "stdout_preview": completed.stdout[:1000],
            "stderr_preview": completed.stderr[:1000],
        }
        smoke_results.append(smoke_record)
        assert completed.returncode == expected_exit, (
            f"{scenario.slug}: smoke command {index} expected exit {expected_exit}, "
            f"got {completed.returncode}: {smoke_record}"
        )
        for expected in command.get("stdout_contains", ()):
            assert expected in completed.stdout, (
                f"{scenario.slug}: smoke command {index} stdout missing {expected!r}: {smoke_record}"
            )
        for expected in command.get("stderr_contains", ()):
            assert expected in completed.stderr, (
                f"{scenario.slug}: smoke command {index} stderr missing {expected!r}: {smoke_record}"
            )
    for output_path in scenario.declared_output_paths:
        expected = _resolve_canonical_path(workdir, output_path)
        assert expected.exists(), f"{scenario.slug}: expected declared output path {expected} to exist"
    return smoke_results


# ---------------------------------------------------------------------------
# Live invocation (opt-in via OUROBOROS_RUN_CANONICAL=1)
# ---------------------------------------------------------------------------


async def _invoke_ouroboros_auto(scenario: CanonicalScenario, workdir: Path) -> Any:
    """Programmatically invoke the ``ouroboros_auto`` MCP tool for a
    canonical scenario. Returns the handler's ``Result`` so the test
    can assert on its terminal shape.

    Kept inside the test module (rather than under
    ``tests/canonical/_runner.py``) because it is invoked only on the
    opt-in live-run path and pulls in heavy MCP-handler dependencies.
    """
    from ouroboros.auto.state import AutoStore
    from ouroboros.mcp.tools.auto_handler import AutoHandler

    # Use a per-scenario, in-process store rooted under the operator's
    # workdir so the run does not collide with concurrent ``ooo auto``
    # invocations and so a re-run picks a fresh ``auto_session_id``.
    store = AutoStore(workdir / ".ouroboros-canonical")
    handler = AutoHandler(store=store)
    arguments = {
        "goal": scenario.goal,
        "cwd": str(workdir),
        "skip_run": False,
        "complete_product": scenario.completion_mode == "product_complete",
        # Bounded budget so a stuck scenario does not hang the
        # operator. The watchdog (L2) catches anything past this on
        # its own.
        "pipeline_timeout_seconds": float(scenario.wall_clock_budget_seconds),
    }
    return await handler.handle(arguments)


def _persist_raw_evidence(
    scenario: CanonicalScenario,
    workdir: Path,
    result: Any,
    request: pytest.FixtureRequest,
    *,
    product_smoke_results: list[dict[str, Any]] | None = None,
) -> Path:
    """Persist the verbatim MCP handler response under
    ``<workdir>/.ooo-observability/`` as JSON evidence.

    PR-γ / #1170 evidence-integrity contract: the on-disk artifact must be
    a faithful 1:1 capture of what the MCP tool emitted — no paraphrase,
    no field-name normalization. R2-1709 of #1170 contained two fabricated
    field values (``interview_closure_mode="max_rounds_reached"``,
    ``stop_reason_code="interview_max_rounds_no_closure"``) that did not
    exist anywhere in source; reading evidence after the fact wasted
    investigation time chasing the imagined translation layer. The helper
    writes whatever the handler returned plus the preflight runtime
    metadata so future readers can verify which binary produced it.
    """
    from datetime import UTC, datetime
    import json

    obs_dir = workdir / ".ooo-observability"
    obs_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
    out_path = obs_dir / f"canonical-{scenario.slug}-{ts}.json"

    preflight = request.config.stash.get(_RUNTIME_PREFLIGHT_KEY_RUNTIME, None)

    raw_meta: dict[str, Any] = {}
    raw_is_error: Any = None
    raw_content_repr: list[Any] = []
    raw_text: Any = None
    if getattr(result, "is_ok", False):
        try:
            tool_result = result.unwrap()
            raw_meta = dict(getattr(tool_result, "meta", {}) or {})
            raw_is_error = bool(getattr(tool_result, "is_error", False))
            content = getattr(tool_result, "content", []) or []
            raw_content_repr = [
                {
                    "type": getattr(item, "type", item.__class__.__name__),
                    "text": getattr(item, "text", None),
                }
                for item in content
            ]
        except Exception as exc:  # noqa: BLE001
            raw_text = f"unwrap_failed: {exc!r}"
    else:
        err = result.error if getattr(result, "is_err", False) else "unknown"
        raw_text = f"result_not_ok: {err!r}"

    payload = {
        "scenario": scenario.slug,
        "goal": scenario.goal,
        "workdir": str(workdir),
        "captured_at_utc": ts,
        "preflight": preflight,
        "scenario_metadata": dict(scenario.metadata),
        # Raw passthrough — keep these keys verbatim from the MCP envelope.
        "mcp_result_is_ok": bool(getattr(result, "is_ok", False)),
        "mcp_result_is_error": raw_is_error,
        "mcp_result_meta": raw_meta,
        "mcp_result_content": raw_content_repr,
        "mcp_result_fallback_text": raw_text,
        "product_reality": {
            "artifact_path": scenario.product_artifact_path,
            "declared_output_paths": list(scenario.declared_output_paths),
            "smoke_results": product_smoke_results or [],
            "auto_session_id": raw_meta.get("auto_session_id"),
            "execution_id": raw_meta.get("execution_id"),
            "run_session_id": raw_meta.get("run_session_id"),
        },
    }
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return out_path


# Pytest stash key alias for the runtime preflight (declared in conftest).
# Re-exported here so ``_persist_raw_evidence`` can read it without taking
# a fixture dependency. The conftest module is loaded before test_canonical
# at collection time, so the StashKey instance is guaranteed to exist.
def _runtime_preflight_key() -> Any:
    from tests.canonical.conftest import _RUNTIME_PREFLIGHT_KEY  # type: ignore[attr-defined]

    return _RUNTIME_PREFLIGHT_KEY


_RUNTIME_PREFLIGHT_KEY_RUNTIME = _runtime_preflight_key()


@pytest.mark.asyncio
async def test_scenario_live_run_or_skip(
    scenario: CanonicalScenario,
    live_run_enabled: bool,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    """Live invocation of ``ouroboros_auto`` against the scenario.

    Skipped by default — costs real LLM tokens. Set
    ``OUROBOROS_RUN_CANONICAL=1`` to opt in. When enabled, the test:

    1. Copies the scenario's ``env/`` fixture into a fresh ``tmp_path``.
    2. Calls ``AutoHandler.handle({"goal": scenario.goal, ...})``.
    3. Asserts the result is OK (not an MCP-level error) and that the
       documented terminal status is reached.

    Any LLM / MCP misconfiguration surfaces as a failure with the
    underlying error message so the operator sees what's missing.
    """
    if not live_run_enabled:
        pytest.skip(
            "live canonical run disabled; set OUROBOROS_RUN_CANONICAL=1 to "
            "invoke ouroboros_auto against this scenario (costs real LLM tokens)"
        )

    workdir = tmp_path / scenario.slug
    workdir.mkdir(parents=True, exist_ok=True)
    if scenario.env_dir is not None:
        # Seed the env/ fixture into the run workdir if present.
        for entry in scenario.env_dir.iterdir():
            target = workdir / entry.name
            if entry.is_dir():
                import shutil

                shutil.copytree(entry, target)
            else:
                target.write_bytes(entry.read_bytes())

    result = await _invoke_ouroboros_auto(scenario, workdir)

    # PR-γ / #1170 evidence-integrity contract: persist the RAW MCP response
    # verbatim BEFORE any assertion. R2-1709 (#1170) saved a paraphrased
    # envelope (fabricated `interview_closure_mode="max_rounds_reached"` and
    # `stop_reason_code="interview_max_rounds_no_closure"` — neither string
    # exists in source). The harness must not be subject to the same defect.
    # Even on assertion failure, the on-disk evidence is the verbatim raw
    # handler response plus the runtime-preflight context.
    evidence_path = _persist_raw_evidence(scenario, workdir, result, request)
    print(f"CANONICAL {scenario.slug}: raw evidence -> {evidence_path}")

    assert result.is_ok, (
        f"{scenario.slug}: ouroboros_auto returned MCP error: "
        f"{result.error if result.is_err else 'unknown'}"
    )

    tool_result = result.unwrap()
    assert not tool_result.is_error, (
        f"{scenario.slug}: ouroboros_auto reached a failed/blocked terminal: "
        f"{tool_result.content[0].text if tool_result.content else tool_result.meta}"
    )
    assert tool_result.meta["status"] == "complete", (
        f"{scenario.slug}: expected complete terminal status; "
        f"got {tool_result.meta.get('status')!r} with meta {tool_result.meta}"
    )
    # PR-β / SSOT #1157 closure-policy assertion: under the ledger-primary
    # policy, the canonical scenario must terminate with closure_mode in
    # {ledger_only, mutual_agreement}. ``max_rounds_reached`` is not a real
    # closure mode (it was a #1170 R2-1709 paraphrase artifact) and any
    # ``interview_max_rounds_exhausted`` blocker indicates the AND-gate is
    # back / not deployed (see #1170 acceptance criteria).
    closure_mode = tool_result.meta.get("interview_closure_mode")
    if closure_mode is not None:
        assert closure_mode in {"ledger_only", "mutual_agreement", "safe_default"}, (
            f"{scenario.slug}: unexpected interview_closure_mode={closure_mode!r}; "
            f"valid modes per SSOT #1157 Closure Policy are "
            f"{{ledger_only, mutual_agreement, safe_default}}. "
            f"Full envelope persisted at {evidence_path}."
        )
    if scenario.completion_mode == "product_complete":
        assert tool_result.meta.get("product_status") != "not_verified_complete", (
            f"{scenario.slug}: product_complete scenario must not stop at an "
            f"unverified run handoff: {tool_result.meta}"
        )
    product_smoke_results = _run_product_reality_smoke_checks(scenario, workdir, tool_result.meta)
    if product_smoke_results:
        evidence_path = _persist_raw_evidence(
            scenario,
            workdir,
            result,
            request,
            product_smoke_results=product_smoke_results,
        )
        print(f"CANONICAL {scenario.slug}: product reality smoke -> {evidence_path}")
    print(
        f"CANONICAL {scenario.slug}: status={tool_result.meta['status']} "
        f"phase={tool_result.meta.get('phase')} "
        f"closure_mode={closure_mode} completion_mode={scenario.completion_mode} "
        f"artifact={scenario.product_artifact_path or 'none'} "
        f"smoke_commands={len(product_smoke_results)} "
        f"auto_session_id={tool_result.meta.get('auto_session_id')} "
        f"execution_id={tool_result.meta.get('execution_id')} "
        f"run_session_id={tool_result.meta.get('run_session_id')}"
    )


@pytest.mark.asyncio
async def test_live_run_opt_in_invokes_auto_handler(
    scenario: CanonicalScenario,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    """Hermetically pin that the opt-in path calls the live runner."""

    calls: list[tuple[str, Path]] = []

    class _ToolResult:
        is_error = False
        content: list[object] = []
        meta = {
            "status": "complete",
            "phase": "done",
            "product_status": "verified_complete",
            "interview_closure_mode": "ledger_only",
        }

    async def fake_invoke(selected: CanonicalScenario, workdir: Path) -> Result[object, str]:
        calls.append((selected.slug, workdir))
        (workdir / "habit_tracker.py").write_text(
            """
from __future__ import annotations
import json
import sys
from pathlib import Path

store = Path('habits.json')
habits = json.loads(store.read_text()) if store.exists() else []
if len(sys.argv) < 2:
    sys.exit(2)
cmd = sys.argv[1]
name = ' '.join(sys.argv[2:])
if cmd == 'add' and name:
    habits.append({'name': name, 'done': False})
    store.write_text(json.dumps(habits))
    print(name)
elif cmd == 'list':
    for item in habits:
        print(item['name'])
elif cmd == 'done' and name:
    for item in habits:
        if item['name'] == name:
            item['done'] = True
    store.write_text(json.dumps(habits))
    print(name)
else:
    sys.exit(2)
""".strip(),
            encoding="utf-8",
        )
        return Result.ok(_ToolResult())

    monkeypatch.setattr(
        "tests.canonical.test_canonical._invoke_ouroboros_auto",
        fake_invoke,
    )

    await test_scenario_live_run_or_skip(
        scenario=scenario,
        live_run_enabled=True,
        tmp_path=tmp_path,
        request=request,
    )

    assert calls == [(scenario.slug, tmp_path / scenario.slug)]
    # PR-γ evidence-integrity contract: the raw envelope must be persisted.
    obs_files = list((tmp_path / scenario.slug / ".ooo-observability").glob("canonical-*.json"))
    assert obs_files, "expected at least one evidence file"
    evidence_payloads = [json.loads(path.read_text(encoding="utf-8")) for path in obs_files]
    assert any(
        payload["product_reality"]["smoke_results"] for payload in evidence_payloads
    ), "expected product-reality smoke results in persisted evidence"
