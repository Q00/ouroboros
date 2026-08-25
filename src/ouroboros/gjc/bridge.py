"""Rendering and ownership judgment for the GJC compatibility input bridge."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from ouroboros.gjc.paths import gjc_bridge_path

_OWNERSHIP_PREFIX = "// ouroboros-setup-sha256:"


def _gjc_ooo_bridge_source_body(command: str, args: list[str]) -> str:
    default_command = json.dumps(command)
    default_args = json.dumps(args)
    return f"""// Managed by `ouroboros setup --runtime gjc`.
import {{ execFile }} from "node:child_process";
import {{ promisify }} from "node:util";

const execFileAsync = promisify(execFile);
const COMMAND_RE = /^\\s*ooo(?:\\s+|$)/i;
const UNSUPPORTED_DISPATCH_EXIT_CODE = 78;
const DEPTH_ENV = "_OUROBOROS_GJC_BRIDGE_DEPTH";
const TIMEOUT_MS = Number(process.env.OUROBOROS_GJC_BRIDGE_TIMEOUT_MS || 6 * 60 * 60 * 1000);
const DEFAULT_COMMAND = {default_command};
const DEFAULT_ARGS: string[] = {default_args};

type InputEvent = {{ text?: string }};
type InputContext = {{ cwd: string }};
type InputResult = {{ handled?: boolean; text?: string; images?: unknown[] }} | void;
type ExtensionAPI = {{
  on(
    event: "input",
    handler: (event: InputEvent, ctx: InputContext) => Promise<InputResult> | InputResult,
  ): void;
}};

type ExecResult = {{ stdout: string; stderr: string; code: number | null }};

function ouroborosEntry(): {{ command: string; args: string[] }} {{
  if (process.env.OUROBOROS_CLI) return {{ command: process.env.OUROBOROS_CLI, args: [] }};
  return {{ command: DEFAULT_COMMAND, args: DEFAULT_ARGS }};
}}

function outputText(stdout: string, stderr: string): string {{
  const out = stdout.trim();
  const err = stderr.trim();
  if (out && err) return `${{out}}\\n\\n${{err}}`;
  return out || err || "(no output)";
}}

async function dispatch(text: string, cwd: string): Promise<ExecResult> {{
  const env = {{ ...process.env, [DEPTH_ENV]: "1" }};
  const entry = ouroborosEntry();
  const args = [...entry.args, "dispatch", "--runtime", "gjc", "--cwd", cwd, text];
  try {{
    const result = await execFileAsync(entry.command, args, {{ cwd, env, timeout: TIMEOUT_MS }});
    return {{ stdout: result.stdout || "", stderr: result.stderr || "", code: 0 }};
  }} catch (error) {{
    const err = error as {{ stdout?: string; stderr?: string; code?: number | null; signal?: string }};
    return {{
      stdout: err.stdout || "",
      stderr: err.stderr || err.signal || "",
      code: typeof err.code === "number" ? err.code : 1,
    }};
  }}
}}

export default function ouroborosBridge(gjc: ExtensionAPI) {{
  gjc.on("input", async (event, ctx) => {{
    const text = (event.text || "").trim();
    if (!COMMAND_RE.test(text) || process.env[DEPTH_ENV]) return {{ handled: false }};

    const result = await dispatch(text, ctx.cwd);
    if (result.code === UNSUPPORTED_DISPATCH_EXIT_CODE) return {{ handled: false, text: event.text }};
    const body = outputText(result.stdout, result.stderr);
    if (result.code === 0) return {{ handled: true, text: body }};
    return {{ handled: true, text: `Ouroboros dispatch failed (${{result.code ?? "unknown"}})\\n\\n${{body}}` }};
  }});
}}
"""


def gjc_ooo_bridge_source_text(command: str, args: list[str]) -> str:
    """Render an exactly identifiable GJC compatibility bridge generation."""
    body = _gjc_ooo_bridge_source_body(command, args)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return f"{body}{_OWNERSHIP_PREFIX}{digest}\n"


def is_gjc_ooo_bridge_source_text(source: str) -> bool:
    """Return whether *source* is a complete setup-rendered bridge generation."""
    body, separator, digest_line = source.rpartition(_OWNERSHIP_PREFIX)
    if separator:
        digest = digest_line.removesuffix("\n")
        return (
            len(digest) == 64
            and all(character in "0123456789abcdef" for character in digest)
            and source == f"{body}{_OWNERSHIP_PREFIX}{digest}\n"
            and hashlib.sha256(body.encode("utf-8")).hexdigest() == digest
        )

    command_match = re.search(r"^const DEFAULT_COMMAND = (.+);$", source, re.MULTILINE)
    args_match = re.search(r"^const DEFAULT_ARGS: string\[\] = (.+);$", source, re.MULTILINE)
    if command_match is None or args_match is None:
        return False
    try:
        command = json.loads(command_match.group(1))
        args = json.loads(args_match.group(1))
    except json.JSONDecodeError:
        return False
    return (
        isinstance(command, str)
        and isinstance(args, list)
        and all(isinstance(arg, str) for arg in args)
        and source == _gjc_ooo_bridge_source_body(command, args)
    )


def is_setup_managed_gjc_bridge(path: Path | None = None) -> bool:
    """Return whether the bridge is a complete setup-rendered generation."""
    candidate = path or gjc_bridge_path()
    try:
        source = candidate.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return False
    return not candidate.is_symlink() and is_gjc_ooo_bridge_source_text(source)
