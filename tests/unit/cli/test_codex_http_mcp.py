"""Contracts for immutable Codex Desktop MCP lifecycle generations."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from ouroboros.cli import codex_http_mcp as mcp

IDENTITY = "S-1-5-21-100-200-300-400"
LAUNCHER = ("ouroboros.exe", ["mcp", "serve"])


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
    arguments = mcp._task_arguments(LAUNCHER[1], root, generation, token)
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
        patch("ouroboros.cli.codex_http_mcp.shutil.which", return_value="schtasks.exe"),
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
        patch("ouroboros.cli.codex_http_mcp.shutil.which", return_value="schtasks.exe"),
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
        patch("ouroboros.cli.codex_http_mcp.shutil.which", return_value="schtasks.exe"),
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
        patch("ouroboros.cli.codex_http_mcp.shutil.which", return_value="schtasks.exe"),
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
        patch("ouroboros.cli.codex_http_mcp.shutil.which", return_value="schtasks.exe"),
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
    restart_prior.assert_called_once_with("schtasks.exe", root, IDENTITY)
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
        patch("ouroboros.cli.codex_http_mcp.shutil.which", return_value="schtasks.exe"),
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
        patch("ouroboros.cli.codex_http_mcp.shutil.which", return_value="schtasks.exe"),
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
            "schtasks.exe", IDENTITY, root, disabled, prior, (42, 99)
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
        restart_prior.assert_called_once_with("schtasks.exe", root, IDENTITY)
        if restart:
            wait_ready.assert_called_once_with(root, prior, "ready", prior_token)
        else:
            wait_ready.assert_not_called()


def test_disable_does_not_succeed_when_prior_stop_is_unverified(tmp_path: Path) -> None:
    root = _root(tmp_path)
    prior, _ = _generation(root)
    mcp._commit_generation(root, prior)

    with (
        patch("ouroboros.cli.codex_http_mcp.shutil.which", return_value="schtasks.exe"),
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
    compensate.assert_called_once_with(
        "schtasks.exe", IDENTITY, root, disabled.name, prior, (42, 99)
    )


def test_disable_fails_closed_without_committing_when_prior_ready_identity_is_missing(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    prior, _ = _generation(root)
    mcp._commit_generation(root, prior)

    with (
        patch("ouroboros.cli.codex_http_mcp.shutil.which", return_value="schtasks.exe"),
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
            "schtasks.exe", IDENTITY, root, generation, LAUNCHER[0], LAUNCHER[1], token
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
