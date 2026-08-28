#!/usr/bin/env python3
"""Read-only Ouroboros local-environment harness.

The harness records enough evidence to distinguish a broken Ouroboros runtime
from a local installation/configuration drift. It does not delete, reinstall,
or rewrite user state.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import textwrap
import tomllib
from typing import Any

_REPO_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_REPO_SRC) not in sys.path:
    sys.path.insert(0, str(_REPO_SRC))
from ouroboros.codex import resolve_codex_home  # noqa: E402

REQUIRED_MCP_TOOLS = {
    "ouroboros_generate_seed",
    "ouroboros_start_execute_seed",
    "ouroboros_session_status",
    "ouroboros_job_status",
    "ouroboros_qa",
}


@dataclass
class Check:
    name: str
    status: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.details = redact_secrets(self.details)


@dataclass
class CommandResult:
    command: list[str]
    returncode: int | None
    stdout_path: str
    stderr_path: str
    timed_out: bool = False


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [_jsonable(v) for v in value]
    return value


def _text_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


_SECRET_KEY_RE = __import__("re").compile(
    r"(token|secret|password|api[_-]?key|credential|authorization|private[_-]?key)",
    __import__("re").I,
)


def redact_secrets(value: Any) -> Any:
    """Project diagnostic evidence without credential-bearing values."""
    if isinstance(value, dict):
        return {
            str(key): "<redacted>" if _SECRET_KEY_RE.search(str(key)) else redact_secrets(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple | set):
        return [redact_secrets(item) for item in value]
    return value


def run_command(
    command: list[str],
    *,
    cwd: Path,
    log_dir: Path,
    name: str,
    timeout: int,
    env: dict[str, str] | None = None,
) -> CommandResult:
    stdout_path = log_dir / f"{name}.stdout.log"
    stderr_path = log_dir / f"{name}.stderr.log"
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=merged_env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        stdout_path.write_text(completed.stdout, encoding="utf-8")
        stderr_path.write_text(completed.stderr, encoding="utf-8")
        return CommandResult(command, completed.returncode, str(stdout_path), str(stderr_path))
    except subprocess.TimeoutExpired as exc:
        stdout_path.write_text(_text_output(exc.stdout), encoding="utf-8")
        stderr_path.write_text(_text_output(exc.stderr), encoding="utf-8")
        return CommandResult(command, None, str(stdout_path), str(stderr_path), timed_out=True)
    except OSError as exc:
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text(str(exc), encoding="utf-8")
        return CommandResult(command, None, str(stdout_path), str(stderr_path))


def read_mcp_entry(path: Path, *, sanitize: bool = True) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "command": None,
        "args": [],
        "env": {},
        "error": None,
    }
    if not path.exists():
        return entry
    try:
        raw = path.read_text(encoding="utf-8")
        data = tomllib.loads(raw) if path.suffix == ".toml" else json.loads(raw)
        servers = data.get("mcp_servers" if path.suffix == ".toml" else "mcpServers", {})
        server = servers.get("ouroboros")
        if not isinstance(server, dict):
            entry["error"] = "missing mcpServers.ouroboros object"
            return entry
        entry["command"] = server.get("command")
        entry["args"] = server.get("args") if isinstance(server.get("args"), list) else []
        entry["env"] = server.get("env") if isinstance(server.get("env"), dict) else {}
        return redact_secrets(entry) if sanitize else entry
    except Exception as exc:  # pragma: no cover - defensive diagnostics
        entry["error"] = str(exc)
        return entry


def classify_mcp_entry(entry: dict[str, Any], expected_script: Path) -> tuple[str, str]:
    if not entry.get("exists"):
        return "warn", "config file is absent"
    if entry.get("error"):
        return "fail", str(entry["error"])
    command = entry.get("command")
    args = entry.get("args") or []
    expected = str(expected_script)
    if command == expected:
        return "pass", "uses the local repository MCP launcher"
    if command == "uvx" and "ouroboros-ai" in " ".join(str(arg) for arg in args):
        return "warn", "uses uvx/PyPI entrypoint; this can drift from the local checkout"
    if command in {"ouroboros", "ooo"}:
        return (
            "warn",
            "uses PATH-resolved Ouroboros entrypoint; verify PATH points at intended install",
        )
    return "warn", f"uses non-local MCP command: {command!r}"


def discover_mcp_config_paths(repo: Path, codex_home: Path | None = None) -> list[Path]:
    home = Path.home()
    paths = [repo / ".mcp.json"]
    if codex_home is not None:
        paths.append(codex_home / "config.toml")
    paths.extend(sorted((home / ".codex/plugins/cache/personal/ouroboros").glob("*/.mcp.json")))
    paths.append(home / ".codex/.tmp/marketplaces/ouroboros/.mcp.json")
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in paths:
        resolved = path.expanduser()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def effective_codex_entry() -> dict[str, Any]:
    """Resolve the authoritative Codex MCP entry using Codex's home resolver."""
    path = resolve_codex_home() / "config.toml"
    entry = read_mcp_entry(path, sanitize=False)
    entry["effective"] = True
    if not entry.get("exists") or entry.get("error"):
        return entry
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
        server = config["mcp_servers"]["ouroboros"]
        from ouroboros.cli.commands.codex import (
            _check_mcp_activation_surface,
            _check_mcp_execution_surface,
            _check_mcp_runtime_dependency_surface,
            _check_mcp_schema_surface,
        )

        failures: list[str] = []
        _check_mcp_schema_surface(server, failures)
        _check_mcp_activation_surface(server, failures)
        _check_mcp_execution_surface(server, failures)
        if isinstance(entry.get("command"), str):
            _check_mcp_runtime_dependency_surface(
                entry["command"], entry.get("args", []), entry.get("env", {}), failures
            )
        if failures:
            entry["error"] = "; ".join(failures)
    except (KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        entry["error"] = str(exc)
    return entry


def mcp_stdio_smoke(
    repo: Path, log_dir: Path, timeout: int, entry: dict[str, Any] | None = None
) -> tuple[Check, CommandResult]:
    # Use the interpreter that successfully loaded this harness and its doctor
    # machinery; a stale checkout-local venv is itself separate diagnostic data.
    python = Path(sys.executable)
    configured = entry or {"command": str(repo / "scripts/mcp-serve.sh"), "args": [], "env": {}}
    if configured.get("error") or not isinstance(configured.get("command"), str):
        details = redact_secrets(configured)
        result = CommandResult([], None, "", "")
        return Check(
            "mcp_stdio_smoke", "fail", "effective Codex MCP launcher is invalid", details
        ), result
    launcher = [str(configured["command"]), *(str(arg) for arg in configured.get("args", []))]
    code = f"""
import asyncio
import json
import os
from ouroboros.cli.commands.codex import _list_stdio_mcp_tool_names

async def main():
    tools = await _list_stdio_mcp_tool_names(
        {launcher[0]!r},
        {tuple(launcher[1:])!r},
        dict(os.environ),
    )
    print(json.dumps({{"tool_count": len(tools), "tools": sorted(tools)}}))

asyncio.run(main())
"""
    command = [str(python), "-c", code]
    result = run_command(
        command,
        cwd=repo,
        log_dir=log_dir,
        name="mcp_stdio_smoke",
        timeout=timeout,
        env={str(k): str(v) for k, v in (configured.get("env") or {}).items()},
    )
    details: dict[str, Any] = redact_secrets(
        {"launcher": launcher, "command_result": _jsonable(result.__dict__)}
    )
    if result.timed_out:
        return Check("mcp_stdio_smoke", "fail", "MCP stdio probe timed out", details), result
    if result.returncode != 0:
        return Check("mcp_stdio_smoke", "fail", "MCP stdio probe failed", details), result
    try:
        payload = json.loads(Path(result.stdout_path).read_text(encoding="utf-8").splitlines()[-1])
    except Exception as exc:
        details["parse_error"] = str(exc)
        return Check(
            "mcp_stdio_smoke", "fail", "MCP stdio probe returned unparsable output", details
        ), result
    tools = set(payload.get("tools", []))
    missing = sorted(REQUIRED_MCP_TOOLS - tools)
    details["tool_count"] = payload.get("tool_count")
    details["missing_required_tools"] = missing
    if missing:
        return Check("mcp_stdio_smoke", "fail", "MCP missing required tools", details), result
    return Check("mcp_stdio_smoke", "pass", "MCP initialize/tools-list succeeded", details), result


def write_smoke_seed(project: Path, *, verify_command: str) -> Path:
    project.mkdir(parents=True, exist_ok=True)
    seed = project / "ooo_env_smoke_seed.yaml"
    seed.write_text(
        textwrap.dedent(
            f"""\
            goal: "Create a minimal deterministic Ouroboros run smoke module"
            task_type: code
            constraints:
              - "Only create or modify files inside this temporary smoke project."
              - "Use Python standard library only."
              - "Do not read or modify user project files."
            acceptance_criteria:
              - description: "Create ooo_smoke.py with a status() function that returns 'ok', and create test_ooo_smoke.py with unittest coverage for that behavior."
                verify_command: "{verify_command}"
                expected_artifacts:
                  - "ooo_smoke.py"
                  - "test_ooo_smoke.py"
            ontology_schema:
              name: "OuroborosEnvSmoke"
              description: "Minimal local execution smoke for the Ouroboros runtime."
              fields:
                - name: "module"
                  type: "entity"
                  description: "The generated smoke module."
                - name: "test"
                  type: "entity"
                  description: "The generated unittest file."
            evaluation_principles:
              - name: "mechanical_verifiability"
                description: "The smoke project must pass the explicit unittest command."
                weight: 1.0
            metadata:
              seed_id: "ooo_env_harness_smoke"
              ambiguity_score: 0.05
              project_dir: "{project}"
            """
        ),
        encoding="utf-8",
    )
    return seed


def run_smoke(repo: Path, log_dir: Path, *, timeout: int, runtime: str) -> Check:
    project = log_dir / "run-smoke-project"
    seed = write_smoke_seed(project, verify_command="python3 -m unittest -v")
    local_ooo = repo / ".venv/bin/ooo"
    command = [
        str(local_ooo),
        "run",
        "workflow",
        str(seed),
        "--project-dir",
        str(project),
        "--runtime",
        runtime,
        "--sequential",
        "--max-decomposition-depth",
        "0",
        "--no-qa",
    ]
    env = {
        "OUROBOROS_AGENT_RUNTIME": runtime,
        "OUROBOROS_LLM_BACKEND": runtime,
        "OUROBOROS_MAX_PARALLEL_WORKERS": "1",
    }
    result = run_command(
        command, cwd=repo, log_dir=log_dir, name="run_smoke", timeout=timeout, env=env
    )
    details = {
        "project": str(project),
        "seed": str(seed),
        "command_result": _jsonable(result.__dict__),
        "created_artifacts": {
            "ooo_smoke.py": (project / "ooo_smoke.py").exists(),
            "test_ooo_smoke.py": (project / "test_ooo_smoke.py").exists(),
        },
    }
    if result.timed_out:
        return Check("run_smoke", "fail", "minimal ooo run smoke timed out", details)
    if result.returncode != 0:
        return Check("run_smoke", "fail", "minimal ooo run smoke failed", details)
    return Check("run_smoke", "pass", "minimal ooo run smoke completed", details)


def write_markdown_report(log_dir: Path, checks: list[Check]) -> Path:
    report = log_dir / "report.md"
    lines = [
        "# Ouroboros Environment Harness Report",
        "",
        f"- Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"- Host: {platform.platform()}",
        f"- Python: {sys.version.split()[0]} ({sys.executable})",
        "",
        "## Checks",
        "",
    ]
    for check in checks:
        lines.append(f"- **{check.status.upper()}** `{check.name}`: {check.message}")
        if check.details:
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(_jsonable(check.details), indent=2, ensure_ascii=False))
            lines.append("```")
            lines.append("")
    report.write_text("\n".join(lines), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--log-dir", type=Path, default=None)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--mcp-timeout", type=int, default=90)
    parser.add_argument("--run-timeout", type=int, default=7200)
    parser.add_argument("--runtime", default="codex")
    parser.add_argument("--skip-mcp-smoke", action="store_true")
    parser.add_argument("--include-run-smoke", action="store_true")
    parser.add_argument(
        "--non-strict", action="store_true", help="Always exit 0 after writing report."
    )
    args = parser.parse_args(argv)

    repo = args.repo.expanduser().resolve()
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_dir = args.log_dir or Path.home() / ".ouroboros/harness" / f"ooo-env-{timestamp}"
    log_dir = log_dir.expanduser().resolve()
    log_dir.mkdir(parents=True, exist_ok=True)

    checks: list[Check] = []
    expected_script = repo / "scripts/mcp-serve.sh"
    checks.append(
        Check(
            "repo",
            "pass" if (repo / "pyproject.toml").exists() else "fail",
            f"repo={repo}",
            {"expected_mcp_launcher": str(expected_script)},
        )
    )

    process_result = run_command(
        [
            "ps",
            "-axo",
            "pid,ppid,stat,pcpu,pmem,etime,command",
        ],
        cwd=repo,
        log_dir=log_dir,
        name="processes",
        timeout=args.timeout,
    )
    checks.append(
        Check(
            "process_snapshot",
            "pass" if process_result.returncode == 0 else "warn",
            "process snapshot recorded",
            {"command_result": _jsonable(process_result.__dict__)},
        )
    )

    for binary in ("python", "python3", "ooo", "ouroboros", "uv", "codex", "caffeinate"):
        checks.append(
            Check(
                f"which_{binary}",
                "pass" if shutil.which(binary) else "warn",
                shutil.which(binary) or "not found on PATH",
            )
        )

    for label, command in {
        "local_ooo_version": [str(repo / ".venv/bin/ooo"), "--version"],
        "global_ooo_version": ["ooo", "--version"],
        "local_mcp_doctor": [str(repo / ".venv/bin/ouroboros"), "mcp", "doctor", "--json"],
    }.items():
        result = run_command(command, cwd=repo, log_dir=log_dir, name=label, timeout=args.timeout)
        checks.append(
            Check(
                label,
                "pass" if result.returncode == 0 else "warn",
                "command completed" if result.returncode == 0 else "command failed or unavailable",
                {"command_result": _jsonable(result.__dict__)},
            )
        )

    codex_home = resolve_codex_home()
    effective_entry = effective_codex_entry()
    config_entries: list[dict[str, Any]] = []
    for path in discover_mcp_config_paths(repo, codex_home):
        entry = read_mcp_entry(path)
        status, message = classify_mcp_entry(entry, expected_script)
        config_entries.append(entry)
        checks.append(Check(f"mcp_config:{path}", status, message, redact_secrets(entry)))

    unique_signatures = {
        (entry.get("command"), tuple(entry.get("args") or []))
        for entry in config_entries
        if entry.get("exists") and not entry.get("error")
    }
    drift_status = "pass" if len(unique_signatures) <= 1 else "warn"
    checks.append(
        Check(
            "mcp_config_drift",
            drift_status,
            "all discovered MCP configs use one command signature"
            if drift_status == "pass"
            else "discovered MCP configs use multiple command signatures",
            {"signatures": [list(signature) for signature in sorted(unique_signatures)]},
        )
    )

    if args.skip_mcp_smoke:
        checks.append(Check("mcp_stdio_smoke", "warn", "skipped by flag"))
    else:
        check, _ = mcp_stdio_smoke(repo, log_dir, args.mcp_timeout, effective_entry)
        checks.append(check)

    if args.include_run_smoke:
        checks.append(run_smoke(repo, log_dir, timeout=args.run_timeout, runtime=args.runtime))
    else:
        checks.append(Check("run_smoke", "warn", "skipped; pass --include-run-smoke to execute"))

    report = write_markdown_report(log_dir, checks)
    (log_dir / "checks.json").write_text(
        json.dumps([_jsonable(check.__dict__) for check in checks], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"Report: {report}")
    failed = [check for check in checks if check.status == "fail"]
    if failed:
        print("Failed checks: " + ", ".join(check.name for check in failed), file=sys.stderr)
    return 0 if args.non_strict or not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
