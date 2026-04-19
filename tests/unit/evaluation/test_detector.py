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


class TestMavenWrapper:
    """`./mvnw` / `./gradlew` wrappers must keep working for pre-PR projects."""

    def test_mvnw_accepted_when_wrapper_and_pom_exist(self, tmp_path: Path) -> None:
        (tmp_path / "pom.xml").write_text("<project/>")
        (tmp_path / "mvnw").write_text("#!/bin/sh\n")
        adapter = _FakeAdapter(response=json.dumps({"test": "./mvnw test"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True
        config = build_mechanical_config(tmp_path)
        assert config.test_command == ("./mvnw", "test")

    def test_mvnw_dropped_when_wrapper_missing(self, tmp_path: Path) -> None:
        (tmp_path / "pom.xml").write_text("<project/>")
        adapter = _FakeAdapter(response=json.dumps({"test": "./mvnw test"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_gradlew_accepted_when_wrapper_and_build_gradle_exist(self, tmp_path: Path) -> None:
        (tmp_path / "build.gradle.kts").write_text("")
        (tmp_path / "gradlew").write_text("#!/bin/sh\n")
        adapter = _FakeAdapter(response=json.dumps({"build": "./gradlew build"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True
        config = build_mechanical_config(tmp_path)
        assert config.build_command == ("./gradlew", "build")


class TestBackendModelResolution:
    """The detector must delegate model selection to the config resolver."""

    def test_model_defaults_to_resolver_when_none_provided(self, tmp_path: Path) -> None:
        """When ``model`` is None the resolver supplies a backend-safe model."""
        from unittest.mock import patch

        _make_node_project(tmp_path, {"test": "jest"})
        adapter = _FakeAdapter(response=json.dumps({"test": "npm test"}))
        with patch(
            "ouroboros.config.loader.get_mechanical_detector_model",
            return_value="sentinel-model",
        ) as resolver:
            ok = _run(ensure_mechanical_toml(tmp_path, adapter, backend="codex"))
        assert ok is True
        resolver.assert_called_once_with(backend="codex")
        assert adapter.calls, "detector should have invoked the adapter"
        _messages, config = adapter.calls[0]
        assert config.model == "sentinel-model"

    def test_explicit_model_overrides_resolver(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        _make_node_project(tmp_path, {"test": "jest"})
        adapter = _FakeAdapter(response=json.dumps({"test": "npm test"}))
        with patch(
            "ouroboros.config.loader.get_mechanical_detector_model",
            side_effect=AssertionError("resolver must not be called"),
        ):
            ok = _run(ensure_mechanical_toml(tmp_path, adapter, model="explicit-model"))
        assert ok is True
        _messages, config = adapter.calls[0]
        assert config.model == "explicit-model"


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


class TestToolchainSubcommandValidation:
    """`uv run <tool>` / `cargo <sub>` / `go <sub>` must prove the tool exists."""

    def test_uv_run_dropped_when_tool_missing(self, tmp_path: Path) -> None:
        """`uv run pyright` with no pyright dependency or binary is dropped."""
        from unittest.mock import patch

        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = ["requests"]\n'
        )
        adapter = _FakeAdapter(response=json.dumps({"static": "uv run pyright ."}))
        with patch("ouroboros.evaluation.detector.shutil.which", return_value=None):
            ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False
        assert not has_mechanical_toml(tmp_path)

    def test_uv_run_accepted_when_dependency_declared(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = ["pyright>=1.0"]\n'
        )
        adapter = _FakeAdapter(response=json.dumps({"static": "uv run pyright ."}))
        with patch("ouroboros.evaluation.detector.shutil.which", return_value=None):
            ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True
        assert build_mechanical_config(tmp_path).static_command == ("uv", "run", "pyright", ".")

    def test_uv_run_accepted_when_dep_group_declares_tool(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "demo"\n[dependency-groups]\ndev = ["pyright==1.1", "pytest"]\n'
        )
        adapter = _FakeAdapter(response=json.dumps({"static": "uv run pyright"}))
        with patch("ouroboros.evaluation.detector.shutil.which", return_value=None):
            ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_uv_run_accepted_when_tool_in_venv(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')
        (tmp_path / ".venv" / "bin").mkdir(parents=True)
        (tmp_path / ".venv" / "bin" / "pyright").write_text("")
        adapter = _FakeAdapter(response=json.dumps({"static": "uv run pyright"}))
        with patch("ouroboros.evaluation.detector.shutil.which", return_value=None):
            ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_cargo_unknown_subcommand_dropped(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        (tmp_path / "Cargo.toml").write_text('[package]\nname = "demo"\n')
        adapter = _FakeAdapter(response=json.dumps({"test": "cargo nextest run"}))
        with patch("ouroboros.evaluation.detector.shutil.which", return_value=None):
            ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_cargo_extension_accepted_when_binary_on_path(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        (tmp_path / "Cargo.toml").write_text('[package]\nname = "demo"\n')
        adapter = _FakeAdapter(response=json.dumps({"test": "cargo nextest run"}))

        def fake_which(name: str) -> str | None:
            return "/usr/local/bin/cargo-nextest" if name == "cargo-nextest" else None

        with patch("ouroboros.evaluation.detector.shutil.which", side_effect=fake_which):
            ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_cargo_builtin_subcommand_accepted(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text('[package]\nname = "demo"\n')
        adapter = _FakeAdapter(response=json.dumps({"test": "cargo test --workspace"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_go_non_builtin_subcommand_dropped(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").write_text("module demo\n")
        adapter = _FakeAdapter(response=json.dumps({"lint": "go lint ./..."}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_go_builtin_subcommand_accepted(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").write_text("module demo\n")
        adapter = _FakeAdapter(response=json.dumps({"test": "go test ./..."}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_zig_non_builtin_subcommand_dropped(self, tmp_path: Path) -> None:
        (tmp_path / "build.zig").write_text("")
        adapter = _FakeAdapter(response=json.dumps({"lint": "zig lint"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False


class TestJustValidation:
    """`just` commands must reference a recipe declared in the justfile."""

    def test_just_recipe_accepted_when_declared(self, tmp_path: Path) -> None:
        (tmp_path / "justfile").write_text("test:\n    pytest\n\nbuild:\n    python -m build\n")
        adapter = _FakeAdapter(response=json.dumps({"test": "just test"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True
        assert build_mechanical_config(tmp_path).test_command == ("just", "test")

    def test_just_recipe_dropped_when_missing(self, tmp_path: Path) -> None:
        (tmp_path / "justfile").write_text("build:\n    python -m build\n")
        adapter = _FakeAdapter(response=json.dumps({"test": "just test"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_just_accepts_quiet_recipe_prefix(self, tmp_path: Path) -> None:
        (tmp_path / "justfile").write_text("@fast-test:\n    pytest -x\n")
        adapter = _FakeAdapter(response=json.dumps({"test": "just fast-test"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_just_recipe_with_args_accepted(self, tmp_path: Path) -> None:
        (tmp_path / "justfile").write_text("lint tag='latest':\n    docker build\n")
        adapter = _FakeAdapter(response=json.dumps({"lint": "just lint"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True


class TestBunRuntimeBuiltins:
    """`bun test` / `bun build` / `bun x` are Bun runtime builtins, not scripts."""

    def test_bun_test_accepted_without_scripts(self, tmp_path: Path) -> None:
        """`bun test` uses Bun's built-in test runner; no scripts entry needed."""
        (tmp_path / "package.json").write_text('{"name": "demo"}')
        adapter = _FakeAdapter(response=json.dumps({"test": "bun test"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True
        assert build_mechanical_config(tmp_path).test_command == ("bun", "test")

    def test_bun_build_accepted_without_scripts(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text('{"name": "demo"}')
        adapter = _FakeAdapter(response=json.dumps({"build": "bun build ./index.ts"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_bun_run_still_requires_script(self, tmp_path: Path) -> None:
        """`bun run <script>` still follows the script-lookup contract."""
        _make_node_project(tmp_path, {"lint": "eslint ."})
        adapter = _FakeAdapter(response=json.dumps({"lint": "bun run lint"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_bun_run_dropped_when_script_missing(self, tmp_path: Path) -> None:
        _make_node_project(tmp_path, {"test": "bun test"})
        adapter = _FakeAdapter(response=json.dumps({"lint": "bun run lint"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False


class TestUvRunOptionParsing:
    """`uv run --group dev pytest` must parse ``pytest`` as the tool."""

    def test_uv_run_with_group_option_parses_tool_correctly(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "demo"\n[dependency-groups]\ndev = ["pytest>=8"]\n'
        )
        adapter = _FakeAdapter(response=json.dumps({"test": "uv run --group dev pytest -q"}))
        with patch("ouroboros.evaluation.detector.shutil.which", return_value=None):
            ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True
        assert build_mechanical_config(tmp_path).test_command == (
            "uv",
            "run",
            "--group",
            "dev",
            "pytest",
            "-q",
        )

    def test_uv_run_with_provides_tool(self, tmp_path: Path) -> None:
        """`uv run --with pytest pytest` is valid even without pytest declared."""
        from unittest.mock import patch

        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')
        adapter = _FakeAdapter(response=json.dumps({"test": "uv run --with pytest pytest -q"}))
        with patch("ouroboros.evaluation.detector.shutil.which", return_value=None):
            ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_uv_run_inline_equals_option_value(self, tmp_path: Path) -> None:
        """`--with=pytest` (inline) is self-contained."""
        from unittest.mock import patch

        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')
        adapter = _FakeAdapter(response=json.dumps({"test": "uv run --with=pytest pytest"}))
        with patch("ouroboros.evaluation.detector.shutil.which", return_value=None):
            ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True


class TestBunXValidation:
    """`bun x` must not be treated as a self-contained builtin."""

    def test_bun_x_dropped_when_package_not_declared(self, tmp_path: Path) -> None:
        """`bun x biome check` with biome not in deps → dropped (no remote exec)."""
        (tmp_path / "package.json").write_text('{"name": "demo"}')
        adapter = _FakeAdapter(response=json.dumps({"lint": "bun x biome check ."}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_bun_x_accepted_when_dependency_declared(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "demo", "devDependencies": {"biome": "^1"}})
        )
        adapter = _FakeAdapter(response=json.dumps({"lint": "bun x biome check ."}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True


class TestRepoCoupledRunners:
    """Host-installed binaries still need matching repo config to be accepted."""

    def test_gradle_dropped_without_build_gradle(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        (tmp_path / "package.json").write_text("{}")  # sanity — any manifest
        adapter = _FakeAdapter(response=json.dumps({"build": "gradle build"}))
        with patch("ouroboros.evaluation.detector.shutil.which", return_value="/opt/bin/gradle"):
            ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_gradle_accepted_with_build_gradle_kts(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        (tmp_path / "build.gradle.kts").write_text("")
        (tmp_path / "package.json").write_text("{}")
        adapter = _FakeAdapter(response=json.dumps({"build": "gradle build"}))
        with patch("ouroboros.evaluation.detector.shutil.which", return_value="/opt/bin/gradle"):
            ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_task_dropped_without_taskfile(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        (tmp_path / "package.json").write_text("{}")
        adapter = _FakeAdapter(response=json.dumps({"test": "task test"}))
        with patch("ouroboros.evaluation.detector.shutil.which", return_value="/opt/bin/task"):
            ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_task_accepted_with_taskfile(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        (tmp_path / "Taskfile.yml").write_text("version: 3\n")
        (tmp_path / "package.json").write_text("{}")
        adapter = _FakeAdapter(response=json.dumps({"test": "task test"}))
        with patch("ouroboros.evaluation.detector.shutil.which", return_value="/opt/bin/task"):
            ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_rake_dropped_without_rakefile(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        (tmp_path / "package.json").write_text("{}")
        adapter = _FakeAdapter(response=json.dumps({"test": "rake test"}))
        with patch("ouroboros.evaluation.detector.shutil.which", return_value="/opt/bin/rake"):
            ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_rake_accepted_with_rakefile(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        (tmp_path / "Rakefile").write_text("task :test\n")
        (tmp_path / "package.json").write_text("{}")
        adapter = _FakeAdapter(response=json.dumps({"test": "rake test"}))
        with patch("ouroboros.evaluation.detector.shutil.which", return_value="/opt/bin/rake"):
            ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_phpunit_dropped_without_config(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        (tmp_path / "composer.json").is_file()  # intentionally absent
        (tmp_path / "package.json").write_text("{}")
        adapter = _FakeAdapter(response=json.dumps({"test": "phpunit --verbose"}))
        with patch("ouroboros.evaluation.detector.shutil.which", return_value="/opt/bin/phpunit"):
            ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False


class TestEvaluationPublicSurface:
    """Deprecated compat symbols must remain importable."""

    def test_language_preset_is_importable(self) -> None:
        from ouroboros.evaluation import LanguagePreset  # noqa: F401

    def test_detect_language_is_importable_and_returns_none(self, tmp_path: Path) -> None:
        from ouroboros.evaluation import detect_language

        assert detect_language(tmp_path) is None


class TestAutoDetectBackendPropagation:
    """_auto_detect_mechanical_toml must thread backend into adapter construction."""

    def test_default_adapter_inherits_backend(self, tmp_path: Path) -> None:
        """When no adapter is supplied, the default adapter is built for ``llm_backend``."""
        import asyncio
        from unittest.mock import AsyncMock, patch

        from ouroboros.evaluation.verification_artifacts import (
            _auto_detect_mechanical_toml,
        )

        calls: list[dict[str, object]] = []

        def fake_factory(**kwargs: object) -> object:
            calls.append(kwargs)
            return object()

        ensure_mock = AsyncMock(return_value=True)
        with (
            patch(
                "ouroboros.providers.factory.create_llm_adapter",
                side_effect=fake_factory,
            ),
            patch(
                "ouroboros.evaluation.verification_artifacts.ensure_mechanical_toml",
                new=ensure_mock,
            ),
        ):
            asyncio.run(_auto_detect_mechanical_toml(tmp_path, None, "codex"))
        assert calls and calls[0].get("backend") == "codex"


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
