"""The start receipt names a defaulted quality-first policy and how to change it."""

from unittest.mock import patch

import pytest

from ouroboros.config import policy_notice

_TARGET = "ouroboros.config.policy_notice.default_execution_efficiency_mode"


def test_defaulted_quality_first_start_names_the_change_command() -> None:
    with patch(_TARGET, return_value="quality_first"):
        notice = policy_notice({"goal": "migrate to JWT"})
    assert "default" in notice
    assert "ouroboros config set execution.default_policy efficient" in notice
    assert "ouroboros config init" in notice


@pytest.mark.parametrize(
    "arguments",
    [
        {"efficiency_mode": "quality_first"},
        {"efficiency_mode": "adaptive"},
        {"resume": "auto_1"},
        {"session_id": "orch_1"},
    ],
)
def test_no_notice_when_the_caller_chose_or_resumes(arguments: dict[str, str]) -> None:
    with patch(_TARGET, return_value="quality_first"):
        assert policy_notice(arguments) == ""


@pytest.mark.parametrize("configured", ["adaptive", None])
def test_no_notice_when_the_configured_default_is_not_quality_first(configured: str | None) -> None:
    with patch(_TARGET, return_value=configured):
        assert policy_notice({}) == ""
