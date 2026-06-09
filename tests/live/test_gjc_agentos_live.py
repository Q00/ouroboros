"""Opt-in live QA for the GJC AgentOS integration.

These tests intentionally skip by default. They exercise a real local ``gjc``
installation only when OUROBOROS_LIVE_GJC=1, a GJC binary is resolvable, and a
cheap RPC readiness/auth probe succeeds.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
from typing import Any

import pytest

from ouroboros.config import get_gjc_cli_path
from ouroboros.orchestrator.gjc_runtime import GjcRuntime
from ouroboros.providers.base import CompletionConfig, Message, MessageRole
from ouroboros.providers.gjc_llm_adapter import GjcLLMAdapter

pytestmark = pytest.mark.live_gjc

_READY_TIMEOUT_SECONDS = 45.0
_TASK_TIMEOUT_SECONDS = 180.0


def _resolved_gjc_cli() -> str | None:
    configured = get_gjc_cli_path()
    if configured:
        configured_path = Path(configured).expanduser()
        if configured_path.exists():
            return str(configured_path)
        resolved_configured = shutil.which(configured)
        if resolved_configured:
            return resolved_configured
        return None
    return shutil.which("gjc")


async def _readiness_probe(cli_path: str, tmp_path: Path) -> tuple[bool, str]:
    runtime = GjcRuntime(
        cli_path=cli_path,
        cwd=tmp_path,
        startup_output_timeout_seconds=_READY_TIMEOUT_SECONDS,
        stdout_idle_timeout_seconds=_READY_TIMEOUT_SECONDS,
    )
    try:
        result = await asyncio.wait_for(
            runtime.execute_task_to_result(
                prompt=(
                    "Readiness probe only. Reply with the exact token "
                    "OUROBOROS_GJC_READY and do not create or modify files."
                )
            ),
            timeout=_READY_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # pragma: no cover - live environment dependent
        return False, f"GJC readiness probe failed: {type(exc).__name__}: {exc}"
    if result.is_err:
        return False, f"GJC readiness probe failed: {result.error.message}"
    if "OUROBOROS_GJC_READY" not in result.value.final_message:
        return False, "GJC readiness probe did not return the expected readiness token"
    return True, ""


@pytest.fixture(scope="module")
def live_gjc_cli(tmp_path_factory: pytest.TempPathFactory) -> str:
    if os.environ.get("OUROBOROS_LIVE_GJC") != "1":
        pytest.skip("Set OUROBOROS_LIVE_GJC=1 to opt in to live GJC QA.")
    cli_path = _resolved_gjc_cli()
    if not cli_path:
        pytest.skip(
            "GJC CLI is not resolvable; install gjc, put it on PATH, or set OUROBOROS_GJC_CLI_PATH."
        )
    probe_cwd = tmp_path_factory.mktemp("gjc-readiness")
    ok, reason = asyncio.run(_readiness_probe(cli_path, probe_cwd))
    if not ok:
        pytest.skip(reason)
    return cli_path


async def _run_runtime(cli_path: str, cwd: Path, prompt: str) -> Any:
    runtime = GjcRuntime(
        cli_path=cli_path,
        cwd=cwd,
        startup_output_timeout_seconds=_READY_TIMEOUT_SECONDS,
        stdout_idle_timeout_seconds=_TASK_TIMEOUT_SECONDS,
    )
    result = await asyncio.wait_for(
        runtime.execute_task_to_result(prompt=prompt),
        timeout=_TASK_TIMEOUT_SECONDS,
    )
    assert result.is_ok, result.error.message if result.is_err else "runtime failed"
    assert result.value.success is True
    return result.value


def _relative_file_set(root: Path) -> set[Path]:
    return {
        path.relative_to(root)
        for path in root.rglob("*")
        if path.is_file() and ".gjc" not in path.parts
    }


@pytest.mark.asyncio
async def test_real_gjc_runtime_creates_file_inside_tmp_path_only(
    live_gjc_cli: str,
    tmp_path: Path,
) -> None:
    token = "OUROBOROS-LIVE-GJC-HELLO-7f4b2c"
    before = _relative_file_set(tmp_path)

    await _run_runtime(
        live_gjc_cli,
        tmp_path,
        (
            f"In the current working directory only, create hello-gjc.txt containing {token}. "
            "Do not write any other files. When done, briefly report completion."
        ),
    )

    target = tmp_path / "hello-gjc.txt"
    assert target.exists()
    assert token in target.read_text(encoding="utf-8")
    after = _relative_file_set(tmp_path)
    assert after - before == {Path("hello-gjc.txt")}


@pytest.mark.asyncio
async def test_real_gjc_runtime_reads_seed_and_writes_derived_output(
    live_gjc_cli: str,
    tmp_path: Path,
) -> None:
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps({"name": "live-gjc", "count": 3}), encoding="utf-8")

    await _run_runtime(
        live_gjc_cli,
        tmp_path,
        (
            "Read seed.json in the current working directory. Write derived.json as JSON with "
            "keys sourceName and doubledCount, where sourceName is the seed name and "
            "doubledCount is count multiplied by 2. Do not write any other output files."
        ),
    )

    payload = json.loads((tmp_path / "derived.json").read_text(encoding="utf-8"))
    assert payload == {"sourceName": "live-gjc", "doubledCount": 6}


@pytest.mark.asyncio
async def test_gjc_llm_adapter_json_schema_returns_schema_valid_payload(
    live_gjc_cli: str,
    tmp_path: Path,
) -> None:
    adapter = GjcLLMAdapter(cli_path=live_gjc_cli, cwd=tmp_path, timeout=_TASK_TIMEOUT_SECONDS)
    result = await adapter.complete(
        [
            Message(
                role=MessageRole.USER,
                content=(
                    "Return a compact JSON object for a QA receipt with status exercised "
                    "and checks containing readiness and schema."
                ),
            )
        ],
        CompletionConfig(
            model="default",
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["status", "checks"],
                    "properties": {
                        "status": {"type": "string"},
                        "checks": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string"},
                        },
                    },
                },
            },
        ),
    )

    assert result.is_ok, result.error.message if result.is_err else "completion failed"
    payload = json.loads(result.value.content)
    assert isinstance(payload["status"], str)
    assert isinstance(payload["checks"], list)
    assert payload["checks"]
    assert all(isinstance(item, str) for item in payload["checks"])


@pytest.mark.asyncio
async def test_gjc_runtime_telemetry_logs_duration_fields(
    live_gjc_cli: str,
    tmp_path: Path,
) -> None:
    import structlog

    required = {
        "gjc_runtime.ready_received": "spawn_to_ready_ms",
        "gjc_runtime.prompt_acknowledged": "prompt_ack_ms",
        "gjc_runtime.task_completed": "task_wall_ms",
    }
    with structlog.testing.capture_logs() as logs:
        await _run_runtime(
            live_gjc_cli,
            tmp_path,
            "Reply with the exact token OUROBOROS_GJC_TELEMETRY and do not modify files.",
        )

    seen: dict[str, float] = {}
    for entry in logs:
        event_name = entry.get("event")
        field_name = required.get(event_name)
        if field_name is None:
            continue
        value = entry.get(field_name)
        assert isinstance(value, int | float)
        assert value >= 0
        seen[field_name] = value
    assert set(seen) == set(required.values())


def test_ooo_bridge_installed_source_dispatches_to_gjc_runtime(live_gjc_cli: str) -> None:
    del live_gjc_cli
    from ouroboros.cli.commands import setup as setup_cmd

    source = setup_cmd._gjc_bridge_source_text()
    assert source is not None
    assert '"dispatch", "--runtime", "gjc"' in source
    assert '"--cwd", cwd' in source
    assert "execFileAsync" in source
    assert "ouroboros" in source
    # Interactive GJC PTY input round-trip is intentionally a MANUAL receipt step.
    # This deterministic proof verifies the installed extension dispatch wiring only;
    # it does not claim automated PTY confidence.
