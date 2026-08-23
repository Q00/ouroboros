"""Unit tests for Codex integration helper commands."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
import textwrap
import time
import tomllib
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from ouroboros.cli.commands import codex as codex_command
from ouroboros.cli.commands import setup as setup_command
from ouroboros.cli.commands.codex import (
    _MCP_PROTOCOL_VERSION,
    _check_auto_dispatch_surface,
    _list_stdio_mcp_tool_names,
    _should_retry_stdio_mcp_framing,
    app,
)
from ouroboros.codex import CodexArtifactInstallResult, install_codex_artifacts
from ouroboros.codex import artifacts as codex_artifacts

runner = CliRunner()
_REQUIRED_CODEX_AUTO_TOOLS_FOR_TEST = {
    "ouroboros_auto",
    "ouroboros_start_auto",
    "ouroboros_job_status",
    "ouroboros_job_wait",
    "ouroboros_job_result",
    "ouroboros_interview",
    "ouroboros_generate_seed",
}
_REPO_ROOT = Path(__file__).resolve().parents[3]
_PLUGIN_VERSION = json.loads(
    (_REPO_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
)["version"]
_CANONICAL_CODEX_MCP_ENTRY = (
    "[mcp_servers.ouroboros]\n"
    'command = "uvx"\n'
    'args = ["--isolated", "--python", ">=3.12", "--from", '
    '"ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"]\n'
    'env = { OUROBOROS_AGENT_RUNTIME = "codex", OUROBOROS_LLM_BACKEND = "codex" }\n'
)


class TestCodexRefresh:
    """Tests for `ouroboros codex refresh`."""

    def test_refresh_installs_rules_and_skills_without_config_files(self, tmp_path: Path) -> None:
        rules_path = tmp_path / ".codex" / "rules" / "ouroboros.md"
        skill_paths = (
            tmp_path / ".codex" / "skills" / "ouroboros-interview",
            tmp_path / ".codex" / "skills" / "ouroboros-run",
        )
        result = CodexArtifactInstallResult(rules_path=rules_path, skill_paths=skill_paths)

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.codex.install_codex_artifacts", return_value=result
            ) as mock_install,
        ):
            cli_result = runner.invoke(app, ["refresh"])

        assert cli_result.exit_code == 0
        mock_install.assert_called_once_with(codex_dir=tmp_path / ".codex", prune=False)
        assert "Installed Codex rules" in cli_result.output
        assert "Installed 2 Codex skills" in cli_result.output
        assert not (tmp_path / ".codex" / "config.toml").exists()
        assert not (tmp_path / ".ouroboros" / "config.yaml").exists()

    def test_refresh_restores_existing_skill_when_final_swap_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Standalone refresh must preserve the installed generation on swap failure."""
        codex_dir = tmp_path / ".codex"
        target_path = codex_dir / "skills" / "ouroboros-run"
        target_path.mkdir(parents=True)
        target_path.joinpath("SKILL.md").write_text("installed run skill", encoding="utf-8")
        original_rename_noreplace = codex_artifacts._rename_noreplace

        def _fail_run_swap(source: Path, destination: Path) -> None:
            if destination == target_path and source.name.endswith(".tmp"):
                raise OSError("synthetic refresh swap failure")
            original_rename_noreplace(source, destination)

        monkeypatch.setenv("CODEX_HOME", str(codex_dir))
        monkeypatch.setattr(codex_artifacts, "_rename_noreplace", _fail_run_swap)

        cli_result = runner.invoke(app, ["refresh"])

        assert cli_result.exit_code == 1
        assert "synthetic refresh swap failure" in cli_result.output
        assert target_path.joinpath("SKILL.md").read_text(encoding="utf-8") == (
            "installed run skill"
        )
        assert not tuple(target_path.parent.glob(f".{target_path.name}.*.tmp"))
        assert not tuple(target_path.parent.glob(f".{target_path.name}.*.backup"))

    def test_refresh_keeps_new_skill_when_old_generation_cleanup_partially_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Standalone refresh must not roll back from a damaged disposal generation."""
        codex_dir = tmp_path / ".codex"
        target_path = codex_dir / "skills" / "ouroboros-run"
        target_path.mkdir(parents=True)
        target_path.joinpath("SKILL.md").write_text("installed run skill", encoding="utf-8")
        target_path.joinpath("operator.txt").write_text("old companion", encoding="utf-8")
        original_remove = codex_artifacts._remove_installed_artifact

        def _partially_remove_disposal(path: Path) -> None:
            if path.name.endswith(".discard"):
                path.joinpath("SKILL.md").unlink()
                raise OSError("synthetic partial refresh disposal failure")
            original_remove(path)

        monkeypatch.setenv("CODEX_HOME", str(codex_dir))
        monkeypatch.setattr(
            codex_artifacts,
            "_remove_installed_artifact",
            _partially_remove_disposal,
        )

        cli_result = runner.invoke(app, ["refresh"])

        assert cli_result.exit_code == 0
        assert target_path.joinpath("SKILL.md").read_text(encoding="utf-8") != (
            "installed run skill"
        )
        assert not target_path.joinpath("operator.txt").exists()
        assert not tuple(target_path.parent.glob(f".{target_path.name}.*.backup"))
        disposal_paths = tuple(target_path.parent.glob(".*.discard"))
        assert len(disposal_paths) == 1
        assert disposal_paths[0].joinpath("operator.txt").read_text(encoding="utf-8") == (
            "old companion"
        )

    def test_refresh_restores_complete_artifact_bundle_after_late_skill_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A late skill failure must roll rules and already-swapped skills back together."""
        codex_dir = tmp_path / ".codex"
        rule_path = codex_dir / "rules" / "ouroboros.md"
        skill_path = codex_dir / "skills" / "ouroboros-run"
        rule_path.parent.mkdir(parents=True)
        skill_path.mkdir(parents=True)
        rule_path.write_text("operator rule generation\n", encoding="utf-8")
        skill_path.joinpath("SKILL.md").write_text("operator skill generation\n", encoding="utf-8")
        skill_path.joinpath("operator.txt").write_text("keep together", encoding="utf-8")

        packaged_skills_dir = tmp_path / "packaged-skills"
        packaged_run = packaged_skills_dir / "run"
        packaged_run.mkdir(parents=True)
        packaged_run.joinpath("SKILL.md").write_text(
            "replacement skill generation\n", encoding="utf-8"
        )
        packaged_fresh = packaged_skills_dir / "fresh"
        packaged_fresh.mkdir(parents=True)
        packaged_fresh.joinpath("SKILL.md").write_text("new skill generation\n", encoding="utf-8")
        fresh_skill_path = codex_dir / "skills" / "ouroboros-fresh"
        original_install_skills = codex_artifacts.install_codex_skills

        def _install_skill_then_fail(**kwargs: object) -> tuple[Path, ...]:
            original_install_skills(
                skills_dir=packaged_skills_dir,
                **kwargs,
            )
            assert skill_path.joinpath("SKILL.md").read_text(encoding="utf-8") == (
                "replacement skill generation\n"
            )
            assert fresh_skill_path.joinpath("SKILL.md").read_text(encoding="utf-8") == (
                "new skill generation\n"
            )
            raise OSError("synthetic late skill failure")

        monkeypatch.setenv("CODEX_HOME", str(codex_dir))
        monkeypatch.setattr(
            codex_artifacts,
            "install_codex_skills",
            _install_skill_then_fail,
        )

        cli_result = runner.invoke(app, ["refresh"])

        assert cli_result.exit_code == 1
        assert "synthetic late skill failure" in cli_result.output
        assert rule_path.read_text(encoding="utf-8") == "operator rule generation\n"
        assert skill_path.joinpath("SKILL.md").read_text(encoding="utf-8") == (
            "operator skill generation\n"
        )
        assert skill_path.joinpath("operator.txt").read_text(encoding="utf-8") == ("keep together")
        assert not fresh_skill_path.exists()
        assert not tuple(codex_dir.rglob("*.backup"))
        assert not tuple(codex_dir.rglob("*.rollback"))
        assert not tuple(codex_dir.rglob("*.discard"))

    def test_refresh_restores_bundle_when_batch_commit_detach_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A commit-boundary failure must restore every still-intact prior generation."""
        codex_dir = tmp_path / ".codex"
        rule_path = codex_dir / "rules" / "ouroboros.md"
        skill_path = codex_dir / "skills" / "ouroboros-run"
        rule_path.parent.mkdir(parents=True)
        skill_path.mkdir(parents=True)
        rule_path.write_text("operator rule generation\n", encoding="utf-8")
        skill_path.joinpath("SKILL.md").write_text("operator skill generation\n", encoding="utf-8")
        original_replace = os.replace
        detach_count = 0

        def _fail_second_backup_detach(source: str | Path, destination: str | Path) -> None:
            nonlocal detach_count
            if Path(source).name.endswith(".backup") and Path(destination).name.endswith(
                ".discard"
            ):
                detach_count += 1
                if detach_count == 2:
                    raise OSError("synthetic batch commit failure")
            original_replace(source, destination)

        monkeypatch.setenv("CODEX_HOME", str(codex_dir))
        monkeypatch.setattr(os, "replace", _fail_second_backup_detach)

        cli_result = runner.invoke(app, ["refresh"])

        assert cli_result.exit_code == 1
        assert "synthetic batch commit failure" in cli_result.output
        assert detach_count == 2
        assert rule_path.read_text(encoding="utf-8") == "operator rule generation\n"
        assert skill_path.joinpath("SKILL.md").read_text(encoding="utf-8") == (
            "operator skill generation\n"
        )
        assert not tuple(codex_dir.rglob("*.backup"))
        assert not tuple(codex_dir.rglob("*.rollback"))
        assert not tuple(codex_dir.rglob("*.discard"))

    def test_refresh_preserves_concurrent_rule_edit_during_batch_rollback(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Standalone rollback must not delete an active generation it did not author."""
        codex_dir = tmp_path / ".codex"
        rule_path = codex_dir / "rules" / "ouroboros.md"
        rule_path.parent.mkdir(parents=True)
        rule_path.write_text("old rule generation\n", encoding="utf-8")

        def _edit_rule_then_fail(**_kwargs: object) -> tuple[Path, ...]:
            rule_path.write_text("operator concurrent edit\n", encoding="utf-8")
            raise OSError("synthetic late skill failure")

        monkeypatch.setenv("CODEX_HOME", str(codex_dir))
        monkeypatch.setattr(
            codex_artifacts,
            "install_codex_skills",
            _edit_rule_then_fail,
        )

        cli_result = runner.invoke(app, ["refresh"])

        assert cli_result.exit_code == 1
        assert "changed during batch rollback" in cli_result.output
        assert rule_path.read_text(encoding="utf-8") == "operator concurrent edit\n"
        backup_paths = tuple(rule_path.parent.glob(f".{rule_path.name}.*.backup"))
        assert len(backup_paths) == 1
        assert backup_paths[0].read_text(encoding="utf-8") == "old rule generation\n"
        assert not tuple(rule_path.parent.glob(f".{rule_path.name}.*.rollback"))

    def test_refresh_preserves_rule_created_after_rollback_verification(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No-replace restoration must close the post-fingerprint creation window."""
        codex_dir = tmp_path / ".codex"
        rule_path = codex_dir / "rules" / "ouroboros.md"
        rule_path.parent.mkdir(parents=True)
        rule_path.write_text("old rule generation\n", encoding="utf-8")
        original_fingerprint = codex_artifacts._artifact_fingerprint
        injected = False

        def _fingerprint_then_recreate(path: Path) -> str:
            nonlocal injected
            fingerprint = original_fingerprint(path)
            if path.name.endswith(".rollback") and not injected:
                injected = True
                rule_path.write_text("post-verification operator edit\n", encoding="utf-8")
            return fingerprint

        def _fail_skills(**_kwargs: object) -> tuple[Path, ...]:
            raise OSError("synthetic late skill failure")

        monkeypatch.setenv("CODEX_HOME", str(codex_dir))
        monkeypatch.setattr(codex_artifacts, "_artifact_fingerprint", _fingerprint_then_recreate)
        monkeypatch.setattr(codex_artifacts, "install_codex_skills", _fail_skills)

        cli_result = runner.invoke(app, ["refresh"])

        assert cli_result.exit_code == 1
        assert "changed during batch rollback" in cli_result.output
        assert injected is True
        assert rule_path.read_text(encoding="utf-8") == ("post-verification operator edit\n")
        backup_paths = tuple(rule_path.parent.glob(f".{rule_path.name}.*.backup"))
        assert len(backup_paths) == 1
        assert backup_paths[0].read_text(encoding="utf-8") == "old rule generation\n"
        assert not tuple(rule_path.parent.glob(f".{rule_path.name}.*.rollback"))

    def test_refresh_preserves_rule_recreated_before_forward_activation(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The final staged activation must not replace a concurrently recreated rule."""
        codex_dir = tmp_path / ".codex"
        rule_path = codex_dir / "rules" / "ouroboros.md"
        rule_path.parent.mkdir(parents=True)
        rule_path.write_text("old rule generation\n", encoding="utf-8")
        original_rename_noreplace = codex_artifacts._rename_noreplace
        injected = False

        def _recreate_before_activation(source: Path, target: Path) -> None:
            nonlocal injected
            if target == rule_path and source.name.endswith(".tmp") and not injected:
                injected = True
                rule_path.write_text("concurrent operator rule\n", encoding="utf-8")
            original_rename_noreplace(source, target)

        monkeypatch.setenv("CODEX_HOME", str(codex_dir))
        monkeypatch.setattr(codex_artifacts, "_rename_noreplace", _recreate_before_activation)

        cli_result = runner.invoke(app, ["refresh"])

        assert cli_result.exit_code == 1
        assert "changed during rollback" in cli_result.output
        assert injected is True
        assert rule_path.read_text(encoding="utf-8") == "concurrent operator rule\n"
        backup_paths = tuple(rule_path.parent.glob(f".{rule_path.name}.*.backup"))
        assert len(backup_paths) == 1
        assert backup_paths[0].read_text(encoding="utf-8") == "old rule generation\n"
        assert not tuple(rule_path.parent.glob(f".{rule_path.name}.*.tmp"))

    def test_refresh_restores_rule_replaced_during_backup_acquisition(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Refresh must verify the exact generation atomically acquired for rollback."""
        codex_dir = tmp_path / ".codex"
        rule_path = codex_dir / "rules" / "ouroboros.md"
        rule_path.parent.mkdir(parents=True)
        rule_path.write_text("old rule generation\n", encoding="utf-8")
        original_replace = os.replace
        injected = False

        def _replace_rule_before_backup(source: str | Path, destination: str | Path) -> None:
            nonlocal injected
            source_path = Path(source)
            destination_path = Path(destination)
            if (
                source_path == rule_path
                and destination_path.name.endswith(".backup")
                and not injected
            ):
                injected = True
                rule_path.write_text("concurrent operator rule\n", encoding="utf-8")
            original_replace(source, destination)

        monkeypatch.setenv("CODEX_HOME", str(codex_dir))
        monkeypatch.setattr(os, "replace", _replace_rule_before_backup)

        cli_result = runner.invoke(app, ["refresh"])

        assert cli_result.exit_code == 1
        assert "changed before activation" in cli_result.output
        assert injected is True
        assert rule_path.read_text(encoding="utf-8") == "concurrent operator rule\n"
        assert not tuple(rule_path.parent.glob(f".{rule_path.name}.*.tmp"))
        assert not tuple(rule_path.parent.glob(f".{rule_path.name}.*.backup"))

    def test_refresh_preserves_removed_target_recreated_at_rollback_commit(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Prune rollback must not replace a target recreated after its absence check."""
        codex_dir = tmp_path / ".codex"
        stale_path = codex_dir / "skills" / "ouroboros-stale"
        stale_path.mkdir(parents=True)
        stale_path.joinpath("SKILL.md").write_text("old stale generation\n", encoding="utf-8")
        original_rename_noreplace = codex_artifacts._rename_noreplace
        injected = False

        def _remove_stale_then_fail(**kwargs: object) -> tuple[Path, ...]:
            transaction = kwargs["_transaction"]
            assert isinstance(transaction, codex_artifacts._CodexArtifactBatchTransaction)
            codex_artifacts._remove_artifact_transactionally(
                stale_path,
                before_mutation=None,
                on_generation=None,
                transaction=transaction,
            )
            raise OSError("synthetic late skill failure")

        def _recreate_before_restore(source: Path, target: Path) -> None:
            nonlocal injected
            if target == stale_path and source.name.endswith(".backup") and not injected:
                injected = True
                stale_path.mkdir(parents=True)
                stale_path.joinpath("SKILL.md").write_text(
                    "concurrent stale generation\n", encoding="utf-8"
                )
            original_rename_noreplace(source, target)

        monkeypatch.setenv("CODEX_HOME", str(codex_dir))
        monkeypatch.setattr(codex_artifacts, "_rename_noreplace", _recreate_before_restore)
        monkeypatch.setattr(codex_artifacts, "install_codex_skills", _remove_stale_then_fail)

        cli_result = runner.invoke(app, ["refresh"])

        assert cli_result.exit_code == 1
        assert "changed during batch rollback" in cli_result.output
        assert injected is True
        assert stale_path.joinpath("SKILL.md").read_text(encoding="utf-8") == (
            "concurrent stale generation\n"
        )
        backup_paths = tuple(stale_path.parent.glob(f".{stale_path.name}.*.backup"))
        assert len(backup_paths) == 1
        assert backup_paths[0].joinpath("SKILL.md").read_text(encoding="utf-8") == (
            "old stale generation\n"
        )

    def test_refresh_preserves_directory_shaped_rule_when_staging_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Standalone refresh must not delete directory-shaped rule topology."""
        codex_dir = tmp_path / ".codex"
        target_path = codex_dir / "rules" / "ouroboros.md"
        target_path.mkdir(parents=True)
        target_path.joinpath("operator.txt").write_text("keep", encoding="utf-8")
        original_write_bytes = Path.write_bytes

        def _fail_rule_staging(path: Path, data: bytes) -> int:
            if path.parent == target_path.parent and path.name.endswith(".tmp"):
                raise OSError("synthetic refresh rule staging failure")
            return original_write_bytes(path, data)

        monkeypatch.setenv("CODEX_HOME", str(codex_dir))
        monkeypatch.setattr(Path, "write_bytes", _fail_rule_staging)

        cli_result = runner.invoke(app, ["refresh"])

        assert cli_result.exit_code == 1
        assert "synthetic refresh rule staging failure" in cli_result.output
        assert target_path.joinpath("operator.txt").read_text(encoding="utf-8") == "keep"
        assert not tuple(target_path.parent.glob(f".{target_path.name}.*.tmp"))


class TestCodexDoctor:
    """Tests for `ouroboros codex doctor`."""

    @staticmethod
    def _write_healthy_codex_surface(codex_dir: Path) -> None:
        rules_path = codex_dir / "rules" / "ouroboros.md"
        rules_path.parent.mkdir(parents=True, exist_ok=True)
        rules_path.write_text(
            "| `ooo auto ...` | `ouroboros_start_auto` |\n"
            "Do not emulate it with manual work. If unavailable, stop.\n",
            encoding="utf-8",
        )

        skill_path = codex_dir / "skills" / "ouroboros-auto" / "SKILL.md"
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text(
            "---\n"
            "name: auto\n"
            "mcp_tool: ouroboros_start_auto\n"
            "---\n"
            "Manual fallback is not allowed when the tool is unavailable.\n",
            encoding="utf-8",
        )

        (codex_dir / "config.toml").write_text(
            "[mcp_servers.ouroboros]\n"
            'command = "uvx"\n'
            'args = ["--isolated", "--python", ">=3.12", "--from", '
            '"ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"]\n'
            "[mcp_servers.ouroboros.env]\n"
            'OUROBOROS_AGENT_RUNTIME = "codex"\n'
            'OUROBOROS_LLM_BACKEND = "codex"\n',
            encoding="utf-8",
        )

    def test_check_auto_dispatch_surface_passes_for_healthy_install(
        self,
        tmp_path: Path,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)

        assert _check_auto_dispatch_surface(codex_dir) == []

    def test_check_auto_dispatch_surface_accepts_url_mcp_server_entry(
        self,
        tmp_path: Path,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        (codex_dir / "config.toml").write_text(
            '[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:12000/mcp"\n',
            encoding="utf-8",
        )

        assert _check_auto_dispatch_surface(codex_dir) == []

    def test_check_auto_dispatch_surface_live_mcp_rejects_url_mcp_server_entry(
        self,
        tmp_path: Path,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        (codex_dir / "config.toml").write_text(
            '[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:12000/mcp"\n',
            encoding="utf-8",
        )

        with patch("ouroboros.cli.commands.codex._list_stdio_mcp_tool_names") as live_probe:
            failures = _check_auto_dispatch_surface(codex_dir, live_mcp=True)

        live_probe.assert_not_called()
        assert any("uses `url`" in failure for failure in failures)
        assert any("verifies only stdio `command` entries" in failure for failure in failures)

    @pytest.mark.parametrize("live_mcp", [False, True], ids=["static", "live"])
    @pytest.mark.parametrize(
        ("config_text", "expected_failure"),
        [
            (
                _CANONICAL_CODEX_MCP_ENTRY.replace(
                    "[mcp_servers.ouroboros]\n",
                    '[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:12000/mcp"\n',
                    1,
                ),
                "mixes stdio `command` with HTTP `url`",
            ),
            (
                '[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:12000/mcp"\nargs = []\n',
                ".args is not supported for HTTP `url`",
            ),
            (
                '[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:12000/mcp"\nenv = {}\n',
                ".env is not supported for HTTP `url`",
            ),
            *[
                (
                    _CANONICAL_CODEX_MCP_ENTRY.replace(
                        "[mcp_servers.ouroboros]\n",
                        "[mcp_servers.ouroboros]\n" + field_config,
                        1,
                    ),
                    expected_failure,
                )
                for field_config, expected_failure in [
                    (
                        'bearer_token_env_var = "OUROBOROS_TOKEN"\n',
                        ".bearer_token_env_var is not supported for stdio `command`",
                    ),
                    (
                        'http_headers = { X_Test = "value" }\n',
                        ".http_headers is not supported for stdio `command`",
                    ),
                    (
                        'env_http_headers = { X_Token = "OUROBOROS_TOKEN" }\n',
                        ".env_http_headers is not supported for stdio `command`",
                    ),
                    (
                        'oauth = { client_id = "ouroboros" }\n',
                        ".oauth is not supported for stdio `command`",
                    ),
                    (
                        'oauth_resource = "https://ouroboros.example"\n',
                        ".oauth_resource is not supported for stdio `command`",
                    ),
                    (
                        'bearer_token = "plaintext-secret"\n',
                        ".bearer_token is unsupported",
                    ),
                    (
                        'environment_id = "remote"\n',
                        "contains unsupported fields: environment_id",
                    ),
                ]
            ],
            (
                "[mcp_servers.ouroboros]\nurl = 7\n",
                ".url must be a string",
            ),
            (
                '[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:12000/mcp"\n'
                "bearer_token_env_var = 7\n",
                ".bearer_token_env_var must be a string",
            ),
            (
                '[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:12000/mcp"\noauth_resource = 7\n',
                ".oauth_resource must be a string",
            ),
            (
                '[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:12000/mcp"\n'
                'http_headers = "invalid"\n',
                ".http_headers must be a table of strings",
            ),
            (
                '[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:12000/mcp"\n'
                "http_headers = { X_Test = 7 }\n",
                ".http_headers contains non-string values",
            ),
            (
                '[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:12000/mcp"\n'
                'env_http_headers = "invalid"\n',
                ".env_http_headers must be a table of strings",
            ),
            (
                '[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:12000/mcp"\n'
                "env_http_headers = { X_Token = 7 }\n",
                ".env_http_headers contains non-string values",
            ),
            (
                '[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:12000/mcp"\nrequired = "true"\n',
                ".required must be a boolean",
            ),
            (
                '[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:12000/mcp"\n'
                "supports_parallel_tool_calls = 1\n",
                ".supports_parallel_tool_calls must be a boolean",
            ),
            (
                '[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:12000/mcp"\n'
                'default_tools_approval_mode = "ask"\n',
                ".default_tools_approval_mode must be one of:",
            ),
            (
                '[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:12000/mcp"\n'
                "default_tools_approval_mode = 1\n",
                ".default_tools_approval_mode must be one of:",
            ),
            (
                '[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:12000/mcp"\nscopes = "openid"\n',
                ".scopes must be an array of strings",
            ),
            (
                '[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:12000/mcp"\nscopes = [""]\n',
                ".scopes contains empty or non-string values",
            ),
            (
                '[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:12000/mcp"\n'
                'scopes = ["openid", 7]\n',
                ".scopes contains empty or non-string values",
            ),
            (
                '[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:12000/mcp"\nname = 7\n',
                ".name must be a string",
            ),
            (
                '[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:12000/mcp"\noauth = "invalid"\n',
                ".oauth must be a table",
            ),
            (
                '[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:12000/mcp"\n'
                "oauth = { client_id = 7 }\n",
                ".oauth.client_id must be a string",
            ),
            (
                '[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:12000/mcp"\n'
                'oauth = { client_id = "ouroboros", redirect_uri = "https://example.com" }\n',
                ".oauth contains unsupported fields: redirect_uri",
            ),
            (
                '[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:12000/mcp"\ntools = "invalid"\n',
                ".tools must be a table",
            ),
            (
                '[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:12000/mcp"\n'
                'tools = { "" = { approval_mode = "auto" } }\n',
                ".tools contains an empty or non-string tool name",
            ),
            (
                '[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:12000/mcp"\n'
                'tools = { ouroboros_start_auto = "approve" }\n',
                ".tools.ouroboros_start_auto must be a table",
            ),
            (
                '[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:12000/mcp"\n'
                "tools = { ouroboros_start_auto = { extra = true } }\n",
                ".tools.ouroboros_start_auto contains unsupported fields: extra",
            ),
            (
                '[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:12000/mcp"\n'
                'tools = { ouroboros_start_auto = { approval_mode = "ask" } }\n',
                ".tools.ouroboros_start_auto.approval_mode must be one of:",
            ),
            (
                '[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:12000/mcp"\n'
                "tools = { ouroboros_start_auto = { approval_mode = 7 } }\n",
                ".tools.ouroboros_start_auto.approval_mode must be one of:",
            ),
        ],
        ids=[
            "mixed-transports",
            "http-args",
            "http-env",
            "stdio-token-env",
            "stdio-headers",
            "stdio-env-headers",
            "stdio-oauth",
            "stdio-oauth-resource",
            "plaintext-token",
            "unknown-field",
            "untyped-url",
            "untyped-token-env",
            "untyped-oauth-resource",
            "scalar-http-headers",
            "untyped-http-header",
            "scalar-env-http-headers",
            "untyped-env-http-header",
            "untyped-required",
            "untyped-parallel-tools",
            "invalid-default-approval",
            "untyped-default-approval",
            "scalar-scopes",
            "empty-scope",
            "untyped-scope",
            "untyped-name",
            "scalar-oauth",
            "untyped-oauth-client-id",
            "unknown-oauth-field",
            "scalar-tools",
            "empty-tool-name",
            "scalar-tool-config",
            "unknown-tool-field",
            "invalid-tool-approval",
            "untyped-tool-approval",
        ],
    )
    def test_check_auto_dispatch_surface_rejects_invalid_mcp_schema_before_probe(
        self,
        tmp_path: Path,
        live_mcp: bool,
        config_text: str,
        expected_failure: str,
    ) -> None:
        """Codex transport and shared config must fail closed before live launch."""
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        (codex_dir / "config.toml").write_text(config_text, encoding="utf-8")
        live_probe = AsyncMock(return_value=_REQUIRED_CODEX_AUTO_TOOLS_FOR_TEST)

        with patch("ouroboros.cli.commands.codex._list_stdio_mcp_tool_names", live_probe):
            failures = _check_auto_dispatch_surface(codex_dir, live_mcp=live_mcp)

        assert any(expected_failure in failure for failure in failures)
        live_probe.assert_not_awaited()

    @pytest.mark.parametrize("live_mcp", [False, True], ids=["static", "live"])
    def test_check_auto_dispatch_surface_accepts_canonical_stdio_schema(
        self,
        tmp_path: Path,
        live_mcp: bool,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        (codex_dir / "config.toml").write_text(
            _CANONICAL_CODEX_MCP_ENTRY,
            encoding="utf-8",
        )
        live_probe = AsyncMock(return_value=_REQUIRED_CODEX_AUTO_TOOLS_FOR_TEST)

        with patch("ouroboros.cli.commands.codex._list_stdio_mcp_tool_names", live_probe):
            assert _check_auto_dispatch_surface(codex_dir, live_mcp=live_mcp) == []

        if live_mcp:
            live_probe.assert_awaited_once()
        else:
            live_probe.assert_not_awaited()

    @pytest.mark.parametrize("live_mcp", [False, True], ids=["static", "live"])
    @pytest.mark.parametrize("approval_mode", ["auto", "prompt", "approve"])
    def test_check_auto_dispatch_surface_accepts_shared_stdio_schema(
        self,
        tmp_path: Path,
        live_mcp: bool,
        approval_mode: str,
    ) -> None:
        shared_fields = (
            "required = true\n"
            "supports_parallel_tool_calls = true\n"
            f'default_tools_approval_mode = "{approval_mode}"\n'
            'scopes = ["openid", "profile"]\n'
            'name = "Ouroboros"\n'
            "tools = { ouroboros_start_auto = "
            f'{{ approval_mode = "{approval_mode}" }} }}\n'
        )
        config_text = _CANONICAL_CODEX_MCP_ENTRY.replace(
            "[mcp_servers.ouroboros]\n",
            "[mcp_servers.ouroboros]\n" + shared_fields,
            1,
        )
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        (codex_dir / "config.toml").write_text(config_text, encoding="utf-8")
        live_probe = AsyncMock(return_value=_REQUIRED_CODEX_AUTO_TOOLS_FOR_TEST)

        with patch("ouroboros.cli.commands.codex._list_stdio_mcp_tool_names", live_probe):
            assert _check_auto_dispatch_surface(codex_dir, live_mcp=live_mcp) == []

        if live_mcp:
            live_probe.assert_awaited_once()
        else:
            live_probe.assert_not_awaited()

    @pytest.mark.parametrize("live_mcp", [False, True], ids=["static", "live"])
    def test_check_auto_dispatch_surface_accepts_extended_http_schema(
        self,
        tmp_path: Path,
        live_mcp: bool,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        (codex_dir / "config.toml").write_text(
            "[mcp_servers.ouroboros]\n"
            'url = "https://ouroboros.example/mcp"\n'
            'bearer_token_env_var = "OUROBOROS_TOKEN"\n'
            'http_headers = { X_Client = "ouroboros" }\n'
            'env_http_headers = { X_Api_Key = "OUROBOROS_API_KEY" }\n'
            "enabled = true\n"
            "required = true\n"
            "supports_parallel_tool_calls = true\n"
            'default_tools_approval_mode = "prompt"\n'
            'scopes = ["openid", "profile"]\n'
            'oauth = { client_id = "ouroboros" }\n'
            'oauth_resource = "https://ouroboros.example"\n'
            'name = "Ouroboros"\n'
            'tools = { ouroboros_start_auto = { approval_mode = "approve" } }\n',
            encoding="utf-8",
        )
        live_probe = AsyncMock(return_value=_REQUIRED_CODEX_AUTO_TOOLS_FOR_TEST)

        with patch("ouroboros.cli.commands.codex._list_stdio_mcp_tool_names", live_probe):
            failures = _check_auto_dispatch_surface(codex_dir, live_mcp=live_mcp)

        if live_mcp:
            assert any("uses `url`" in failure for failure in failures)
            assert any("verifies only stdio `command` entries" in failure for failure in failures)
        else:
            assert failures == []
        live_probe.assert_not_awaited()

    @pytest.mark.parametrize("live_mcp", [False, True], ids=["static", "live"])
    @pytest.mark.parametrize(
        ("activation_config", "expected_failure"),
        [
            ("enabled = false\n", "is disabled (`enabled = false`)"),
            (
                'disabled_tools = ["ouroboros_start_auto"]\n',
                ".disabled_tools blocks required auto tools: ouroboros_start_auto",
            ),
            (
                'enabled_tools = ["ouroboros_start_auto"]\n',
                ".enabled_tools omits required auto tools:",
            ),
        ],
        ids=["server-disabled", "required-tool-denied", "required-tools-not-allowed"],
    )
    def test_check_auto_dispatch_surface_rejects_inactive_mcp_contract_before_probe(
        self,
        tmp_path: Path,
        live_mcp: bool,
        activation_config: str,
        expected_failure: str,
    ) -> None:
        """A manually launchable server is not proof that Codex exposes its tools."""
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        (codex_dir / "config.toml").write_text(
            "[mcp_servers.ouroboros]\n"
            + activation_config
            + 'command = "uvx"\n'
            + 'args = ["--isolated", "--python", ">=3.12", "--from", '
            '"ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"]\n'
            "[mcp_servers.ouroboros.env]\n"
            'OUROBOROS_AGENT_RUNTIME = "codex"\n'
            'OUROBOROS_LLM_BACKEND = "codex"\n',
            encoding="utf-8",
        )
        live_probe = AsyncMock(return_value=_REQUIRED_CODEX_AUTO_TOOLS_FOR_TEST)

        with patch("ouroboros.cli.commands.codex._list_stdio_mcp_tool_names", live_probe):
            failures = _check_auto_dispatch_surface(codex_dir, live_mcp=live_mcp)

        assert any(expected_failure in failure for failure in failures)
        live_probe.assert_not_awaited()

    @pytest.mark.parametrize("live_mcp", [False, True], ids=["static", "live"])
    def test_check_auto_dispatch_surface_accepts_positive_mcp_tool_filters(
        self,
        tmp_path: Path,
        live_mcp: bool,
    ) -> None:
        """Allow-lists may include extras and deny-lists may block unrelated tools."""
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        enabled_tools = sorted({*_REQUIRED_CODEX_AUTO_TOOLS_FOR_TEST, "unrelated_extra"})
        (codex_dir / "config.toml").write_text(
            "[mcp_servers.ouroboros]\n"
            "enabled = true\n"
            f"enabled_tools = {json.dumps(enabled_tools)}\n"
            'disabled_tools = ["unrelated_disabled"]\n'
            'command = "uvx"\n'
            'args = ["--isolated", "--python", ">=3.12", "--from", '
            '"ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"]\n'
            "[mcp_servers.ouroboros.env]\n"
            'OUROBOROS_AGENT_RUNTIME = "codex"\n'
            'OUROBOROS_LLM_BACKEND = "codex"\n',
            encoding="utf-8",
        )
        live_probe = AsyncMock(return_value=_REQUIRED_CODEX_AUTO_TOOLS_FOR_TEST)

        with patch("ouroboros.cli.commands.codex._list_stdio_mcp_tool_names", live_probe):
            assert _check_auto_dispatch_surface(codex_dir, live_mcp=live_mcp) == []

        if live_mcp:
            live_probe.assert_awaited_once()
        else:
            live_probe.assert_not_awaited()

    @pytest.mark.parametrize("live_mcp", [False, True], ids=["static", "live"])
    @pytest.mark.parametrize(
        ("activation_config", "expected_failure"),
        [
            ('enabled = "false"\n', ".enabled must be a boolean"),
            ('enabled_tools = "ouroboros_start_auto"\n', ".enabled_tools must be an array"),
            (
                'enabled_tools = ["ouroboros_start_auto", 7]\n',
                ".enabled_tools contains non-string values: index 1 (int)",
            ),
            ('disabled_tools = "unrelated"\n', ".disabled_tools must be an array"),
            (
                'disabled_tools = ["unrelated", false]\n',
                ".disabled_tools contains non-string values: index 1 (bool)",
            ),
        ],
        ids=[
            "untyped-enabled",
            "scalar-enabled-tools",
            "untyped-enabled-tool",
            "scalar-disabled-tools",
            "untyped-disabled-tool",
        ],
    )
    def test_check_auto_dispatch_surface_rejects_untyped_activation_before_probe(
        self,
        tmp_path: Path,
        live_mcp: bool,
        activation_config: str,
        expected_failure: str,
    ) -> None:
        """Doctor mirrors Codex's boolean and array-of-string config types."""
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        (codex_dir / "config.toml").write_text(
            "[mcp_servers.ouroboros]\n"
            + activation_config
            + 'command = "uvx"\n'
            + 'args = ["--isolated", "--python", ">=3.12", "--from", '
            '"ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"]\n'
            "[mcp_servers.ouroboros.env]\n"
            'OUROBOROS_AGENT_RUNTIME = "codex"\n'
            'OUROBOROS_LLM_BACKEND = "codex"\n',
            encoding="utf-8",
        )
        live_probe = AsyncMock(return_value=_REQUIRED_CODEX_AUTO_TOOLS_FOR_TEST)

        with patch("ouroboros.cli.commands.codex._list_stdio_mcp_tool_names", live_probe):
            failures = _check_auto_dispatch_surface(codex_dir, live_mcp=live_mcp)

        assert any(expected_failure in failure for failure in failures)
        live_probe.assert_not_awaited()

    @pytest.mark.parametrize("live_mcp", [False, True], ids=["static", "live"])
    @pytest.mark.parametrize(
        ("execution_config", "field"),
        [
            ('cwd = "."\n', "cwd"),
            ('env_vars = ["PATH"]\n', "env_vars"),
            ('experimental_environment = "remote"\n', "experimental_environment"),
            ("startup_timeout_sec = 0.25\n", "startup_timeout_sec"),
            ("startup_timeout_ms = 250\n", "startup_timeout_ms"),
            ("tool_timeout_sec = 0.001\n", "tool_timeout_sec"),
        ],
        ids=[
            "working-directory",
            "ambient-environment",
            "experimental-execution-environment",
            "startup-timeout-seconds",
            "startup-timeout-milliseconds",
            "tool-timeout-seconds",
        ],
    )
    def test_check_auto_dispatch_surface_rejects_unmodeled_execution_fields_before_probe(
        self,
        tmp_path: Path,
        live_mcp: bool,
        execution_config: str,
        field: str,
    ) -> None:
        """Doctor cannot approve a launcher after dropping Codex execution controls."""
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        (codex_dir / "config.toml").write_text(
            "[mcp_servers.ouroboros]\n"
            + execution_config
            + 'command = "uvx"\n'
            + 'args = ["--isolated", "--python", ">=3.12", "--from", '
            '"ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"]\n'
            "[mcp_servers.ouroboros.env]\n"
            'OUROBOROS_AGENT_RUNTIME = "codex"\n'
            'OUROBOROS_LLM_BACKEND = "codex"\n',
            encoding="utf-8",
        )
        live_probe = AsyncMock(return_value=_REQUIRED_CODEX_AUTO_TOOLS_FOR_TEST)

        with patch("ouroboros.cli.commands.codex._list_stdio_mcp_tool_names", live_probe):
            failures = _check_auto_dispatch_surface(codex_dir, live_mcp=live_mcp)

        assert any(
            f"[mcp_servers.ouroboros].{field} is unsupported by doctor" in failure
            for failure in failures
        )
        live_probe.assert_not_awaited()

    def test_check_auto_dispatch_surface_rejects_custom_command_mcp_entry(
        self,
        tmp_path: Path,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        (codex_dir / "config.toml").write_text(
            '[mcp_servers.ouroboros]\ncommand = "/opt/bin/ob-mcp-wrapper"\nargs = ["--stdio"]\n',
            encoding="utf-8",
        )

        failures = _check_auto_dispatch_surface(codex_dir)

        assert any("not a setup-supported executable" in failure for failure in failures)

    def test_check_auto_dispatch_surface_reports_uvx_without_mcp_extra(
        self,
        tmp_path: Path,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        (codex_dir / "config.toml").write_text(
            "[mcp_servers.ouroboros]\n"
            'command = "uvx"\n'
            'args = ["--from", "ouroboros-ai", "ouroboros", "mcp", "serve"]\n',
            encoding="utf-8",
        )

        failures = _check_auto_dispatch_surface(codex_dir)

        assert any("without the `mcp` extra" in failure for failure in failures)

    def test_check_auto_dispatch_surface_reports_uvx_without_tool_isolation(
        self,
        tmp_path: Path,
    ) -> None:
        """An installed MCP 1 tool must not satisfy an MCP 2 ``--from`` entry."""
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        (codex_dir / "config.toml").write_text(
            "[mcp_servers.ouroboros]\n"
            'command = "uvx"\n'
            'args = ["--from", "ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"]\n',
            encoding="utf-8",
        )

        failures = _check_auto_dispatch_surface(codex_dir)

        assert any("may reuse an installed MCP 1.x tool" in failure for failure in failures)

    def test_check_auto_dispatch_surface_reports_uvx_without_python_floor(
        self,
        tmp_path: Path,
    ) -> None:
        """Isolation still needs a supported interpreter for package resolution."""
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        (codex_dir / "config.toml").write_text(
            "[mcp_servers.ouroboros]\n"
            'command = "uvx"\n'
            'args = ["--isolated", "--from", "ouroboros-ai[mcp]", '
            '"ouroboros", "mcp", "serve"]\n',
            encoding="utf-8",
        )

        failures = _check_auto_dispatch_surface(codex_dir)

        assert any("does not select the supported Python floor" in failure for failure in failures)

    def test_check_auto_dispatch_surface_reports_incompatible_python_selector(
        self,
        tmp_path: Path,
    ) -> None:
        """A present but incompatible selector must not satisfy the floor contract."""
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        (codex_dir / "config.toml").write_text(
            "[mcp_servers.ouroboros]\n"
            'command = "uvx"\n'
            'args = ["--isolated", "--python", "3.11", "--from", '
            '"ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"]\n',
            encoding="utf-8",
        )

        failures = _check_auto_dispatch_surface(codex_dir)

        assert any("does not select the supported Python floor" in failure for failure in failures)

    @pytest.mark.parametrize(
        ("args", "expected_failure"),
        [
            (
                [
                    "--isolated",
                    "--python",
                    ">=3.12",
                    "--from",
                    "ouroboros-ai[mcp]>=0.26.0",
                    "ouroboros",
                    "mcp",
                    "serve",
                ],
                "exactly one `--from ouroboros-ai[mcp]`",
            ),
            (
                [
                    "--isolated",
                    "--python",
                    ">=3.12",
                    "--from",
                    "ouroboros-ai[MCP]",
                    "ouroboros",
                    "mcp",
                    "serve",
                ],
                "exactly one `--from ouroboros-ai[mcp]`",
            ),
            (
                [
                    "--isolated",
                    "--python",
                    ">=3.12",
                    "--python",
                    "3.11",
                    "--from",
                    "ouroboros-ai[mcp]",
                    "ouroboros",
                    "mcp",
                    "serve",
                ],
                "exactly one `--python >=3.12`",
            ),
            (
                [
                    "--isolated",
                    "--python",
                    ">=3.12",
                    "--from",
                    "ouroboros-ai[mcp]",
                    "ouroboros",
                    "mcp",
                    "serve",
                    "--python=3.11",
                ],
                "arbitrary arguments are not allowed after",
            ),
            (
                [
                    "ouroboros",
                    "mcp",
                    "serve",
                    "--isolated",
                    "--python",
                    ">=3.12",
                    "--from",
                    "ouroboros-ai[mcp]",
                ],
                "arbitrary arguments are not allowed after",
            ),
            (
                [
                    "--isolated",
                    "--python",
                    ">=3.12",
                    "--from",
                    "ouroboros-ai[mcp]",
                    "--isolated",
                    "ouroboros",
                    "mcp",
                    "serve",
                ],
                "exactly one `--isolated`",
            ),
        ],
    )
    def test_check_auto_dispatch_surface_rejects_noncanonical_uvx_semantics(
        self,
        tmp_path: Path,
        args: list[str],
        expected_failure: str,
    ) -> None:
        """Equivalent-looking argv must not weaken the isolated launcher contract."""
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        rendered_args = ", ".join(repr(argument) for argument in args)
        (codex_dir / "config.toml").write_text(
            f'[mcp_servers.ouroboros]\ncommand = "uvx"\nargs = [{rendered_args}]\n',
            encoding="utf-8",
        )

        failures = _check_auto_dispatch_surface(codex_dir)

        assert any(expected_failure in failure for failure in failures)
        assert any("is not canonical" in failure for failure in failures)

    def test_check_auto_dispatch_surface_accepts_shipped_codex_config(self, tmp_path: Path) -> None:
        """The repository's host-specific Codex config is a doctor-positive contract."""
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        (codex_dir / "config.toml").write_text(
            (_REPO_ROOT / ".codex" / "config.toml").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        assert _check_auto_dispatch_surface(codex_dir) == []

    def test_shipped_mcp_codex_json_uses_doctor_accepted_launcher(self) -> None:
        """The second shipped Codex registration must share the same static contract."""
        document = json.loads((_REPO_ROOT / ".mcp.codex.json").read_text(encoding="utf-8"))
        entry = document["mcpServers"]["ouroboros"]
        failures: list[str] = []

        codex_command._check_mcp_runtime_dependency_surface(
            entry["command"], entry["args"], entry.get("env", {}), failures
        )

        assert failures == []

    @pytest.mark.parametrize("pin", ["ouroboros-ai[mcp]", f"ouroboros-ai[mcp]=={_PLUGIN_VERSION}"])
    def test_doctor_accepts_unpinned_and_release_pinned_requirements(self, pin: str) -> None:
        """#2066: the shipped plugin descriptors pin the served package, so
        the canonical launcher is accepted with the exact release pin too."""
        args = [
            "--isolated",
            "--python",
            ">=3.12",
            "--from",
            pin,
            "ouroboros",
            "mcp",
            "serve",
            "--runtime",
            "codex",
            "--llm-backend",
            "codex",
        ]
        failures: list[str] = []

        codex_command._check_mcp_runtime_dependency_surface("uvx", args, {}, failures)

        assert failures == []

    def test_doctor_rejects_release_pin_that_does_not_match_manifest(self) -> None:
        args = [
            "--isolated",
            "--python",
            ">=3.12",
            "--from",
            "ouroboros-ai[mcp]==99.99.99",
            "ouroboros",
            "mcp",
            "serve",
            "--runtime",
            "codex",
            "--llm-backend",
            "codex",
        ]
        failures: list[str] = []

        codex_command._check_mcp_runtime_dependency_surface("uvx", args, {}, failures)

        assert any("manifest-matching" in failure for failure in failures)
        assert any("is not canonical" in failure for failure in failures)

    @pytest.mark.parametrize(
        "requirement",
        [
            "ouroboros-ai[mcp]>=0.51.2",
            "ouroboros-ai==0.51.2",
            "other-package[mcp]==0.51.2",
            "ouroboros-ai[mcp]==0.51.2.dev5",
            "ouroboros-ai[mcp]==0.51.2+local",
        ],
    )
    def test_doctor_still_rejects_arbitrary_requirements(self, requirement: str) -> None:
        """#2066: only the canonical requirement — unpinned or with a
        release-shaped exact pin — is accepted; ranges, other packages,
        a missing mcp extra, and non-release pins stay rejected."""
        args = [
            "--isolated",
            "--python",
            ">=3.12",
            "--from",
            requirement,
            "ouroboros",
            "mcp",
            "serve",
            "--runtime",
            "codex",
            "--llm-backend",
            "codex",
        ]
        failures: list[str] = []

        codex_command._check_mcp_runtime_dependency_surface("uvx", args, {}, failures)

        assert failures != []

    def test_check_auto_dispatch_surface_accepts_setup_generated_release_config(
        self,
        tmp_path: Path,
    ) -> None:
        """Setup's env-selected release launcher must remain doctor-compatible."""
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        uvx_path = shutil.which("uvx")
        assert uvx_path is not None
        with (
            patch.object(setup_command, "_is_dev_ouroboros_build", return_value=False),
            patch.object(
                setup_command,
                "_codex_release_mcp_launcher",
                return_value=(uvx_path, list(setup_command._CODEX_UVX_MCP_ARGS)),
            ),
        ):
            rendered = setup_command._render_codex_mcp_section()

        assert rendered is not None
        parsed = tomllib.loads(rendered)
        entry = parsed["mcp_servers"]["ouroboros"]
        assert entry["env"] == {
            "OUROBOROS_AGENT_RUNTIME": "codex",
            "OUROBOROS_LLM_BACKEND": "codex",
        }
        (codex_dir / "config.toml").write_text(rendered, encoding="utf-8")
        assert _check_auto_dispatch_surface(codex_dir) == []

    @pytest.mark.parametrize(
        "env_lines",
        [
            "",
            '[mcp_servers.ouroboros.env]\nOUROBOROS_AGENT_RUNTIME = "codex"\n',
            (
                '[mcp_servers.ouroboros.env]\nOUROBOROS_AGENT_RUNTIME = "codex"\n'
                'OUROBOROS_LLM_BACKEND = "claude"\n'
            ),
            (
                '[mcp_servers.ouroboros.env]\nOUROBOROS_AGENT_RUNTIME = "codex"\n'
                'OUROBOROS_LLM_BACKEND = "codex"\nOUROBOROS_RUNTIME = "claude"\n'
            ),
        ],
    )
    def test_check_auto_dispatch_surface_rejects_unsafe_base_launcher_runtime_env(
        self,
        tmp_path: Path,
        env_lines: str,
    ) -> None:
        """The default server runtime must never choose a non-Codex backend implicitly."""
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        (codex_dir / "config.toml").write_text(
            "[mcp_servers.ouroboros]\n"
            'command = "uvx"\n'
            'args = ["--isolated", "--python", ">=3.12", "--from", '
            '"ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"]\n' + env_lines,
            encoding="utf-8",
        )

        failures = _check_auto_dispatch_surface(codex_dir)

        assert any(
            "requires runtime environment selectors" in failure
            or "conflicting selectors" in failure
            for failure in failures
        )

    def test_check_auto_dispatch_surface_live_rejects_hostile_runtime_env_before_probe(
        self,
        tmp_path: Path,
    ) -> None:
        """Live tool exposure cannot override an unsafe static runtime selection."""
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        (codex_dir / "config.toml").write_text(
            "[mcp_servers.ouroboros]\n"
            'command = "uvx"\n'
            'args = ["--isolated", "--python", ">=3.12", "--from", '
            '"ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"]\n'
            "[mcp_servers.ouroboros.env]\n"
            'OUROBOROS_AGENT_RUNTIME = "codex"\n'
            'OUROBOROS_LLM_BACKEND = "claude"\n',
            encoding="utf-8",
        )
        live_probe = AsyncMock(return_value=_REQUIRED_CODEX_AUTO_TOOLS_FOR_TEST)

        with patch("ouroboros.cli.commands.codex._list_stdio_mcp_tool_names", live_probe):
            failures = _check_auto_dispatch_surface(codex_dir, live_mcp=True)

        assert any("conflicting selectors" in failure for failure in failures)
        live_probe.assert_not_awaited()

    @pytest.mark.parametrize("live_mcp", [False, True], ids=["static", "live"])
    @pytest.mark.parametrize("nested_value", ["", "0", "false", "1"])
    def test_check_auto_dispatch_surface_rejects_persisted_nested_guard_before_probe(
        self,
        tmp_path: Path,
        live_mcp: bool,
        nested_value: str,
    ) -> None:
        """The process-internal recursion guard cannot be persisted in Codex config."""
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        with (codex_dir / "config.toml").open("a", encoding="utf-8") as config:
            config.write(f'_OUROBOROS_NESTED = "{nested_value}"\n')
        live_probe = AsyncMock(return_value=_REQUIRED_CODEX_AUTO_TOOLS_FOR_TEST)

        with patch("ouroboros.cli.commands.codex._list_stdio_mcp_tool_names", live_probe):
            failures = _check_auto_dispatch_surface(codex_dir, live_mcp=live_mcp)

        assert any("persists `_OUROBOROS_NESTED`" in failure for failure in failures)
        live_probe.assert_not_awaited()

    @pytest.mark.parametrize(
        "near_miss_key",
        ["OUROBOROS_NESTED", "_ouroboros_nested", "_OUROBOROS_NESTED_EXTRA"],
    )
    def test_check_auto_dispatch_surface_matches_nested_guard_key_exactly(
        self,
        tmp_path: Path,
        near_miss_key: str,
    ) -> None:
        """Unrelated environment keys must not be mistaken for the private sentinel."""
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        with (codex_dir / "config.toml").open("a", encoding="utf-8") as config:
            config.write(f'{near_miss_key} = "1"\n')

        assert _check_auto_dispatch_surface(codex_dir) == []

    @pytest.mark.parametrize("live_mcp", [False, True], ids=["static", "live"])
    @pytest.mark.parametrize(
        ("replacement", "expected_failure"),
        [
            ("args = 7\n", ".args must be an array of strings"),
            (
                'args = ["--isolated", 7, "ouroboros", "mcp", "serve"]\n',
                ".args contains non-string values",
            ),
            (
                'args = ["--isolated", ["--python", ">=3.12"], "ouroboros"]\n',
                ".args contains non-string values",
            ),
            (
                'args = ["mcp", "serve"]\nenv = ["codex"]\n',
                ".env must be a table of string values",
            ),
            (
                'args = ["mcp", "serve"]\n[mcp_servers.ouroboros.env]\n'
                "OUROBOROS_AGENT_RUNTIME = 7\n",
                ".env contains non-string values",
            ),
            (
                'args = ["mcp", "serve"]\n[mcp_servers.ouroboros.env]\n'
                'OUROBOROS_AGENT_RUNTIME = ["codex"]\n',
                ".env contains non-string values",
            ),
        ],
    )
    def test_check_auto_dispatch_surface_rejects_untyped_persisted_contract_before_probe(
        self,
        tmp_path: Path,
        live_mcp: bool,
        replacement: str,
        expected_failure: str,
    ) -> None:
        """Doctor must reject, not sanitize, non-string persisted argv and env values."""
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        (codex_dir / "config.toml").write_text(
            '[mcp_servers.ouroboros]\ncommand = "uvx"\n' + replacement,
            encoding="utf-8",
        )
        live_probe = AsyncMock(return_value=_REQUIRED_CODEX_AUTO_TOOLS_FOR_TEST)

        with patch("ouroboros.cli.commands.codex._list_stdio_mcp_tool_names", live_probe):
            failures = _check_auto_dispatch_surface(codex_dir, live_mcp=live_mcp)

        assert any(expected_failure in failure for failure in failures)
        live_probe.assert_not_awaited()

    @pytest.mark.parametrize(
        ("args", "env", "expected_failure"),
        [
            (
                ["--isolated", None, "ouroboros", "mcp", "serve"],
                {
                    "OUROBOROS_AGENT_RUNTIME": "codex",
                    "OUROBOROS_LLM_BACKEND": "codex",
                },
                "args contains non-string values",
            ),
            (
                list(codex_command._CANONICAL_CODEX_UVX_MCP_ARGS),
                {"OUROBOROS_AGENT_RUNTIME": "codex", "OUROBOROS_LLM_BACKEND": None},
                "env contains non-string values",
            ),
            (
                list(codex_command._CANONICAL_CODEX_UVX_MCP_ARGS),
                None,
                "env must be a mapping",
            ),
        ],
    )
    def test_runtime_dependency_surface_rejects_null_without_sanitizing(
        self,
        args: list[object],
        env: object,
        expected_failure: str,
    ) -> None:
        """Defense-in-depth callers cannot use JSON-like null to change the argv contract."""
        failures: list[str] = []

        codex_command._check_mcp_runtime_dependency_surface("uvx", args, env, failures)

        assert any(expected_failure in failure for failure in failures)

    def test_check_auto_dispatch_surface_reports_direct_ouroboros_without_mcp_import(
        self,
        tmp_path: Path,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        (codex_dir / "config.toml").write_text(
            "[mcp_servers.ouroboros]\n"
            'command = "/home/user/.local/bin/ouroboros"\n'
            'args = ["mcp", "serve", "--runtime", "codex", '
            '"--llm-backend", "codex"]\n',
            encoding="utf-8",
        )

        with patch("ouroboros.cli.commands.codex.importlib.util.find_spec", return_value=None):
            failures = _check_auto_dispatch_surface(codex_dir)

        assert any("cannot import `mcp`" in failure for failure in failures)
        assert any("--mcp-mode stdio" in failure for failure in failures)

    def test_check_auto_dispatch_surface_accepts_direct_ouroboros_with_mcp_import(
        self,
        tmp_path: Path,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        (codex_dir / "config.toml").write_text(
            "[mcp_servers.ouroboros]\n"
            'command = "ouroboros"\n'
            'args = ["mcp", "serve", "--runtime", "codex", '
            '"--llm-backend", "codex"]\n',
            encoding="utf-8",
        )

        with patch("ouroboros.cli.commands.codex.importlib.util.find_spec", return_value=object()):
            assert _check_auto_dispatch_surface(codex_dir) == []

    def test_check_auto_dispatch_surface_live_mcp_uses_configured_direct_command_without_local_mcp(
        self,
        tmp_path: Path,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        (codex_dir / "config.toml").write_text(
            "[mcp_servers.ouroboros]\n"
            'command = "ouroboros"\n'
            'args = ["mcp", "serve", "--runtime", "codex", '
            '"--llm-backend", "codex"]\n',
            encoding="utf-8",
        )
        live_probe = AsyncMock(return_value=_REQUIRED_CODEX_AUTO_TOOLS_FOR_TEST)

        with (
            patch("ouroboros.cli.commands.codex.importlib.util.find_spec", return_value=None),
            patch("ouroboros.cli.commands.codex._list_stdio_mcp_tool_names", live_probe),
        ):
            assert _check_auto_dispatch_surface(codex_dir, live_mcp=True) == []

        live_probe.assert_awaited_once_with(
            "ouroboros",
            (
                "mcp",
                "serve",
                "--runtime",
                "codex",
                "--llm-backend",
                "codex",
            ),
            {},
        )

    def test_check_auto_dispatch_surface_live_mcp_verifies_required_tools(
        self,
        tmp_path: Path,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        (codex_dir / "config.toml").write_text(
            "[mcp_servers.ouroboros]\n"
            'command = "ouroboros"\n'
            'args = ["mcp", "serve"]\n'
            "[mcp_servers.ouroboros.env]\n"
            'OUROBOROS_AGENT_RUNTIME = "codex"\n'
            'OUROBOROS_LLM_BACKEND = "codex"\n',
            encoding="utf-8",
        )

        live_probe = AsyncMock(
            return_value={
                "ouroboros_auto",
                "ouroboros_start_auto",
                "ouroboros_job_status",
                "ouroboros_job_wait",
                "ouroboros_job_result",
                "ouroboros_interview",
                "ouroboros_generate_seed",
            }
        )
        with (
            patch("ouroboros.cli.commands.codex.importlib.util.find_spec", return_value=object()),
            patch("ouroboros.cli.commands.codex._list_stdio_mcp_tool_names", live_probe),
        ):
            assert _check_auto_dispatch_surface(codex_dir, live_mcp=True) == []

        live_probe.assert_awaited_once_with(
            "ouroboros",
            ("mcp", "serve"),
            {
                "OUROBOROS_AGENT_RUNTIME": "codex",
                "OUROBOROS_LLM_BACKEND": "codex",
            },
        )

    @pytest.mark.parametrize("live_mcp", [False, True], ids=["static", "live"])
    @pytest.mark.parametrize(
        ("command", "args", "env_lines"),
        [
            (
                "ouroboros",
                '["mcp", "serve", "--runtime", "codex"]',
                ('OUROBOROS_AGENT_RUNTIME = "codex"\nOUROBOROS_LLM_BACKEND = "claude_code"\n'),
            ),
            (
                sys.executable,
                '["-m", "ouroboros", "mcp", "serve", "--runtime", "claude-cli"]',
                "",
            ),
            (
                "uv",
                '["run", "--with", "ouroboros-ai[mcp]", "ouroboros", "mcp", '
                '"serve", "--runtime", "claude-cli"]',
                "",
            ),
        ],
    )
    def test_check_auto_dispatch_surface_rejects_non_codex_runtime_for_every_launcher(
        self,
        tmp_path: Path,
        live_mcp: bool,
        command: str,
        args: str,
        env_lines: str,
    ) -> None:
        """Recognized launchers cannot route the MCP host to another backend."""
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        env_table = "[mcp_servers.ouroboros.env]\n" + env_lines if env_lines else ""
        (codex_dir / "config.toml").write_text(
            "[mcp_servers.ouroboros]\n"
            f"command = {json.dumps(command)}\n"
            f"args = {args}\n" + env_table,
            encoding="utf-8",
        )
        live_probe = AsyncMock(return_value=_REQUIRED_CODEX_AUTO_TOOLS_FOR_TEST)

        with (
            patch("ouroboros.cli.commands.codex.importlib.util.find_spec", return_value=object()),
            patch("ouroboros.cli.commands.codex._list_stdio_mcp_tool_names", live_probe),
        ):
            failures = _check_auto_dispatch_surface(codex_dir, live_mcp=live_mcp)

        assert failures
        live_probe.assert_not_awaited()

    @pytest.mark.parametrize(
        ("command", "args", "env_lines"),
        [
            (
                "ouroboros",
                '["mcp", "serve", "--runtime", "codex", "--llm-backend", "codex"]',
                "",
            ),
            (
                "ouroboros",
                '["mcp", "serve"]',
                ('OUROBOROS_AGENT_RUNTIME = "codex"\nOUROBOROS_LLM_BACKEND = "codex"\n'),
            ),
            (
                sys.executable,
                '["-m", "ouroboros", "mcp", "serve", "--runtime", "codex", '
                '"--llm-backend", "codex"]',
                "",
            ),
            (
                sys.executable,
                '["-m", "ouroboros", "mcp", "serve"]',
                ('OUROBOROS_AGENT_RUNTIME = "codex"\nOUROBOROS_LLM_BACKEND = "codex"\n'),
            ),
        ],
    )
    def test_check_auto_dispatch_surface_accepts_setup_generated_local_runtime_forms(
        self,
        tmp_path: Path,
        command: str,
        args: str,
        env_lines: str,
    ) -> None:
        """Direct and module launchers accept only setup's two exact runtime forms."""
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        env_table = "[mcp_servers.ouroboros.env]\n" + env_lines if env_lines else ""
        (codex_dir / "config.toml").write_text(
            "[mcp_servers.ouroboros]\n"
            f"command = {json.dumps(command)}\n"
            f"args = {args}\n" + env_table,
            encoding="utf-8",
        )

        with patch("ouroboros.cli.commands.codex.importlib.util.find_spec", return_value=object()):
            assert _check_auto_dispatch_surface(codex_dir) == []

    @pytest.mark.parametrize("live_mcp", [False, True], ids=["static", "live"])
    @pytest.mark.parametrize(
        ("command", "args"),
        [
            ("/bin/true", "[]"),
            (
                "/bin/echo",
                '["-m", "ouroboros", "mcp", "serve", "--runtime", "codex", '
                '"--llm-backend", "codex"]',
            ),
            (
                sys.executable,
                '["-I", "-m", "ouroboros", "mcp", "serve", "--runtime", "claude-cli"]',
            ),
        ],
    )
    def test_check_auto_dispatch_surface_rejects_unsupported_executable_arg_pairs(
        self,
        tmp_path: Path,
        live_mcp: bool,
        command: str,
        args: str,
    ) -> None:
        """Canonical-looking argv cannot authorize an unknown or modified executable."""
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        (codex_dir / "config.toml").write_text(
            f"[mcp_servers.ouroboros]\ncommand = {json.dumps(command)}\nargs = {args}\n",
            encoding="utf-8",
        )
        live_probe = AsyncMock(return_value=_REQUIRED_CODEX_AUTO_TOOLS_FOR_TEST)

        with patch("ouroboros.cli.commands.codex._list_stdio_mcp_tool_names", live_probe):
            failures = _check_auto_dispatch_surface(codex_dir, live_mcp=live_mcp)

        assert failures
        live_probe.assert_not_awaited()

    def test_list_stdio_mcp_tool_names_uses_jsonl_protocol_without_local_mcp_import(
        self,
        tmp_path: Path,
    ) -> None:
        assert _MCP_PROTOCOL_VERSION == "2024-11-05"
        server_path = tmp_path / "fake_mcp_server.py"
        server_path.write_text(
            textwrap.dedent(
                r"""
                import json
                import sys

                sys.stderr.buffer.write(b"startup noise\\n" * 20000)
                sys.stderr.buffer.flush()

                def read_message():
                    line = sys.stdin.buffer.readline()
                    if not line:
                        raise SystemExit(0)
                    return json.loads(line)

                def write_message(message):
                    body = json.dumps(message).encode("utf-8")
                    sys.stdout.buffer.write(body + b"\n")
                    sys.stdout.buffer.flush()

                initialize = read_message()
                if initialize["params"]["protocolVersion"] != "2024-11-05":
                    raise SystemExit(
                        f"unsupported protocol {initialize['params']['protocolVersion']}"
                    )
                write_message({
                    "jsonrpc": "2.0",
                    "id": initialize["id"],
                    "result": {
                        "protocolVersion": initialize["params"]["protocolVersion"],
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake", "version": "1.0.0"},
                    },
                })
                read_message()  # notifications/initialized
                write_message({
                    "jsonrpc": "2.0",
                    "method": "notifications/message",
                    "params": {"level": "info", "data": "skip non-response messages"},
                })
                tools_list = read_message()
                write_message({
                    "jsonrpc": "2.0",
                    "id": tools_list["id"],
                    "result": {
                        "tools": [
                            {"name": "ouroboros_auto", "inputSchema": {"type": "object"}},
                            {"name": "ouroboros_start_auto", "inputSchema": {"type": "object"}},
                            {"name": "ouroboros_job_status", "inputSchema": {"type": "object"}},
                            {"name": "ouroboros_job_wait", "inputSchema": {"type": "object"}},
                            {"name": "ouroboros_job_result", "inputSchema": {"type": "object"}},
                            {"name": "ouroboros_interview", "inputSchema": {"type": "object"}},
                            {"name": "ouroboros_generate_seed", "inputSchema": {"type": "object"}},
                        ]
                    },
                })
                """
            ),
            encoding="utf-8",
        )

        tool_names = asyncio.run(
            _list_stdio_mcp_tool_names(sys.executable, (str(server_path),), {})
        )

        assert tool_names >= _REQUIRED_CODEX_AUTO_TOOLS_FOR_TEST

    @pytest.mark.skipif(os.name != "posix", reason="POSIX process-group contract")
    @pytest.mark.parametrize(
        ("ignore_sigterm", "expects_force", "wrapper_exits"),
        [
            (False, False, False),
            (True, True, False),
            (False, False, True),
            (True, True, True),
        ],
        ids=[
            "waiting-wrapper-normal-sigterm",
            "waiting-wrapper-forced-sigkill",
            "exited-wrapper-normal-sigterm",
            "exited-wrapper-forced-sigkill",
        ],
    )
    def test_list_stdio_mcp_tool_names_cleans_up_wrapper_process_group(
        self,
        tmp_path: Path,
        ignore_sigterm: bool,
        expects_force: bool,
        wrapper_exits: bool,
    ) -> None:
        """The live probe must reap both a launcher wrapper and its MCP child."""
        wrapper_path = tmp_path / "wrapper.py"
        child_path = tmp_path / "child_mcp.py"
        wrapper_pid_path = tmp_path / "wrapper.pid"
        child_pid_path = tmp_path / "child.pid"
        tool_names = sorted(_REQUIRED_CODEX_AUTO_TOOLS_FOR_TEST)
        child_path.write_text(
            textwrap.dedent(
                f"""
                import json
                import os
                from pathlib import Path
                import signal
                import sys
                import time

                if {ignore_sigterm!r}:
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
                Path({str(child_pid_path)!r}).write_text(str(os.getpid()), encoding="utf-8")

                def read_message():
                    return json.loads(sys.stdin.buffer.readline())

                def write_message(message):
                    sys.stdout.buffer.write(json.dumps(message).encode("utf-8") + b"\\n")
                    sys.stdout.buffer.flush()

                initialize = read_message()
                write_message({{
                    "jsonrpc": "2.0",
                    "id": initialize["id"],
                    "result": {{
                        "protocolVersion": initialize["params"]["protocolVersion"],
                        "capabilities": {{"tools": {{}}}},
                        "serverInfo": {{"name": "tree-child", "version": "1"}},
                    }},
                }})
                read_message()
                tools_list = read_message()
                write_message({{
                    "jsonrpc": "2.0",
                    "id": tools_list["id"],
                    "result": {{
                        "tools": [{{"name": name, "inputSchema": {{"type": "object"}}}}
                                  for name in {tool_names!r}],
                    }},
                }})
                while True:
                    time.sleep(1)
                """
            ),
            encoding="utf-8",
        )
        wrapper_path.write_text(
            textwrap.dedent(
                f"""
                import os
                from pathlib import Path
                import signal
                import subprocess
                import sys

                if {ignore_sigterm!r}:
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
                Path({str(wrapper_pid_path)!r}).write_text(str(os.getpid()), encoding="utf-8")
                child = subprocess.Popen([sys.executable, {str(child_path)!r}])
                {"" if wrapper_exits else "child.wait()"}
                """
            ),
            encoding="utf-8",
        )
        real_create_subprocess = asyncio.create_subprocess_exec
        real_signal_process = codex_command._signal_stdio_mcp_process
        spawn_options: dict[str, object] = {}

        async def _capturing_create_subprocess(*command: str, **kwargs: object):
            spawn_options.update(kwargs)
            return await real_create_subprocess(*command, **kwargs)

        with (
            patch(
                "ouroboros.cli.commands.codex.asyncio.create_subprocess_exec",
                side_effect=_capturing_create_subprocess,
            ),
            patch(
                "ouroboros.cli.commands.codex._signal_stdio_mcp_process",
                wraps=real_signal_process,
            ) as signal_process,
        ):
            exposed_tools = asyncio.run(
                _list_stdio_mcp_tool_names(sys.executable, (str(wrapper_path),), {})
            )

        assert exposed_tools >= _REQUIRED_CODEX_AUTO_TOOLS_FOR_TEST
        assert spawn_options["start_new_session"] is True
        force_calls = [call.kwargs["force"] for call in signal_process.call_args_list]
        assert force_calls[0] is False
        assert (True in force_calls) is expects_force

        wrapper_pid = int(wrapper_pid_path.read_text(encoding="utf-8"))
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            live_pids = []
            for pid in (wrapper_pid, child_pid):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    continue
                live_pids.append(pid)
            if not live_pids:
                break
            time.sleep(0.02)
        assert not live_pids

    @pytest.mark.skipif(os.name != "posix", reason="POSIX process-group contract")
    @pytest.mark.parametrize(
        ("probe_errors", "expected_sleeps"),
        [
            ([ProcessLookupError()], 0),
            ([PermissionError(), ProcessLookupError()], 1),
        ],
        ids=["missing-group-is-gone", "inaccessible-group-still-exists"],
    )
    def test_wait_for_stdio_mcp_process_group_exit_classifies_probe_errors(
        self,
        probe_errors: list[OSError],
        expected_sleeps: int,
    ) -> None:
        """EPERM is existence evidence while ESRCH proves the group is gone."""
        sleep = AsyncMock()
        with (
            patch("ouroboros.cli.commands.codex.os.killpg", side_effect=probe_errors) as probe,
            patch("ouroboros.cli.commands.codex.asyncio.sleep", sleep),
        ):
            asyncio.run(codex_command._wait_for_stdio_mcp_process_group_exit(4321))

        assert probe.call_count == len(probe_errors)
        assert sleep.await_count == expected_sleeps

    @pytest.mark.skipif(os.name != "posix", reason="POSIX process-group contract")
    def test_terminate_stdio_mcp_process_kills_group_when_wrapper_exits_first(self) -> None:
        """A reaped wrapper must not prevent TERM -> wait -> KILL for its child group."""

        class _WrapperProcess:
            stdin = None
            pid = 4321
            returncode: int | None = None
            wait = AsyncMock()

        proc = _WrapperProcess()
        signal_forces: list[bool] = []

        def _record_signal(process: _WrapperProcess, *, force: bool) -> None:
            assert process is proc
            signal_forces.append(force)
            if not force:
                process.returncode = 0

        group_wait = AsyncMock(side_effect=TimeoutError)
        with (
            patch(
                "ouroboros.cli.commands.codex._signal_stdio_mcp_process",
                side_effect=_record_signal,
            ),
            patch(
                "ouroboros.cli.commands.codex._wait_for_stdio_mcp_process_group_exit",
                group_wait,
            ),
        ):
            asyncio.run(codex_command._terminate_stdio_mcp_process(proc))  # type: ignore[arg-type]

        assert signal_forces == [False, True]
        group_wait.assert_awaited_once_with(proc.pid)
        proc.wait.assert_not_awaited()

    def test_list_stdio_mcp_tool_names_preserves_content_length_fallback(
        self,
        tmp_path: Path,
    ) -> None:
        server_path = tmp_path / "fake_header_mcp_server.py"
        server_path.write_text(
            textwrap.dedent(
                r"""
                import json
                import sys

                first_line = sys.stdin.buffer.readline()
                if first_line and first_line.lstrip().startswith(b"{"):
                    raise SystemExit(2)

                def read_message():
                    headers = {}
                    line = first_line
                    while True:
                        if not line:
                            raise SystemExit(0)
                        stripped = line.strip()
                        if not stripped:
                            break
                        name, value = stripped.decode("ascii").split(":", 1)
                        headers[name.lower()] = value.strip()
                        line = sys.stdin.buffer.readline()
                    return json.loads(sys.stdin.buffer.read(int(headers["content-length"])))

                def write_message(message):
                    body = json.dumps(message).encode("utf-8")
                    sys.stdout.buffer.write(
                        f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
                    )
                    sys.stdout.buffer.flush()

                initialize = read_message()
                write_message({
                    "jsonrpc": "2.0",
                    "id": initialize["id"],
                    "result": {
                        "protocolVersion": initialize["params"]["protocolVersion"],
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake", "version": "1.0.0"},
                    },
                })
                read_message()
                tools_list = read_message()
                write_message({
                    "jsonrpc": "2.0",
                    "id": tools_list["id"],
                    "result": {
                        "tools": [
                            {"name": "ouroboros_auto", "inputSchema": {"type": "object"}},
                            {"name": "ouroboros_start_auto", "inputSchema": {"type": "object"}},
                            {"name": "ouroboros_job_status", "inputSchema": {"type": "object"}},
                            {"name": "ouroboros_job_wait", "inputSchema": {"type": "object"}},
                            {"name": "ouroboros_job_result", "inputSchema": {"type": "object"}},
                            {"name": "ouroboros_interview", "inputSchema": {"type": "object"}},
                            {"name": "ouroboros_generate_seed", "inputSchema": {"type": "object"}},
                        ]
                    },
                })
                """
            ),
            encoding="utf-8",
        )

        tool_names = asyncio.run(
            _list_stdio_mcp_tool_names(sys.executable, (str(server_path),), {})
        )

        assert tool_names >= _REQUIRED_CODEX_AUTO_TOOLS_FOR_TEST

    def test_list_stdio_mcp_tool_names_retries_when_jsonl_initialize_send_fails(
        self,
        tmp_path: Path,
    ) -> None:
        server_path = tmp_path / "fake_header_mcp_server.py"
        server_path.write_text(
            textwrap.dedent(
                r"""
                import json
                import sys

                def read_message():
                    headers = {}
                    while True:
                        line = sys.stdin.buffer.readline()
                        if not line:
                            raise SystemExit(0)
                        stripped = line.strip()
                        if not stripped:
                            break
                        name, value = stripped.decode("ascii").split(":", 1)
                        headers[name.lower()] = value.strip()
                    return json.loads(sys.stdin.buffer.read(int(headers["content-length"])))

                def write_message(message):
                    body = json.dumps(message).encode("utf-8")
                    sys.stdout.buffer.write(
                        f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
                    )
                    sys.stdout.buffer.flush()

                initialize = read_message()
                write_message({
                    "jsonrpc": "2.0",
                    "id": initialize["id"],
                    "result": {
                        "protocolVersion": initialize["params"]["protocolVersion"],
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake", "version": "1.0.0"},
                    },
                })
                read_message()
                tools_list = read_message()
                write_message({
                    "jsonrpc": "2.0",
                    "id": tools_list["id"],
                    "result": {
                        "tools": [
                            {"name": "ouroboros_auto", "inputSchema": {"type": "object"}},
                            {"name": "ouroboros_start_auto", "inputSchema": {"type": "object"}},
                            {"name": "ouroboros_job_status", "inputSchema": {"type": "object"}},
                            {"name": "ouroboros_job_wait", "inputSchema": {"type": "object"}},
                            {"name": "ouroboros_job_result", "inputSchema": {"type": "object"}},
                            {"name": "ouroboros_interview", "inputSchema": {"type": "object"}},
                            {"name": "ouroboros_generate_seed", "inputSchema": {"type": "object"}},
                        ]
                    },
                })
                """
            ),
            encoding="utf-8",
        )
        original_send = codex_command._send_stdio_mcp_message

        async def fail_jsonl_initialize_send(proc, message, *, framing):
            if framing == "jsonl" and message.get("method") == "initialize":
                raise BrokenPipeError("pipe closed during JSONL initialize write")
            await original_send(proc, message, framing=framing)

        with patch(
            "ouroboros.cli.commands.codex._send_stdio_mcp_message",
            side_effect=fail_jsonl_initialize_send,
        ):
            tool_names = asyncio.run(
                _list_stdio_mcp_tool_names(sys.executable, (str(server_path),), {})
            )

        assert tool_names >= _REQUIRED_CODEX_AUTO_TOOLS_FOR_TEST

    def test_list_stdio_mcp_tool_names_surfaces_json_rpc_errors_without_framing_retry(
        self,
        tmp_path: Path,
    ) -> None:
        server_path = tmp_path / "fake_error_mcp_server.py"
        server_path.write_text(
            textwrap.dedent(
                r"""
                import json
                import sys

                initialize = json.loads(sys.stdin.buffer.readline())
                sys.stdout.buffer.write(json.dumps({
                    "jsonrpc": "2.0",
                    "id": initialize["id"],
                    "error": {"code": -32000, "message": "initialize rejected"},
                }).encode("utf-8") + b"\n")
                sys.stdout.buffer.flush()
                """
            ),
            encoding="utf-8",
        )

        with pytest.raises(RuntimeError, match="initialize rejected"):
            asyncio.run(_list_stdio_mcp_tool_names(sys.executable, (str(server_path),), {}))

    def test_stdio_mcp_framing_retry_policy_distinguishes_probe_failures(self) -> None:
        assert _should_retry_stdio_mcp_framing(TimeoutError("timed out waiting"))
        assert _should_retry_stdio_mcp_framing(BrokenPipeError("pipe closed"))
        assert _should_retry_stdio_mcp_framing(
            RuntimeError("MCP stdio process exited before response")
        )
        assert not _should_retry_stdio_mcp_framing(RuntimeError("initialize rejected"))

    def test_list_stdio_mcp_tool_names_does_not_retry_after_initialize_response(
        self,
        tmp_path: Path,
    ) -> None:
        server_path = tmp_path / "fake_post_initialize_exit_mcp_server.py"
        server_path.write_text(
            textwrap.dedent(
                r"""
                import json
                import sys

                initialize = json.loads(sys.stdin.buffer.readline())
                sys.stdout.buffer.write(json.dumps({
                    "jsonrpc": "2.0",
                    "id": initialize["id"],
                    "result": {
                        "protocolVersion": initialize["params"]["protocolVersion"],
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake", "version": "1.0.0"},
                    },
                }).encode("utf-8") + b"\n")
                sys.stdout.buffer.flush()
                sys.stdin.buffer.readline()  # notifications/initialized
                sys.stdin.buffer.readline()  # tools/list
                raise SystemExit(3)
                """
            ),
            encoding="utf-8",
        )

        with pytest.raises(RuntimeError, match="exited before response"):
            asyncio.run(_list_stdio_mcp_tool_names(sys.executable, (str(server_path),), {}))

    def test_check_auto_dispatch_surface_live_mcp_accepts_uvx_mcp_extra_without_local_mcp(
        self,
        tmp_path: Path,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        live_probe = AsyncMock(return_value=_REQUIRED_CODEX_AUTO_TOOLS_FOR_TEST)

        with (
            patch("ouroboros.cli.commands.codex.importlib.util.find_spec", return_value=None),
            patch("ouroboros.cli.commands.codex._list_stdio_mcp_tool_names", live_probe),
        ):
            assert _check_auto_dispatch_surface(codex_dir, live_mcp=True) == []

        live_probe.assert_awaited_once_with(
            "uvx",
            (
                "--isolated",
                "--python",
                ">=3.12",
                "--from",
                "ouroboros-ai[mcp]",
                "ouroboros",
                "mcp",
                "serve",
            ),
            {
                "OUROBOROS_AGENT_RUNTIME": "codex",
                "OUROBOROS_LLM_BACKEND": "codex",
            },
        )

    def test_check_auto_dispatch_surface_live_mcp_reports_missing_auto_tool(
        self,
        tmp_path: Path,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)

        with patch(
            "ouroboros.cli.commands.codex._list_stdio_mcp_tool_names",
            AsyncMock(return_value={"ouroboros_interview", "ouroboros_generate_seed"}),
        ):
            failures = _check_auto_dispatch_surface(codex_dir, live_mcp=True)

        assert any("missing required auto tools" in failure for failure in failures)
        assert any("ouroboros_auto" in failure for failure in failures)

    def test_check_auto_dispatch_surface_live_mcp_reports_missing_job_polling_tools(
        self,
        tmp_path: Path,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)

        with patch(
            "ouroboros.cli.commands.codex._list_stdio_mcp_tool_names",
            AsyncMock(
                return_value={
                    "ouroboros_auto",
                    "ouroboros_start_auto",
                    "ouroboros_interview",
                    "ouroboros_generate_seed",
                }
            ),
        ):
            failures = _check_auto_dispatch_surface(codex_dir, live_mcp=True)

        assert any("missing required auto tools" in failure for failure in failures)
        assert any("ouroboros_job_wait" in failure for failure in failures)
        assert any("ouroboros_job_result" in failure for failure in failures)

    def test_check_auto_dispatch_surface_live_mcp_reports_handshake_failure(
        self,
        tmp_path: Path,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)

        with patch(
            "ouroboros.cli.commands.codex._list_stdio_mcp_tool_names",
            AsyncMock(side_effect=RuntimeError("connection closed during initialize")),
        ):
            failures = _check_auto_dispatch_surface(codex_dir, live_mcp=True)

        assert any("initialize/list_tools failed" in failure for failure in failures)
        assert any("connection closed during initialize" in failure for failure in failures)

    def test_packaged_codex_artifacts_satisfy_doctor_contract(
        self,
        tmp_path: Path,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        install_codex_artifacts(codex_dir=codex_dir, prune=False)
        (codex_dir / "config.toml").write_text(
            '[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:12000/mcp"\n',
            encoding="utf-8",
        )

        assert _check_auto_dispatch_surface(codex_dir) == []

    def test_check_auto_dispatch_surface_reports_missing_auto_contract(
        self,
        tmp_path: Path,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        (codex_dir / "rules").mkdir(parents=True)
        (codex_dir / "rules" / "ouroboros.md").write_text(
            "| `ooo run <seed.yaml>` | `ouroboros_execute_seed` |\n",
            encoding="utf-8",
        )
        (codex_dir / "skills" / "ouroboros-auto").mkdir(parents=True)
        (codex_dir / "skills" / "ouroboros-auto" / "SKILL.md").write_text(
            "---\nname: auto\n---\n# Auto\n",
            encoding="utf-8",
        )
        (codex_dir / "config.toml").write_text("[mcp_servers]\n", encoding="utf-8")

        failures = _check_auto_dispatch_surface(codex_dir)

        assert "Codex rules do not map `ooo auto` to `ouroboros_start_auto`" in failures
        assert "auto skill does not declare `mcp_tool: ouroboros_start_auto`" in failures
        assert "Codex config does not contain [mcp_servers.ouroboros]" in failures

    def test_doctor_command_exits_nonzero_when_dispatch_surface_is_broken(
        self,
        tmp_path: Path,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()

        cli_result = runner.invoke(app, ["doctor", "--codex-dir", str(codex_dir)])

        assert cli_result.exit_code == 1
        assert "Codex ooo auto dispatch: BROKEN" in cli_result.output
        assert "missing Codex rules file" in cli_result.output

    def test_doctor_command_renders_optional_extra_remediation_literally(
        self, tmp_path: Path
    ) -> None:
        """Rich markup must not consume package extras in actionable diagnostics."""
        codex_dir = tmp_path / ".codex"

        with patch(
            "ouroboros.cli.commands.codex._check_auto_dispatch_surface",
            return_value=["install ouroboros-ai[mcp]"],
        ):
            cli_result = runner.invoke(app, ["doctor", "--codex-dir", str(codex_dir)])

        assert cli_result.exit_code == 1
        assert "ouroboros-ai[mcp]" in cli_result.output

    def test_doctor_command_reports_unreadable_artifact_without_traceback(
        self,
        tmp_path: Path,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        (codex_dir / "skills" / "ouroboros-auto" / "SKILL.md").write_bytes(b"\xff")

        cli_result = runner.invoke(app, ["doctor", "--codex-dir", str(codex_dir)])

        assert cli_result.exit_code == 1
        assert "auto skill is not valid UTF-8" in cli_result.output
        assert not isinstance(cli_result.exception, UnicodeDecodeError)

    def test_doctor_command_reports_ok_for_healthy_install(self, tmp_path: Path) -> None:
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)

        cli_result = runner.invoke(app, ["doctor", "--codex-dir", str(codex_dir)])

        assert cli_result.exit_code == 0
        assert "Codex ooo auto dispatch: OK" in cli_result.output

    def test_doctor_live_mcp_url_config_fails_without_stdio_success_claim(
        self, tmp_path: Path
    ) -> None:
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        (codex_dir / "config.toml").write_text(
            '[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:12000/mcp"\n',
            encoding="utf-8",
        )

        cli_result = runner.invoke(app, ["doctor", "--codex-dir", str(codex_dir), "--live-mcp"])

        assert cli_result.exit_code == 1
        assert "Codex ooo auto dispatch: BROKEN" in cli_result.output
        assert "currently verifies only" in cli_result.output
        assert "stdio `command` entries" in cli_result.output
        assert (
            "live stdio initialize/list_tools exposes required auto tools" not in cli_result.output
        )
