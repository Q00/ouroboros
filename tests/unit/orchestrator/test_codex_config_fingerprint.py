from pathlib import Path

import pytest

from ouroboros.orchestrator.codex_config_fingerprint import (
    fingerprint_codex_config_files,
)


@pytest.mark.parametrize(
    ("initial_config", "updated_config"),
    [
        pytest.param(
            '[mcp_servers.ouroboros]\ncommand = "/usr/local/bin/ouroboros"\n',
            '[mcp_servers.ouroboros]\ncommand = "/tmp/replacement"\n',
            id="mcp-command",
        ),
        pytest.param(
            'sandbox_mode = "read-only"\n',
            'sandbox_mode = "danger-full-access"\n',
            id="sandbox-mode",
        ),
        pytest.param(
            'approval_policy = "on-request"\n',
            'approval_policy = "never"\n',
            id="approval-policy",
        ),
    ],
)
def test_execution_setting_change_updates_fingerprint(
    tmp_path: Path,
    initial_config: str,
    updated_config: str,
) -> None:
    # Given
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text(initial_config, encoding="utf-8")
    initial_fingerprint = fingerprint_codex_config_files(codex_home)

    # When
    config_path.write_text(updated_config, encoding="utf-8")

    # Then
    assert fingerprint_codex_config_files(codex_home) != initial_fingerprint


def test_codex_managed_state_change_preserves_fingerprint(tmp_path: Path) -> None:
    # Given
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text(
        '[projects."/tmp/existing"]\ntrust_level = "trusted"\n',
        encoding="utf-8",
    )
    initial_fingerprint = fingerprint_codex_config_files(codex_home)

    # When
    config_path.write_text(
        '[projects."/tmp/new"]\n'
        'trust_level = "trusted"\n'
        '\n[hooks.state."plugin:session_start:0"]\n'
        'trusted_hash = "desktop-managed-state"\n',
        encoding="utf-8",
    )

    # Then
    assert fingerprint_codex_config_files(codex_home) == initial_fingerprint
