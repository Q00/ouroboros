"""Unit tests for observability-only parameter-level capability negotiation.

The orchestrator surfaces when a runtime will not honor a requested execution
parameter in its supplied form. These tests pin the pure negotiation logic:
non-native handling of a *requested* parameter yields a degradation record;
native handling, or an absent parameter, yields nothing.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from ouroboros.orchestrator.adapter import ParamSupport, RuntimeCapabilities
from ouroboros.orchestrator.runtime_param_negotiation import (
    adapter_requested_permission_mode,
    announce_execution_param_degradations,
    negotiate_execution_params,
)


def _caps(
    *,
    system_prompt_support: ParamSupport = ParamSupport.NATIVE,
    tool_restriction_support: ParamSupport = ParamSupport.NATIVE,
    permission_mode_support: ParamSupport = ParamSupport.NATIVE,
) -> RuntimeCapabilities:
    return RuntimeCapabilities(
        skill_dispatch=True,
        targeted_resume=True,
        structured_output=True,
        system_prompt_support=system_prompt_support,
        tool_restriction_support=tool_restriction_support,
        permission_mode_support=permission_mode_support,
    )


def test_all_native_yields_no_degradations() -> None:
    result = negotiate_execution_params(
        _caps(),
        system_prompt="be terse",
        tools=["Read", "Edit"],
        permission_mode="acceptEdits",
    )

    assert result == ()


def test_translated_system_prompt_is_reported_when_requested() -> None:
    result = negotiate_execution_params(
        _caps(system_prompt_support=ParamSupport.TRANSLATED),
        system_prompt="be terse",
        tools=None,
        permission_mode=None,
    )

    assert len(result) == 1
    assert result[0].parameter == "system_prompt"
    assert result[0].support is ParamSupport.TRANSLATED
    assert "translation" in result[0].detail


def test_ignored_tools_is_reported_when_requested() -> None:
    result = negotiate_execution_params(
        _caps(tool_restriction_support=ParamSupport.IGNORED),
        system_prompt=None,
        tools=["Read"],
        permission_mode=None,
    )

    assert len(result) == 1
    assert result[0].parameter == "tools"
    assert result[0].support is ParamSupport.IGNORED
    assert "dropped" in result[0].detail


def test_absent_parameter_is_never_degraded() -> None:
    # The runtime does not honor system_prompt natively, but none was supplied.
    result = negotiate_execution_params(
        _caps(system_prompt_support=ParamSupport.IGNORED),
        system_prompt=None,
        tools=None,
        permission_mode=None,
    )

    assert result == ()


def test_empty_strings_count_as_absent_but_empty_tools_is_requested() -> None:
    result = negotiate_execution_params(
        _caps(
            system_prompt_support=ParamSupport.IGNORED,
            tool_restriction_support=ParamSupport.IGNORED,
        ),
        system_prompt="",
        tools=[],
        permission_mode="",
    )

    assert len(result) == 1
    assert result[0].parameter == "tools"
    assert result[0].support is ParamSupport.IGNORED


def test_multiple_non_native_params_are_all_reported() -> None:
    result = negotiate_execution_params(
        _caps(
            system_prompt_support=ParamSupport.TRANSLATED,
            permission_mode_support=ParamSupport.TRANSLATED,
        ),
        system_prompt="be terse",
        tools=["Read"],  # native → not reported
        permission_mode="acceptEdits",
    )

    reported = {item.parameter for item in result}
    assert reported == {"system_prompt", "permission_mode"}


def test_adapter_default_permission_mode_is_not_requested() -> None:
    adapter = SimpleNamespace(permission_mode="bypassPermissions")

    assert adapter_requested_permission_mode(adapter) is None


def test_adapter_explicit_permission_mode_is_requested() -> None:
    adapter = SimpleNamespace(
        permission_mode="acceptEdits",
        permission_mode_requested=True,
    )

    assert adapter_requested_permission_mode(adapter) == "acceptEdits"


def test_shared_notice_suppresses_unrequested_permission_default() -> None:
    adapter = SimpleNamespace(
        capabilities=_caps(permission_mode_support=ParamSupport.IGNORED),
        runtime_backend="opencode",
        permission_mode="bypassPermissions",
    )
    console = MagicMock()

    announce_execution_param_degradations(
        adapter,
        system_prompt=None,
        tools=None,
        console=console,
    )

    console.print.assert_not_called()


def test_shared_notice_surfaces_requested_permission_degradation_once() -> None:
    adapter = SimpleNamespace(
        capabilities=_caps(permission_mode_support=ParamSupport.IGNORED),
        runtime_backend="opencode",
        permission_mode="acceptEdits",
        permission_mode_requested=True,
    )
    console = MagicMock()
    announced: set[tuple[str, str]] = set()

    announce_execution_param_degradations(
        adapter,
        system_prompt=None,
        tools=None,
        announced=announced,
        console=console,
    )
    announce_execution_param_degradations(
        adapter,
        system_prompt=None,
        tools=None,
        announced=announced,
        console=console,
    )

    assert console.print.call_count == 1
    assert "permission_mode" in console.print.call_args.args[0]
