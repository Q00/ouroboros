"""Unit tests for Windows Codex HTTP MCP task provisioning."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

import ouroboros.cli.codex_http_mcp as codex_http_mcp

LAUNCHER = (r"C:\Program Files\Ouroboros\ouroboros.exe", ["mcp", "serve"])


def _managed_task_xml(runner_path: Path) -> str:
    arguments = codex_http_mcp._task_arguments(runner_path)
    return f"""<Task>
  <Actions><Exec><Command>powershell.exe</Command><Arguments>{arguments}</Arguments></Exec></Actions>
</Task>"""


def _absent_task_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
    """Model Task Scheduler with no reserved task before provisioning."""
    return subprocess.CompletedProcess(args=command, returncode=int("/Query" in command))


def test_provision_creates_idempotent_task_with_deduplicated_arguments(tmp_path: Path) -> None:
    """The task is replaceable and supplements, but does not duplicate, launcher args."""
    launcher = (
        LAUNCHER[0],
        ["mcp", "serve", "--runtime", "codex", "--llm-backend", "codex"],
    )
    task_xml: str | None = None

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal task_xml
        if "/Query" in command:
            return subprocess.CompletedProcess(
                args=command, returncode=0 if task_xml is not None else 1, stdout=task_xml or ""
            )
        if "/Create" in command:
            task_xml = Path(command[command.index("/XML") + 1]).read_text(encoding="utf-16")
        return subprocess.CompletedProcess(args=command, returncode=0)

    with (
        patch("ouroboros.cli.codex_http_mcp.shutil.which", return_value="schtasks.exe"),
        patch(
            "ouroboros.cli.codex_http_mcp._current_windows_identity",
            return_value=r"DOMAIN\codex",
        ),
        patch("ouroboros.cli.codex_http_mcp.subprocess.run", side_effect=run) as run_mock,
        patch(
            "ouroboros.cli.codex_http_mcp._wait_for_http_readiness",
            side_effect=(False, True, False, True),
        ),
        patch("ouroboros.cli.codex_http_mcp._scheduled_task_is_running", return_value=True),
        patch("ouroboros.cli.codex_http_mcp._owns_generation", return_value=True),
    ):
        assert codex_http_mcp.provision_windows_codex_mcp_http(tmp_path, launcher) is None
        assert codex_http_mcp.provision_windows_codex_mcp_http(tmp_path, launcher) is None

    create_calls = [call.args[0] for call in run_mock.call_args_list if "/Create" in call.args[0]]
    assert len(create_calls) == 2
    assert all("/F" in call for call in create_calls)
    runner = (tmp_path / "ouroboros-mcp-http.ps1").read_text(encoding="utf-8")
    assert "'--transport'," in runner
    assert "'streamable-http'," in runner
    assert "'--host'," in runner
    assert "'127.0.0.1'," in runner
    assert "'--port'," in runner
    assert "'8765'" in runner
    assert runner.count("'--runtime'") == 1
    assert runner.count("'--llm-backend'") == 1


def test_provision_writes_escaped_identity_and_task_settings(tmp_path: Path) -> None:
    """Task XML uses the interactive identity and safe long-running settings."""
    completed = subprocess.CompletedProcess(args=[], returncode=0)
    task_xml: list[str] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "/Query" in command:
            return subprocess.CompletedProcess(args=command, returncode=1)
        if "/Create" in command:
            task_xml.append(Path(command[command.index("/XML") + 1]).read_text(encoding="utf-16"))
        return completed

    with (
        patch("ouroboros.cli.codex_http_mcp.shutil.which", return_value="schtasks.exe"),
        patch(
            "ouroboros.cli.codex_http_mcp._current_windows_identity",
            return_value=r"DOMAIN\codex&owner",
        ),
        patch("ouroboros.cli.codex_http_mcp.subprocess.run", side_effect=run),
        patch("ouroboros.cli.codex_http_mcp._wait_for_http_readiness", side_effect=(False, True)),
        patch("ouroboros.cli.codex_http_mcp._owns_generation", return_value=True),
        patch("ouroboros.cli.codex_http_mcp._scheduled_task_is_running", return_value=True),
    ):
        assert codex_http_mcp.provision_windows_codex_mcp_http(tmp_path, LAUNCHER) is None

    assert task_xml[0].count(r"DOMAIN\codex&amp;owner") == 2
    assert "-WindowStyle Hidden" in task_xml[0]
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in task_xml[0]
    assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in task_xml[0]
    assert "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>" in task_xml[0]
    assert (
        "<RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure>"
        in task_xml[0]
    )
    assert "<Hidden>true</Hidden>" in task_xml[0]


def test_provision_quotes_powershell_literals_and_windows_runner_path(tmp_path: Path) -> None:
    """Commands and arguments remain literal across PowerShell and Windows parsing."""
    completed = subprocess.CompletedProcess(args=[], returncode=0)
    task_xml: list[str] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "/Query" in command:
            return subprocess.CompletedProcess(args=command, returncode=1)
        if "/Create" in command:
            task_xml.append(Path(command[command.index("/XML") + 1]).read_text(encoding="utf-16"))
        return completed

    config_dir = tmp_path / "MCP user's task"
    launcher = (r"C:\O'Brien\ouroboros.exe", ["mcp", "serve", '--label="quoted"'])
    with (
        patch("ouroboros.cli.codex_http_mcp.shutil.which", return_value="schtasks.exe"),
        patch(
            "ouroboros.cli.codex_http_mcp._current_windows_identity",
            return_value=r"DOMAIN\codex",
        ),
        patch("ouroboros.cli.codex_http_mcp.subprocess.run", side_effect=run),
        patch("ouroboros.cli.codex_http_mcp._wait_for_http_readiness", side_effect=(False, True)),
        patch("ouroboros.cli.codex_http_mcp._owns_generation", return_value=True),
        patch("ouroboros.cli.codex_http_mcp._scheduled_task_is_running", return_value=True),
    ):
        assert codex_http_mcp.provision_windows_codex_mcp_http(config_dir, launcher) is None

    runner = (config_dir / "ouroboros-mcp-http.ps1").read_text(encoding="utf-8")
    assert "& 'C:\\O''Brien\\ouroboros.exe' @arguments" in runner
    assert "'--label=\"quoted\"'," in runner


def test_provision_rolls_back_new_task_when_readiness_fails(tmp_path: Path) -> None:
    """An unready replacement leaves neither a runner nor a scheduled task."""
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(args=command, returncode=0)

    with (
        patch("ouroboros.cli.codex_http_mcp.shutil.which", return_value="schtasks.exe"),
        patch(
            "ouroboros.cli.codex_http_mcp._current_windows_identity",
            return_value=r"DOMAIN\codex",
        ),
        patch("ouroboros.cli.codex_http_mcp.subprocess.run", side_effect=run),
        patch("ouroboros.cli.codex_http_mcp._wait_for_http_readiness", return_value=False),
        patch("ouroboros.cli.codex_http_mcp._owns_generation", return_value=True),
        patch("ouroboros.cli.codex_http_mcp._scheduled_task_is_running", return_value=False),
    ):
        assert (
            codex_http_mcp.provision_windows_codex_mcp_http(tmp_path, LAUNCHER)
            == "The Ouroboros MCP HTTP scheduled task did not become ready."
        )

    assert not (tmp_path / "ouroboros-mcp-http.ps1").exists()
    assert any("/Delete" in command for command in calls)


def test_provision_rejects_ready_listener_when_new_task_is_not_running(tmp_path: Path) -> None:
    """An existing Ouroboros listener cannot satisfy readiness for a dead new task."""
    completed = subprocess.CompletedProcess(args=[], returncode=0)
    with (
        patch("ouroboros.cli.codex_http_mcp.shutil.which", return_value="schtasks.exe"),
        patch(
            "ouroboros.cli.codex_http_mcp._current_windows_identity", return_value=r"DOMAIN\codex"
        ),
        patch("ouroboros.cli.codex_http_mcp.subprocess.run", return_value=completed),
        patch("ouroboros.cli.codex_http_mcp._wait_for_http_readiness", return_value=True),
        patch("ouroboros.cli.codex_http_mcp._owns_generation", return_value=True),
        patch("ouroboros.cli.codex_http_mcp._scheduled_task_is_running", return_value=False),
    ):
        assert codex_http_mcp.provision_windows_codex_mcp_http(tmp_path, LAUNCHER) == (
            "Refusing to provision while an existing Ouroboros MCP HTTP endpoint is listening."
        )


def test_provision_restores_and_restarts_existing_managed_task_when_readiness_fails(
    tmp_path: Path,
) -> None:
    """Readiness failure restores and restarts the managed task and runner it replaced."""
    runner_path = tmp_path / "ouroboros-mcp-http.ps1"
    prior_runner = f"{codex_http_mcp._RUNNER_MARKER}\n"
    runner_path.write_text(prior_runner, encoding="utf-8")
    prior_xml = _managed_task_xml(runner_path)

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "/Query" in command:
            return subprocess.CompletedProcess(args=command, returncode=0, stdout=prior_xml)
        return subprocess.CompletedProcess(args=command, returncode=0)

    with (
        patch("ouroboros.cli.codex_http_mcp.shutil.which", return_value="schtasks.exe"),
        patch(
            "ouroboros.cli.codex_http_mcp._current_windows_identity", return_value=r"DOMAIN\codex"
        ),
        patch("ouroboros.cli.codex_http_mcp.subprocess.run", side_effect=run) as mocked_run,
        patch("ouroboros.cli.codex_http_mcp._scheduled_task_is_running", return_value=True),
        patch("ouroboros.cli.codex_http_mcp._wait_for_http_readiness", return_value=False),
        patch("ouroboros.cli.codex_http_mcp._owns_generation", return_value=True),
    ):
        assert codex_http_mcp.provision_windows_codex_mcp_http(tmp_path, LAUNCHER) is not None

    assert runner_path.read_text(encoding="utf-8") == prior_runner
    calls = [call.args[0] for call in mocked_run.call_args_list]
    restore_index = max(index for index, call in enumerate(calls) if "/Create" in call)
    assert next(call for call in calls[restore_index + 1 :] if "/Run" in call) == [
        "schtasks.exe",
        "/Run",
        "/TN",
        "Ouroboros MCP HTTP",
    ]


def test_provision_readiness_timeout_is_bounded() -> None:
    """Readiness polling stops at its configured deadline instead of spinning forever."""
    with (
        patch("ouroboros.cli.codex_http_mcp.time.monotonic", side_effect=(0.0, 0.0, 11.0)),
        patch("ouroboros.cli.codex_http_mcp.time.sleep"),
        patch("ouroboros.cli.codex_http_mcp.urlrequest.urlopen", side_effect=OSError),
    ):
        assert not codex_http_mcp._wait_for_http_readiness()


def test_remove_windows_http_task_preserves_unmanaged_task_and_runner(tmp_path: Path) -> None:
    """An unrelated same-name task and runner are not stopped, deleted, or unlinked."""
    runner_path = tmp_path / "ouroboros-mcp-http.ps1"
    runner_path.write_text("user script", encoding="utf-8")
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if "/Query" in command:
            return subprocess.CompletedProcess(args=command, returncode=0, stdout="<Task />")
        return subprocess.CompletedProcess(args=command, returncode=0)

    with (
        patch("ouroboros.cli.codex_http_mcp.shutil.which", return_value="schtasks.exe"),
        patch("ouroboros.cli.codex_http_mcp.subprocess.run", side_effect=run),
    ):
        assert codex_http_mcp.remove_windows_codex_mcp_http(tmp_path) is None

    assert runner_path.exists()
    assert not any("/End" in command or "/Delete" in command for command in calls)


def test_remove_windows_http_task_deletes_exact_marker_runner(tmp_path: Path) -> None:
    """Only a runner with setup's exact marker is eligible for deletion."""
    runner_path = tmp_path / "ouroboros-mcp-http.ps1"
    runner_path.write_text(f"{codex_http_mcp._RUNNER_MARKER}\nuser script\n", encoding="utf-8")

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=command, returncode=1 if "/Query" in command else 0)

    with (
        patch("ouroboros.cli.codex_http_mcp.shutil.which", return_value="schtasks.exe"),
        patch("ouroboros.cli.codex_http_mcp.subprocess.run", side_effect=run),
    ):
        assert codex_http_mcp.remove_windows_codex_mcp_http(tmp_path) == (
            "Refusing to remove task and runner with mismatched Ouroboros MCP ownership claims."
        )

    assert runner_path.exists()


