"""Unit tests for Codex integration helper commands."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import textwrap
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from ouroboros.cli.commands import codex as codex_command
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
            'args = ["--from", "ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"]\n',
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

    def test_check_auto_dispatch_surface_accepts_custom_command_mcp_entry(
        self,
        tmp_path: Path,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        (codex_dir / "config.toml").write_text(
            '[mcp_servers.ouroboros]\ncommand = "/opt/bin/ob-mcp-wrapper"\nargs = ["--stdio"]\n',
            encoding="utf-8",
        )

        assert _check_auto_dispatch_surface(codex_dir) == []

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

    def test_check_auto_dispatch_surface_reports_direct_ouroboros_without_mcp_import(
        self,
        tmp_path: Path,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        (codex_dir / "config.toml").write_text(
            "[mcp_servers.ouroboros]\n"
            'command = "/home/user/.local/bin/ouroboros"\n'
            'args = ["mcp", "serve", "--runtime", "codex"]\n',
            encoding="utf-8",
        )

        with patch("ouroboros.cli.commands.codex.importlib.util.find_spec", return_value=None):
            failures = _check_auto_dispatch_surface(codex_dir)

        assert any("cannot import `mcp`" in failure for failure in failures)

    def test_check_auto_dispatch_surface_accepts_direct_ouroboros_with_mcp_import(
        self,
        tmp_path: Path,
    ) -> None:
        codex_dir = tmp_path / ".codex"
        self._write_healthy_codex_surface(codex_dir)
        (codex_dir / "config.toml").write_text(
            "[mcp_servers.ouroboros]\n"
            'command = "ouroboros"\n'
            'args = ["mcp", "serve", "--runtime", "codex"]\n',
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
            'args = ["mcp", "serve", "--runtime", "codex"]\n',
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
            ("mcp", "serve", "--runtime", "codex"),
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
            'args = ["mcp", "serve", "--runtime", "codex"]\n'
            "[mcp_servers.ouroboros.env]\n"
            'OUROBOROS_AGENT_RUNTIME = "codex"\n',
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
            ("mcp", "serve", "--runtime", "codex"),
            {"OUROBOROS_AGENT_RUNTIME": "codex"},
        )

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
            ("--from", "ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"),
            {},
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
