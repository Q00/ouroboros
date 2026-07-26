"""Tests for PM CLI adapter selection and runtime wiring."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import stat
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import typer

from ouroboros.cli.commands.pm import _run_pm_interview, _save_cli_pm_meta, pm_command
from ouroboros.core.types import Result


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _make_pm_meta_engine() -> SimpleNamespace:
    return SimpleNamespace(
        _reframe_map={},
        deferred_items=[],
        decide_later_items=[],
        codebase_context="",
        _selected_brownfield_repos=[],
        classifications=[],
    )


def test_save_cli_pm_meta_is_owner_only_under_umask_022(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI pm_meta lives in the package-owned data dir and is always 0600."""
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    previous_umask = os.umask(0o022)
    try:
        _save_cli_pm_meta("sess-owner", _make_pm_meta_engine())
    finally:
        os.umask(previous_umask)

    data_dir = tmp_path / ".ouroboros" / "data"
    meta_path = data_dir / "pm_meta_sess-owner.json"
    assert _mode(data_dir) == 0o700
    assert _mode(meta_path) == 0o600


def test_save_cli_pm_meta_failure_propagates_without_leftover_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI pm_meta persistence keeps the owner-only writer's failure contract."""
    import ouroboros.cli.commands.pm as pm

    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))

    def _fail_write(*_args: object, **_kwargs: object) -> bool:
        raise OSError("disk full")

    monkeypatch.setattr(pm, "write_owner_only", _fail_write)

    with pytest.raises(OSError, match="disk full"):
        _save_cli_pm_meta("sess-fail", _make_pm_meta_engine())

    assert not (tmp_path / ".ouroboros" / "data" / "pm_meta_sess-fail.json").exists()


