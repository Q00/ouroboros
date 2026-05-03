from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from unittest.mock import Mock

from ouroboros.core.lineage import EvaluationSummary, OntologyLineage
from ouroboros.core.seed import OntologyField, OntologySchema, Seed, SeedMetadata
from ouroboros.core.types import Result
from ouroboros.evolution.reflect import ReflectEngine
from ouroboros.evolution.wonder import WonderEngine, WonderOutput
from ouroboros.providers.base import CompletionConfig, CompletionResponse, Message, UsageInfo


@dataclass
class _Adapter:
    name: str
    _cwd: str = "/repo"
    _max_turns: int = 3
    _allowed_tools: list[str] | None = None
    _permission_mode: str | None = None
    _timeout: float | None = None
    _max_retries: int | None = None


@dataclass
class _CompletingAdapter(_Adapter):
    content: str = (
        '{"questions": [], "ontology_tensions": [], "should_continue": false, "reasoning": "ok"}'
    )
    seen_configs: list[CompletionConfig] = field(default_factory=list)

    async def complete(self, messages: list[Message], config: CompletionConfig):
        self.seen_configs.append(config)
        return Result.ok(
            CompletionResponse(
                content=self.content,
                model=config.model,
                usage=UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )
        )


def _seed() -> Seed:
    return Seed(
        metadata=SeedMetadata(ambiguity_score=0.1),
        goal="Build a login system",
        constraints=("Must use OAuth",),
        acceptance_criteria=("User can log in",),
        ontology_schema=OntologySchema(
            name="login",
            description="Login ontology",
            fields=(OntologyField(name="user", field_type="entity", description="A user"),),
        ),
    )


def _summary() -> EvaluationSummary:
    return EvaluationSummary(final_approved=True, highest_stage_passed=3, score=0.95)