def test_remove_windows_http_task_restores_running_task_when_delete_fails(tmp_path: Path) -> None:
    """A failed delete restarts the prior running generation and preserves its runner."""
    runner_path = tmp_path / codex_http_mcp._RUNNER_NAME
    runner_path.write_text(f"{codex_http_mcp._RUNNER_MARKER}\n", encoding="utf-8")
    task_xml = _managed_task_xml(runner_path)

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "/Query" in command:
            return subprocess.CompletedProcess(args=command, returncode=0, stdout=task_xml)
        return subprocess.CompletedProcess(args=command, returncode=int("/Delete" in command))

    with (
        patch("ouroboros.cli.codex_http_mcp.shutil.which", return_value="schtasks.exe"),
        patch("ouroboros.cli.codex_http_mcp.subprocess.run", side_effect=run) as mocked_run,
        patch("ouroboros.cli.codex_http_mcp._scheduled_task_is_running", return_value=True),
        patch("ouroboros.cli.codex_http_mcp._wait_for_http_readiness", return_value=True),
    ):
        assert codex_http_mcp.remove_windows_codex_mcp_http(tmp_path) == (
            "Could not remove the Ouroboros MCP HTTP scheduled task; previous state was restored."
        )

    assert runner_path.exists()
    assert any("/Run" in call.args[0] for call in mocked_run.call_args_list)


