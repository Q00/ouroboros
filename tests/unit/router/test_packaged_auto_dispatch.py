from __future__ import annotations

from pathlib import Path

from ouroboros.router import Resolved, resolve_skill_dispatch


def test_packaged_auto_skill_dispatches_to_ouroboros_auto(tmp_path: Path) -> None:
    """Lock the packaged ``ooo auto`` dispatch metadata used by Codex runtimes."""
    prompt = 'ooo auto "Audit the open PRs" --skip-run'

    result = resolve_skill_dispatch(prompt, cwd=tmp_path)

    assert isinstance(result, Resolved)
    assert result.command_prefix == "ooo auto"
    assert result.mcp_tool == "ouroboros_auto"
    assert result.mcp_args == {
        "goal": "Audit the open PRs",
        "resume": "",
        "cwd": str(tmp_path),
        "max_interview_rounds": "",
        "max_repair_rounds": "",
        "skip_run": True,
    }
    assert result.first_argument == "Audit the open PRs --skip-run"
