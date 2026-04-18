"""Tests for ``evaluation.detector`` — the AI-driven mechanical.toml author.

The detector's invariants under test:

1. When ``mechanical.toml`` already exists, no LLM call is made.
2. When manifests are absent, no LLM call is made (nothing to detect).
3. LLM proposals that cannot be verified on disk are dropped, never written.
4. LLM failures, unparseable responses, and filesystem errors are silent —
   the caller gets ``False`` and never sees an exception.
5. Successful detection writes a deterministic TOML body that
   ``build_mechanical_config`` reads verbatim.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

import pytest

from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result
from ouroboros.evaluation.detector import (
    ensure_mechanical_toml,
    has_mechanical_toml,
    toml_path,
)
from ouroboros.evaluation.languages import build_mechanical_config
from ouroboros.providers.base import (
    CompletionConfig,
    CompletionResponse,
    Message,
    UsageInfo,
)


@dataclass
class _FakeAdapter:
    """Minimal stand-in for an ``LLMAdapter`` used in tests."""

    response: str | None = None
    error: ProviderError | None = None
    calls: list[tuple[tuple[Message, ...], CompletionConfig]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.calls is None:
            self.calls = []

    async def complete(
        self,
        messages: list[Message],
        config: CompletionConfig,
    ) -> Result[CompletionResponse, ProviderError]:
        self.calls.append((tuple(messages), config))
        if self.error is not None:
            return Result.err(self.error)
        assert self.response is not None
        return Result.ok(
            CompletionResponse(
                content=self.response,
                model=config.model,
                usage=UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )
        )


def _make_node_project(path: Path, scripts: dict[str, str]) -> None:
    (path / "package.json").write_text(json.dumps({"scripts": scripts}))


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class TestEnsureMechanicalToml:
    def test_existing_toml_short_circuits(self, tmp_path: Path) -> None:
        """No LLM call fires when the toml is already present."""
        (tmp_path / ".ouroboros").mkdir()
        (tmp_path / ".ouroboros" / "mechanical.toml").write_text('test = "pytest -q"\n')
        adapter = _FakeAdapter(response="{}")
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True
        assert adapter.calls == []

    def test_no_manifests_skips_llm(self, tmp_path: Path) -> None:
        """Empty project → detector refuses rather than hallucinate."""
        adapter = _FakeAdapter(response="{}")
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False
        assert adapter.calls == []
        assert not has_mechanical_toml(tmp_path)

    def test_llm_failure_is_silent(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}')
        adapter = _FakeAdapter(error=ProviderError("network error"))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False
        assert not has_mechanical_toml(tmp_path)

    def test_unparseable_llm_response_is_silent(self, tmp_path: Path) -> None:
        _make_node_project(tmp_path, {"test": "jest"})
        adapter = _FakeAdapter(response="not json at all")
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False
        assert not has_mechanical_toml(tmp_path)

    def test_validated_proposals_are_written(self, tmp_path: Path) -> None:
        """A valid proposal round-trips into a usable MechanicalConfig."""
        _make_node_project(tmp_path, {"lint": "eslint .", "test": "jest"})
        adapter = _FakeAdapter(
            response=json.dumps(
                {"lint": "npm run lint", "test": "npm test", "build": "npm run build"}
            )
        )
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True
        assert has_mechanical_toml(tmp_path)

        config = build_mechanical_config(tmp_path)
        assert config.lint_command == ("npm", "run", "lint")
        assert config.test_command == ("npm", "test")
        # build referred to `npm run build` which is not in package.json scripts
        # → dropped by validator, never written.
        assert config.build_command is None

    def test_hallucinated_script_is_dropped(self, tmp_path: Path) -> None:
        _make_node_project(tmp_path, {"test": "jest"})
        adapter = _FakeAdapter(response=json.dumps({"test": "npm run nonexistent"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False  # everything dropped → nothing to write
        assert not has_mechanical_toml(tmp_path)

    def test_shell_chaining_is_rejected(self, tmp_path: Path) -> None:
        _make_node_project(tmp_path, {"test": "jest"})
        adapter = _FakeAdapter(response=json.dumps({"test": "npm test && rm -rf /"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False
        assert not has_mechanical_toml(tmp_path)

    def test_force_overwrites_existing_toml(self, tmp_path: Path) -> None:
        (tmp_path / ".ouroboros").mkdir()
        (tmp_path / ".ouroboros" / "mechanical.toml").write_text('test = "old command"\n')
        _make_node_project(tmp_path, {"test": "jest"})
        adapter = _FakeAdapter(response=json.dumps({"test": "npm test"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter, force=True))
        assert ok is True
        body = toml_path(tmp_path).read_text()
        assert 'test = "npm test"' in body
        assert "old command" not in body

    def test_make_target_validation(self, tmp_path: Path) -> None:
        """`make test` passes only when the Makefile actually declares ``test``."""
        (tmp_path / "Makefile").write_text(".PHONY: build\nbuild:\n\techo building\n")
        adapter = _FakeAdapter(response=json.dumps({"build": "make build", "test": "make test"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True
        config = build_mechanical_config(tmp_path)
        assert config.build_command == ("make", "build")
        assert config.test_command is None  # undeclared target → dropped

    def test_response_wrapped_in_prose_is_still_parsed(self, tmp_path: Path) -> None:
        """LLMs that prepend commentary around the JSON must still work."""
        _make_node_project(tmp_path, {"test": "jest"})
        adapter = _FakeAdapter(
            response='Here is my proposal:\n```json\n{"test": "npm test"}\n```\nDone.'
        )
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True
        config = build_mechanical_config(tmp_path)
        assert config.test_command == ("npm", "test")


class TestTomlPath:
    def test_canonical_location(self, tmp_path: Path) -> None:
        assert toml_path(tmp_path) == tmp_path / ".ouroboros" / "mechanical.toml"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