def test_provision_refuses_to_replace_unmanaged_runner(tmp_path: Path) -> None:
    """A same-name runner without the exact marker is preserved."""
    runner_path = tmp_path / "ouroboros-mcp-http.ps1"
    runner_path.write_text("user script", encoding="utf-8")

    with (
        patch("ouroboros.cli.codex_http_mcp.shutil.which", return_value="schtasks.exe"),
        patch(
            "ouroboros.cli.codex_http_mcp._current_windows_identity", return_value=r"DOMAIN\codex"
        ),
        patch("ouroboros.cli.codex_http_mcp.subprocess.run") as run,
    ):
        assert (
            codex_http_mcp.provision_windows_codex_mcp_http(tmp_path, LAUNCHER)
            == "Refusing to overwrite a runner not managed by Ouroboros MCP HTTP setup."
        )

    assert runner_path.read_text(encoding="utf-8") == "user script"
    assert not any("/Create" in call.args[0] for call in run.call_args_list)


def test_provision_refuses_to_replace_unmanaged_task(tmp_path: Path) -> None:
    """A same-name task without setup's exact action is preserved."""
    runner_path = tmp_path / "ouroboros-mcp-http.ps1"
    runner_path.write_text(f"{codex_http_mcp._RUNNER_MARKER}\n", encoding="utf-8")

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "/Query" in command:
            return subprocess.CompletedProcess(args=command, returncode=0, stdout="<Task />")
        return subprocess.CompletedProcess(args=command, returncode=0)

    with (
        patch("ouroboros.cli.codex_http_mcp.shutil.which", return_value="schtasks.exe"),
        patch(
            "ouroboros.cli.codex_http_mcp._current_windows_identity", return_value=r"DOMAIN\codex"
        ),
        patch("ouroboros.cli.codex_http_mcp.subprocess.run", side_effect=run) as mocked_run,
    ):
        assert (
            codex_http_mcp.provision_windows_codex_mcp_http(tmp_path, LAUNCHER)
            == "Refusing to overwrite a reserved task not managed by Ouroboros MCP HTTP setup."
        )

    assert runner_path.read_text(encoding="utf-8") == f"{codex_http_mcp._RUNNER_MARKER}\n"
    assert not any("/Create" in call.args[0] for call in mocked_run.call_args_list)