def test_save_cli_pm_meta_logs_unconfirmed_durability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI helper returns None, so unconfirmed durability is logged."""
    import ouroboros.cli.commands.pm as pm

    warnings: list[tuple[str, dict[str, object]]] = []

    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setattr(pm, "write_owner_only", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        pm.log,
        "warning",
        lambda event, **kwargs: warnings.append((event, kwargs)),
    )

    _save_cli_pm_meta("sess-uncertain", _make_pm_meta_engine())

    assert warnings == [
        (
            "pm.meta_save_durability_unconfirmed",
            {
                "session_id": "sess-uncertain",
                "path": str(tmp_path / ".ouroboros" / "data" / "pm_meta_sess-uncertain.json"),
            },
        )
    ]


def test_run_pm_interview_uses_factory_for_interview_adapter_on_resume() -> None:
    """Resume mode should build the adapter through the shared interview factory."""
    sentinel_adapter = object()
    engine = SimpleNamespace(
        load_state=AsyncMock(return_value=SimpleNamespace(is_err=True, error="boom")),
    )

    with (
        patch(
            "ouroboros.cli.commands.pm.create_llm_adapter", return_value=sentinel_adapter
        ) as mock_factory,
        patch(
            "ouroboros.bigbang.pm_interview.PMInterviewEngine.create", return_value=engine
        ) as mock_create,
    ):
        try:
            asyncio.run(
                _run_pm_interview(
                    resume_id="session-123",
                    model="default",
                    backend="codex",
                    debug=False,
                    output_dir=None,
                )
            )
        except typer.Exit:
            pass
        else:
            raise AssertionError("Expected typer.Exit when mocked load_state returns an error")

    mock_factory.assert_called_once_with(
        backend="codex",
        use_case="interview",
        allowed_tools=None,
        max_turns=5,
        on_message=None,
        cwd=Path.cwd(),
    )
    mock_create.assert_called_once_with(llm_adapter=sentinel_adapter, model="default")


def test_run_pm_interview_uses_interview_runtime_options_on_new_session() -> None:
    """New sessions should pass backend-aware interview options into the factory."""
    sentinel_adapter = object()
    state = SimpleNamespace(
        interview_id="pm-session-123",
        is_complete=True,
        rounds=[],
    )
    engine = SimpleNamespace(
        get_opening_question=lambda: "What do you want to build?",
        ask_opening_and_start=AsyncMock(return_value=Result.ok(state)),
        deferred_items=[],
        decide_later_items=[],
        codebase_context="",
        format_decide_later_summary=lambda: "",
        _reframe_map={},
        _selected_brownfield_repos=[],
        classifications=[],
    )

    with (
        patch(
            "ouroboros.cli.commands.pm.create_llm_adapter", return_value=sentinel_adapter
        ) as mock_factory,
        patch(
            "ouroboros.bigbang.pm_interview.PMInterviewEngine.create", return_value=engine
        ) as mock_create,
        patch("ouroboros.cli.commands.pm._check_existing_pm_seeds", return_value=True),
        patch("ouroboros.cli.commands.pm._load_brownfield_from_db", return_value=[]),
        patch("ouroboros.cli.commands.pm._select_repos", return_value=[]),
        patch("ouroboros.cli.commands.pm._save_cli_pm_meta"),
        patch(
            "ouroboros.cli.commands.pm.multiline_prompt_async",
            new=AsyncMock(return_value="Build a PM workflow"),
        ),
    ):
        asyncio.run(
            _run_pm_interview(
                resume_id=None,
                model="default",
                backend="codex",
                debug=True,
                output_dir=None,
            )
        )

    mock_factory.assert_called_once()
    factory_kwargs = mock_factory.call_args.kwargs
    assert factory_kwargs["backend"] == "codex"
    assert factory_kwargs["use_case"] == "interview"
    assert factory_kwargs["allowed_tools"] is None
    assert factory_kwargs["max_turns"] == 5
    assert callable(factory_kwargs["on_message"])
    assert factory_kwargs["cwd"] == Path.cwd()
    mock_create.assert_called_once_with(llm_adapter=sentinel_adapter, model="default")
    engine.ask_opening_and_start.assert_called_once_with(
        user_response="Build a PM workflow",
        brownfield_repos=None,
    )


def test_pm_command_uses_backend_safe_default_model() -> None:
    """CLI entrypoint should normalize the default model for the configured backend."""
    ctx = SimpleNamespace(invoked_subcommand=None)

    with (
        patch("ouroboros.cli.commands.pm.get_llm_backend", return_value="codex"),
        patch(
            "ouroboros.cli.commands.pm.get_clarification_model",
            side_effect=lambda backend=None: (
                "backend-safe-default" if backend == "codex" else "generic-default"
            ),
        ) as mock_get_model,
        patch("ouroboros.cli.commands.pm.resolve_llm_backend", return_value="codex"),
        patch(
            "ouroboros.cli.commands.pm.resolve_llm_permission_mode",
            return_value="bypassPermissions",
        ),
        patch(
            "ouroboros.cli.commands.pm._run_pm_interview",
            new=AsyncMock(return_value=None),
        ) as mock_run,
        patch("ouroboros.cli.commands.pm.print_warning") as mock_warning,
    ):
        pm_command(
            ctx=ctx,
            resume=None,
            output=None,
            model=None,
            debug=False,
        )

    mock_run.assert_awaited_once_with(
        resume_id=None,
        model="backend-safe-default",
        backend="codex",
        debug=False,
        output_dir=None,
    )
    mock_get_model.assert_called_once_with("codex")
    mock_warning.assert_called_once()
    assert "bypassPermissions" in mock_warning.call_args.args[0]


def test_pm_command_formats_factory_errors() -> None:
    """Backend/config errors should exit cleanly instead of surfacing a traceback."""
    ctx = SimpleNamespace(invoked_subcommand=None)

    with (
        patch("ouroboros.cli.commands.pm.get_llm_backend", return_value="invalid_backend_xyz"),
        patch("ouroboros.cli.commands.pm.get_clarification_model", return_value="default"),
        patch("ouroboros.cli.commands.pm.print_error") as mock_error,
    ):
        try:
            pm_command(
                ctx=ctx,
                resume=None,
                output=None,
                model=None,
                debug=False,
            )
        except typer.Exit as exc:
            assert exc.exit_code == 1
        else:
            raise AssertionError("Expected typer.Exit for backend configuration errors")

    mock_error.assert_called_once_with("Unsupported LLM backend: invalid_backend_xyz")


def test_pm_command_formats_missing_litellm_dependency() -> None:
    """LiteLLM installs should fail with actionable guidance instead of a traceback."""
    ctx = SimpleNamespace(invoked_subcommand=None)

    with (
        patch("ouroboros.cli.commands.pm.get_llm_backend", return_value="litellm"),
        patch(
            "ouroboros.cli.commands.pm.get_clarification_model",
            side_effect=lambda backend=None: "litellm-model" if backend else "generic-default",
        ),
        patch("ouroboros.cli.commands.pm.resolve_llm_backend", return_value="litellm"),
        patch(
            "ouroboros.cli.commands.pm.resolve_llm_permission_mode",
            return_value="default",
        ),
        patch(
            "ouroboros.cli.commands.pm.create_llm_adapter",
            side_effect=ModuleNotFoundError("No module named 'litellm'"),
        ),
        patch("ouroboros.cli.commands.pm.print_error") as mock_error,
    ):
        try:
            pm_command(
                ctx=ctx,
                resume=None,
                output=None,
                model=None,
                debug=False,
            )
        except typer.Exit as exc:
            assert exc.exit_code == 1
        else:
            raise AssertionError("Expected typer.Exit for missing optional litellm dependency")

    message = mock_error.call_args.args[0]
    assert "PM interviews require the optional LiteLLM dependency." in message
    assert "pip install 'ouroboros-ai[litellm]'" in message


def test_pm_command_formats_missing_litellm_on_python_314_with_supported_range() -> None:
    """Command-level error formatting should preserve the Python 3.13 remediation."""
    ctx = SimpleNamespace(invoked_subcommand=None)

    with (
        patch("ouroboros.cli.commands.pm.get_llm_backend", return_value="litellm"),
        patch(
            "ouroboros.cli.commands.pm.get_clarification_model",
            side_effect=lambda backend=None: "litellm-model" if backend else "generic-default",
        ),
        patch("ouroboros.cli.commands.pm.resolve_llm_backend", return_value="litellm"),
        patch(
            "ouroboros.cli.commands.pm.resolve_llm_permission_mode",
            return_value="default",
        ),
        patch(
            "ouroboros.cli.commands.pm.create_llm_adapter",
            side_effect=ModuleNotFoundError("No module named 'litellm'"),
        ),
        patch("ouroboros.cli.commands.pm.print_error") as mock_error,
        patch("ouroboros.providers.factory.sys.version_info", (3, 14, 0, "final", 0)),
    ):
        try:
            pm_command(
                ctx=ctx,
                resume=None,
                output=None,
                model=None,
                debug=False,
            )
        except typer.Exit as exc:
            assert exc.exit_code == 1
        else:
            raise AssertionError("Expected typer.Exit for missing optional litellm dependency")

    message = mock_error.call_args.args[0]
    assert "Python >=3.12,<3.14" in message
    assert "Python 3.13" in message
    assert "python3.13 -m pip install 'ouroboros-ai[litellm]'" in message
    assert "python3.14" not in message.lower()
