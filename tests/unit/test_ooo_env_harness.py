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