def test_provision_does_not_touch_legacy_task_xml_file(tmp_path: Path) -> None:
    """A historical deterministic task XML remains user-owned and untouched."""
    legacy_task_xml = tmp_path / "ouroboros-mcp-http-task.xml"
    legacy_task_xml.write_text("user task XML", encoding="utf-8")

    with (
        patch("ouroboros.cli.codex_http_mcp.shutil.which", return_value="schtasks.exe"),
        patch(
            "ouroboros.cli.codex_http_mcp._current_windows_identity", return_value=r"DOMAIN\codex"
        ),
        patch("ouroboros.cli.codex_http_mcp.subprocess.run", side_effect=_absent_task_run),
        patch("ouroboros.cli.codex_http_mcp._wait_for_http_readiness", side_effect=(False, True)),
        patch("ouroboros.cli.codex_http_mcp._owns_generation", return_value=True),
        patch("ouroboros.cli.codex_http_mcp._scheduled_task_is_running", return_value=True),
    ):
        assert codex_http_mcp.provision_windows_codex_mcp_http(tmp_path, LAUNCHER) is None

    assert legacy_task_xml.read_text(encoding="utf-8") == "user task XML"


@pytest.mark.parametrize(
    ("failed_step", "rollback_error"),
    [
        ("create", "Could not restore the previous scheduled task."),
        ("run", "Could not restart the previous scheduled task."),
        ("readiness", "The restored scheduled task did not become ready."),
    ],
)
def test_provision_reports_failed_managed_task_rollback(
    tmp_path: Path, failed_step: str, rollback_error: str
) -> None:
    """A failed replacement reports a failed create, run, or readiness restore."""
    runner_path = tmp_path / "ouroboros-mcp-http.ps1"
    runner_path.write_text(f"{codex_http_mcp._RUNNER_MARKER}\n", encoding="utf-8")
    prior_xml = _managed_task_xml(runner_path)
    create_count = 0

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal create_count
        if "/Query" in command:
            return subprocess.CompletedProcess(args=command, returncode=0, stdout=prior_xml)
        if "/Create" in command:
            create_count += 1
            return subprocess.CompletedProcess(
                args=command, returncode=int(create_count == 1 and failed_step == "create")
            )
        if "/Run" in command and failed_step == "run":
            return subprocess.CompletedProcess(args=command, returncode=1)
        return subprocess.CompletedProcess(args=command, returncode=0)

    readiness = [False, False, False]
    with (
        patch("ouroboros.cli.codex_http_mcp.shutil.which", return_value="schtasks.exe"),
        patch(
            "ouroboros.cli.codex_http_mcp._current_windows_identity", return_value=r"DOMAIN\codex"
        ),
        patch("ouroboros.cli.codex_http_mcp.subprocess.run", side_effect=run),
        patch("ouroboros.cli.codex_http_mcp._scheduled_task_is_running", return_value=True),
        patch("ouroboros.cli.codex_http_mcp._wait_for_http_readiness", side_effect=readiness),
        patch("ouroboros.cli.codex_http_mcp._owns_generation", return_value=True),
    ):
        error = codex_http_mcp.provision_windows_codex_mcp_http(tmp_path, LAUNCHER)

    assert error is not None
    if failed_step != "create":
        assert f"Rollback failed: {rollback_error}" in error


def test_remove_windows_http_task_requires_exact_runner_marker(tmp_path: Path) -> None:
    """A marker substring does not establish ownership of a same-name runner."""
    runner_path = tmp_path / "ouroboros-mcp-http.ps1"
    runner_path.write_text(f"prefix {codex_http_mcp._RUNNER_MARKER}\n", encoding="utf-8")

    with (
        patch("ouroboros.cli.codex_http_mcp.shutil.which", return_value="schtasks.exe"),
        patch(
            "ouroboros.cli.codex_http_mcp.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=1),
        ),
    ):
        assert codex_http_mcp.remove_windows_codex_mcp_http(tmp_path) is None

    assert runner_path.exists()


