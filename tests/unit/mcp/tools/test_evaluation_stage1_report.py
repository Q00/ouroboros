"""Shared mechanical diagnostics survive multi-AC and durable job presentation."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from ouroboros.evaluation.mechanical import CommandResult, MechanicalConfig
from ouroboros.evaluation.models import CheckResult, CheckType, EvaluationResult, MechanicalResult
from ouroboros.evaluation.pipeline import EvaluationPipeline, PipelineConfig
from ouroboros.mcp.job_manager import JobManager
from ouroboros.mcp.tools.evaluation_handlers import EvaluateHandler
from ouroboros.mcp.tools.evaluation_stage1_report import (
    format_stage1_result,
    serialize_stage1_result,
)
from ouroboros.persistence.event_store import EventStore


async def _multi_ac_result(tmp_path: Path, command_result: CommandResult):
    handler = EvaluateHandler()
    pipeline = EvaluationPipeline(
        MagicMock(),
        PipelineConfig(
            mechanical=MechanicalConfig(
                build_command=("npm", "run", "build"),
                test_command=("npm", "run", "test:unit"),
                timeout_seconds=17,
                working_dir=tmp_path,
            ),
        ),
    )
    with (
        patch(
            "ouroboros.evaluation.mechanical.run_command",
            new=AsyncMock(return_value=command_result),
        ) as run,
        patch.object(handler, "_has_code_changes", new=AsyncMock(return_value=None)),
    ):
        result = await handler._handle_multi_ac(
            session_id="stage1-diagnostics",
            seed_id="diagnostics-seed",
            acceptance_criteria=("Launches" + " long requirement" * 1300, "Plays", "Saves"),
            artifact="local artifact",
            artifact_type="code",
            goal="Preserve verification diagnostics",
            constraints=(),
            trigger_consensus=False,
            artifact_bundle=None,
            pipeline=pipeline,
            working_dir=tmp_path,
            emit_terminal_telemetry=False,
        )
    assert result.is_ok
    # Two configured checks, not two checks repeated for each of three ACs.
    assert run.await_count == 2
    assert [call.args[0] for call in run.await_args_list] == [
        ("npm", "run", "build"),
        ("npm", "run", "test:unit"),
    ]
    return result.value


async def test_shared_failure_has_commands_cwd_and_bounded_tails(tmp_path: Path) -> None:
    result = await _multi_ac_result(
        tmp_path,
        CommandResult(-1, "discard stdout prefix" + "o" * 500, "discard stderr prefix" + "e" * 500),
    )

    assert result.meta["final_approved"] is False
    assert result.meta["highest_stage"] == 1
    assert result.meta["passed_count"] == 0
    assert all(
        item["failure_reason"] == "Stage 1 failed: build, test" for item in result.meta["checklist"]
    )
    stage1 = result.meta["stage1_result"]
    assert stage1["passed"] is False
    failures = [check for check in stage1["checks"] if not check["passed"]]
    assert [check["check_type"] for check in failures] == ["build", "test"]
    assert failures[0]["details"] == {
        "command": ["npm", "run", "build"],
        "working_dir": str(tmp_path),
        "return_code": -1,
        "stdout_tail": "o" * 500,
        "stderr_tail": "e" * 500,
    }
    text = result.text_content
    assert text.count("Stage 1: Mechanical Verification") == 1
    assert text.count("command: npm run build") == 1
    assert text.count("command: npm run test:unit") == 1
    assert f"cwd: {tmp_path}" in text
    assert "exit code: -1" in text
    assert "discard stdout prefix" not in text
    assert "discard stderr prefix" not in text
    assert "o" * 500 in text
    assert "e" * 500 in text


async def test_shared_timeout_reports_available_details_without_invented_output(
    tmp_path: Path,
) -> None:
    result = await _multi_ac_result(tmp_path, CommandResult(-1, "", "Command timed out", True))

    failures = [check for check in result.meta["stage1_result"]["checks"] if not check["passed"]]
    assert failures[0]["details"] == {
        "command": ["npm", "run", "build"],
        "working_dir": str(tmp_path),
        "timed_out": True,
    }
    assert "timed out after 17s" in result.text_content
    assert "timed out: True" in result.text_content
    assert "exit code:" not in result.text_content
    assert "stdout tail:" not in result.text_content
    assert "stderr tail:" not in result.text_content


async def test_multi_ac_diagnostics_survive_persisted_job_snapshot(tmp_path: Path) -> None:
    executed = ("C:/Program Files/nodejs/npm.CMD", "run", "build")
    result = await _multi_ac_result(
        tmp_path,
        CommandResult(7, "build output", "actual failure", executed_command=executed),
    )
    url = f"sqlite+aiosqlite:///{tmp_path / 'diagnostics.db'}"
    store = EventStore(url)
    await store.initialize()
    manager = JobManager(store, durable_jobs=False)

    async def completed_evaluation():
        return result

    try:
        job = await manager.start_job(
            job_type="evaluate", initial_message="Evaluate", runner=completed_evaluation()
        )
        await manager.drain(grace_seconds=5)
    finally:
        await store.close()

    # Reopen the database and reconstruct without relying on the live job object.
    restored_store = EventStore(url)
    await restored_store.initialize()
    try:
        restored = await JobManager(restored_store, durable_jobs=False).get_snapshot(job.job_id)
        assert restored.is_terminal
        assert restored.status.value == "completed"
        assert restored.result_meta["final_approved"] is False
        assert restored.result_meta["stage1_result"] == result.meta["stage1_result"]
        build_check = next(
            check
            for check in restored.result_meta["stage1_result"]["checks"]
            if check["check_type"] == "build"
        )
        assert build_check["details"]["executed_command"] == list(executed)
        assert "actual failure" in restored.result_text
        assert "executed command: C:/Program Files/nodejs/npm.CMD run build" in restored.result_text
        assert restored.result_text.count("Stage 1: Mechanical Verification") == 1
    finally:
        await restored_store.close()


def test_single_ac_existing_stage1_presentation_is_preserved() -> None:
    evaluation = EvaluationResult(
        execution_id="single",
        stage1_result=MechanicalResult(
            passed=False,
            checks=(
                CheckResult(
                    check_type=CheckType.BUILD,
                    passed=False,
                    message="Check build failed (exit code 7)",
                    details={
                        "command": ["npm", "run", "build"],
                        "working_dir": "project",
                        "return_code": 7,
                        "stdout_tail": "build output",
                        "stderr_tail": "actual failure",
                    },
                ),
            ),
        ),
    )
    text = EvaluateHandler()._format_evaluation_result(evaluation)
    assert (
        "Stage 1: Mechanical Verification\n"
        "----------------------------------------\n"
        "Status: FAILED\n"
        "Coverage: N/A\n"
        "  [FAIL] build: Check build failed (exit code 7)\n"
        "    command: npm run build\n"
        "    cwd: project\n"
        "    stdout tail:\n"
        "      build output\n"
        "    stderr tail:\n"
        "      actual failure\n"
    ) in text
    assert "exit code: 7" not in text


def test_serializer_keeps_only_known_details_and_bounds_oversized_tails() -> None:
    requested = ["npm", "run", "build"]
    executed = ["C:/Program Files/nodejs/npm.CMD", "run", "build"]
    details = {
        "command": requested,
        "executed_command": executed,
        "working_dir": "C:/작업 폴더/project",
        "return_code": 9,
        "timed_out": False,
        "stdout_tail": "s" * 600,
        "stderr_tail": "e" * 600,
        "stdout": "full raw output must not be copied",
        "private_extra": object(),
    }
    stage1 = MechanicalResult(
        passed=False,
        checks=(CheckResult(CheckType.BUILD, False, "failed", details),),
    )
    serialized = serialize_stage1_result(stage1)
    assert serialized is not None
    saved_details = serialized["checks"][0]["details"]
    assert saved_details["command"] == requested
    assert saved_details["executed_command"] == executed
    assert saved_details["stdout_tail"] == "s" * 500
    assert saved_details["stderr_tail"] == "e" * 500
    assert "stdout" not in saved_details
    assert "private_extra" not in saved_details
    assert stage1.checks[0].details["stdout_tail"] == "s" * 600
    text = "\n".join(format_stage1_result(stage1, include_exit_status=True))
    assert "executed command: C:/Program Files/nodejs/npm.CMD run build" in text
    assert "cwd: C:/작업 폴더/project" in text
    assert "s" * 501 not in text


def test_passing_skipped_and_absent_stage1_are_not_changed() -> None:
    skipped = MechanicalResult(
        passed=True,
        checks=(CheckResult(CheckType.TEST, True, "not configured", {"skipped": True}),),
    )
    serialized = serialize_stage1_result(skipped)
    assert serialized is not None
    assert serialized["passed"] is True
    assert serialized["checks"][0]["details"] == {"skipped": True}
    assert "Status: PASSED" in format_stage1_result(skipped)
    assert serialize_stage1_result(None) is None
    assert format_stage1_result(None) == []