def _lineage() -> OntologyLineage:
    return OntologyLineage(goal="Build a login system")


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
        monkeypatch.setattr(
            "ouroboros.evolution.reflect.get_reflect_model",
            Mock(side_effect=lambda backend=None: f"{backend}-reflect"),
        )
        engine = ReflectEngine(
            llm_adapter=_Adapter(
                "initial",
                _allowed_tools=["Read"],
                _permission_mode="default",
                _timeout=12.5,
                _max_retries=7,
            ),
        )
        monkeypatch.setattr(
            "ouroboros.evolution.reflect.get_llm_backend", Mock(return_value="gemini")
        )
        # model selection is already patched above and should now refresh to gemini.
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
        monkeypatch.setattr(
            "ouroboros.evolution.wonder.get_wonder_model",
            Mock(side_effect=lambda backend=None: f"{backend}-wonder"),
        )
        engine = WonderEngine(
            llm_adapter=_Adapter(
                "initial",
                _allowed_tools=["Read"],
                _permission_mode="default",
                _timeout=12.5,
                _max_retries=7,
            ),
        )
        monkeypatch.setattr(
            "ouroboros.evolution.wonder.get_llm_backend", Mock(return_value="gemini")
        )
        # model selection is already patched above and should now refresh to gemini.
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
        monkeypatch.setattr(
            "ouroboros.evolution.reflect.get_reflect_model",
            Mock(side_effect=lambda backend=None: f"{backend}-reflect"),
        )
        fresh = _Adapter("fresh", _cwd="/safe", _max_turns=1)
        engine = ReflectEngine(
            llm_adapter=_Adapter("initial"),
            adapter_factory=lambda: fresh,
        )
        monkeypatch.setattr(
            "ouroboros.evolution.reflect.get_llm_backend", Mock(return_value="codex")
        )

        assert engine._resolve_adapter() is fresh
        assert engine.llm_adapter is fresh
        assert engine.model == "codex-reflect"

    def test_reflect_factory_same_backend_model_refresh_reaches_completion_config(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "ouroboros.evolution.reflect.get_llm_backend", Mock(return_value="codex")
        )
        monkeypatch.setattr(
            "ouroboros.evolution.reflect.get_reflect_model", Mock(return_value="new-reflect")
        )
        fresh = _CompletingAdapter(
            "fresh",
            content=(
                '{"refined_goal": "Build a login system", "refined_constraints": [], '
                '"refined_acs": [], "ontology_mutations": [], "reasoning": "ok"}'
            ),
        )
        engine = ReflectEngine(
            llm_adapter=_Adapter("initial"),
            adapter_factory=lambda: fresh,
        )

        result = asyncio.run(
            engine.reflect(
                _seed(),
                "execution output",
                _summary(),
                WonderOutput(questions=(), ontology_tensions=(), should_continue=False),
                _lineage(),
            )
        )

        assert result.is_ok
        assert engine.model == "new-reflect"
        assert fresh.seen_configs[-1].model == "new-reflect"

    def test_wonder_factory_same_backend_model_refresh_reaches_completion_config(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "ouroboros.evolution.wonder.get_llm_backend", Mock(return_value="codex")
        )
        monkeypatch.setattr(
            "ouroboros.evolution.wonder.get_wonder_model", Mock(return_value="new-wonder")
        )
        fresh = _CompletingAdapter("fresh")
        engine = WonderEngine(
            llm_adapter=_Adapter("initial"),
            adapter_factory=lambda: fresh,
        )

        result = asyncio.run(
            engine.wonder(
                _seed().ontology_schema,
                _summary(),
                "execution output",
                _lineage(),
                _seed(),
            )
        )

        assert result.is_ok
        assert engine.model == "new-wonder"
        assert fresh.seen_configs[-1].model == "new-wonder"

    def test_factory_failure_falls_back_to_latest_successful_reflect_adapter(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "ouroboros.evolution.reflect.get_llm_backend", Mock(return_value="claude")
        )
        monkeypatch.setattr(
            "ouroboros.evolution.reflect.get_reflect_model",
            Mock(side_effect=lambda backend=None: f"{backend}-reflect"),
        )
        initial = _Adapter("initial")
        fresh = _Adapter("fresh", _cwd="/safe", _max_turns=1)
        calls = 0

        def flaky_factory():
            nonlocal calls
            calls += 1
            if calls == 1:
                return fresh
            raise RuntimeError("temporary factory failure")

        engine = ReflectEngine(
            llm_adapter=initial,
            adapter_factory=flaky_factory,
        )
        monkeypatch.setattr(
            "ouroboros.evolution.reflect.get_llm_backend", Mock(return_value="codex")
        )

        assert engine._resolve_adapter() is fresh
        assert engine.llm_adapter is fresh
        assert engine.model == "codex-reflect"
        assert engine._resolve_adapter() is fresh
        assert engine.llm_adapter is fresh
        assert engine.model == "codex-reflect"

    def test_factory_failure_falls_back_to_latest_successful_wonder_adapter(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "ouroboros.evolution.wonder.get_llm_backend", Mock(return_value="claude")
        )
        monkeypatch.setattr(
            "ouroboros.evolution.wonder.get_wonder_model",
            Mock(side_effect=lambda backend=None: f"{backend}-wonder"),
        )
        initial = _Adapter("initial")
        fresh = _Adapter("fresh", _cwd="/safe", _max_turns=1)
        calls = 0

        def flaky_factory():
            nonlocal calls
            calls += 1
            if calls == 1:
                return fresh
            raise RuntimeError("temporary factory failure")

        engine = WonderEngine(
            llm_adapter=initial,
            adapter_factory=flaky_factory,
        )
        monkeypatch.setattr(
            "ouroboros.evolution.wonder.get_llm_backend", Mock(return_value="codex")
        )

        assert engine._resolve_adapter() is fresh
        assert engine.llm_adapter is fresh
        assert engine.model == "codex-wonder"
        assert engine._resolve_adapter() is fresh
        assert engine.llm_adapter is fresh
        assert engine.model == "codex-wonder"

    def test_factory_pinned_backend_keeps_model_on_config_drift(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "ouroboros.evolution.reflect.get_llm_backend", Mock(return_value="codex")
        )
        get_model = Mock(return_value="codex-reflect-updated")
        monkeypatch.setattr("ouroboros.evolution.reflect.get_reflect_model", get_model)
        fresh = _Adapter("fresh", _cwd="/safe", _max_turns=1)
        engine = ReflectEngine(
            llm_adapter=_Adapter("initial"),
            adapter_factory=lambda: fresh,
            adapter_backend="codex",
        )
        monkeypatch.setattr(
            "ouroboros.evolution.reflect.get_llm_backend", Mock(return_value="gemini")
        )

        assert engine._resolve_adapter() is fresh
        assert engine.model == "codex-reflect-updated"
        assert get_model.call_args_list[-1].args == ("codex",)

    def test_wonder_factory_pinned_backend_keeps_model_on_config_drift(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "ouroboros.evolution.wonder.get_llm_backend", Mock(return_value="codex")
        )
        get_model = Mock(return_value="codex-wonder-updated")
        monkeypatch.setattr("ouroboros.evolution.wonder.get_wonder_model", get_model)
        fresh = _Adapter("fresh", _cwd="/safe", _max_turns=1)
        engine = WonderEngine(
            llm_adapter=_Adapter("initial"),
            adapter_factory=lambda: fresh,
            adapter_backend="codex",
        )
        monkeypatch.setattr(
            "ouroboros.evolution.wonder.get_llm_backend", Mock(return_value="gemini")
        )

        assert engine._resolve_adapter() is fresh
        assert engine.model == "codex-wonder-updated"
        assert get_model.call_args_list[-1].args == ("codex",)