class _Response:
    def __init__(self, content_type: str, body: str) -> None:
        self.headers = {"Content-Type": content_type}
        self.body = body.encode()
        self.status = 200
        self.closed = False

    def read(self) -> bytes:
        return self.body

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("content_type", ["application/json", "text/event-stream"])
def test_readiness_accepts_expected_mcp_identity(content_type: str) -> None:
    """JSON and SSE initialize responses must identify the local Ouroboros MCP."""
    response_id = "ouroboros-readiness"
    message = {
        "jsonrpc": "2.0",
        "id": response_id,
        "result": {
            "protocolVersion": "2025-03-26",
            "serverInfo": {"name": "ouroboros-mcp", "version": "0.51.1"},
        },
    }
    body = json.dumps(message)
    if content_type == "text/event-stream":
        body = f"event: message\ndata: {body}\n\n"
    response = _Response(content_type, body)

    with (
        patch("ouroboros.cli.codex_http_mcp.time.monotonic", side_effect=(0.0, 0.0)),
        patch("ouroboros.cli.codex_http_mcp.urlrequest.urlopen", return_value=response) as urlopen,
    ):
        assert codex_http_mcp._wait_for_http_readiness()

    request = urlopen.call_args.args[0]
    assert request.full_url == "http://127.0.0.1:8765/mcp"
    assert request.get_method() == "POST"
    assert json.loads(request.data) == {
        "jsonrpc": "2.0",
        "id": response_id,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "ouroboros-setup", "version": "1"},
        },
    }
    assert response.closed


@pytest.mark.parametrize(
    "body",
    [
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "ouroboros-readiness",
                "result": {"serverInfo": {"name": "foreign"}},
            }
        ),
        "not json",
        json.dumps({"jsonrpc": "2.0", "id": "wrong", "error": {"code": -1}}),
    ],
)
def test_readiness_rejects_foreign_or_invalid_mcp_response(body: str) -> None:
    """A listener is not ready unless it returns the expected initialize identity."""
    response = _Response("application/json", body)
    with (
        patch("ouroboros.cli.codex_http_mcp.time.monotonic", side_effect=(0.0, 0.0, 11.0)),
        patch("ouroboros.cli.codex_http_mcp.time.sleep"),
        patch("ouroboros.cli.codex_http_mcp.urlrequest.urlopen", return_value=response),
    ):
        assert not codex_http_mcp._wait_for_http_readiness()
    assert response.closed


def test_provision_ends_running_task_before_replacement(tmp_path: Path) -> None:
    """A running managed generation is ended before its XML and runner are replaced."""
    runner_path = tmp_path / "ouroboros-mcp-http.ps1"
    runner_path.write_text(f"{codex_http_mcp._RUNNER_MARKER}\n", encoding="utf-8")
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if "/Query" in command:
            return subprocess.CompletedProcess(
                args=command, returncode=0, stdout=_managed_task_xml(runner_path)
            )
        return subprocess.CompletedProcess(args=command, returncode=0)

    with (
        patch("ouroboros.cli.codex_http_mcp.shutil.which", return_value="schtasks.exe"),
        patch(
            "ouroboros.cli.codex_http_mcp._current_windows_identity", return_value=r"DOMAIN\codex"
        ),
        patch("ouroboros.cli.codex_http_mcp._scheduled_task_is_running", return_value=True),
        patch("ouroboros.cli.codex_http_mcp.subprocess.run", side_effect=run),
        patch("ouroboros.cli.codex_http_mcp._wait_for_http_readiness", side_effect=(False, True)),
        patch("ouroboros.cli.codex_http_mcp._owns_generation", return_value=True),
    ):
        assert codex_http_mcp.provision_windows_codex_mcp_http(tmp_path, LAUNCHER) is None

    assert calls.index(["schtasks.exe", "/End", "/TN", "Ouroboros MCP HTTP"]) < next(
        index for index, call in enumerate(calls) if "/Create" in call
    )


def test_provision_rollback_preserves_stopped_task_state(tmp_path: Path) -> None:
    """Rollback restores a stopped task without restarting the old generation."""
    runner_path = tmp_path / "ouroboros-mcp-http.ps1"
    prior_runner = f"{codex_http_mcp._RUNNER_MARKER}\n"
    runner_path.write_text(prior_runner, encoding="utf-8")
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if "/Query" in command:
            return subprocess.CompletedProcess(
                args=command, returncode=0, stdout=_managed_task_xml(runner_path)
            )
        return subprocess.CompletedProcess(args=command, returncode=0)

    with (
        patch("ouroboros.cli.codex_http_mcp.shutil.which", return_value="schtasks.exe"),
        patch(
            "ouroboros.cli.codex_http_mcp._current_windows_identity", return_value=r"DOMAIN\codex"
        ),
        patch("ouroboros.cli.codex_http_mcp._scheduled_task_is_running", return_value=False),
        patch("ouroboros.cli.codex_http_mcp.subprocess.run", side_effect=run),
        patch(
            "ouroboros.cli.codex_http_mcp._wait_for_http_readiness", return_value=False
        ) as readiness,
        patch("ouroboros.cli.codex_http_mcp._owns_generation", return_value=True),
    ):
        assert codex_http_mcp.provision_windows_codex_mcp_http(tmp_path, LAUNCHER) is not None

    assert runner_path.read_text(encoding="utf-8") == prior_runner
    assert calls.count(["schtasks.exe", "/Run", "/TN", "Ouroboros MCP HTTP"]) == 1
    assert readiness.call_count == 2


