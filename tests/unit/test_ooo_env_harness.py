from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch


def _load_harness():
    path = Path(__file__).resolve().parents[2] / "scripts" / "ooo-env-harness.py"
    spec = importlib.util.spec_from_file_location("ooo_env_harness", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_classifies_local_mcp_launcher(tmp_path: Path) -> None:
    harness = _load_harness()
    config = tmp_path / ".mcp.json"
    expected = tmp_path / "scripts" / "mcp-serve.sh"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "ouroboros": {
                        "command": str(expected),
                        "args": [],
                        "env": {"OUROBOROS_AGENT_RUNTIME": "codex"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    entry = harness.read_mcp_entry(config)
    assert harness.classify_mcp_entry(entry, expected) == (
        "pass",
        "uses the local repository MCP launcher",
    )


def test_classifies_uvx_mcp_launcher_as_drift(tmp_path: Path) -> None:
    harness = _load_harness()
    config = tmp_path / ".mcp.json"
    expected = tmp_path / "scripts" / "mcp-serve.sh"
    config.write_text(
        """
        {
          "mcpServers": {
            "ouroboros": {
              "command": "uvx",
              "args": ["--from", "ouroboros-ai[mcp,claude]", "ouroboros", "mcp", "serve"]
            }
          }
        }
        """,
        encoding="utf-8",
    )

    entry = harness.read_mcp_entry(config)
    status, message = harness.classify_mcp_entry(entry, expected)
    assert status == "warn"
    assert "drift" in message


def test_run_command_records_bytes_timeout_output(tmp_path: Path) -> None:
    harness = _load_harness()

    with patch.object(
        harness.subprocess,
        "run",
        side_effect=subprocess.TimeoutExpired(
            ["slow-tool"],
            timeout=1,
            output=b"partial stdout",
            stderr=b"partial stderr",
        ),
    ):
        result = harness.run_command(
            ["slow-tool"],
            cwd=tmp_path,
            log_dir=tmp_path,
            name="slow",
            timeout=1,
        )

    assert result.timed_out is True
    assert result.returncode is None
    assert Path(result.stdout_path).read_text(encoding="utf-8") == "partial stdout"
    assert Path(result.stderr_path).read_text(encoding="utf-8") == "partial stderr"


def test_run_command_records_real_timeout(tmp_path: Path) -> None:
    harness = _load_harness()

    result = harness.run_command(
        [sys.executable, "-c", "import time; time.sleep(2)"],
        cwd=tmp_path,
        log_dir=tmp_path,
        name="real_timeout",
        timeout=1,
    )

    assert result.timed_out is True
    assert result.returncode is None
    assert Path(result.stdout_path).read_text(encoding="utf-8") == ""
    assert Path(result.stderr_path).read_text(encoding="utf-8") == ""


def test_run_command_records_missing_executable(tmp_path: Path) -> None:
    harness = _load_harness()

    result = harness.run_command(
        [str(tmp_path / "missing-tool"), "--version"],
        cwd=tmp_path,
        log_dir=tmp_path,
        name="missing_tool",
        timeout=1,
    )

    assert result.timed_out is False
    assert result.returncode is None
    assert Path(result.stdout_path).read_text(encoding="utf-8") == ""
    assert "No such file or directory" in Path(result.stderr_path).read_text(encoding="utf-8")


def test_reports_redact_mcp_environment_secrets(tmp_path: Path) -> None:
    harness = _load_harness()
    secret = "sk-review-secret"
    config = tmp_path / ".mcp.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "ouroboros": {
                        "command": "ouroboros",
                        "args": ["mcp", "serve"],
                        "env": {"OPENAI_API_KEY": secret, "SAFE_SETTING": "visible"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    entry = harness.read_mcp_entry(config)
    check = harness.Check("config", "pass", "loaded", entry)
    report = harness.write_markdown_report(tmp_path, [check])
    checks_path = tmp_path / "checks.json"
    checks_path.write_text(json.dumps([harness._jsonable(check.__dict__)]), encoding="utf-8")

    combined = report.read_text(encoding="utf-8") + checks_path.read_text(encoding="utf-8")
    assert secret not in combined
    assert "<redacted>" in combined
    assert "visible" in combined


def test_redacts_launcher_arguments_and_process_output(tmp_path: Path) -> None:
    harness = _load_harness()
    arg_secret = "sk-arg-current-head-2289"
    output_secret = "sk-process-current-head-2289"
    result = harness.run_command(
        [sys.executable, "-c", f"print('{output_secret}')", "--api-key", arg_secret],
        cwd=tmp_path,
        log_dir=tmp_path,
        name="secret_probe",
        timeout=2,
    )
    persisted = "\n".join(
        [
            str(result.command),
            Path(result.stdout_path).read_text(),
            Path(result.stderr_path).read_text(),
        ]
    )
    assert arg_secret not in persisted
    assert output_secret not in persisted
    assert "<redacted>" in persisted


def test_redacts_structured_authorization_header_in_persisted_reports(tmp_path: Path) -> None:
    harness = _load_harness()
    secret = "definitely-secret-2289"
    config = tmp_path / ".mcp.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "ouroboros": {
                        "command": "ouroboros",
                        "args": ["--header", f"Authorization: Bearer {secret}"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    entry = harness.read_mcp_entry(config)
    check = harness.Check("config", "pass", "loaded", entry)
    report = harness.write_markdown_report(tmp_path, [check])
    checks_path = tmp_path / "checks.json"
    checks_path.write_text(json.dumps([harness._jsonable(check.__dict__)]), encoding="utf-8")

    combined = report.read_text(encoding="utf-8") + checks_path.read_text(encoding="utf-8")
    assert secret not in combined
    assert "<redacted>" in combined


def test_effective_codex_entry_uses_codex_home_and_configured_launcher(
    tmp_path: Path, monkeypatch
) -> None:
    harness = _load_harness()
    codex_home = tmp_path / "custom-codex"
    codex_home.mkdir()
    config = codex_home / "config.toml"
    config.write_text(
        f'''\
[mcp_servers.ouroboros]
command = "{sys.executable}"
args = ["-m", "ouroboros", "mcp", "serve", "--runtime", "codex", "--llm-backend", "codex"]
env = {{ OUROBOROS_AGENT_RUNTIME = "codex", OUROBOROS_LLM_BACKEND = "codex" }}
''',
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    with patch(
        "ouroboros.cli.commands.codex._check_mcp_runtime_dependency_surface"
    ) as dependency_check:
        entry = harness.effective_codex_entry()

    assert entry["path"] == str(config)
    assert entry["command"] == sys.executable
    dependency_check.assert_called_once()


def test_stdio_smoke_probes_configured_launcher(tmp_path: Path) -> None:
    harness = _load_harness()
    entry = {"command": "/configured/ouroboros", "args": ["mcp", "serve"], "env": {}}
    observed: dict[str, object] = {}

    def fake_run(command, **kwargs):
        observed["code"] = command[-1]
        stdout_path = tmp_path / "smoke.stdout.log"
        stderr_path = tmp_path / "smoke.stderr.log"
        stdout_path.write_text(
            json.dumps(
                {
                    "tool_count": len(harness.REQUIRED_MCP_TOOLS),
                    "tools": sorted(harness.REQUIRED_MCP_TOOLS),
                }
            ),
            encoding="utf-8",
        )
        stderr_path.write_text("", encoding="utf-8")
        return harness.CommandResult(command, 0, str(stdout_path), str(stderr_path))

    with patch.object(harness, "run_command", side_effect=fake_run):
        check, _ = harness.mcp_stdio_smoke(tmp_path, tmp_path, 1, entry)

    assert check.status == "pass"
    assert "'/configured/ouroboros'" in str(observed["code"])
    assert "('mcp', 'serve')" in str(observed["code"])
