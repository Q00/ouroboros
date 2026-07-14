"""ChatGPT host profile composition tests."""

from pathlib import Path

from ouroboros.cli.commands.mcp import _host_dispatch_context_from_env
from ouroboros.mcp.tools.host_bridge import HostAuthoritySource


def test_chatgpt_profile_builds_host_authority_from_active_workspace(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OUROBOROS_HOST_PROFILE", "chatgpt")
    monkeypatch.delenv("OUROBOROS_HOST_WORKSPACE_ROOT", raising=False)

    context = _host_dispatch_context_from_env()

    assert context is not None
    assert context.workspace_root == tmp_path.resolve()
    assert context.authority_source is HostAuthoritySource.CHATGPT
    assert context.sandbox_mode == "host-enforced"
    assert context.approval_policy == "host-enforced"


def test_non_chatgpt_profile_does_not_enable_host_dispatch(monkeypatch) -> None:
    monkeypatch.delenv("OUROBOROS_HOST_PROFILE", raising=False)
    assert _host_dispatch_context_from_env() is None
