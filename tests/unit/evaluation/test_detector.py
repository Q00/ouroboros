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


class TestNpxValidation:
    def test_npx_dropped_when_package_not_declared(self, tmp_path: Path) -> None:
        """``npx <pkg>`` must reference an installed/declared package."""
        _make_node_project(tmp_path, {"test": "jest"})
        adapter = _FakeAdapter(response=json.dumps({"lint": "npx eslint ."}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False  # eslint not in deps → dropped → empty proposal
        assert not has_mechanical_toml(tmp_path)

    def test_npx_accepted_when_in_dev_dependencies(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(
            json.dumps({"scripts": {}, "devDependencies": {"eslint": "^9.0.0"}})
        )
        adapter = _FakeAdapter(response=json.dumps({"lint": "npx eslint ."}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True
        config = build_mechanical_config(tmp_path)
        assert config.lint_command == ("npx", "eslint", ".")

    def test_npx_accepted_when_installed_in_node_modules_bin(self, tmp_path: Path) -> None:
        _make_node_project(tmp_path, {"test": "jest"})
        bin_dir = tmp_path / "node_modules" / ".bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "eslint").write_text("#!/bin/sh\n")
        adapter = _FakeAdapter(response=json.dumps({"lint": "npx --yes eslint ."}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True
        config = build_mechanical_config(tmp_path)
        assert config.lint_command == ("npx", "--yes", "eslint", ".")

    def test_npx_scoped_package_matches_dependency(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(
            json.dumps({"scripts": {}, "devDependencies": {"@biomejs/biome": "^1.0.0"}})
        )
        adapter = _FakeAdapter(response=json.dumps({"lint": "npx @biomejs/biome check ."}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True
        config = build_mechanical_config(tmp_path)
        assert config.lint_command == ("npx", "@biomejs/biome", "check", ".")


class TestNodePackageManagerValidation:
    """`yarn typecheck`, `pnpm check`, `bun foo` must reference a real script."""

    def test_yarn_typecheck_dropped_when_script_absent(self, tmp_path: Path) -> None:
        _make_node_project(tmp_path, {"test": "jest"})
        adapter = _FakeAdapter(response=json.dumps({"static": "yarn typecheck"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False
        assert not has_mechanical_toml(tmp_path)

    def test_yarn_typecheck_accepted_when_script_present(self, tmp_path: Path) -> None:
        _make_node_project(tmp_path, {"test": "jest", "typecheck": "tsc --noEmit"})
        adapter = _FakeAdapter(response=json.dumps({"static": "yarn typecheck"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True
        config = build_mechanical_config(tmp_path)
        assert config.static_command == ("yarn", "typecheck")

    def test_pnpm_check_dropped_when_script_absent(self, tmp_path: Path) -> None:
        _make_node_project(tmp_path, {"test": "jest"})
        adapter = _FakeAdapter(response=json.dumps({"lint": "pnpm check"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_bun_foo_dropped_when_script_absent(self, tmp_path: Path) -> None:
        _make_node_project(tmp_path, {"test": "bun test"})
        adapter = _FakeAdapter(response=json.dumps({"lint": "bun foo"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_npm_bare_subcommand_dropped(self, tmp_path: Path) -> None:
        """`npm typecheck` is NOT a script shortcut — only `npm run typecheck` is."""
        _make_node_project(tmp_path, {"typecheck": "tsc --noEmit"})
        adapter = _FakeAdapter(response=json.dumps({"static": "npm typecheck"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_npm_test_is_lifecycle_shortcut(self, tmp_path: Path) -> None:
        _make_node_project(tmp_path, {"test": "jest"})
        adapter = _FakeAdapter(response=json.dumps({"test": "npm test"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True
        assert build_mechanical_config(tmp_path).test_command == ("npm", "test")

    def test_pnpm_install_not_treated_as_script(self, tmp_path: Path) -> None:
        """Built-in pm commands must not be validated as scripts — drop them."""
        (tmp_path / "package.json").write_text(json.dumps({"scripts": {"install": "echo hi"}}))
        adapter = _FakeAdapter(response=json.dumps({"build": "pnpm install"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False


class TestTomlSerialization:
    def test_commands_with_quotes_roundtrip(self, tmp_path: Path) -> None:
        """Commands containing ``"`` must survive the toml round-trip.

        Previous implementation wrote ``test = "pytest -k "slow""`` which is
        malformed TOML; the escaped serializer must produce a readable file.
        """
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')
        adapter = _FakeAdapter(response=json.dumps({"test": 'pytest -k "slow"'}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True
        body = toml_path(tmp_path).read_text()
        assert 'test = "pytest -k \\"slow\\""' in body
        config = build_mechanical_config(tmp_path)
        assert config.test_command == ("pytest", "-k", "slow")


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
