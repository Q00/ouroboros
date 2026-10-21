"""Managed OMP bridge extension source for ``ooo`` frontdoor dispatch.

``ouroboros setup --runtime omp`` renders the TypeScript below and installs it
as ``~/.omp/agent/extensions/ouroboros-ooo-bridge.ts``. OMP (Oh My Pi) is a
Pi-family coding agent that auto-discovers global extensions in that
directory. The template mirrors :mod:`ouroboros.cli.commands.pi_bridge` with
OMP-specific deltas:

- ``dispatch --runtime omp`` so the MCP dispatch server binds the omp backend.
- No ``@earendil-works/pi-coding-agent`` type import: OMP does not ship that
  package path, so the extension declares structural types locally.
- ``OUROBOROS_OMP_BRIDGE_TIMEOUT_MS`` names the dispatch timeout env var.

The renderer lives in this module so :mod:`ouroboros.cli.commands.setup`
stays within its module-size budget; only pure rendering lives here, while
install-site and launcher decisions stay in setup.
"""

from __future__ import annotations

import json


def omp_ooo_bridge_source_text(*, command: str, args: list[str]) -> str:
    """Render the managed OMP ``ooo`` bridge extension TypeScript source.

    ``command``/``args`` name the Ouroboros launcher the rendered bridge
    should dispatch through; setup decides that launcher from the install
    context (:func:`_detect_omp_bridge_dispatch_entry` in
    :mod:`ouroboros.cli.commands.setup`) so this module stays a pure
    renderer with no setup dependencies.
    """
    default_command = json.dumps(command)
    default_args = json.dumps(args)
    return f"""/**
 * Ouroboros ooo bridge for OMP (Oh My Pi).
 *
 * Managed by `ouroboros setup --runtime omp`.
 * Routes exact-prefix `ooo ...` inputs from interactive OMP into Ouroboros'
 * shared skill dispatcher instead of sending them to the model as chat.
 * The registered `/ooo` command also provides TAB argument completion for
 * dispatchable subcommands and Seed files.
 */
import {{ readdirSync }} from "node:fs";
import {{ homedir }} from "node:os";
import * as path from "node:path";

// Structural stand-ins for OMP's Pi-family extension API. OMP does not ship
// the `@earendil-works/pi-coding-agent` module path, so the bridge declares
// the surface it uses instead of importing vendor types.
type CompletionItem = {{ value: string; label: string; description?: string }};
interface ExecResult {{
  code: number | null;
  stdout?: string;
  stderr?: string;
}}
interface ExtensionContext {{
  cwd: string;
  ui: {{ notify(message: string, level: string): void }};
}}
interface ExtensionAPI {{
  exec(command: string, args: string[], options: {{ cwd: string; timeout: number }}): Promise<ExecResult>;
  sendMessage(message: {{ customType: string; content: string; display: boolean; details: unknown }}): void;
  registerCommand(
    name: string,
    spec: {{
      description: string;
      getArgumentCompletions: (prefix: string) => CompletionItem[] | null;
      handler: (args: string, ctx: ExtensionContext) => Promise<void>;
    }},
  ): void;
  on(
    event: "input",
    handler: (
      event: {{ text: string; source?: string }},
      ctx: ExtensionContext,
    ) => Promise<{{ action: "handled" | "continue" }}>,
  ): void;
}}

// Dispatchable `ooo` subcommands: the packaged skills whose SKILL.md declares
// `mcp_tool` frontmatter, i.e. the commands the shared dispatcher can execute
// as one deterministic MCP call. Kept in sync by a unit test.
const DISPATCHABLE_COMMANDS: Array<{{ cmd: string; description: string }}> = [
  {{ cmd: "auto", description: "Interview, Seed generation, and run handoff" }},
  {{ cmd: "interview", description: "Socratic interview to clarify requirements" }},
  {{ cmd: "run", description: "Execute a Seed specification" }},
  {{ cmd: "seed", description: "Generate a Seed from an interview session" }},
  {{ cmd: "status", description: "Session status and drift check" }},
  {{ cmd: "ralph", description: "Evolutionary loop until QA passes" }},
];

function matchesPrefix(value: string, prefix: string): boolean {{
  return value.toLowerCase().startsWith(prefix.toLowerCase());
}}

function seedFileCompletions(prefix: string): CompletionItem[] | null {{
  const seedsDir = path.join(homedir(), ".ouroboros", "seeds");
  let names: string[];
  try {{
    names = readdirSync(seedsDir);
  }} catch {{
    return null;
  }}
  // The autocomplete provider replaces the *entire* argument prefix with
  // item.value — so the completed value must include the `run` subcommand
  // to remain dispatchable. The absolute path is POSIX single-argument
  // quoted so every pathname character supported by shlex survives; embedded
  // single quotes use the standard '\\'' sequence.
  // Names containing whitespace are skipped because the dispatcher
  // tokenizes the completion prefix on whitespace before this replacement.
  const items = names
    .filter((name) => name.endsWith(".yaml") || name.endsWith(".yml"))
    .filter((name) => !/\\s/.test(name) && matchesPrefix(name, prefix))
    .sort()
    .map((name) => {{
      const abs = path.join(seedsDir, name);
      const quoted = `'${{abs.replace(/'/g, `'\\''`)}}'`;
      return {{
        value: `run ${{quoted}}`,
        label: name,
        description: "Seed file",
      }};
    }});
  return items.length > 0 ? items : null;
}}

// TAB completions for `/ooo <TAB>`: dispatchable subcommands for the first
// argument, Seed files from `~/.ouroboros/seeds/` for `ooo run <TAB>`.
// The provider replaces the entire argument prefix with item.value, so Seed
// items carry `run <quoted-absolute-path>` to remain dispatchable after
// selection.
function argumentCompletions(argumentPrefix: string): CompletionItem[] | null {{
  const tokens = argumentPrefix.replace(/^\\s+/, "").split(/\\s+/);
  const completing = tokens[tokens.length - 1] ?? "";
  if (tokens.length <= 1) {{
    const items = DISPATCHABLE_COMMANDS.filter((entry) =>
      matchesPrefix(entry.cmd, completing),
    ).map((entry) => ({{ value: entry.cmd, label: entry.cmd, description: entry.description }}));
    return items.length > 0 ? items : null;
  }}
  if (tokens[0].toLowerCase() === "run" && tokens.length === 2) {{
    return seedFileCompletions(completing);
  }}
  return null;
}}

const COMMAND_RE = /^\\s*ooo(?:\\s+|$)/i;
const UNSUPPORTED_DISPATCH_EXIT_CODE = 78;
const TIMEOUT_MS = Number(process.env.OUROBOROS_OMP_BRIDGE_TIMEOUT_MS || 6 * 60 * 60 * 1000);
const DEFAULT_COMMAND = {default_command};
const DEFAULT_ARGS = {default_args};

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

async function dispatch(omp: ExtensionAPI, text: string, ctx: ExtensionContext): Promise<boolean> {{
  ctx.ui.notify(`Ouroboros dispatch: ${{text}}`, "info");
  const entry = ouroborosEntry();
  const result = await omp.exec(
    entry.command,
    [...entry.args, "dispatch", "--runtime", "omp", "--cwd", ctx.cwd, text],
    {{ cwd: ctx.cwd, timeout: TIMEOUT_MS }},
  );
  if (result.code === UNSUPPORTED_DISPATCH_EXIT_CODE) {{
    ctx.ui.notify(`Ouroboros did not claim command; continuing in OMP`, "info");
    return false;
  }}
  const body = outputText(result.stdout || "", result.stderr || "");
  omp.sendMessage({{
    customType: "ouroboros",
    content: body,
    display: true,
    details: {{ command: text, exitCode: result.code }},
  }});
  if (result.code !== 0) {{
    ctx.ui.notify(`Ouroboros dispatch failed (${{result.code ?? "unknown"}})`, "error");
  }}
  return true;
}}

export default function ouroborosBridge(omp: ExtensionAPI) {{
  omp.registerCommand("ooo", {{
    description: "Dispatch an Ouroboros ooo command",
    getArgumentCompletions: argumentCompletions,
    handler: async (args, ctx) => {{
      const text = `ooo ${{args}}`.trim();
      await dispatch(omp, text, ctx);
    }},
  }});

  omp.on("input", async (event, ctx) => {{
    if (event.source === "extension") {{
      return {{ action: "continue" }};
    }}
    if (!COMMAND_RE.test(event.text)) {{
      return {{ action: "continue" }};
    }}
    const handled = await dispatch(omp, event.text.trim(), ctx);
    return {{ action: handled ? "handled" : "continue" }};
  }});
}}
"""
