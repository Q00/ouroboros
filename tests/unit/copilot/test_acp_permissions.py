"""ACP retains the tool envelope without blanket permission switches."""

from __future__ import annotations

from pathlib import Path

import pytest

from ouroboros.copilot.acp_permissions import CopilotAcpPermissions, resolve_acp_permission_mode


def request(kind: str, **raw_input: object) -> dict:
    return {
        "toolCall": {"toolCallId": "tool-1", "kind": kind, "rawInput": raw_input},
        "options": [
            {"optionId": "always", "kind": "allow_always"},
            {"optionId": "once", "kind": "allow_once"},
        ],
    }


def test_default_and_empty_envelopes_have_no_tools(tmp_path: Path) -> None:
    for mode, tools in (("default", ["Bash"]), ("acceptEdits", [])):
        permissions = CopilotAcpPermissions.for_task(str(tmp_path), mode, tools)
        assert permissions.cli_args() == [f"--available-tools={permissions.empty_tool_marker}"]
        assert permissions.empty_tool_marker.startswith("ouroboros_no_tools_")
        assert "--available-tools=" not in permissions.cli_args()
        assert (
            permissions.request_permission(request("execute"))["outcome"]["outcome"] == "cancelled"
        )


def test_tool_mapping_is_native_and_never_adds_blanket_flags(tmp_path: Path) -> None:
    permissions = CopilotAcpPermissions.for_task(str(tmp_path), "acceptEdits", ["Read", "Bash"])
    assert set(permissions.tools) == {"view", "bash", "read_bash", "stop_bash", "list_bash"}
    assert all("--allow-all" not in argument for argument in permissions.cli_args())
    assert permissions.request_permission(request("execute", command="python3 -m unittest")) == {
        "outcome": {"outcome": "selected", "optionId": "once"}
    }


def test_runner_bypass_is_explicitly_capped_to_workspace_permissions() -> None:
    assert resolve_acp_permission_mode("bypassPermissions") == "acceptEdits"


@pytest.mark.parametrize("name", ["", "--allow-all", "view,bash", "view\n--allow-all", "view*"])
def test_invalid_tool_names_cannot_inject_flags(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="tool name"):
        CopilotAcpPermissions.for_task(str(tmp_path), "acceptEdits", [name])


def test_edits_require_a_workspace_path(tmp_path: Path) -> None:
    permissions = CopilotAcpPermissions.for_task(str(tmp_path), "acceptEdits", ["Edit"])
    assert permissions.request_permission(request("edit"))["outcome"]["outcome"] == "cancelled"
    assert permissions.request_permission(request("edit", path="app.py"))["outcome"] == {
        "outcome": "selected",
        "optionId": "once",
    }
    assert (
        permissions.request_permission(request("edit", path="../outside.py"))["outcome"]["outcome"]
        == "cancelled"
    )


def test_symlink_and_location_escapes_are_denied(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "link").symlink_to(tmp_path, target_is_directory=True)
    permissions = CopilotAcpPermissions.for_task(str(workspace), "acceptEdits", ["Edit"])
    assert (
        permissions.request_permission(request("edit", path="link/outside.py"))["outcome"][
            "outcome"
        ]
        == "cancelled"
    )
    params = request("edit", path="app.py")
    params["toolCall"]["locations"] = [{"path": str(tmp_path / "outside.py")}]
    assert permissions.request_permission(params)["outcome"]["outcome"] == "cancelled"
    params = request("edit", fileName=str(tmp_path / "outside.py"))
    params["toolCall"]["locations"] = [{"path": str(workspace / "inside.py")}]
    assert permissions.request_permission(params)["outcome"]["outcome"] == "cancelled"


def test_measured_glob_string_path_is_checked_like_path_lists(tmp_path: Path) -> None:
    permissions = CopilotAcpPermissions.for_task(str(tmp_path), "acceptEdits", ["Glob"])
    allowed = permissions.request_permission(request("read", paths=str(tmp_path)))
    assert allowed["outcome"]["outcome"] == "selected"
    denied = permissions.request_permission(request("read", paths=str(tmp_path.parent)))
    assert denied["outcome"]["outcome"] == "cancelled"


@pytest.mark.parametrize("kind", ["fetch", "other", "unknown", None, []])
def test_unknown_or_network_permissions_fail_closed(tmp_path: Path, kind: object) -> None:
    permissions = CopilotAcpPermissions.for_task(str(tmp_path), "acceptEdits", None)
    assert permissions.request_permission(request(kind))["outcome"]["outcome"] == "cancelled"


def test_fallback_never_removes_mutation_approval_rpc(tmp_path: Path) -> None:
    assert CopilotAcpPermissions.for_task(str(tmp_path), "default", None).allows_cli_fallback
    assert CopilotAcpPermissions.for_task(
        str(tmp_path), "acceptEdits", ["Read", "Glob", "Grep"]
    ).allows_cli_fallback
    assert not CopilotAcpPermissions.for_task(
        str(tmp_path), "acceptEdits", ["Edit"]
    ).allows_cli_fallback
