from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import Mock

from ouroboros.evolution.reflect import ReflectEngine
from ouroboros.evolution.wonder import WonderEngine


@dataclass
class _Adapter:
    name: str
    _cwd: str = "/repo"
    _max_turns: int = 3
    _allowed_tools: list[str] | None = None
    _permission_mode: str | None = None
    _timeout: float | None = None
    _max_retries: int | None = None


class TestEvolutionBackendDrift:
    def test_reflect_rebuild_preserves_runtime_options_and_refreshes_model(
        self, monkeypatch
    ) -> None:
        created: dict[str, object] = {}

        def fake_create_llm_adapter(**kwargs):
            created.update(kwargs)
            return _Adapter("rebuilt", _cwd=str(kwargs.get("cwd")), _max_turns=kwargs["max_turns"])

        monkeypatch.setattr(
            "ouroboros.evolution.reflect.get_llm_backend", Mock(return_value="claude")
        )
        engine = ReflectEngine(
            llm_adapter=_Adapter(
                "initial",
                _allowed_tools=["Read"],
                _permission_mode="default",
                _timeout=12.5,
                _max_retries=7,
            ),
            model="claude-old",
        )
        monkeypatch.setattr(
            "ouroboros.evolution.reflect.get_llm_backend", Mock(return_value="gemini")
        )
        monkeypatch.setattr(
            "ouroboros.evolution.reflect.get_reflect_model", Mock(return_value="gemini-reflect")
        )
        monkeypatch.setattr(
            "ouroboros.providers.factory.create_llm_adapter", fake_create_llm_adapter
        )

        rebuilt = engine._resolve_adapter()

        assert rebuilt.name == "rebuilt"
        assert created == {
            "backend": "gemini",
            "cwd": "/repo",
            "max_turns": 3,
            "allowed_tools": ["Read"],
            "permission_mode": "default",
            "timeout": 12.5,
            "max_retries": 7,
        }
        assert engine.model == "gemini-reflect"

    def test_wonder_rebuild_preserves_runtime_options_and_refreshes_model(
        self, monkeypatch
    ) -> None:
        created: dict[str, object] = {}

        def fake_create_llm_adapter(**kwargs):
            created.update(kwargs)
            return _Adapter("rebuilt", _cwd=str(kwargs.get("cwd")), _max_turns=kwargs["max_turns"])

        monkeypatch.setattr(
            "ouroboros.evolution.wonder.get_llm_backend", Mock(return_value="claude")
        )
        engine = WonderEngine(
            llm_adapter=_Adapter(
                "initial",
                _allowed_tools=["Read"],
                _permission_mode="default",
                _timeout=12.5,
                _max_retries=7,
            ),
            model="claude-old",
        )
        monkeypatch.setattr(
            "ouroboros.evolution.wonder.get_llm_backend", Mock(return_value="gemini")
        )
        monkeypatch.setattr(
            "ouroboros.evolution.wonder.get_wonder_model", Mock(return_value="gemini-wonder")
        )
        monkeypatch.setattr(
            "ouroboros.providers.factory.create_llm_adapter", fake_create_llm_adapter
        )

        rebuilt = engine._resolve_adapter()

        assert rebuilt.name == "rebuilt"
        assert created == {
            "backend": "gemini",
            "cwd": "/repo",
            "max_turns": 3,
            "allowed_tools": ["Read"],
            "permission_mode": "default",
            "timeout": 12.5,
            "max_retries": 7,
        }
        assert engine.model == "gemini-wonder"

    def test_factory_fresh_adapter_refreshes_model_on_backend_drift(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "ouroboros.evolution.reflect.get_llm_backend", Mock(return_value="claude")
        )
        fresh = _Adapter("fresh", _cwd="/safe", _max_turns=1)
        engine = ReflectEngine(
            llm_adapter=_Adapter("initial"),
            model="claude-old",
            adapter_factory=lambda: fresh,
        )
        monkeypatch.setattr(
            "ouroboros.evolution.reflect.get_llm_backend", Mock(return_value="codex")
        )
        monkeypatch.setattr(
            "ouroboros.evolution.reflect.get_reflect_model", Mock(return_value="codex-reflect")
        )

        assert engine._resolve_adapter() is fresh
        assert engine.model == "codex-reflect"

    def test_factory_pinned_backend_keeps_model_on_config_drift(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "ouroboros.evolution.reflect.get_llm_backend", Mock(return_value="codex")
        )
        fresh = _Adapter("fresh", _cwd="/safe", _max_turns=1)
        engine = ReflectEngine(
            llm_adapter=_Adapter("initial"),
            model="codex-reflect",
            adapter_factory=lambda: fresh,
            adapter_backend="codex",
        )
        get_model = Mock(return_value="gemini-reflect")
        monkeypatch.setattr(
            "ouroboros.evolution.reflect.get_llm_backend", Mock(return_value="gemini")
        )
        monkeypatch.setattr("ouroboros.evolution.reflect.get_reflect_model", get_model)

        assert engine._resolve_adapter() is fresh
        assert engine.model == "codex-reflect"
        get_model.assert_not_called()

    def test_wonder_factory_pinned_backend_keeps_model_on_config_drift(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "ouroboros.evolution.wonder.get_llm_backend", Mock(return_value="codex")
        )
        fresh = _Adapter("fresh", _cwd="/safe", _max_turns=1)
        engine = WonderEngine(
            llm_adapter=_Adapter("initial"),
            model="codex-wonder",
            adapter_factory=lambda: fresh,
            adapter_backend="codex",
        )
        get_model = Mock(return_value="gemini-wonder")
        monkeypatch.setattr(
            "ouroboros.evolution.wonder.get_llm_backend", Mock(return_value="gemini")
        )
        monkeypatch.setattr("ouroboros.evolution.wonder.get_wonder_model", get_model)

        assert engine._resolve_adapter() is fresh
        assert engine.model == "codex-wonder"
        get_model.assert_not_called()