def test_provision_rollback_restarts_and_verifies_running_task(tmp_path: Path) -> None:
    """Rollback starts and verifies a previously running generation."""
    runner_path = tmp_path / "ouroboros-mcp-http.ps1"
    runner_path.write_text(f"{codex_http_mcp._RUNNER_MARKER}\n", encoding="utf-8")
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if "/Query" in command:
            return subprocess.CompletedProcess(
                args=command, returncode=0, stdout=_managed_task_xml(runner_path)
            )
        return subprocess.CompletedProcess(args=command, returncode=0)

    with (
        patch("ouroboros.cli.codex_http_mcp.shutil.which", return_value="schtasks.exe"),
        patch(
            "ouroboros.cli.codex_http_mcp._current_windows_identity", return_value=r"DOMAIN\codex"
        ),
        patch("ouroboros.cli.codex_http_mcp._scheduled_task_is_running", return_value=True),
        patch("ouroboros.cli.codex_http_mcp.subprocess.run", side_effect=run),
        patch(
            "ouroboros.cli.codex_http_mcp._wait_for_http_readiness",
            side_effect=(False, False, True),
        ) as readiness,
        patch("ouroboros.cli.codex_http_mcp._owns_generation", return_value=True),
    ):
        assert codex_http_mcp.provision_windows_codex_mcp_http(tmp_path, LAUNCHER) is not None

    restore_create = max(index for index, call in enumerate(calls) if "/Create" in call)
    assert next(call for call in calls[restore_create + 1 :] if "/Run" in call) == [
        "schtasks.exe",
        "/Run",
        "/TN",
        "Ouroboros MCP HTTP",
    ]
    assert readiness.call_count == 3


def test_provision_surfaces_previous_task_state_query_failure(tmp_path: Path) -> None:
    """An unreadable managed task state aborts before any replacement mutation."""
    runner_path = tmp_path / "ouroboros-mcp-http.ps1"
    runner_path.write_text(f"{codex_http_mcp._RUNNER_MARKER}\n", encoding="utf-8")

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "/Query" in command:
            return subprocess.CompletedProcess(
                args=command, returncode=0, stdout=_managed_task_xml(runner_path)
            )
        return subprocess.CompletedProcess(args=command, returncode=1)

    with (
        patch("ouroboros.cli.codex_http_mcp.shutil.which", return_value="schtasks.exe"),
        patch(
            "ouroboros.cli.codex_http_mcp._current_windows_identity", return_value=r"DOMAIN\codex"
        ),
        patch("ouroboros.cli.codex_http_mcp.subprocess.run", side_effect=run) as mocked_run,
    ):
        assert (
            codex_http_mcp.provision_windows_codex_mcp_http(tmp_path, LAUNCHER)
            == "Could not query the previous scheduled task state."
        )

    assert not any("/Create" in call.args[0] for call in mocked_run.call_args_list)


def test_provision_surfaces_failure_to_end_running_task(tmp_path: Path) -> None:
    """A running generation is never replaced when Task Scheduler cannot end it."""
    runner_path = tmp_path / "ouroboros-mcp-http.ps1"
    prior_runner = f"{codex_http_mcp._RUNNER_MARKER}\n"
    runner_path.write_text(prior_runner, encoding="utf-8")

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "/Query" in command:
            return subprocess.CompletedProcess(
                args=command, returncode=0, stdout=_managed_task_xml(runner_path)
            )
        if "/End" in command:
            return subprocess.CompletedProcess(args=command, returncode=1)
        return subprocess.CompletedProcess(args=command, returncode=0)

    with (
        patch("ouroboros.cli.codex_http_mcp.shutil.which", return_value="schtasks.exe"),
        patch(
            "ouroboros.cli.codex_http_mcp._current_windows_identity", return_value=r"DOMAIN\codex"
        ),
        patch("ouroboros.cli.codex_http_mcp._scheduled_task_is_running", return_value=True),
        patch("ouroboros.cli.codex_http_mcp.subprocess.run", side_effect=run) as mocked_run,
    ):
        assert (
            codex_http_mcp.provision_windows_codex_mcp_http(tmp_path, LAUNCHER)
            == "Could not end the previous scheduled task."
        )

    assert runner_path.read_text(encoding="utf-8") == prior_runner
    assert not any("/Create" in call.args[0] for call in mocked_run.call_args_list)


def test_generation_claim_requires_matching_task_and_runner(tmp_path: Path) -> None:
    """A matching action alone cannot claim a different task generation."""
    runner = tmp_path / codex_http_mcp._RUNNER_NAME
    generation = "new-generation"
    contents = (
        f"{codex_http_mcp._RUNNER_MARKER}\n{codex_http_mcp._RUNNER_GENERATION_PREFIX}{generation}\n"
    ).encode()
    runner.write_bytes(contents)
    task_xml = _managed_task_xml(runner).replace(
        "<Actions",
        f"<RegistrationInfo><Description>{codex_http_mcp._TASK_GENERATION_PREFIX}"
        "other-generation</Description></RegistrationInfo><Actions",
    )
    with patch("ouroboros.cli.codex_http_mcp._task_xml", return_value=task_xml):
        assert not codex_http_mcp._owns_generation("schtasks.exe", runner, generation, contents)


