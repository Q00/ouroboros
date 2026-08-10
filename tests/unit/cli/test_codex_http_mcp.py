"""Contracts for immutable Codex Desktop MCP lifecycle generations."""

from __future__ import annotations

import json
import os
from pathlib import Path, PureWindowsPath
import subprocess
from unittest.mock import patch

import pytest

from ouroboros.cli import codex_http_mcp as mcp

IDENTITY = "S-1-5-21-100-200-300-400"
LAUNCHER = ("ouroboros.exe", ["mcp", "serve"])
SCHEDULER = r"C:\Windows\System32\schtasks.exe"
POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"


class _WindowsPath(PureWindowsPath):
    """Pure Windows paths with a mockable filesystem predicate."""

    def is_file(self) -> bool:
        return False


class _NoEnvironment:
    def __getitem__(self, name: str) -> str:
        raise AssertionError(f"unexpected environment lookup: {name}")

    def get(self, name: str, _default: object = None) -> object:
        raise AssertionError(f"unexpected environment lookup: {name}")


class _Response:
    def __init__(self, status: int, content_type: str, body: str) -> None:
        self.status = status
        self.headers = {"Content-Type": content_type}
        self._body = body.encode()

    def read(self) -> bytes:
        return self._body


def _initialize_result(request_id: str, name: str = "ouroboros-mcp") -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2025-03-26",
                "serverInfo": {"name": name},
            },
        }
    )


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        ("application/json; charset=utf-8", _initialize_result("request")),
        (
            "text/event-stream",
            f"event: message\ndata: {_initialize_result('request')}\n\n",
        ),
    ],
)
def test_initialize_response_accepts_json_and_sse(content_type: str, body: str) -> None:
    assert mcp._mcp_initialize_response(_Response(200, content_type, body), "request")


@pytest.mark.parametrize(
    "response",
    [
        _Response(406, "application/json", _initialize_result("request")),
        _Response("200", "application/json", _initialize_result("request")),
        _Response(200, "text/plain", _initialize_result("request")),
        _Response(200, "application/json", _initialize_result("other")),
        _Response(200, "application/json", _initialize_result("request", "other-server")),
    ],
)
def test_initialize_response_rejects_invalid_responses(response: _Response) -> None:
    assert not mcp._mcp_initialize_response(response, "request")


def _root(tmp_path: Path) -> Path:
    root = tmp_path / mcp._ROOT_NAME
    root.mkdir()
    mcp._bootstrap(root)
    return root


def _generation(root: Path, mode: str = "serve") -> tuple[str, str | None]:
    return mcp._publish_generation(root, mode, LAUNCHER if mode == "serve" else None)


def _task_xml(root: Path, generation: str, token: str) -> tuple[str, str]:
    action = mcp._task_arguments(LAUNCHER[1], root, generation, token)
    arguments = " ".join(mcp._windows_command_line_argument(value) for value in action)
    return mcp._create_task_xml(IDENTITY, LAUNCHER[0], arguments), arguments


def test_rendered_section_is_http_configuration() -> None:
    assert 'url = "http://127.0.0.1:8765/mcp"' in mcp.render_codex_mcp_http_section()


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({}, False),
        ({"plugins": {}}, False),
        ({"plugins": {"ouroboros@ouroboros": {"enabled": False}}}, False),
        ({"plugins": {"ouroboros@ouroboros": {}}}, True),
        ({"plugins": {"ouroboros@ouroboros": "invalid"}}, True),
    ],
)
def test_plugin_scoped_configuration_detection(data: dict[str, object], expected: bool) -> None:
    assert mcp.has_active_plugin_scoped_codex_mcp(data) is expected


def test_generations_are_unique_prepared_and_immutable(tmp_path: Path) -> None:
    root = _root(tmp_path)
    first, first_token = _generation(root)
    second, second_token = _generation(root)
    assert first < second
    assert first_token != second_token
    directory = root / mcp._GENERATIONS_NAME / first
    assert (directory / "manifest.json").is_file()
    assert not (directory / "commit.json").exists()
    assert not (directory / "prepare.json").exists()


def test_prepared_matching_token_is_eligible(tmp_path: Path) -> None:
    root = _root(tmp_path)
    generation, token = _generation(root)
    assert token is not None
    assert mcp.validate_managed_lifecycle(str(root), generation, mcp._INSTALLATION_ID, token)
    assert not mcp.validate_managed_lifecycle(str(root), generation, mcp._INSTALLATION_ID, "wrong")


def test_expired_and_aborted_prepares_are_ineligible(tmp_path: Path) -> None:
    root = _root(tmp_path)
    generation, token = _generation(root)
    assert token is not None
    manifest_path = root / mcp._GENERATIONS_NAME / generation / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["expires_ns"] = 0
    manifest_path.write_text(json.dumps(manifest))
    assert not mcp.validate_managed_lifecycle(str(root), generation, mcp._INSTALLATION_ID, token)
    generation, token = _generation(root)
    mcp._abort_generation(root, generation)
    assert not mcp.validate_managed_lifecycle(
        str(root), generation, mcp._INSTALLATION_ID, token or ""
    )


def test_latest_committed_generation_is_eligible_and_stale_task_is_not(tmp_path: Path) -> None:
    root = _root(tmp_path)
    old, old_token = _generation(root)
    mcp._commit_generation(root, old)
    new, new_token = _generation(root)
    assert old_token is not None
    assert new_token is not None
    assert not mcp.validate_managed_lifecycle(str(root), old, mcp._INSTALLATION_ID, old_token)
    assert mcp.validate_managed_lifecycle(str(root), new, mcp._INSTALLATION_ID, new_token)
    mcp._commit_generation(root, new)
    assert not mcp.validate_managed_lifecycle(str(root), old, mcp._INSTALLATION_ID, old_token)
    assert mcp.validate_managed_lifecycle(str(root), new, mcp._INSTALLATION_ID, new_token)


def test_disabled_generation_supersedes_server(tmp_path: Path) -> None:
    root = _root(tmp_path)
    serve, token = _generation(root)
    mcp._commit_generation(root, serve)
    disabled, _ = _generation(root, "disabled")
    assert not mcp.lifecycle_should_stop(str(root), serve, mcp._INSTALLATION_ID, token or "")
    mcp._commit_generation(root, disabled)
    assert mcp.lifecycle_should_stop(str(root), serve, mcp._INSTALLATION_ID, token or "")


