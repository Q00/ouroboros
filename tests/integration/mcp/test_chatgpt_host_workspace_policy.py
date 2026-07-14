"""Canonical workspace and authority-policy tests for host dispatch."""

from __future__ import annotations

from pathlib import Path, PureWindowsPath
import sys

from pydantic import ValidationError
import pytest

from ouroboros.mcp.tools.host_bridge import (
    HostAuthoritySource,
    HostCompletionReceipt,
    HostDispatchContext,
    validate_changed_path_lexical,
)


def _receipt_data(workspace: Path, changed_path: str) -> dict[str, object]:
    return {
        "dispatch_id": "dispatch-policy",
        "session_id": "session-policy",
        "lineage_id": "lineage-policy",
        "workspace_id": "workspace-policy",
        "workspace_root": workspace,
        "sandbox_mode": "workspace-write",
        "approval_policy": "on-request",
        "terminal_status": "completed",
        "criterion_results": (),
        "evidence": (),
        "changed_paths": (changed_path,),
        "completed_at": "2026-07-14T06:00:00Z",
        "receipt_sha256": "a" * 64,
    }


@pytest.mark.parametrize(
    "changed_path",
    (
        "../outside.txt",
        r"..\outside.txt",
        "//server/share/outside.txt",
    ),
)
def test_receipt_rejects_cross_boundary_path_forms(tmp_path: Path, changed_path: str) -> None:
    with pytest.raises(ValidationError, match="changed_paths"):
        HostCompletionReceipt.model_validate(_receipt_data(tmp_path, changed_path))


def test_receipt_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-host-workspace"
    outside.mkdir(exist_ok=True)
    (tmp_path / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValidationError, match="changed_paths"):
        HostCompletionReceipt.model_validate(_receipt_data(tmp_path, "escape/result.txt"))


@pytest.mark.parametrize(
    "path",
    (
        PureWindowsPath(r"C:\outside.txt"),
        PureWindowsPath(r"D:\outside.txt"),
        PureWindowsPath(r"\\server\share\outside.txt"),
        PureWindowsPath(r"..\outside.txt"),
    ),
)
def test_windows_lexical_policy_is_platform_independent(path: PureWindowsPath) -> None:
    with pytest.raises(ValueError, match="changed_paths"):
        validate_changed_path_lexical(str(path))


@pytest.mark.skipif(sys.platform != "win32", reason="junction semantics require win32")
def test_receipt_rejects_windows_junction_escape(tmp_path: Path) -> None:
    pytest.fail("win32 clean-machine junction fixture required")


def test_receipt_rejects_case_folded_alias(tmp_path: Path) -> None:
    (tmp_path / "ExactCase").mkdir()

    with pytest.raises(ValidationError, match="changed_paths"):
        HostCompletionReceipt.model_validate(_receipt_data(tmp_path, "exactcase/result.txt"))


def test_host_authority_context_is_frozen_and_fixture_only(tmp_path: Path) -> None:
    context = HostDispatchContext(
        workspace_id="workspace-policy",
        workspace_root=tmp_path,
        sandbox_mode="workspace-write",
        approval_policy="on-request",
        authority_source="fixture",
    )

    assert context.authority_source is HostAuthoritySource.FIXTURE
    with pytest.raises(ValidationError, match="frozen"):
        context.approval_policy = "never"