def test_rollback_does_not_mutate_concurrently_replaced_generation(tmp_path: Path) -> None:
    """Rollback fails closed before ending or deleting an operator replacement."""
    runner = tmp_path / codex_http_mcp._RUNNER_NAME
    generated = (
        f"{codex_http_mcp._RUNNER_MARKER}\n{codex_http_mcp._RUNNER_GENERATION_PREFIX}ours\n"
    ).encode()
    with (
        patch("ouroboros.cli.codex_http_mcp._owns_generation", return_value=False),
        patch("ouroboros.cli.codex_http_mcp.subprocess.run") as run,
    ):
        assert (
            codex_http_mcp._rollback_task(
                "schtasks.exe",
                tmp_path / "rollback.xml",
                None,
                False,
                "ours",
                runner,
                generated,
                None,
            )
            == "Replacement task or runner changed ownership before rollback."
        )
    run.assert_not_called()


def test_provision_refuses_public_flow_generation_mismatch(tmp_path: Path) -> None:
    """Provisioning refuses a task and runner that claim different generations."""
    runner_path = tmp_path / codex_http_mcp._RUNNER_NAME
    runner_path.write_text(
        f"{codex_http_mcp._RUNNER_MARKER}\n{codex_http_mcp._RUNNER_GENERATION_PREFIX}runner\n",
        encoding="utf-8",
    )
    task_xml = _managed_task_xml(runner_path).replace(
        "<Actions",
        f"<RegistrationInfo><Description>{codex_http_mcp._TASK_GENERATION_PREFIX}"
        "task</Description></RegistrationInfo><Actions",
    )
    with (
        patch("ouroboros.cli.codex_http_mcp.shutil.which", return_value="schtasks.exe"),
        patch(
            "ouroboros.cli.codex_http_mcp._current_windows_identity", return_value=r"DOMAIN\codex"
        ),
        patch("ouroboros.cli.codex_http_mcp._task_xml", return_value=task_xml),
        patch("ouroboros.cli.codex_http_mcp.subprocess.run") as run,
    ):
        assert codex_http_mcp.provision_windows_codex_mcp_http(tmp_path, LAUNCHER) == (
            "Refusing to modify task and runner with mismatched Ouroboros MCP ownership claims."
        )
    run.assert_not_called()


def test_remove_refuses_mismatched_task_and_runner_generations(tmp_path: Path) -> None:
    """Removal must not mutate separately claimed task and runner generations."""
    runner_path = tmp_path / codex_http_mcp._RUNNER_NAME
    runner_path.write_text(
        f"{codex_http_mcp._RUNNER_MARKER}\n{codex_http_mcp._RUNNER_GENERATION_PREFIX}runner\n",
        encoding="utf-8",
    )
    task_xml = _managed_task_xml(runner_path).replace(
        "<Actions",
        f"<RegistrationInfo><Description>{codex_http_mcp._TASK_GENERATION_PREFIX}"
        "task</Description></RegistrationInfo><Actions",
    )
    with (
        patch("ouroboros.cli.codex_http_mcp.shutil.which", return_value="schtasks.exe"),
        patch("ouroboros.cli.codex_http_mcp._task_xml", return_value=task_xml),
        patch("ouroboros.cli.codex_http_mcp.subprocess.run") as run,
    ):
        assert codex_http_mcp.remove_windows_codex_mcp_http(tmp_path) == (
            "Refusing to remove task and runner with mismatched Ouroboros MCP ownership claims."
        )
    run.assert_not_called()


def test_coherent_pair_rejects_malformed_generation_claim(tmp_path: Path) -> None:
    """An empty claim is not interchangeable with an absent legacy claim."""
    runner = tmp_path / codex_http_mcp._RUNNER_NAME
    runner.write_text(
        f"{codex_http_mcp._RUNNER_MARKER}\n{codex_http_mcp._RUNNER_GENERATION_PREFIX}\n",
        encoding="utf-8",
    )
    assert not codex_http_mcp._is_coherent_managed_pair(
        _managed_task_xml(runner), runner.read_bytes(), runner
    )


def test_restore_prior_task_accepts_unchanged_prior_runner(tmp_path: Path) -> None:
    """Post-End listener rejection can restart an untouched prior runner."""
    runner = tmp_path / codex_http_mcp._RUNNER_NAME
    prior = f"{codex_http_mcp._RUNNER_MARKER}\n".encode()
    runner.write_bytes(prior)
    task = _managed_task_xml(runner)
    with (
        patch("ouroboros.cli.codex_http_mcp._task_xml", return_value=task),
        patch("ouroboros.cli.codex_http_mcp._scheduled_task_is_running", return_value=True),
        patch("ouroboros.cli.codex_http_mcp._wait_for_http_readiness", return_value=True),
        patch("ouroboros.cli.codex_http_mcp._unchanged", return_value=True),
        patch(
            "ouroboros.cli.codex_http_mcp.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0),
        ),
    ):
        assert (
            codex_http_mcp._restore_prior_task(
                "schtasks.exe", task, True, runner, prior, b"generated"
            )
            is None
        )


