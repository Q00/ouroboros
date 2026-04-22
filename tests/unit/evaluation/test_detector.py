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

    def test_force_preserves_old_toml_when_no_manifests(self, tmp_path: Path) -> None:
        """Refresh must not destroy the prior config before a replacement is ready."""
        (tmp_path / ".ouroboros").mkdir()
        old_body = 'test = "old command"\n'
        (tmp_path / ".ouroboros" / "mechanical.toml").write_text(old_body)
        # No manifests present → detector can propose nothing.
        adapter = _FakeAdapter(response="{}")
        ok = _run(ensure_mechanical_toml(tmp_path, adapter, force=True))
        assert ok is False
        assert toml_path(tmp_path).read_text() == old_body

    def test_force_preserves_old_toml_on_llm_failure(self, tmp_path: Path) -> None:
        (tmp_path / ".ouroboros").mkdir()
        old_body = 'test = "old command"\n'
        (tmp_path / ".ouroboros" / "mechanical.toml").write_text(old_body)
        _make_node_project(tmp_path, {"test": "jest"})
        adapter = _FakeAdapter(error=ProviderError("network"))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter, force=True))
        assert ok is False
        assert toml_path(tmp_path).read_text() == old_body

    def test_force_preserves_old_toml_when_all_proposals_dropped(self, tmp_path: Path) -> None:
        """Refresh whose proposal fails validation keeps the prior known-good file."""
        (tmp_path / ".ouroboros").mkdir()
        old_body = 'test = "old command"\n'
        (tmp_path / ".ouroboros" / "mechanical.toml").write_text(old_body)
        _make_node_project(tmp_path, {"test": "jest"})
        # Proposal references a script that does not exist → dropped.
        adapter = _FakeAdapter(response=json.dumps({"test": "npm run nonexistent"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter, force=True))
        assert ok is False
        assert toml_path(tmp_path).read_text() == old_body

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


class TestWrapperInvocationForm:
    """Build wrappers must be invoked via ``./name`` so execvp can resolve them."""

    def test_bare_mvnw_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "pom.xml").write_text("<project/>")
        (tmp_path / "mvnw").write_text("#!/bin/sh\n")
        adapter = _FakeAdapter(response=json.dumps({"test": "mvnw test"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_bare_gradlew_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "build.gradle.kts").write_text("")
        (tmp_path / "gradlew").write_text("#!/bin/sh\n")
        adapter = _FakeAdapter(response=json.dumps({"build": "gradlew build"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_windows_bat_wrapper_rejected_on_posix(self, tmp_path: Path) -> None:
        import os as _os

        if _os.name == "nt":
            pytest.skip("POSIX-only rejection test")
        (tmp_path / "pom.xml").write_text("<project/>")
        (tmp_path / "mvnw.cmd").write_text("")
        adapter = _FakeAdapter(response=json.dumps({"test": "mvnw.cmd test"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False


class TestMavenGoalAllowlist:
    """Only non-mutating Maven phases / goals may be persisted."""

    def test_mvn_deploy_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "pom.xml").write_text("<project/>")
        adapter = _FakeAdapter(response=json.dumps({"build": "mvn deploy"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_mvn_install_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "pom.xml").write_text("<project/>")
        adapter = _FakeAdapter(response=json.dumps({"build": "mvn install"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_mvn_release_goal_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "pom.xml").write_text("<project/>")
        adapter = _FakeAdapter(response=json.dumps({"build": "mvn release:perform"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_mvn_test_accepted(self, tmp_path: Path) -> None:
        (tmp_path / "pom.xml").write_text("<project/>")
        adapter = _FakeAdapter(response=json.dumps({"test": "mvn test"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_mvn_verify_accepted(self, tmp_path: Path) -> None:
        (tmp_path / "pom.xml").write_text("<project/>")
        adapter = _FakeAdapter(response=json.dumps({"build": "mvn verify"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_mvnw_versions_set_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "pom.xml").write_text("<project/>")
        (tmp_path / "mvnw").write_text("#!/bin/sh\n")
        adapter = _FakeAdapter(response=json.dumps({"build": "./mvnw versions:set"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False


class TestDotnetSubcommandAllowlist:
    """`dotnet publish`/`pack`/`nuget push` must not survive validation."""

    def test_dotnet_build_accepted_with_csproj_only(self, tmp_path: Path) -> None:
        """SDK-style repos with only ``*.csproj`` still seed the detector."""
        (tmp_path / "app.csproj").write_text("<Project/>")
        adapter = _FakeAdapter(response=json.dumps({"build": "dotnet build"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_dotnet_publish_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "global.json").write_text("{}")
        (tmp_path / "app.csproj").write_text("<Project/>")
        adapter = _FakeAdapter(response=json.dumps({"build": "dotnet publish"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_dotnet_pack_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "global.json").write_text("{}")
        (tmp_path / "app.csproj").write_text("<Project/>")
        adapter = _FakeAdapter(response=json.dumps({"build": "dotnet pack"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_dotnet_nuget_push_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "global.json").write_text("{}")
        (tmp_path / "app.csproj").write_text("<Project/>")
        adapter = _FakeAdapter(response=json.dumps({"build": "dotnet nuget push foo.nupkg"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_dotnet_build_accepted(self, tmp_path: Path) -> None:
        (tmp_path / "global.json").write_text("{}")
        (tmp_path / "app.csproj").write_text("<Project/>")
        adapter = _FakeAdapter(response=json.dumps({"build": "dotnet build"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_dotnet_test_accepted(self, tmp_path: Path) -> None:
        (tmp_path / "global.json").write_text("{}")
        (tmp_path / "app.sln").write_text("")
        adapter = _FakeAdapter(response=json.dumps({"test": "dotnet test"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_dotnet_subproject_path_accepted_without_root_marker(self, tmp_path: Path) -> None:
        app = tmp_path / "src" / "App"
        app.mkdir(parents=True)
        (app / "App.csproj").write_text("<Project/>")
        adapter = _FakeAdapter(response=json.dumps({"test": "dotnet test src/App/App.csproj"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_dotnet_subproject_path_must_exist(self, tmp_path: Path) -> None:
        app = tmp_path / "src" / "App"
        app.mkdir(parents=True)
        (app / "App.csproj").write_text("<Project/>")
        adapter = _FakeAdapter(response=json.dumps({"test": "dotnet test src/App/Missing.csproj"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False


class TestExecutablePathEscapes:
    """Relative-path escapes must be refused even when the basename is allowlisted."""

    def test_parent_directory_escape_dropped(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = ["pytest>=8"]\n'
        )
        adapter = _FakeAdapter(response=json.dumps({"test": "../../tmp/pytest -q"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_absolute_path_dropped(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = ["pytest>=8"]\n'
        )
        adapter = _FakeAdapter(response=json.dumps({"test": "/usr/bin/pytest -q"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_home_relative_path_dropped(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = ["pytest>=8"]\n'
        )
        adapter = _FakeAdapter(response=json.dumps({"test": "~/bin/pytest"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_parent_escape_with_mvnw_basename_dropped(self, tmp_path: Path) -> None:
        """Even with pom.xml and mvnw in place, ../ escapes must not slip through."""
        (tmp_path / "pom.xml").write_text("<project/>")
        (tmp_path / "mvnw").write_text("#!/bin/sh\n")
        adapter = _FakeAdapter(response=json.dumps({"test": "../../tmp/mvnw test"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_cargo_manifest_path_parent_escape_dropped(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        other = tmp_path / "other"
        repo.mkdir()
        other.mkdir()
        (repo / "Cargo.toml").write_text('[package]\nname = "demo"\n')
        (other / "Cargo.toml").write_text('[package]\nname = "other"\n')
        adapter = _FakeAdapter(
            response=json.dumps({"test": "cargo test --manifest-path ../other/Cargo.toml"})
        )
        ok = _run(ensure_mechanical_toml(repo, adapter))
        assert ok is False

    def test_pytest_path_parent_escape_dropped(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        other = tmp_path / "other"
        repo.mkdir()
        other.mkdir()
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = ["pytest>=8"]\n'
        )
        adapter = _FakeAdapter(response=json.dumps({"test": "pytest ../other"}))
        ok = _run(ensure_mechanical_toml(repo, adapter))
        assert ok is False

    def test_pytest_absolute_path_argument_dropped(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = ["pytest>=8"]\n'
        )
        adapter = _FakeAdapter(response=json.dumps({"test": "pytest /tmp/other"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_cargo_manifest_path_absolute_value_dropped(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text('[package]\nname = "demo"\n')
        adapter = _FakeAdapter(
            response=json.dumps({"test": "cargo test --manifest-path=/tmp/Cargo.toml"})
        )
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_python_m_pytest_path_parent_escape_dropped(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        other = tmp_path / "other"
        repo.mkdir()
        other.mkdir()
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = ["pytest>=8"]\n'
        )
        adapter = _FakeAdapter(response=json.dumps({"test": "python -m pytest ../other"}))
        ok = _run(ensure_mechanical_toml(repo, adapter))
        assert ok is False

    def test_pytest_in_repo_path_argument_allowed(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = ["pytest>=8"]\n'
        )
        (tmp_path / "tests").mkdir()
        adapter = _FakeAdapter(response=json.dumps({"test": "pytest tests -q"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_path_argument_symlink_escape_dropped(self, tmp_path: Path) -> None:
        if not hasattr(Path, "symlink_to"):
            pytest.skip("symlink support unavailable")
        repo = tmp_path / "repo"
        other = tmp_path / "other"
        repo.mkdir()
        other.mkdir()
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = ["pytest>=8"]\n'
        )
        link = repo / "outside"
        try:
            link.symlink_to(other, target_is_directory=True)
        except OSError:
            pytest.skip("symlink creation unavailable")
        adapter = _FakeAdapter(response=json.dumps({"test": "pytest outside"}))
        ok = _run(ensure_mechanical_toml(repo, adapter))
        assert ok is False


class TestFormatterCheckMode:
    """`cargo fmt` / `zig fmt` rewrite sources unless ``--check`` is set."""

    def test_cargo_fmt_without_check_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text('[package]\nname = "demo"\n')
        adapter = _FakeAdapter(response=json.dumps({"lint": "cargo fmt"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_cargo_fmt_with_check_accepted(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text('[package]\nname = "demo"\n')
        adapter = _FakeAdapter(response=json.dumps({"lint": "cargo fmt -- --check"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_zig_fmt_without_check_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "build.zig").write_text("")
        adapter = _FakeAdapter(response=json.dumps({"lint": "zig fmt src"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_zig_fmt_with_check_accepted(self, tmp_path: Path) -> None:
        (tmp_path / "build.zig").write_text("")
        adapter = _FakeAdapter(response=json.dumps({"lint": "zig fmt --check src"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True


class TestUvSubdirectoryProject:
    """`uv run --directory backend` must validate against backend/pyproject.toml."""

    def test_uv_run_directory_subpackage_accepted(self, tmp_path: Path) -> None:
        (tmp_path / "backend").mkdir()
        (tmp_path / "backend" / "pyproject.toml").write_text(
            '[project]\nname = "svc"\ndependencies = ["pytest>=8"]\n'
        )
        # Root also needs *some* manifest to get through ``_collect_manifests``.
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "root"\n')
        adapter = _FakeAdapter(response=json.dumps({"test": "uv run --directory backend pytest"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_uv_run_project_flag_accepted(self, tmp_path: Path) -> None:
        (tmp_path / "backend").mkdir()
        (tmp_path / "backend" / "pyproject.toml").write_text(
            '[project]\nname = "svc"\ndependencies = ["ruff>=0.7"]\n'
        )
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "root"\n')
        adapter = _FakeAdapter(
            response=json.dumps({"lint": "uv run --project=backend ruff check ."})
        )
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_uv_global_project_flag_before_subcommand(self, tmp_path: Path) -> None:
        """``uv --project backend run pytest`` resolves to ``run`` subcommand."""
        (tmp_path / "backend").mkdir()
        (tmp_path / "backend" / "pyproject.toml").write_text(
            '[project]\nname = "svc"\ndependencies = ["pytest>=8"]\n'
        )
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "root"\n')
        adapter = _FakeAdapter(response=json.dumps({"test": "uv --project backend run pytest"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_uv_run_directory_without_subpackage_manifest_dropped(self, tmp_path: Path) -> None:
        (tmp_path / "backend").mkdir()
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "root"\n')
        adapter = _FakeAdapter(response=json.dumps({"test": "uv run --directory backend pytest"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_uv_directory_escaping_repo_dropped(self, tmp_path: Path) -> None:
        """``--directory ../other`` must not validate against a sibling checkout."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "pyproject.toml").write_text('[project]\nname = "root"\n')
        other = tmp_path / "other"
        other.mkdir()
        (other / "pyproject.toml").write_text(
            '[project]\nname = "evil"\ndependencies = ["pytest>=8"]\n'
        )
        adapter = _FakeAdapter(response=json.dumps({"test": "uv run --directory ../other pytest"}))
        ok = _run(ensure_mechanical_toml(repo, adapter))
        assert ok is False

    def test_uv_project_absolute_path_dropped(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "root"\n')
        adapter = _FakeAdapter(response=json.dumps({"test": "uv --project=/tmp/other run pytest"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False


class TestPathPrefixedExecutableExists:
    """Path-prefixed executables must exist in the repo, not just match basename."""

    def test_bin_pytest_dropped_when_file_missing(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = ["pytest>=8"]\n'
        )
        adapter = _FakeAdapter(response=json.dumps({"test": "bin/pytest -q"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_tools_eslint_dropped_when_file_missing(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "demo", "devDependencies": {"eslint": "^9"}})
        )
        adapter = _FakeAdapter(response=json.dumps({"lint": "tools/eslint ."}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False


class TestDotnetMsbuildRejected:
    """`dotnet msbuild /t:Publish` must not be accepted as Stage 1."""

    def test_msbuild_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "global.json").write_text("{}")
        (tmp_path / "app.csproj").write_text("<Project/>")
        adapter = _FakeAdapter(response=json.dumps({"build": "dotnet msbuild /t:Publish"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False


class TestDestructiveTargetsBlocked:
    """Task runners must reject deploy/publish/release style targets."""

    def test_make_deploy_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "Makefile").write_text("deploy:\n\techo deploying\n")
        (tmp_path / "package.json").write_text("{}")
        adapter = _FakeAdapter(response=json.dumps({"build": "make deploy"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_just_release_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "justfile").write_text("release:\n    cargo publish\n")
        adapter = _FakeAdapter(response=json.dumps({"build": "just release"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_task_publish_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "Taskfile.yml").write_text(
            "version: '3'\ntasks:\n  publish:\n    cmds:\n      - echo publishing\n"
        )
        adapter = _FakeAdapter(response=json.dumps({"build": "task publish"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_gradle_publish_rejected_even_if_declared(self, tmp_path: Path) -> None:
        (tmp_path / "build.gradle.kts").write_text(
            'plugins { java }\n\ntasks.register("publish") {}\n'
        )
        adapter = _FakeAdapter(response=json.dumps({"build": "gradle publish"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_rake_deploy_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "Rakefile").write_text("task :deploy do\nend\n")
        adapter = _FakeAdapter(response=json.dumps({"build": "rake deploy"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_bundle_exec_capistrano_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "Gemfile").write_text('gem "capistrano"\n')
        adapter = _FakeAdapter(response=json.dumps({"build": "bundle exec capistrano deploy"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_suffix_match_deploy_staging_rejected(self, tmp_path: Path) -> None:
        """Compound names like ``deploy-staging`` must also be caught."""
        (tmp_path / "Makefile").write_text("deploy-staging:\n\techo\n")
        (tmp_path / "package.json").write_text("{}")
        adapter = _FakeAdapter(response=json.dumps({"build": "make deploy-staging"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False


class TestGmakeValidation:
    """`gmake` must enforce the same explicit-target contract as ``make``."""

    def test_bare_gmake_rejected_even_with_makefile(self, tmp_path: Path) -> None:
        (tmp_path / "Makefile").write_text("all:\n\techo ok\n")
        (tmp_path / "package.json").write_text("{}")
        adapter = _FakeAdapter(response=json.dumps({"build": "gmake"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_gmake_with_declared_target_accepted(self, tmp_path: Path) -> None:
        (tmp_path / "Makefile").write_text("test:\n\tpytest\n")
        (tmp_path / "package.json").write_text("{}")
        adapter = _FakeAdapter(response=json.dumps({"test": "gmake test"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_gmake_with_undeclared_target_dropped(self, tmp_path: Path) -> None:
        (tmp_path / "Makefile").write_text("build:\n\techo\n")
        (tmp_path / "package.json").write_text("{}")
        adapter = _FakeAdapter(response=json.dumps({"test": "gmake test"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_gmake_destructive_target_rejected(self, tmp_path: Path) -> None:
        """`gmake deploy` must be dropped even when the Makefile declares it."""
        (tmp_path / "Makefile").write_text("deploy:\n\techo deploying\n")
        (tmp_path / "package.json").write_text("{}")
        adapter = _FakeAdapter(response=json.dumps({"build": "gmake deploy"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False


class TestComposerBuiltinPrecedence:
    """`composer install` must never validate, even if scripts.install exists."""

    def test_composer_install_rejected_even_with_shadow_script(self, tmp_path: Path) -> None:
        (tmp_path / "composer.json").write_text(
            json.dumps({"scripts": {"install": "echo shadow", "test": "phpunit"}})
        )
        adapter = _FakeAdapter(response=json.dumps({"build": "composer install"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_composer_update_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "composer.json").write_text(json.dumps({"scripts": {"update": "echo shadow"}}))
        adapter = _FakeAdapter(response=json.dumps({"build": "composer update"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False


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

    def test_cargo_extension_is_host_dependent_and_dropped(self, tmp_path: Path) -> None:
        """``cargo nextest`` is host-installed — PATH must not unlock it."""
        from unittest.mock import patch

        (tmp_path / "Cargo.toml").write_text('[package]\nname = "demo"\n')
        adapter = _FakeAdapter(response=json.dumps({"test": "cargo nextest run"}))

        def fake_which(name: str) -> str | None:
            return "/usr/local/bin/cargo-nextest" if name == "cargo-nextest" else None

        with patch("ouroboros.evaluation.detector.shutil.which", side_effect=fake_which):
            ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_cargo_builtin_subcommand_accepted(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text('[package]\nname = "demo"\n')
        adapter = _FakeAdapter(response=json.dumps({"test": "cargo test --workspace"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_cargo_manifest_path_subproject_accepted_without_root_marker(
        self, tmp_path: Path
    ) -> None:
        crate = tmp_path / "crates" / "demo"
        crate.mkdir(parents=True)
        (crate / "Cargo.toml").write_text('[package]\nname = "demo"\n')
        adapter = _FakeAdapter(
            response=json.dumps({"test": "cargo test --manifest-path crates/demo/Cargo.toml"})
        )
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_cargo_without_manifest_path_still_requires_root_marker(self, tmp_path: Path) -> None:
        crate = tmp_path / "crates" / "demo"
        crate.mkdir(parents=True)
        (crate / "Cargo.toml").write_text('[package]\nname = "demo"\n')
        adapter = _FakeAdapter(response=json.dumps({"test": "cargo test"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

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

    def test_uv_run_tool_on_host_path_is_dropped(self, tmp_path: Path) -> None:
        """Host-only pyright is not repo-coupled → drop even if on PATH."""
        from unittest.mock import patch

        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')
        adapter = _FakeAdapter(response=json.dumps({"static": "uv run pyright ."}))
        with patch(
            "ouroboros.evaluation.detector.shutil.which",
            return_value="/usr/bin/pyright",
        ):
            ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_uv_run_tool_in_requirements_txt_is_accepted(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')
        (tmp_path / "requirements-dev.txt").write_text("pyright==1.1\n")
        adapter = _FakeAdapter(response=json.dumps({"static": "uv run pyright"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_uv_run_dash_m_module_accepted(self, tmp_path: Path) -> None:
        """``uv run -m pytest`` treats the module as the tool, not a skip value."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = ["pytest>=8"]\n'
        )
        adapter = _FakeAdapter(response=json.dumps({"test": "uv run -m pytest -q"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True
        assert build_mechanical_config(tmp_path).test_command == (
            "uv",
            "run",
            "-m",
            "pytest",
            "-q",
        )

    def test_uv_run_module_long_form_accepted(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = ["pyright>=1"]\n'
        )
        adapter = _FakeAdapter(response=json.dumps({"static": "uv run --module pyright"}))
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


class TestBunProjectMarker:
    """`bun test` must require Bun project evidence, not just any manifest."""

    def test_bun_test_dropped_without_bun_marker(self, tmp_path: Path) -> None:
        """Python-only repo with no package.json → bun commands dropped."""
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')
        adapter = _FakeAdapter(response=json.dumps({"test": "bun test"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_bun_test_accepted_with_package_json(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text('{"name": "demo"}')
        adapter = _FakeAdapter(response=json.dumps({"test": "bun test"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_bun_test_accepted_with_bun_lockb(self, tmp_path: Path) -> None:
        (tmp_path / "bun.lockb").write_bytes(b"")
        (tmp_path / "package.json").write_text('{"name": "demo"}')
        adapter = _FakeAdapter(response=json.dumps({"test": "bun test"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_bun_test_accepted_without_scripts_entry(self, tmp_path: Path) -> None:
        """Regression: builtin ``bun test`` must not fall through to script lookup.

        Earlier structure returned ``True`` from ``_bun_builtin_runner`` but
        the function continued into the shared Node ``scripts`` check and
        silently rejected the command when ``package.json.scripts.test`` was
        absent. The branch is now self-contained.
        """
        (tmp_path / "package.json").write_text(
            '{"name": "demo", "devDependencies": {"bun-types": "^1"}}'
        )
        # Note: no ``scripts`` field at all.
        adapter = _FakeAdapter(response=json.dumps({"test": "bun test"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True
        assert build_mechanical_config(tmp_path).test_command == ("bun", "test")

    def test_bun_custom_script_still_requires_declaration(self, tmp_path: Path) -> None:
        """Non-builtin bun subcommands must still prove the script exists."""
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "demo", "scripts": {"lint": "eslint ."}})
        )
        adapter = _FakeAdapter(response=json.dumps({"lint": "bun lint"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True
        adapter_missing = _FakeAdapter(response=json.dumps({"test": "bun typecheck"}))
        ok_missing = _run(ensure_mechanical_toml(tmp_path, adapter_missing, force=True))
        assert ok_missing is False


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

        (tmp_path / "Taskfile.yml").write_text(
            "version: '3'\ntasks:\n  test:\n    cmds:\n      - pytest\n"
        )
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


class TestBareToolRepoCoupling:
    """Bare binaries must be declared in a repo manifest, not just on PATH."""

    def test_bare_pytest_dropped_when_not_declared(self, tmp_path: Path) -> None:
        """Host-installed pytest alone is not enough to write to mechanical.toml."""
        from unittest.mock import patch

        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')
        adapter = _FakeAdapter(response=json.dumps({"test": "pytest -q"}))
        with patch("ouroboros.evaluation.detector.shutil.which", return_value="/opt/bin/pytest"):
            ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_bare_pytest_accepted_when_in_pyproject(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = ["pytest>=8"]\n'
        )
        adapter = _FakeAdapter(response=json.dumps({"test": "pytest -q"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_bare_pytest_accepted_when_in_requirements_txt(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')
        (tmp_path / "requirements-dev.txt").write_text("pytest==8.0\ncoverage\n")
        adapter = _FakeAdapter(response=json.dumps({"test": "pytest -q"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_bare_eslint_dropped_when_not_declared(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        (tmp_path / "package.json").write_text('{"name": "demo"}')
        adapter = _FakeAdapter(response=json.dumps({"lint": "eslint ."}))
        with patch("ouroboros.evaluation.detector.shutil.which", return_value="/opt/bin/eslint"):
            ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_bare_eslint_accepted_when_in_package_json(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "demo", "devDependencies": {"eslint": "^9"}})
        )
        adapter = _FakeAdapter(response=json.dumps({"lint": "eslint ."}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_bare_eslint_accepted_when_in_node_modules_bin(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text('{"name": "demo"}')
        (tmp_path / "node_modules" / ".bin").mkdir(parents=True)
        (tmp_path / "node_modules" / ".bin" / "eslint").write_text("")
        adapter = _FakeAdapter(response=json.dumps({"lint": "eslint ."}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True


class TestPythonModuleValidation:
    """`python -m <module>` must be validated against repo state / stdlib."""

    def test_python_without_m_flag_dropped(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')
        adapter = _FakeAdapter(response=json.dumps({"test": "python script.py"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_python_m_unittest_accepted_as_stdlib(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')
        adapter = _FakeAdapter(response=json.dumps({"test": "python -m unittest"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_python_m_doctest_with_args_accepted(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')
        adapter = _FakeAdapter(response=json.dumps({"test": "python -m doctest -v module.py"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_python_m_pytest_accepted_when_declared(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = ["pytest>=8"]\n'
        )
        adapter = _FakeAdapter(response=json.dumps({"test": "python -m pytest -q"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_python_m_unknown_module_dropped(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')
        adapter = _FakeAdapter(response=json.dumps({"test": "python -m mystery"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False


class TestPackageManagerStateChanges:
    """State-mutating subcommands must never reach ``mechanical.toml``."""

    def test_cargo_install_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text('[package]\nname = "demo"\n')
        adapter = _FakeAdapter(response=json.dumps({"test": "cargo install demo"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_cargo_publish_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text('[package]\nname = "demo"\n')
        adapter = _FakeAdapter(response=json.dumps({"static": "cargo publish"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_cargo_update_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text('[package]\nname = "demo"\n')
        adapter = _FakeAdapter(response=json.dumps({"test": "cargo update"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_go_install_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").write_text("module demo\n")
        adapter = _FakeAdapter(response=json.dumps({"build": "go install ./..."}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_go_get_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").write_text("module demo\n")
        adapter = _FakeAdapter(response=json.dumps({"build": "go get example.com/foo"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_uv_sync_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')
        adapter = _FakeAdapter(response=json.dumps({"build": "uv sync"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_uv_pip_install_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')
        adapter = _FakeAdapter(response=json.dumps({"build": "uv pip install requests"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_uv_tree_accepted(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')
        adapter = _FakeAdapter(response=json.dumps({"static": "uv tree"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_uv_tree_without_pyproject_rejected(self, tmp_path: Path) -> None:
        """Even read-only ``uv tree`` needs a Python project to act on."""
        # Seed a non-Python manifest so the detector runs at all.
        (tmp_path / "package.json").write_text('{"name": "demo"}')
        adapter = _FakeAdapter(response=json.dumps({"static": "uv tree"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False


class TestManifestCandidates:
    """Common project-manifest filename variants must seed the LLM call."""

    def test_canonical_justfile_detects(self, tmp_path: Path) -> None:
        (tmp_path / "Justfile").write_text("test:\n    pytest\n")
        adapter = _FakeAdapter(response=json.dumps({"test": "just test"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_yaml_taskfile_detects(self, tmp_path: Path) -> None:
        (tmp_path / "Taskfile.yaml").write_text(
            "version: '3'\ntasks:\n  test:\n    cmds:\n      - pytest\n"
        )
        from unittest.mock import patch

        adapter = _FakeAdapter(response=json.dumps({"test": "task test"}))
        with patch("ouroboros.evaluation.detector.shutil.which", return_value="/opt/bin/task"):
            ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_workspace_bazel_detects(self, tmp_path: Path) -> None:
        (tmp_path / "WORKSPACE.bazel").write_text("# bazel workspace\n")
        adapter = _FakeAdapter(response=json.dumps({"test": "bazel test //..."}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True


class TestPythonPackageManagerRun:
    """`poetry run <tool>` / `pdm run` / `hatch run` must validate the tool."""

    def test_poetry_run_dropped_when_tool_not_declared(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')
        adapter = _FakeAdapter(response=json.dumps({"static": "poetry run pyright"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_poetry_run_accepted_when_declared(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = ["pyright>=1"]\n'
        )
        adapter = _FakeAdapter(response=json.dumps({"static": "poetry run pyright"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_poetry_lock_rejected_as_mutation(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')
        adapter = _FakeAdapter(response=json.dumps({"build": "poetry lock"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_pdm_run_requires_declaration(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')
        adapter = _FakeAdapter(response=json.dumps({"test": "pdm run pytest"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False


class TestWorkspaceScopedPMCommands:
    """Workspace-scoped commands resolve against the target workspace, not root."""

    def _make_workspace(self, tmp_path: Path, ws_name: str, script: str) -> None:
        """Create a ``packages/<ws_name>`` package.json with ``scripts[script]``."""
        ws_dir = tmp_path / "packages" / ws_name
        ws_dir.mkdir(parents=True)
        (ws_dir / "package.json").write_text(
            json.dumps({"name": ws_name, "scripts": {script: "echo ok"}})
        )

    def test_pnpm_filter_resolves_to_workspace_script(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "root", "workspaces": ["packages/*"]})
        )
        self._make_workspace(tmp_path, "web", "test")
        adapter = _FakeAdapter(response=json.dumps({"test": "pnpm --filter web test"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_pnpm_filter_dropped_when_workspace_missing_script(self, tmp_path: Path) -> None:
        """Root having ``scripts.test`` is not a valid excuse when workspace drops it."""
        (tmp_path / "package.json").write_text(
            json.dumps(
                {
                    "name": "root",
                    "workspaces": ["packages/*"],
                    "scripts": {"test": "echo root"},
                }
            )
        )
        self._make_workspace(tmp_path, "web", "lint")  # no ``test`` in web
        adapter = _FakeAdapter(response=json.dumps({"test": "pnpm --filter web test"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_npm_workspace_flag_resolves_to_workspace_script(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "root", "workspaces": ["packages/*"]})
        )
        self._make_workspace(tmp_path, "api", "test")
        adapter = _FakeAdapter(response=json.dumps({"test": "npm --workspace api test"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_yarn_workspace_keyword_resolves_to_workspace_script(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "root", "workspaces": ["packages/*"]})
        )
        self._make_workspace(tmp_path, "web", "test")
        adapter = _FakeAdapter(response=json.dumps({"test": "yarn workspace web test"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_pnpm_inline_filter_equals_form(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "root", "workspaces": ["packages/*"]})
        )
        self._make_workspace(tmp_path, "web", "lint")
        adapter = _FakeAdapter(response=json.dumps({"lint": "pnpm --filter=web lint"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_yarn_workspaces_foreach_include_resolves(self, tmp_path: Path) -> None:
        """yarn berry ``workspaces foreach --include <ws> <script>`` is supported."""
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "root", "workspaces": ["packages/*"]})
        )
        self._make_workspace(tmp_path, "web", "test")
        adapter = _FakeAdapter(
            response=json.dumps({"test": "yarn workspaces foreach --include web test"})
        )
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_pnpm_workspace_yaml_glob_resolves(self, tmp_path: Path) -> None:
        """pnpm-workspace.yaml globs also drive workspace resolution."""
        (tmp_path / "package.json").write_text(json.dumps({"name": "root"}))
        (tmp_path / "pnpm-workspace.yaml").write_text("packages:\n  - 'packages/*'\n")
        self._make_workspace(tmp_path, "web", "test")
        adapter = _FakeAdapter(response=json.dumps({"test": "pnpm --filter web test"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_workspace_filter_parent_escape_dropped(self, tmp_path: Path) -> None:
        """Workspace filters must not resolve to sibling directories."""
        repo = tmp_path / "repo"
        other = tmp_path / "other"
        repo.mkdir()
        other.mkdir()
        (repo / "package.json").write_text(json.dumps({"name": "root"}))
        (other / "package.json").write_text(
            json.dumps({"name": "other", "scripts": {"test": "echo escaped"}})
        )
        adapter = _FakeAdapter(response=json.dumps({"test": "pnpm --filter ../other test"}))
        ok = _run(ensure_mechanical_toml(repo, adapter))
        assert ok is False

    def test_package_json_workspace_glob_parent_escape_dropped(self, tmp_path: Path) -> None:
        """Workspace globs must stay inside the repo root."""
        repo = tmp_path / "repo"
        other = tmp_path / "web"
        repo.mkdir()
        other.mkdir()
        (repo / "package.json").write_text(json.dumps({"name": "root", "workspaces": ["../*"]}))
        (other / "package.json").write_text(
            json.dumps({"name": "web", "scripts": {"test": "echo escaped"}})
        )
        adapter = _FakeAdapter(response=json.dumps({"test": "pnpm --filter web test"}))
        ok = _run(ensure_mechanical_toml(repo, adapter))
        assert ok is False

    def test_pnpm_workspace_yaml_glob_parent_escape_dropped(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        other = tmp_path / "web"
        repo.mkdir()
        other.mkdir()
        (repo / "package.json").write_text(json.dumps({"name": "root"}))
        (repo / "pnpm-workspace.yaml").write_text("packages:\n  - '../*'\n")
        (other / "package.json").write_text(
            json.dumps({"name": "web", "scripts": {"test": "echo escaped"}})
        )
        adapter = _FakeAdapter(response=json.dumps({"test": "pnpm --filter web test"}))
        ok = _run(ensure_mechanical_toml(repo, adapter))
        assert ok is False

    def test_npm_prefix_parent_escape_dropped(self, tmp_path: Path) -> None:
        """Path-retargeting flags must not execute outside the repo."""
        repo = tmp_path / "repo"
        other = tmp_path / "other"
        repo.mkdir()
        other.mkdir()
        (repo / "package.json").write_text(
            json.dumps({"name": "root", "scripts": {"test": "echo root"}})
        )
        (other / "package.json").write_text(
            json.dumps({"name": "other", "scripts": {"test": "echo escaped"}})
        )
        adapter = _FakeAdapter(response=json.dumps({"test": "npm --prefix ../other test"}))
        ok = _run(ensure_mechanical_toml(repo, adapter))
        assert ok is False

    def test_yarn_cwd_parent_escape_dropped(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        other = tmp_path / "other"
        repo.mkdir()
        other.mkdir()
        (repo / "package.json").write_text(
            json.dumps({"name": "root", "scripts": {"test": "echo root"}})
        )
        (other / "package.json").write_text(
            json.dumps({"name": "other", "scripts": {"test": "echo escaped"}})
        )
        adapter = _FakeAdapter(response=json.dumps({"test": "yarn --cwd=../other test"}))
        ok = _run(ensure_mechanical_toml(repo, adapter))
        assert ok is False

    def test_npm_prefix_inside_repo_validates_target_script(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        package = repo / "packages" / "web"
        package.mkdir(parents=True)
        (repo / "package.json").write_text(json.dumps({"name": "root"}))
        (package / "package.json").write_text(
            json.dumps({"name": "web", "scripts": {"test": "echo ok"}})
        )
        adapter = _FakeAdapter(response=json.dumps({"test": "npm --prefix packages/web test"}))
        ok = _run(ensure_mechanical_toml(repo, adapter))
        assert ok is True


class TestPoetryPyprojectLayouts:
    """Poetry dependency tables must be recognized as declared deps."""

    def test_tool_poetry_dependencies_recognized(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[tool.poetry]\nname = 'demo'\n"
            "[tool.poetry.dependencies]\npython = '^3.11'\npytest = '^8'\n"
        )
        adapter = _FakeAdapter(response=json.dumps({"test": "poetry run pytest"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_tool_poetry_dev_dependencies_recognized(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[tool.poetry]\nname = 'demo'\n[tool.poetry.dev-dependencies]\npytest = '^8'\n"
        )
        adapter = _FakeAdapter(response=json.dumps({"test": "pytest"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_tool_poetry_group_dependencies_recognized(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[tool.poetry]\nname = 'demo'\n[tool.poetry.group.dev.dependencies]\npytest = '^8'\n"
        )
        adapter = _FakeAdapter(response=json.dumps({"test": "python -m pytest"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True


class TestGradleTaskValidation:
    def test_gradle_lifecycle_task_accepted(self, tmp_path: Path) -> None:
        (tmp_path / "build.gradle.kts").write_text("plugins { java }\n")
        adapter = _FakeAdapter(response=json.dumps({"build": "gradle build"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_gradle_unknown_task_dropped(self, tmp_path: Path) -> None:
        (tmp_path / "build.gradle.kts").write_text("plugins { java }\n")
        adapter = _FakeAdapter(response=json.dumps({"lint": "gradle spotlessCheck"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_gradle_custom_task_accepted_when_declared(self, tmp_path: Path) -> None:
        (tmp_path / "build.gradle.kts").write_text(
            'plugins { java }\n\ntasks.register("customCheck") {}\n'
        )
        adapter = _FakeAdapter(response=json.dumps({"lint": "gradle customCheck"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True


class TestTaskRunnerValidation:
    def test_task_unknown_name_dropped(self, tmp_path: Path) -> None:
        (tmp_path / "Taskfile.yml").write_text(
            "version: '3'\ntasks:\n  build:\n    cmds:\n      - go build\n"
        )
        adapter = _FakeAdapter(response=json.dumps({"test": "task test"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_task_declared_name_accepted(self, tmp_path: Path) -> None:
        (tmp_path / "Taskfile.yml").write_text(
            "version: '3'\ntasks:\n  test:\n    cmds:\n      - go test ./...\n"
        )
        adapter = _FakeAdapter(response=json.dumps({"test": "task test"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True


class TestRakeAndBundleValidation:
    def test_rake_unknown_task_dropped(self, tmp_path: Path) -> None:
        (tmp_path / "Rakefile").write_text("task :build do\nend\n")
        adapter = _FakeAdapter(response=json.dumps({"test": "rake test"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_bundle_exec_requires_gemfile_declaration(self, tmp_path: Path) -> None:
        (tmp_path / "Gemfile").write_text('gem "rspec"\n')
        adapter = _FakeAdapter(response=json.dumps({"test": "bundle exec rspec"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_bundle_exec_dropped_when_gem_missing(self, tmp_path: Path) -> None:
        (tmp_path / "Gemfile").write_text('gem "pry"\n')
        adapter = _FakeAdapter(response=json.dumps({"test": "bundle exec rspec"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False


class TestComposerAndMixValidation:
    def test_composer_script_accepted_when_declared(self, tmp_path: Path) -> None:
        (tmp_path / "composer.json").write_text(json.dumps({"scripts": {"test": "phpunit"}}))
        adapter = _FakeAdapter(response=json.dumps({"test": "composer test"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_composer_install_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "composer.json").write_text("{}")
        adapter = _FakeAdapter(response=json.dumps({"build": "composer install"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_mix_test_accepted_as_builtin(self, tmp_path: Path) -> None:
        (tmp_path / "mix.exs").write_text("defmodule App.MixProject do\nend\n")
        adapter = _FakeAdapter(response=json.dumps({"test": "mix test"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_mix_unknown_task_dropped(self, tmp_path: Path) -> None:
        (tmp_path / "mix.exs").write_text("defmodule App.MixProject do\nend\n")
        adapter = _FakeAdapter(response=json.dumps({"build": "mix weird"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_mix_third_party_tasks_dropped(self, tmp_path: Path) -> None:
        """`mix credo` / `mix dialyzer` are third-party plugins, not builtins."""
        (tmp_path / "mix.exs").write_text("defmodule App.MixProject do\nend\n")
        for task in ("mix credo", "mix dialyzer"):
            adapter = _FakeAdapter(response=json.dumps({"lint": task}))
            ok = _run(ensure_mechanical_toml(tmp_path, adapter, force=True))
            assert ok is False, f"{task!r} must not be treated as a builtin"


class TestMakeValidation:
    """`make` without a Makefile must be dropped."""

    def test_bare_make_dropped_without_makefile(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("{}")
        adapter = _FakeAdapter(response=json.dumps({"build": "make"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_bare_make_rejected_even_with_makefile(self, tmp_path: Path) -> None:
        """Bare ``make`` runs the first target — which may be install/deploy.

        The detector now refuses to persist a command whose effective target
        cannot be determined statically. Users must name the target
        explicitly (``make all``).
        """
        (tmp_path / "Makefile").write_text("all:\n\techo ok\n")
        (tmp_path / "package.json").write_text("{}")
        adapter = _FakeAdapter(response=json.dumps({"build": "make"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is False

    def test_explicit_make_target_accepted(self, tmp_path: Path) -> None:
        (tmp_path / "Makefile").write_text("all:\n\techo ok\n")
        (tmp_path / "package.json").write_text("{}")
        adapter = _FakeAdapter(response=json.dumps({"build": "make all"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True


class TestEvaluationPublicSurface:
    """Legacy preset symbols stay importable as deprecated compat shims."""

    def test_language_preset_importable(self) -> None:
        from ouroboros.evaluation import LanguagePreset

        assert LanguagePreset(name="demo").test_command is None

    def test_detect_language_emits_deprecation_warning(self, tmp_path: Path) -> None:
        """Shim must flag callers so they migrate rather than silently lose config."""
        import warnings

        from ouroboros.evaluation import detect_language

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = detect_language(tmp_path)

        assert result is None
        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert deprecations, "detect_language must emit a DeprecationWarning on call"
        assert "ensure_mechanical_toml" in str(deprecations[0].message)

    def test_detect_language_returns_preset_from_mechanical_toml(self, tmp_path: Path) -> None:
        """Legacy callers still receive runnable commands when the toml exists."""
        import warnings

        from ouroboros.evaluation import LanguagePreset, detect_language

        (tmp_path / "Cargo.toml").write_text('[package]\nname = "demo"\n')
        (tmp_path / ".ouroboros").mkdir()
        (tmp_path / ".ouroboros" / "mechanical.toml").write_text('test = "cargo test"\n')

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            preset = detect_language(tmp_path)

        assert isinstance(preset, LanguagePreset)
        assert preset.test_command == ("cargo", "test")


class TestMonorepoManifestDiscovery:
    """Subdirectory manifests must seed the detect call, not just the root."""

    def test_backend_pyproject_detected(self, tmp_path: Path) -> None:
        (tmp_path / "backend").mkdir()
        (tmp_path / "backend" / "pyproject.toml").write_text(
            '[project]\nname = "svc"\ndependencies = ["pytest>=8"]\n'
        )
        adapter = _FakeAdapter(response=json.dumps({"test": "uv run --directory backend pytest"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True

    def test_packages_glob_detected(self, tmp_path: Path) -> None:
        ws = tmp_path / "packages" / "web"
        ws.mkdir(parents=True)
        (ws / "package.json").write_text(json.dumps({"name": "web", "scripts": {"test": "jest"}}))
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "root", "workspaces": ["packages/*"]})
        )
        adapter = _FakeAdapter(response=json.dumps({"test": "pnpm --filter web test"}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True


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
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\ndependencies = ["pytest>=8"]\n'
        )
        adapter = _FakeAdapter(response=json.dumps({"test": 'pytest -k "slow"'}))
        ok = _run(ensure_mechanical_toml(tmp_path, adapter))
        assert ok is True
        body = toml_path(tmp_path).read_text()
        assert 'test = "pytest -k \\"slow\\""' in body
        config = build_mechanical_config(tmp_path)
        assert config.test_command == ("pytest", "-k", "slow")


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
