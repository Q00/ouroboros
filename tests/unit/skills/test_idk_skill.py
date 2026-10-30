from pathlib import Path

import pytest

from ouroboros.router import Resolved, ResolveRequest, resolve_skill_dispatch


@pytest.mark.parametrize("command", ["idk", "dont-know", "calibrate"])
def test_idk_routes_through_the_interview_session_contract(tmp_path: Path, command: str) -> None:
    result = resolve_skill_dispatch(
        ResolveRequest(
            prompt=f"ooo {command} I do not know idempotency; I built REST APIs",
            cwd=tmp_path,
            skills_dir=Path("skills"),
        )
    )

    assert isinstance(result, Resolved)
    assert result.skill_name == "idk"
    assert result.mcp_tool == "ouroboros_interview"
    assert result.mcp_args == {"calibration_input": "I do not know idempotency; I built REST APIs"}