class TestEvolutionExplicitModelOverride:
    def test_reflect_explicit_model_survives_factory_refresh(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "ouroboros.evolution.reflect.get_llm_backend", Mock(return_value="codex")
        )
        get_model = Mock(return_value="config-reflect")
        monkeypatch.setattr("ouroboros.evolution.reflect.get_reflect_model", get_model)
        fresh = _CompletingAdapter(
            "fresh",
            content=(
                '{"refined_goal": "Build a login system", "refined_constraints": [], '
                '"refined_acs": [], "ontology_mutations": [], "reasoning": "ok"}'
            ),
        )
        engine = ReflectEngine(
            llm_adapter=_Adapter("initial"),
            model="explicit-reflect",
            adapter_factory=lambda: fresh,
        )

        result = asyncio.run(
            engine.reflect(
                _seed(),
                "execution output",
                _summary(),
                WonderOutput(questions=(), ontology_tensions=(), should_continue=False),
                _lineage(),
            )
        )

        assert result.is_ok
        assert engine.model == "explicit-reflect"
        assert fresh.seen_configs[-1].model == "explicit-reflect"
        get_model.assert_not_called()

    def test_wonder_explicit_model_survives_factory_refresh(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "ouroboros.evolution.wonder.get_llm_backend", Mock(return_value="codex")
        )
        get_model = Mock(return_value="config-wonder")
        monkeypatch.setattr("ouroboros.evolution.wonder.get_wonder_model", get_model)
        fresh = _CompletingAdapter("fresh")
        engine = WonderEngine(
            llm_adapter=_Adapter("initial"),
            model="explicit-wonder",
            adapter_factory=lambda: fresh,
        )

        result = asyncio.run(
            engine.wonder(
                _seed().ontology_schema,
                _summary(),
                "execution output",
                _lineage(),
                _seed(),
            )
        )

        assert result.is_ok
        assert engine.model == "explicit-wonder"
        assert fresh.seen_configs[-1].model == "explicit-wonder"
        get_model.assert_not_called()

    def test_reflect_explicit_model_survives_backend_rebuild(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "ouroboros.evolution.reflect.get_llm_backend", Mock(return_value="claude")
        )
        get_model = Mock(return_value="config-reflect")
        monkeypatch.setattr("ouroboros.evolution.reflect.get_reflect_model", get_model)

        def fake_create_llm_adapter(**_kwargs):
            return _Adapter("rebuilt")

        monkeypatch.setattr(
            "ouroboros.providers.factory.create_llm_adapter", fake_create_llm_adapter
        )
        engine = ReflectEngine(llm_adapter=_Adapter("initial"), model="explicit-reflect")
        monkeypatch.setattr(
            "ouroboros.evolution.reflect.get_llm_backend", Mock(return_value="gemini")
        )

        assert engine._resolve_adapter().name == "rebuilt"
        assert engine.model == "explicit-reflect"
        get_model.assert_not_called()

    def test_wonder_explicit_model_survives_backend_rebuild(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "ouroboros.evolution.wonder.get_llm_backend", Mock(return_value="claude")
        )
        get_model = Mock(return_value="config-wonder")
        monkeypatch.setattr("ouroboros.evolution.wonder.get_wonder_model", get_model)

        def fake_create_llm_adapter(**_kwargs):
            return _Adapter("rebuilt")

        monkeypatch.setattr(
            "ouroboros.providers.factory.create_llm_adapter", fake_create_llm_adapter
        )
        engine = WonderEngine(llm_adapter=_Adapter("initial"), model="explicit-wonder")
        monkeypatch.setattr(
            "ouroboros.evolution.wonder.get_llm_backend", Mock(return_value="gemini")
        )

        assert engine._resolve_adapter().name == "rebuilt"
        assert engine.model == "explicit-wonder"
        get_model.assert_not_called()