def test_receipt_filename_is_short_and_receipt_fields_are_exact(tmp_path: Path) -> None:
    root = _root(tmp_path)
    generation, token = _generation(root)
    assert token is not None
    assert mcp.publish_server_receipt(str(root), generation, "ready", os.getpid(), 1, token)
    receipts = list((root / mcp._GENERATIONS_NAME / generation).glob("ready-*.json"))
    assert len(receipts) == 1
    assert generation not in receipts[0].name
    with (
        patch("ouroboros.cli.codex_http_mcp._process_identity_alive", return_value=True),
        patch("ouroboros.cli.codex_http_mcp.tcp_listener_owned_by", return_value=True),
    ):
        assert mcp._receipt_matches(root, generation, "ready", token)
    assert not mcp._receipt_matches(root, generation, "ready", "wrong")
    with patch("ouroboros.cli.codex_http_mcp._process_identity_alive", return_value=True):
        assert not mcp._receipt_matches(root, generation, "stopped", token)


def test_receipts_reject_unrelated_generation_and_missing_process_identity(tmp_path: Path) -> None:
    root = _root(tmp_path)
    generation, token = _generation(root)
    directory = root / mcp._GENERATIONS_NAME / generation
    (directory / "ready-unrelated.json").write_text(
        json.dumps(
            {
                "installation_id": mcp._INSTALLATION_ID,
                "generation": "other",
                "phase": "ready",
                "pid": 1,
                "start_marker": 1,
                "token": token,
            }
        )
    )
    assert not mcp._receipt_matches(root, generation, "ready", token)


def test_ready_receipt_rejects_pid_reuse_or_non_owner(tmp_path: Path) -> None:
    root = _root(tmp_path)
    generation, token = _generation(root)
    assert token is not None
    assert mcp.publish_server_receipt(str(root), generation, "ready", 42, 99, token)
    with patch("ouroboros.cli.codex_http_mcp._process_identity_alive", return_value=False):
        assert not mcp._receipt_matches(root, generation, "ready", token)
    with (
        patch("ouroboros.cli.codex_http_mcp._process_identity_alive", return_value=True),
        patch("ouroboros.cli.codex_http_mcp.tcp_listener_owned_by", return_value=False),
    ):
        assert not mcp._receipt_matches(root, generation, "ready", token)


