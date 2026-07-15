"""Regression tests for PM CLI behavior when LiteLLM is not installed."""

from __future__ import annotations

import builtins
import sys

import pytest

from ouroboros.cli.commands.pm import _create_pm_litellm_adapter


def test_create_pm_litellm_adapter_raises_actionable_error_when_litellm_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing optional dependency should yield install guidance, not a traceback."""
    original_import = builtins.__import__
    original_litellm = sys.modules.get("litellm")
    original_adapter_module = sys.modules.get("ouroboros.providers.litellm_adapter")

    sys.modules.pop("litellm", None)
    sys.modules.pop("ouroboros.providers.litellm_adapter", None)

    def fake_import(
        name: str,
        globals: dict[str, object] | None = None,
        locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ):
        if name == "litellm":
            raise ModuleNotFoundError("No module named 'litellm'", name="litellm")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    try:
        with pytest.raises(RuntimeError, match="optional LiteLLM dependency"):
            _create_pm_litellm_adapter()
    finally:
        if original_litellm is not None:
            sys.modules["litellm"] = original_litellm
        else:
            sys.modules.pop("litellm", None)

        if original_adapter_module is not None:
            sys.modules["ouroboros.providers.litellm_adapter"] = original_adapter_module
        else:
            sys.modules.pop("ouroboros.providers.litellm_adapter", None)


def test_create_pm_litellm_adapter_on_python_314_recommends_python_313(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Python 3.14 must receive the supported LiteLLM interpreter range."""
    original_import = builtins.__import__
    original_litellm = sys.modules.get("litellm")
    original_adapter_module = sys.modules.get("ouroboros.providers.litellm_adapter")

    sys.modules.pop("litellm", None)
    sys.modules.pop("ouroboros.providers.litellm_adapter", None)

    def fake_import(
        name: str,
        globals: dict[str, object] | None = None,
        locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ):
        if name == "litellm":
            raise ModuleNotFoundError("No module named 'litellm'", name="litellm")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr("ouroboros.providers.factory.sys.version_info", (3, 14, 0, "final", 0))

    try:
        with pytest.raises(RuntimeError) as exc_info:
            _create_pm_litellm_adapter()
    finally:
        if original_litellm is not None:
            sys.modules["litellm"] = original_litellm
        else:
            sys.modules.pop("litellm", None)

        if original_adapter_module is not None:
            sys.modules["ouroboros.providers.litellm_adapter"] = original_adapter_module
        else:
            sys.modules.pop("ouroboros.providers.litellm_adapter", None)

    message = str(exc_info.value)
    assert "Python >=3.12,<3.14" in message
    assert "Python 3.13" in message
    assert "python3.13 -m pip install 'ouroboros-ai[litellm]'" in message
