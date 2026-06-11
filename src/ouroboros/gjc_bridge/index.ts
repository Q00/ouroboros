import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const COMMAND_RE = /^\s*ooo(?:\s+|$)/i;
const UNSUPPORTED_DISPATCH_EXIT_CODE = 78;
const DEPTH_ENV = "_OUROBOROS_GJC_BRIDGE_DEPTH";
const TIMEOUT_MS = Number(process.env.OUROBOROS_GJC_BRIDGE_TIMEOUT_MS || 6 * 60 * 60 * 1000);
const DEFAULT_COMMAND = "ouroboros";
const DEFAULT_ARGS: string[] = [];

type InputEvent = { text?: string };
type InputContext = { cwd: string };
type InputResult = { handled?: boolean; text?: string; images?: unknown[] } | void;
type ExtensionAPI = {
  on(
    event: "input",
    handler: (event: InputEvent, ctx: InputContext) => Promise<InputResult> | InputResult,
  ): void;
};

type ExecResult = { stdout: string; stderr: string; code: number | null };

function ouroborosEntry(): { command: string; args: string[] } {
  if (process.env.OUROBOROS_CLI) return { command: process.env.OUROBOROS_CLI, args: [] };
  return { command: DEFAULT_COMMAND, args: DEFAULT_ARGS };
}

function outputText(stdout: string, stderr: string): string {
  const out = stdout.trim();
  const err = stderr.trim();
  if (out && err) return `${out}\n\n${err}`;
  return out || err || "(no output)";
}

async function dispatch(text: string, cwd: string): Promise<ExecResult> {
  const env = { ...process.env, [DEPTH_ENV]: "1" };
  const entry = ouroborosEntry();
  const args = [...entry.args, "dispatch", "--runtime", "gjc", "--cwd", cwd, text];
  try {
    const result = await execFileAsync(entry.command, args, { cwd, env, timeout: TIMEOUT_MS });
    return { stdout: result.stdout || "", stderr: result.stderr || "", code: 0 };
  } catch (error) {
    const err = error as { stdout?: string; stderr?: string; code?: number | null; signal?: string };
    return {
      stdout: err.stdout || "",
      stderr: err.stderr || err.signal || "",
      code: typeof err.code === "number" ? err.code : 1,
    };
  }
}

export default function ouroborosBridge(gjc: ExtensionAPI) {
  gjc.on("input", async (event, ctx) => {
    const text = (event.text || "").trim();
    if (!COMMAND_RE.test(text) || process.env[DEPTH_ENV]) return { handled: false };

    const result = await dispatch(text, ctx.cwd);
    if (result.code === UNSUPPORTED_DISPATCH_EXIT_CODE) return { handled: false, text: event.text };
    const body = outputText(result.stdout, result.stderr);
    if (result.code === 0) return { handled: true, text: body };
    return { handled: true, text: `Ouroboros dispatch failed (${result.code ?? "unknown"})\n\n${body}` };
  });
}