def test_remove_unlink_failure_does_not_overwrite_concurrent_task(tmp_path: Path) -> None:
    """Exception rollback leaves an operator task registered after deletion untouched."""
    runner = tmp_path / codex_http_mcp._RUNNER_NAME
    runner.write_text(f"{codex_http_mcp._RUNNER_MARKER}\n", encoding="utf-8")
    prior_task = _managed_task_xml(runner)
    operator_task = "<Task><Actions /></Task>"
    task_state = [prior_task, prior_task, prior_task, None, operator_task]
    original_unlink = Path.unlink

    def task_xml(_schtasks: str) -> str | None:
        return task_state.pop(0) if task_state else operator_task

    def unlink(path: Path, **kwargs: object) -> None:
        if path == runner:
            raise OSError("runner locked")
        original_unlink(path, **kwargs)

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=command, returncode=0)

    with (
        patch("ouroboros.cli.codex_http_mcp.shutil.which", return_value="schtasks.exe"),
        patch("ouroboros.cli.codex_http_mcp._task_xml", side_effect=task_xml),
        patch("ouroboros.cli.codex_http_mcp._scheduled_task_is_running", return_value=False),
        patch("ouroboros.cli.codex_http_mcp.subprocess.run", side_effect=run) as mocked_run,
        patch.object(Path, "unlink", new=unlink),
    ):
        error = codex_http_mcp.remove_windows_codex_mcp_http(tmp_path)

    assert error is not None and "Rollback failed: task or runner ownership changed." in error
    assert not any("/Create" in call.args[0] for call in mocked_run.call_args_list)


def test_remove_unlink_failure_rejects_ready_unrelated_listener(tmp_path: Path) -> None:
    """Exception rollback requires the restored task to be Running, not merely ready."""
    runner = tmp_path / codex_http_mcp._RUNNER_NAME
    runner.write_text(f"{codex_http_mcp._RUNNER_MARKER}\n", encoding="utf-8")
    prior_task = _managed_task_xml(runner)
    original_unlink = Path.unlink

    def unlink(path: Path, **kwargs: object) -> None:
        if path == runner:
            raise OSError("runner locked")
        original_unlink(path, **kwargs)

    with (
        patch("ouroboros.cli.codex_http_mcp.shutil.which", return_value="schtasks.exe"),
        patch(
            "ouroboros.cli.codex_http_mcp._task_xml",
            side_effect=(prior_task, prior_task, None, None, prior_task),
        ),
        patch("ouroboros.cli.codex_http_mcp._scheduled_task_is_running", side_effect=(True, False)),
        patch("ouroboros.cli.codex_http_mcp._wait_for_http_readiness", return_value=True),
        patch("ouroboros.cli.codex_http_mcp._unchanged", return_value=True),
        patch(
            "ouroboros.cli.codex_http_mcp.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0),
        ),
        patch.object(Path, "unlink", new=unlink),
    ):
        error = codex_http_mcp.remove_windows_codex_mcp_http(tmp_path)

    assert error is not None and "Rollback failed to restore running task." in error


def test_remove_unlink_failure_preserves_concurrent_runner(tmp_path: Path) -> None:
    """Exception rollback fails closed when exclusive runner restoration loses a race."""
    runner = tmp_path / codex_http_mcp._RUNNER_NAME
    runner.write_text(f"{codex_http_mcp._RUNNER_MARKER}\n", encoding="utf-8")
    prior_task = _managed_task_xml(runner)
    original_unlink = Path.unlink

    def unlink(path: Path, **kwargs: object) -> None:
        if path == runner:
            original_unlink(path, **kwargs)
            raise OSError("runner locked")
        original_unlink(path, **kwargs)

    def lose_runner_race(path: Path, _contents: bytes | None) -> bool:
        path.write_text("operator runner", encoding="utf-8")
        return False

    with (
        patch("ouroboros.cli.codex_http_mcp.shutil.which", return_value="schtasks.exe"),
        patch(
            "ouroboros.cli.codex_http_mcp._task_xml",
            side_effect=(prior_task, prior_task, prior_task, None, None),
        ),
        patch("ouroboros.cli.codex_http_mcp._scheduled_task_is_running", return_value=False),
        patch("ouroboros.cli.codex_http_mcp._restore_absent_file", side_effect=lose_runner_race),
        patch(
            "ouroboros.cli.codex_http_mcp.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0),
        ) as mocked_run,
        patch.object(Path, "unlink", new=unlink),
    ):
        error = codex_http_mcp.remove_windows_codex_mcp_http(tmp_path)

    assert error is not None and "Rollback failed: runner ownership changed." in error
    assert runner.read_text(encoding="utf-8") == "operator runner"
    assert not any("/Create" in call.args[0] for call in mocked_run.call_args_list)
