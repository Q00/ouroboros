# Gemini CLI Runtime

Run Ouroboros workflows on top of the locally installed
[`gemini`](https://github.com/google-gemini/gemini-cli) CLI.

The Gemini runtime is a sibling of the Codex / Hermes / OpenCode runtimes:
Ouroboros owns the orchestration loop and shells out to `gemini` per task
instead of talking to a hosted SDK. The runtime is **stateless** — Gemini
does not currently expose a session-resume API, so checkpointing happens at
the Ouroboros layer (event store + lineage), not inside the subprocess.

## Prerequisites

| Requirement       | Why                                                          |
|-------------------|--------------------------------------------------------------|
| `gemini` CLI      | Provider — install via `npm install -g @google/gemini-cli`   |
| Google auth       | `gemini auth` (or `GOOGLE_API_KEY`) once before first use    |
| Ouroboros (base)  | `pip install ouroboros-ai` — no provider-specific extras     |

> Gemini runs on the **base** Ouroboros package. It does **not** require the
> `[claude]` extra; the MCP entry can stay on whichever extra you previously
> configured, or you can use the base `ouroboros-ai[mcp]` entry.

## Quick start

```bash
# 1. Install Gemini CLI (if not already on PATH)
npm install -g @google/gemini-cli
gemini auth                       # one-time auth

# 2. Point Ouroboros at Gemini
ouroboros setup --runtime gemini  # auto-detects PATH or OUROBOROS_GEMINI_CLI_PATH
# or, switch later:
ouroboros config backend gemini

# 3. Run a workflow
ouroboros init "Add a CLI flag to skip eval"
ouroboros run workflow seed.yaml
```

## CLI path resolution

The runtime looks for the binary in this order:

1. Constructor argument `cli_path=...`
2. `OUROBOROS_GEMINI_CLI_PATH` environment variable
3. `orchestrator.gemini_cli_path` in `~/.ouroboros/config.yaml`
4. `gemini` on `$PATH`

This means non-PATH installs (e.g. `~/.local/share/gemini-cli/bin/gemini`)
work without modifying shell init.

## Configuration

```yaml
# ~/.ouroboros/config.yaml
orchestrator:
  runtime_backend: gemini
  gemini_cli_path: /opt/homebrew/bin/gemini   # optional; auto-detected
llm:
  backend: gemini                             # for interview / seed / eval
```

The same `gemini` value is accepted by every CLI surface that takes a
backend name:

- `ouroboros setup --runtime gemini`
- `ouroboros config backend gemini`
- `ouroboros mcp serve --llm-backend gemini`
- `ouroboros init --llm-backend gemini`

## Headless contract

Each task spawns:

```text
gemini --prompt <PROMPT> \
       --non-interactive \
       --output-format stream-json \
       --approval-mode yolo \
       [--model gemini-2.5-pro]
```

| Flag                | Why                                                     |
|---------------------|---------------------------------------------------------|
| `--prompt`          | Carries the request (Gemini's documented headless API)  |
| `--non-interactive` | Disables TTY prompts so the subprocess never blocks     |
| `--output-format`   | NDJSON event stream parsed by `GeminiEventNormalizer`   |
| `--approval-mode`   | `yolo` — required for headless approvals                |
| `--model`           | Optional model override (`gemini-2.5-pro`, `flash`)     |

## Event mapping

The runtime parses Gemini's `stream-json` events through
`GeminiEventNormalizer` and maps them onto Ouroboros' `AgentMessage`:

| Gemini event   | Ouroboros message                                 |
|----------------|---------------------------------------------------|
| `init`         | session metadata only — no message emitted        |
| `message` / `text` | `assistant` message                            |
| `thinking`     | `assistant` with `data.thinking`                  |
| `tool_use`     | `assistant` with `tool_name` + `data.tool_input`  |
| `tool_result`  | `tool` message with `data.is_error`               |
| `error`        | `system` message with `data.is_error=True`        |
| `result`       | **terminal** `assistant` message — Gemini emits the final response in this event when no intermediate `message` event was produced |

The terminal `result` event is critical: it is the only way the final
assistant text reaches the orchestrator when Gemini chose not to emit a
mid-stream `message`. Earlier prototypes dropped this event and lost the
final answer; the runtime now surfaces it explicitly with
`data.terminal=True`.

## Capabilities

| Capability               | Status                                          |
|--------------------------|-------------------------------------------------|
| Headless execution       | ✅                                              |
| Tool calls               | ✅ (Gemini-managed — no Codex permission flags) |
| Recursion guard          | ✅ `_OUROBOROS_DEPTH` (matches Claude/Codex)    |
| Response truncation      | ✅ via `InputValidator` (matches #315)          |
| Session resumption       | ❌ not supported by Gemini CLI                  |

If you need resumable sessions, use the Claude or Codex runtime — Gemini's
recovery happens at the Ouroboros checkpoint layer instead.

## Troubleshooting

**`gemini CLI not found.`**
Install `@google/gemini-cli`, then either let `setup` auto-detect it or set
`OUROBOROS_GEMINI_CLI_PATH=/abs/path/to/gemini`.

**Final response missing.**
You're probably on an old build that ignored `result` events. Upgrade and
re-run; the runtime now surfaces `result.response` as a terminal assistant
message.

**The CLI hangs waiting for input.**
The runtime always passes `--non-interactive`. If you see a hang, check
that you're invoking the runtime through `ouroboros run` (or the MCP
server) rather than driving `gemini` directly without that flag.