def test_ready_receipt_rejects_process_that_dies_during_listener_inspection(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    generation, token = _generation(root)
    assert token is not None
    assert mcp.publish_server_receipt(str(root), generation, "ready", 42, 99, token)
    with (
        patch(
            "ouroboros.cli.codex_http_mcp._process_identity_alive",
            side_effect=[True, False],
        ),
        patch("ouroboros.cli.codex_http_mcp.tcp_listener_owned_by", return_value=True),
    ):
        assert not mcp._receipt_matches(root, generation, "ready", token)


def test_stopped_receipt_requires_the_exact_process_to_be_dead(tmp_path: Path) -> None:
    root = _root(tmp_path)
    generation, token = _generation(root)
    assert token is not None
    assert mcp.publish_server_receipt(str(root), generation, "stopped", 42, 99, token)
    with patch("ouroboros.cli.codex_http_mcp._process_identity_alive", return_value=True):
        assert not mcp._receipt_matches(root, generation, "stopped", token)
    with patch("ouroboros.cli.codex_http_mcp._process_identity_alive", return_value=False):
        assert mcp._receipt_matches(root, generation, "stopped", token)


def test_raw_standby_identity_survives_replacement_death(tmp_path: Path) -> None:
    root = _root(tmp_path)
    generation, token = _generation(root)
    assert token is not None
    assert mcp.publish_server_receipt(str(root), generation, "standby", 42, 99, token)
    with patch("ouroboros.cli.codex_http_mcp._process_identity_alive", return_value=False):
        assert mcp._raw_standby_identity(root, generation, token) == (42, 99)


def test_raw_standby_identity_refuses_missing_or_wrong_token(tmp_path: Path) -> None:
    root = _root(tmp_path)
    generation, token = _generation(root)
    assert token is not None
    assert mcp._raw_standby_identity(root, generation, token) is None
    assert mcp.publish_server_receipt(str(root), generation, "standby", 42, 99, token)
    assert mcp._raw_standby_identity(root, generation, "wrong") is None


def test_replacement_death_wait_accepts_dead_or_exact_stopped(tmp_path: Path) -> None:
    root = _root(tmp_path)
    generation, token = _generation(root)
    assert token is not None
    identity = (42, 99)
    with patch("ouroboros.cli.codex_http_mcp._process_identity_alive", return_value=False):
        assert mcp._wait_for_identity_death(root, generation, identity)
    with (
        patch("ouroboros.cli.codex_http_mcp._process_identity_alive", return_value=True),
        patch("ouroboros.cli.codex_http_mcp._stopped_identity_matches", return_value=True),
    ):
        assert mcp._wait_for_identity_death(root, generation, identity)


def test_handoff_uses_the_exact_live_ready_identity(tmp_path: Path) -> None:
    root = _root(tmp_path)
    generation, token = _generation(root)
    assert token is not None
    assert mcp.publish_server_receipt(str(root), generation, "ready", 42, 99, token)
    with (
        patch("ouroboros.cli.codex_http_mcp._process_identity_alive", return_value=True),
        patch("ouroboros.cli.codex_http_mcp.tcp_listener_owned_by", return_value=True),
    ):
        assert mcp._latest_ready_identity(root, generation) == (42, 99)
    with patch("ouroboros.cli.codex_http_mcp._process_identity_alive", return_value=False):
        assert mcp._latest_ready_identity(root, generation) is None


def test_latest_ready_identity_rejects_process_that_dies_during_listener_inspection(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    generation, token = _generation(root)
    assert token is not None
    assert mcp.publish_server_receipt(str(root), generation, "ready", 42, 99, token)
    with (
        patch(
            "ouroboros.cli.codex_http_mcp._process_identity_alive",
            side_effect=[True, False],
        ),
        patch("ouroboros.cli.codex_http_mcp.tcp_listener_owned_by", return_value=True),
    ):
        assert mcp._latest_ready_identity(root, generation) is None


def test_recovery_restarts_live_listenerless_replacement_after_expiry(tmp_path: Path) -> None:
    root = _root(tmp_path)
    prior, prior_token = _generation(root)
    assert prior_token is not None
    mcp._commit_generation(root, prior)
    generation, generation_token = _generation(root)
    assert generation_token is not None
    assert mcp.publish_server_receipt(str(root), generation, "standby", 42, 99, generation_token)
    manifest_path = root / mcp._GENERATIONS_NAME / generation / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["expires_ns"] = 0
    manifest_path.write_text(json.dumps(manifest))
    with (
        patch(
            "ouroboros.cli.codex_http_mcp._windows_system_executables",
            return_value=(SCHEDULER, POWERSHELL),
        ),
        patch("ouroboros.cli.codex_http_mcp._current_windows_identity", return_value=IDENTITY),
        patch("ouroboros.cli.codex_http_mcp._ensure_task", return_value=mcp._task_name(prior)),
        patch(
            "ouroboros.cli.codex_http_mcp.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0),
        ) as run,
        patch("ouroboros.cli.codex_http_mcp._process_identity_alive", return_value=True),
        patch("ouroboros.cli.codex_http_mcp.tcp_listener_owned_by", return_value=False),
        patch("ouroboros.cli.codex_http_mcp._wait_for_receipt", return_value=True) as wait_ready,
    ):
        assert mcp.recover_managed_lifecycle(str(root), generation)
    assert any("/Run" in call.args[0] for call in run.call_args_list)
    wait_ready.assert_called_once_with(root, prior, "ready", prior_token)


def test_recovery_rejects_live_replacement_that_owns_listener(tmp_path: Path) -> None:
    root = _root(tmp_path)
    prior, _ = _generation(root)
    mcp._commit_generation(root, prior)
    generation, generation_token = _generation(root)
    assert generation_token is not None
    assert mcp.publish_server_receipt(str(root), generation, "standby", 42, 99, generation_token)
    mcp._abort_generation(root, generation)

    with (
        patch(
            "ouroboros.cli.codex_http_mcp._windows_system_executables",
            return_value=(SCHEDULER, POWERSHELL),
        ),
        patch("ouroboros.cli.codex_http_mcp._current_windows_identity", return_value=IDENTITY),
        patch("ouroboros.cli.codex_http_mcp._process_identity_alive", return_value=True),
        patch("ouroboros.cli.codex_http_mcp.tcp_listener_owned_by", return_value=True),
        patch(
            "ouroboros.cli.codex_http_mcp._restore_committed_serve_predecessor",
            return_value=True,
        ) as restore,
    ):
        assert not mcp.recover_managed_lifecycle(str(root), generation)

    restore.assert_not_called()


def test_recovery_requires_token_bound_ready_proof(tmp_path: Path) -> None:
    root = _root(tmp_path)
    prior, prior_token = _generation(root)
    assert prior_token is not None
    mcp._commit_generation(root, prior)
    generation, generation_token = _generation(root)
    assert generation_token is not None
    assert mcp.publish_server_receipt(str(root), generation, "standby", 42, 99, generation_token)
    manifest_path = root / mcp._GENERATIONS_NAME / generation / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["expires_ns"] = 0
    manifest_path.write_text(json.dumps(manifest))

    with (
        patch(
            "ouroboros.cli.codex_http_mcp._windows_system_executables",
            return_value=(SCHEDULER, POWERSHELL),
        ),
        patch("ouroboros.cli.codex_http_mcp._current_windows_identity", return_value=IDENTITY),
        patch("ouroboros.cli.codex_http_mcp._ensure_task", return_value=mcp._task_name(prior)),
        patch(
            "ouroboros.cli.codex_http_mcp.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0),
        ),
        patch("ouroboros.cli.codex_http_mcp._process_identity_alive", return_value=True),
        patch("ouroboros.cli.codex_http_mcp.tcp_listener_owned_by", return_value=False),
        patch("ouroboros.cli.codex_http_mcp._wait_for_receipt", return_value=False) as wait_ready,
    ):
        assert not mcp.recover_managed_lifecycle(str(root), generation)
    wait_ready.assert_called_once_with(root, prior, "ready", prior_token)


def test_provision_waits_for_standby_before_prior_stop(tmp_path: Path) -> None:
    root = _root(tmp_path)
    prior, prior_token = _generation(root)
    assert prior_token is not None
    mcp._commit_generation(root, prior)
    order: list[str] = []

    def wait_receipt(_root: Path, _generation: str, phase: str, _token: str | None = None) -> bool:
        order.append(phase)
        return True

    with (
        patch(
            "ouroboros.cli.codex_http_mcp._windows_system_executables",
            return_value=(SCHEDULER, POWERSHELL),
        ),
        patch("ouroboros.cli.codex_http_mcp._current_windows_identity", return_value=IDENTITY),
        patch("ouroboros.cli.codex_http_mcp._physical_config_dir", return_value=tmp_path),
        patch("ouroboros.cli.codex_http_mcp._bootstrap"),
        patch("ouroboros.cli.codex_http_mcp._windows_directory_lease"),
        patch("ouroboros.cli.codex_http_mcp._legacy_artifacts_present", return_value=False),
        patch("ouroboros.cli.codex_http_mcp._latest_ready_identity", return_value=(42, 99)),
        patch("ouroboros.cli.codex_http_mcp._ensure_task", return_value="new-task"),
        patch(
            "ouroboros.cli.codex_http_mcp.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0),
        ),
        patch("ouroboros.cli.codex_http_mcp._wait_for_receipt", side_effect=wait_receipt),
        patch(
            "ouroboros.cli.codex_http_mcp._wait_for_stopped_identity", return_value=True
        ) as stopped,
        patch("ouroboros.cli.codex_http_mcp._wait_for_http_readiness", return_value=True),
    ):
        assert mcp.provision_windows_codex_mcp_http(tmp_path, LAUNCHER) is None
    assert order[0] == "standby"
    assert stopped.called


def test_serve_commit_authorization_failure_aborts_new_generation(tmp_path: Path) -> None:
    root = _root(tmp_path)
    prior, prior_token = _generation(root)
    assert prior_token is not None
    mcp._commit_generation(root, prior)

    with (
        patch(
            "ouroboros.cli.codex_http_mcp._windows_system_executables",
            return_value=(SCHEDULER, POWERSHELL),
        ),
        patch("ouroboros.cli.codex_http_mcp._current_windows_identity", return_value=IDENTITY),
        patch("ouroboros.cli.codex_http_mcp._physical_config_dir", return_value=tmp_path),
        patch("ouroboros.cli.codex_http_mcp._bootstrap"),
        patch("ouroboros.cli.codex_http_mcp._windows_directory_lease"),
        patch("ouroboros.cli.codex_http_mcp._legacy_artifacts_present", return_value=False),
        patch("ouroboros.cli.codex_http_mcp._latest_ready_identity", return_value=None),
        patch("ouroboros.cli.codex_http_mcp._ensure_task", return_value="new-task"),
        patch(
            "ouroboros.cli.codex_http_mcp.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0),
        ),
        patch("ouroboros.cli.codex_http_mcp._wait_for_receipt", return_value=True) as wait_receipt,
        patch("ouroboros.cli.codex_http_mcp._wait_for_http_readiness", return_value=True),
        patch("ouroboros.cli.codex_http_mcp._raw_standby_identity", return_value=(42, 99)),
        patch("ouroboros.cli.codex_http_mcp._process_identity_alive", return_value=False),
        patch("ouroboros.cli.codex_http_mcp._restart_prior", return_value=True) as restart_prior,
    ):
        error = mcp._operate(tmp_path, LAUNCHER, "serve", commit_authorized=lambda: False)

    assert error == "Codex MCP config changed; lifecycle transition was not committed."
    assert mcp._desired_generation(root)[0] == prior
    aborted = [path for path in (root / mcp._GENERATIONS_NAME).iterdir() if path.name != prior]
    assert len(aborted) == 1
    assert (aborted[0] / "abort.json").is_file()
    restart_prior.assert_called_once_with(SCHEDULER, root, IDENTITY)
    wait_receipt.assert_any_call(root, prior, "ready", prior_token)


@pytest.mark.parametrize(
    ("restart", "prior_ready", "recovery_error"),
    [
        (False, True, "Could not restart committed predecessor"),
        (True, False, "token-bound readiness"),
    ],
)
def test_serve_rollback_reports_unverified_predecessor_recovery(
    tmp_path: Path, restart: bool, prior_ready: bool, recovery_error: str
) -> None:
    root = _root(tmp_path)
    prior, _ = _generation(root)
    mcp._commit_generation(root, prior)

    def wait_receipt(
        _root: Path, receipt_generation: str, _phase: str, _token: str | None = None
    ) -> bool:
        return receipt_generation != prior or prior_ready

    with (
        patch(
            "ouroboros.cli.codex_http_mcp._windows_system_executables",
            return_value=(SCHEDULER, POWERSHELL),
        ),
        patch("ouroboros.cli.codex_http_mcp._current_windows_identity", return_value=IDENTITY),
        patch("ouroboros.cli.codex_http_mcp._physical_config_dir", return_value=tmp_path),
        patch("ouroboros.cli.codex_http_mcp._bootstrap"),
        patch("ouroboros.cli.codex_http_mcp._windows_directory_lease"),
        patch("ouroboros.cli.codex_http_mcp._legacy_artifacts_present", return_value=False),
        patch("ouroboros.cli.codex_http_mcp._latest_ready_identity", return_value=None),
        patch("ouroboros.cli.codex_http_mcp._ensure_task", return_value="new-task"),
        patch(
            "ouroboros.cli.codex_http_mcp.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0),
        ),
        patch("ouroboros.cli.codex_http_mcp._wait_for_receipt", side_effect=wait_receipt),
        patch("ouroboros.cli.codex_http_mcp._wait_for_http_readiness", return_value=True),
        patch("ouroboros.cli.codex_http_mcp._raw_standby_identity", return_value=(42, 99)),
        patch("ouroboros.cli.codex_http_mcp._process_identity_alive", return_value=True),
        patch(
            "ouroboros.cli.codex_http_mcp._wait_for_identity_death", return_value=True
        ) as wait_for_death,
        patch("ouroboros.cli.codex_http_mcp._restart_prior", return_value=restart),
    ):
        error = mcp._operate(tmp_path, LAUNCHER, "serve", commit_authorized=lambda: False)

    assert error is not None
    assert "Codex MCP config changed; lifecycle transition was not committed." in error
    assert "Predecessor recovery failed:" in error
    assert recovery_error in error
    assert mcp._desired_generation(root)[0] == prior
    aborted = [path for path in (root / mcp._GENERATIONS_NAME).iterdir() if path.name != prior]
    assert len(aborted) == 1
    wait_for_death.assert_called_once_with(root, aborted[0].name, (42, 99))


def test_disabled_commit_authorization_failure_aborts_new_generation(tmp_path: Path) -> None:
    root = _root(tmp_path)
    prior, _ = _generation(root)
    mcp._commit_generation(root, prior)

    with (
        patch(
            "ouroboros.cli.codex_http_mcp._windows_system_executables",
            return_value=(SCHEDULER, POWERSHELL),
        ),
        patch("ouroboros.cli.codex_http_mcp._current_windows_identity", return_value=IDENTITY),
        patch("ouroboros.cli.codex_http_mcp._physical_config_dir", return_value=tmp_path),
        patch("ouroboros.cli.codex_http_mcp._bootstrap"),
        patch("ouroboros.cli.codex_http_mcp._windows_directory_lease"),
        patch("ouroboros.cli.codex_http_mcp._legacy_artifacts_present", return_value=False),
        patch("ouroboros.cli.codex_http_mcp._latest_ready_identity", return_value=(42, 99)),
        patch("ouroboros.cli.codex_http_mcp._restart_prior", return_value=True),
    ):
        error = mcp._operate(tmp_path, None, "disabled", commit_authorized=lambda: False)

    assert error == "Codex MCP config changed; lifecycle transition was not committed."
    assert mcp._desired_generation(root)[0] == prior
    aborted = [path for path in (root / mcp._GENERATIONS_NAME).iterdir() if path.name != prior]
    assert len(aborted) == 1
    assert (aborted[0] / "abort.json").is_file()


def test_serve_post_commit_authorization_failure_restores_predecessor(tmp_path: Path) -> None:
    root = _root(tmp_path)
    prior, prior_token = _generation(root)
    assert prior_token is not None
    mcp._commit_generation(root, prior)
    authorized = True
    commit = mcp._commit_generation

    def commit_then_revoke(commit_root: Path, generation: str) -> None:
        nonlocal authorized
        commit(commit_root, generation)
        authorized = False

    with (
        patch(
            "ouroboros.cli.codex_http_mcp._windows_system_executables",
            return_value=(SCHEDULER, POWERSHELL),
        ),
        patch("ouroboros.cli.codex_http_mcp._current_windows_identity", return_value=IDENTITY),
        patch("ouroboros.cli.codex_http_mcp._physical_config_dir", return_value=tmp_path),
        patch("ouroboros.cli.codex_http_mcp._bootstrap"),
        patch("ouroboros.cli.codex_http_mcp._windows_directory_lease"),
        patch("ouroboros.cli.codex_http_mcp._legacy_artifacts_present", return_value=False),
        patch("ouroboros.cli.codex_http_mcp._latest_ready_identity", return_value=(42, 99)),
        patch("ouroboros.cli.codex_http_mcp._ensure_task", return_value="new-task"),
        patch(
            "ouroboros.cli.codex_http_mcp.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0),
        ),
        patch("ouroboros.cli.codex_http_mcp._wait_for_receipt", return_value=True) as wait_receipt,
        patch("ouroboros.cli.codex_http_mcp._wait_for_stopped_identity", return_value=True),
        patch("ouroboros.cli.codex_http_mcp._wait_for_http_readiness", return_value=True),
        patch("ouroboros.cli.codex_http_mcp._raw_standby_identity", return_value=(43, 100)),
        patch(
            "ouroboros.cli.codex_http_mcp._wait_for_identity_death", return_value=True
        ) as wait_for_death,
        patch("ouroboros.cli.codex_http_mcp._restart_prior", return_value=True),
        patch(
            "ouroboros.cli.codex_http_mcp._commit_generation",
            side_effect=commit_then_revoke,
        ),
    ):
        error = mcp._operate(tmp_path, LAUNCHER, "serve", commit_authorized=lambda: authorized)

    assert error == "Codex MCP config changed; lifecycle transition was not committed."
    assert mcp._desired_generation(root)[0] == prior
    replacement = next(
        path for path in (root / mcp._GENERATIONS_NAME).iterdir() if path.name != prior
    )
    assert (replacement / "commit.json").is_file()
    assert (replacement / "abort.json").is_file()
    wait_for_death.assert_called_once_with(root, replacement.name, (43, 100))
    wait_receipt.assert_any_call(root, prior, "ready", prior_token)


def test_serve_post_commit_authorization_failure_without_predecessor_stops_replacement(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    authorized = True
    commit = mcp._commit_generation

    def commit_then_revoke(commit_root: Path, generation: str) -> None:
        nonlocal authorized
        commit(commit_root, generation)
        authorized = False

    with (
        patch(
            "ouroboros.cli.codex_http_mcp._windows_system_executables",
            return_value=(SCHEDULER, POWERSHELL),
        ),
        patch("ouroboros.cli.codex_http_mcp._current_windows_identity", return_value=IDENTITY),
        patch("ouroboros.cli.codex_http_mcp._physical_config_dir", return_value=tmp_path),
        patch("ouroboros.cli.codex_http_mcp._bootstrap"),
        patch("ouroboros.cli.codex_http_mcp._windows_directory_lease"),
        patch("ouroboros.cli.codex_http_mcp._legacy_artifacts_present", return_value=False),
        patch("ouroboros.cli.codex_http_mcp._ensure_task", return_value="new-task"),
        patch(
            "ouroboros.cli.codex_http_mcp.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0),
        ),
        patch("ouroboros.cli.codex_http_mcp._wait_for_receipt", return_value=True),
        patch("ouroboros.cli.codex_http_mcp._wait_for_http_readiness", return_value=True),
        patch("ouroboros.cli.codex_http_mcp._raw_standby_identity", return_value=(43, 100)),
        patch(
            "ouroboros.cli.codex_http_mcp._wait_for_identity_death", return_value=True
        ) as wait_for_death,
        patch(
            "ouroboros.cli.codex_http_mcp._restore_committed_serve_predecessor"
        ) as restore_predecessor,
        patch(
            "ouroboros.cli.codex_http_mcp._commit_generation",
            side_effect=commit_then_revoke,
        ),
    ):
        error = mcp._operate(tmp_path, LAUNCHER, "serve", commit_authorized=lambda: authorized)

    assert error == "Codex MCP config changed; lifecycle transition was not committed."
    assert mcp._desired_generation(root) is None
    replacement = next((root / mcp._GENERATIONS_NAME).iterdir())
    assert (replacement / "commit.json").is_file()
    assert (replacement / "abort.json").is_file()
    wait_for_death.assert_called_once_with(root, replacement.name, (43, 100))
    restore_predecessor.assert_not_called()


def test_disabled_post_commit_authorization_failure_restores_predecessor(tmp_path: Path) -> None:
    root = _root(tmp_path)
    prior, prior_token = _generation(root)
    assert prior_token is not None
    mcp._commit_generation(root, prior)
    authorized = True
    commit = mcp._commit_generation

    def commit_then_revoke(commit_root: Path, generation: str) -> None:
        nonlocal authorized
        commit(commit_root, generation)
        authorized = False

    with (
        patch(
            "ouroboros.cli.codex_http_mcp._windows_system_executables",
            return_value=(SCHEDULER, POWERSHELL),
        ),
        patch("ouroboros.cli.codex_http_mcp._current_windows_identity", return_value=IDENTITY),
        patch("ouroboros.cli.codex_http_mcp._physical_config_dir", return_value=tmp_path),
        patch("ouroboros.cli.codex_http_mcp._bootstrap"),
        patch("ouroboros.cli.codex_http_mcp._windows_directory_lease"),
        patch("ouroboros.cli.codex_http_mcp._legacy_artifacts_present", return_value=False),
        patch("ouroboros.cli.codex_http_mcp._latest_ready_identity", return_value=(42, 99)),
        patch(
            "ouroboros.cli.codex_http_mcp._wait_for_identity_death", return_value=True
        ) as wait_for_death,
        patch("ouroboros.cli.codex_http_mcp._restart_prior", return_value=True),
        patch("ouroboros.cli.codex_http_mcp._wait_for_receipt", return_value=True) as wait_ready,
        patch(
            "ouroboros.cli.codex_http_mcp._commit_generation",
            side_effect=commit_then_revoke,
        ),
    ):
        error = mcp._operate(tmp_path, None, "disabled", commit_authorized=lambda: authorized)

    assert error == (
        "Codex MCP config changed; lifecycle transition was not committed. "
        "Prior HTTP service was restored."
    )
    assert mcp._desired_generation(root)[0] == prior
    replacement = next(
        path for path in (root / mcp._GENERATIONS_NAME).iterdir() if path.name != prior
    )
    assert (replacement / "commit.json").is_file()
    assert (replacement / "abort.json").is_file()
    wait_for_death.assert_called_once_with(root, prior, (42, 99))
    wait_ready.assert_called_once_with(root, prior, "ready", prior_token)


@pytest.mark.parametrize(
    ("death_proven", "restart", "ready", "expected"),
    [
        (False, True, True, "Compensation failed: prior HTTP service did not stop."),
        (True, False, False, "Compensation failed: could not restart"),
        (
            True,
            True,
            False,
            "Compensation failed: prior HTTP service did not become ready.",
        ),
        (True, True, True, "Prior HTTP service was restored."),
    ],
)
def test_disable_compensation_requires_death_then_restarts_and_verifies_prior(
    tmp_path: Path,
    death_proven: bool,
    restart: bool,
    ready: bool,
    expected: str,
) -> None:
    root = _root(tmp_path)
    prior, prior_token = _generation(root)
    assert prior_token is not None
    mcp._commit_generation(root, prior)
    disabled, _ = _generation(root, "disabled")
    mcp._commit_generation(root, disabled)

    with (
        patch(
            "ouroboros.cli.codex_http_mcp._process_identity_alive",
            return_value=True,
        ) as process_alive,
        patch(
            "ouroboros.cli.codex_http_mcp.tcp_listener_owned_by",
            return_value=True,
        ) as listener_owned,
        patch(
            "ouroboros.cli.codex_http_mcp._wait_for_identity_death",
            return_value=death_proven,
        ) as wait_for_death,
        patch("ouroboros.cli.codex_http_mcp._restart_prior", return_value=restart) as restart_prior,
        patch("ouroboros.cli.codex_http_mcp._wait_for_receipt", return_value=ready) as wait_ready,
    ):
        error = mcp._compensate_disabled_transition(
            SCHEDULER, IDENTITY, root, disabled, prior, (42, 99)
        )

    assert expected in error
    assert (root / mcp._GENERATIONS_NAME / disabled / "abort.json").is_file()
    assert mcp._desired_generation(root)[0] == prior
    assert process_alive.call_count == 0
    listener_owned.assert_not_called()
    wait_for_death.assert_called_once_with(root, prior, (42, 99))
    if not death_proven:
        restart_prior.assert_not_called()
        wait_ready.assert_not_called()
    else:
        restart_prior.assert_called_once_with(SCHEDULER, root, IDENTITY)
        if restart:
            wait_ready.assert_called_once_with(root, prior, "ready", prior_token)
        else:
            wait_ready.assert_not_called()


def test_disable_does_not_succeed_when_prior_stop_is_unverified(tmp_path: Path) -> None:
    root = _root(tmp_path)
    prior, _ = _generation(root)
    mcp._commit_generation(root, prior)

    with (
        patch(
            "ouroboros.cli.codex_http_mcp._windows_system_executables",
            return_value=(SCHEDULER, POWERSHELL),
        ),
        patch("ouroboros.cli.codex_http_mcp._current_windows_identity", return_value=IDENTITY),
        patch("ouroboros.cli.codex_http_mcp._physical_config_dir", return_value=tmp_path),
        patch("ouroboros.cli.codex_http_mcp._windows_directory_lease"),
        patch("ouroboros.cli.codex_http_mcp._legacy_artifacts_present", return_value=False),
        patch("ouroboros.cli.codex_http_mcp._latest_ready_identity", return_value=(42, 99)),
        patch("ouroboros.cli.codex_http_mcp._wait_for_stopped_identity", return_value=False),
        patch(
            "ouroboros.cli.codex_http_mcp._compensate_disabled_transition",
            return_value="Existing MCP server did not stop. Prior HTTP service was restored.",
        ) as compensate,
    ):
        error = mcp.remove_windows_codex_mcp_http(tmp_path)

    assert error == "Existing MCP server did not stop. Prior HTTP service was restored."
    disabled = max((root / mcp._GENERATIONS_NAME).iterdir(), key=lambda path: path.name)
    compensate.assert_called_once_with(SCHEDULER, IDENTITY, root, disabled.name, prior, (42, 99))


def test_disable_fails_closed_without_committing_when_prior_ready_identity_is_missing(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    prior, _ = _generation(root)
    mcp._commit_generation(root, prior)

    with (
        patch(
            "ouroboros.cli.codex_http_mcp._windows_system_executables",
            return_value=(SCHEDULER, POWERSHELL),
        ),
        patch("ouroboros.cli.codex_http_mcp._current_windows_identity", return_value=IDENTITY),
        patch("ouroboros.cli.codex_http_mcp._physical_config_dir", return_value=tmp_path),
        patch("ouroboros.cli.codex_http_mcp._windows_directory_lease"),
        patch("ouroboros.cli.codex_http_mcp._legacy_artifacts_present", return_value=False),
        patch("ouroboros.cli.codex_http_mcp._latest_ready_identity", return_value=None),
        patch(
            "ouroboros.cli.codex_http_mcp._publish_generation",
            wraps=mcp._publish_generation,
        ) as publish,
    ):
        error = mcp.remove_windows_codex_mcp_http(tmp_path)

    assert error == (
        "Could not verify the existing MCP server identity; disabled transition was not started."
    )
    assert mcp._desired_generation(root)[0] == prior
    publish.assert_not_called()
    assert [path.name for path in (root / mcp._GENERATIONS_NAME).iterdir()] == [prior]


def test_task_xml_rejects_unmodeled_root_and_exec_content(tmp_path: Path) -> None:
    root = _root(tmp_path)
    generation, token = _generation(root)
    assert token is not None
    xml, arguments = _task_xml(root, generation, token)
    assert not mcp._is_owned_task(
        xml.replace("</Task>", "<Unknown/></Task>"),
        IDENTITY,
        LAUNCHER[0],
        arguments,
    )
    assert not mcp._is_owned_task(
        xml.replace("</Exec>", "<WorkingDirectory>x</WorkingDirectory></Exec>"),
        IDENTITY,
        LAUNCHER[0],
        arguments,
    )


def test_persisted_action_is_reused_for_creation_and_recovery(tmp_path: Path) -> None:
    root = _root(tmp_path)
    created: list[tuple[object, ...]] = []

    def ensure(*args: object) -> str:
        created.append(args)
        return "new-task"

    with (
        patch(
            "ouroboros.cli.codex_http_mcp._windows_system_executables",
            return_value=(SCHEDULER, POWERSHELL),
        ),
        patch("ouroboros.cli.codex_http_mcp._current_windows_identity", return_value=IDENTITY),
        patch("ouroboros.cli.codex_http_mcp._physical_config_dir", return_value=tmp_path),
        patch("ouroboros.cli.codex_http_mcp._bootstrap"),
        patch("ouroboros.cli.codex_http_mcp._windows_directory_lease"),
        patch("ouroboros.cli.codex_http_mcp._legacy_artifacts_present", return_value=False),
        patch("ouroboros.cli.codex_http_mcp._latest_ready_identity", return_value=None),
        patch("ouroboros.cli.codex_http_mcp._ensure_task", side_effect=ensure),
        patch(
            "ouroboros.cli.codex_http_mcp.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0),
        ),
        patch("ouroboros.cli.codex_http_mcp._wait_for_receipt", return_value=True),
        patch("ouroboros.cli.codex_http_mcp._wait_for_http_readiness", return_value=True),
    ):
        assert mcp.provision_windows_codex_mcp_http(tmp_path, LAUNCHER) is None

    generation, manifest, _ = mcp._desired_generation(root)
    action = manifest["arguments"]
    assert isinstance(action, list)
    assert all(isinstance(argument, str) for argument in action)
    assert created[0][4:6] == (manifest["command"], action)
    assert action[:12] == [
        *LAUNCHER[1],
        "--runtime",
        "codex",
        "--llm-backend",
        "codex",
        "--transport",
        "streamable-http",
        "--host",
        "127.0.0.1",
        "--port",
        "8765",
    ]
    assert "--codex-lifecycle-token" in action

    persisted_arguments = " ".join(mcp._windows_command_line_argument(value) for value in action)
    xml = mcp._create_task_xml(IDENTITY, manifest["command"], persisted_arguments)
    old_arguments = " ".join(mcp._windows_command_line_argument(value) for value in LAUNCHER[1])
    assert not mcp._is_owned_task(xml, IDENTITY, manifest["command"], old_arguments)
    assert mcp._is_owned_task(xml, IDENTITY, manifest["command"], persisted_arguments)

    with (
        patch(
            "ouroboros.cli.codex_http_mcp._ensure_task",
            return_value=mcp._task_name(generation),
        ) as restart,
        patch(
            "ouroboros.cli.codex_http_mcp.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0),
        ),
    ):
        assert mcp._restart_prior(SCHEDULER, root, IDENTITY)
    assert restart.call_args.args[4:6] == (manifest["command"], action)


def test_windows_system_executables_non_windows_skips_path_and_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    with (
        patch("ouroboros.cli.codex_http_mcp.sys.platform", "linux"),
        patch("ouroboros.cli.codex_http_mcp.ctypes.WinDLL", create=True) as windll,
        patch("ouroboros.cli.codex_http_mcp.os.getcwd", side_effect=AssertionError),
        patch("ouroboros.cli.codex_http_mcp.os.environ", _NoEnvironment()),
    ):
        assert mcp._windows_system_executables() is None
    windll.assert_not_called()


def test_windows_system_executables_uses_only_system32_utilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for impostor in ("schtasks.exe", "powershell.exe"):
        (tmp_path / impostor).write_text("impostor")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    system_directory = _WindowsPath(r"C:\Windows\System32")
    schtasks = system_directory / "schtasks.exe"
    powershell = system_directory / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    expected = {schtasks, powershell}

    def get_system_directory(buffer: object, _size: int) -> int:
        buffer.value = str(system_directory)
        return len(str(system_directory))

    with (
        patch("ouroboros.cli.codex_http_mcp.sys.platform", "win32"),
        patch("ouroboros.cli.codex_http_mcp.ctypes.WinDLL", create=True) as windll,
        patch("ouroboros.cli.codex_http_mcp.Path", _WindowsPath),
        patch.object(
            _WindowsPath,
            "is_file",
            autospec=True,
            side_effect=lambda path: path in expected,
        ) as is_file,
        patch("ouroboros.cli.codex_http_mcp.os.getcwd", side_effect=AssertionError),
        patch("ouroboros.cli.codex_http_mcp.os.environ", _NoEnvironment()),
    ):
        windll.return_value.GetSystemDirectoryW.side_effect = get_system_directory
        assert mcp._windows_system_executables() == (str(schtasks), str(powershell))

    windll.assert_called_once_with("kernel32", use_last_error=True)
    assert [call.args[0] for call in is_file.call_args_list] == [schtasks, powershell]


@pytest.mark.parametrize(
    ("directory", "result", "failure"),
    [
        (None, 0, None),
        (None, 32768, None),
        (None, None, OSError("GetSystemDirectoryW failed")),
        (r"relative\System32", len(r"relative\System32"), None),
    ],
)
def test_windows_system_executables_rejects_failed_or_invalid_system_directory(
    directory: str | None, result: int | None, failure: OSError | None
) -> None:
    def get_system_directory(buffer: object, _size: int) -> int:
        if failure is not None:
            raise failure
        if directory is not None:
            buffer.value = directory
        assert result is not None
        return result

    with (
        patch("ouroboros.cli.codex_http_mcp.sys.platform", "win32"),
        patch("ouroboros.cli.codex_http_mcp.ctypes.WinDLL", create=True) as windll,
        patch("ouroboros.cli.codex_http_mcp.Path", _WindowsPath),
    ):
        windll.return_value.GetSystemDirectoryW.side_effect = get_system_directory
        assert mcp._windows_system_executables() is None


@pytest.mark.parametrize(
    ("available", "checked"),
    [
        (
            {_WindowsPath(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")},
            [_WindowsPath(r"C:\Windows\System32\schtasks.exe")],
        ),
        (
            {_WindowsPath(r"C:\Windows\System32\schtasks.exe")},
            [
                _WindowsPath(r"C:\Windows\System32\schtasks.exe"),
                _WindowsPath(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"),
            ],
        ),
    ],
)
def test_windows_system_executables_rejects_missing_required_utility(
    available: set[_WindowsPath], checked: list[_WindowsPath]
) -> None:
    system_directory = _WindowsPath(r"C:\Windows\System32")

    def get_system_directory(buffer: object, _size: int) -> int:
        buffer.value = str(system_directory)
        return len(str(system_directory))

    with (
        patch("ouroboros.cli.codex_http_mcp.sys.platform", "win32"),
        patch("ouroboros.cli.codex_http_mcp.ctypes.WinDLL", create=True) as windll,
        patch("ouroboros.cli.codex_http_mcp.Path", _WindowsPath),
        patch.object(
            _WindowsPath, "is_file", autospec=True, side_effect=lambda path: path in available
        ) as is_file,
    ):
        windll.return_value.GetSystemDirectoryW.side_effect = get_system_directory
        assert mcp._windows_system_executables() is None

    assert [call.args[0] for call in is_file.call_args_list] == checked


def test_operate_fails_closed_without_utility_resolver(tmp_path: Path) -> None:
    with (
        patch("ouroboros.cli.codex_http_mcp._windows_system_executables", return_value=None),
        patch("ouroboros.cli.codex_http_mcp.subprocess.run") as run,
        patch("ouroboros.cli.codex_http_mcp._windows_operation_lock") as operation_lock,
        patch("ouroboros.cli.codex_http_mcp._bootstrap") as bootstrap,
        patch("ouroboros.cli.codex_http_mcp._publish_generation") as publish_generation,
    ):
        assert (
            mcp._operate(tmp_path, LAUNCHER, "serve")
            == "Could not find Windows Task Scheduler or current user."
        )

    run.assert_not_called()
    operation_lock.assert_not_called()
    bootstrap.assert_not_called()
    publish_generation.assert_not_called()


def test_recovery_fails_closed_without_utility_resolver() -> None:
    with (
        patch("ouroboros.cli.codex_http_mcp._windows_system_executables", return_value=None),
        patch("ouroboros.cli.codex_http_mcp.subprocess.run") as run,
        patch("ouroboros.cli.codex_http_mcp._abort_generation") as abort_generation,
        patch("ouroboros.cli.codex_http_mcp._restore_committed_serve_predecessor") as restore,
    ):
        assert not mcp.recover_managed_lifecycle("ignored", "ignored")

    run.assert_not_called()
    abort_generation.assert_not_called()
    restore.assert_not_called()


def test_lifecycle_uses_only_mocked_system_directory_utilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for impostor in ("schtasks.exe", "powershell.exe"):
        (tmp_path / impostor).write_text("impostor")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    calls: list[list[str]] = []

    def run(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        stdout = IDENTITY if arguments[0] == POWERSHELL else ""
        return subprocess.CompletedProcess(arguments, 0, stdout=stdout)

    root = _root(tmp_path)
    with (
        patch(
            "ouroboros.cli.codex_http_mcp._windows_system_executables",
            return_value=(SCHEDULER, POWERSHELL),
        ),
        patch("ouroboros.cli.codex_http_mcp._physical_config_dir", return_value=tmp_path),
        patch("ouroboros.cli.codex_http_mcp._bootstrap"),
        patch("ouroboros.cli.codex_http_mcp._windows_directory_lease"),
        patch("ouroboros.cli.codex_http_mcp._legacy_artifacts_present", return_value=False),
        patch("ouroboros.cli.codex_http_mcp._latest_ready_identity", return_value=None),
        patch("ouroboros.cli.codex_http_mcp._ensure_task", return_value="new-task"),
        patch("ouroboros.cli.codex_http_mcp.subprocess.run", side_effect=run),
        patch("ouroboros.cli.codex_http_mcp._wait_for_receipt", return_value=True),
        patch("ouroboros.cli.codex_http_mcp._wait_for_http_readiness", return_value=True),
    ):
        assert mcp.provision_windows_codex_mcp_http(tmp_path, LAUNCHER) is None

    assert root.is_dir()
    assert calls
    assert POWERSHELL in (call[0] for call in calls)
    assert SCHEDULER in (call[0] for call in calls)
    assert all(call[0] in {SCHEDULER, POWERSHELL} for call in calls)


def test_task_xml_is_unique_direct_command_and_exactly_owned(tmp_path: Path) -> None:
    root = _root(tmp_path)
    generation, token = _generation(root)
    assert token is not None
    xml, arguments = _task_xml(root, generation, token)
    assert mcp._task_name(generation).endswith(generation)
    assert "powershell.exe" not in xml
    assert "--codex-lifecycle-root" in arguments
    assert mcp._is_owned_task(xml, IDENTITY, LAUNCHER[0], arguments)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda xml: xml.replace("<Enabled>true</Enabled>", "<Enabled>false</Enabled>", 1),
        lambda xml: xml.replace("</Triggers>", "<TimeTrigger/></Triggers>"),
        lambda xml: xml.replace("</Actions>", "<Exec><Command>x</Command></Exec></Actions>"),
        lambda xml: xml.replace(IDENTITY, "S-1-5-21-1-2-3-4"),
        lambda xml: xml.replace(
            "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>",
            "<ExecutionTimeLimit>PT1M</ExecutionTimeLimit>",
        ),
    ],
)
def test_task_xml_rejects_settings_and_identity_impostors(tmp_path: Path, mutation: object) -> None:
    root = _root(tmp_path)
    generation, token = _generation(root)
    assert token is not None
    xml, arguments = _task_xml(root, generation, token)
    assert not mcp._is_owned_task(mutation(xml), IDENTITY, LAUNCHER[0], arguments)


def test_task_create_race_accepts_only_exact_owned_task(tmp_path: Path) -> None:
    root = _root(tmp_path)
    generation, token = _generation(root)
    assert token is not None
    xml, _ = _task_xml(root, generation, token)
    calls = 0

    def task_xml(_schtasks: str, _name: str) -> str | None:
        nonlocal calls
        calls += 1
        return None if calls == 1 else xml

    with (
        patch("ouroboros.cli.codex_http_mcp._task_xml", side_effect=task_xml),
        patch(
            "ouroboros.cli.codex_http_mcp.subprocess.run",
            return_value=subprocess.CompletedProcess([], 1),
        ) as run,
    ):
        mcp._ensure_task(
            SCHEDULER,
            IDENTITY,
            root,
            generation,
            LAUNCHER[0],
            mcp._task_arguments(LAUNCHER[1], root, generation, token),
        )
    assert all("/F" not in call.args[0] for call in run.call_args_list)


def test_native_scheduler_normalization_is_accepted(tmp_path: Path) -> None:
    root = _root(tmp_path)
    generation, token = _generation(root)
    assert token is not None
    xml, arguments = _task_xml(root, generation, token)
    normalized = (
        xml.replace("<RunLevel>LeastPrivilege</RunLevel>", "")
        .replace("<Enabled>true</Enabled></LogonTrigger>", "</LogonTrigger>")
        .replace("<AllowStartOnDemand>true</AllowStartOnDemand>", "")
        .replace("<Enabled>true</Enabled></Settings>", "</Settings>")
        .replace(
            "</Settings>",
            "<IdleSettings><StopOnIdleEnd>true</StopOnIdleEnd>"
            "<RestartOnIdle>false</RestartOnIdle></IdleSettings>"
            "<UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine></Settings>",
        )
        .replace(
            f"<UserId>{IDENTITY}</UserId></LogonTrigger>",
            "<UserId>DOMAIN\\user</UserId></LogonTrigger>",
        )
        .replace(
            "</Description></RegistrationInfo>",
            f"</Description><URI>\\{mcp._task_name(generation)}</URI></RegistrationInfo>",
        )
    )
    with patch("ouroboros.cli.codex_http_mcp._account_sid", return_value=IDENTITY):
        assert mcp._is_owned_task(
            normalized,
            IDENTITY,
            LAUNCHER[0],
            arguments,
            mcp._task_name(generation),
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda xml: xml.replace(
            "<RunLevel>LeastPrivilege</RunLevel>",
            "<RunLevel>HighestAvailable</RunLevel>",
        ),
        lambda xml: xml.replace("</Settings>", "<Unknown>true</Unknown></Settings>"),
        lambda xml: xml.replace(
            "<Enabled>true</Enabled></LogonTrigger>",
            "<Enabled>false</Enabled></LogonTrigger>",
        ),
        lambda xml: xml.replace(
            "</Settings>",
            "<IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd>"
            "<RestartOnIdle>false</RestartOnIdle></IdleSettings></Settings>",
        ),
    ],
)
def test_normalized_task_impostors_are_rejected(tmp_path: Path, mutation) -> None:
    root = _root(tmp_path)
    generation, token = _generation(root)
    assert token is not None
    xml, arguments = _task_xml(root, generation, token)
    assert not mcp._is_owned_task(mutation(xml), IDENTITY, LAUNCHER[0], arguments)


def test_legacy_artifacts_are_detected_without_mutation(tmp_path: Path) -> None:
    runner = tmp_path / mcp._LEGACY_RUNNER_NAME
    runner.write_text("operator")
    with patch(
        "ouroboros.cli.codex_http_mcp.subprocess.run",
        return_value=subprocess.CompletedProcess([], 1),
    ):
        assert mcp._legacy_artifacts_present("schtasks.exe", tmp_path)
    assert runner.read_text() == "operator"


def test_reparse_generation_is_rejected(tmp_path: Path) -> None:
    root = _root(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    link = (
        root / mcp._GENERATIONS_NAME / "gen-99999999999999999999-0123456789abcdef0123456789abcdef"
    )
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(str(exc))
    with pytest.raises(OSError, match="reparse"):
        mcp._valid_generations(root)


def test_no_fixed_supervisor_or_destructive_task_primitives_remain() -> None:
    source = Path(mcp.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "child-owner",
        "launcher-v1",
        "current.json",
        "runner.ps1",
        '"/Delete"',
        '"/End"',
        '"/Create", "/F"',
    ):
        assert forbidden not in source
