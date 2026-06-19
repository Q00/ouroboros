# Grok Build CLI Runtime

Run Ouroboros workflows on top of the locally installed xAI **Grok Build** CLI
(the `grok` binary).

The Grok runtime is a sibling of the Codex / Gemini / Hermes runtimes:
Ouroboros owns the orchestration loop and shells out to `grok` per task. Grok
emits a structured `streaming-json` event stream, so intermediate reasoning and
output are surfaced live; v1 checkpoints at the Ouroboros layer (event store +
lineage) rather than using Grok's native session resume.

## Prerequisites

| Requirement       | Why                                                              |
|-------------------|------------------------------------------------------------------|
| `grok` CLI        | Provider — install via `curl -fsSL https://x.ai/cli/install.sh \| bash` |
| xAI auth          | `grok login` (browser OAuth, SuperGrok / X Premium+) or `XAI_API_KEY` |
| Ouroboros (base)  | `pip install ouroboros-ai` — no provider-specific extras         |

> Grok Build runs on the **base** Ouroboros package. It does **not** require the
> `[claude]` extra.

## Quick start

```bash
# 1. Install + authenticate the Grok Build CLI
curl -fsSL https://x.ai/cli/install.sh | bash
grok login                           # browser OAuth (SuperGrok / X Premium+)

# 2. Point Ouroboros at Grok (config.yaml or the settings GUI)
ouroboros config                     # GUI: set a stage's runtime to "grok"

# 3. Run a workflow
ouroboros run workflow seed.yaml --runtime grok
```

## CLI path resolution

The runtime looks for the binary in this order:

1. Constructor argument `cli_path=...`
2. `OUROBOROS_GROK_CLI_PATH` environment variable
3. `orchestrator.grok_cli_path` in `~/.ouroboros/config.yaml`
4. `grok` on `$PATH`

## Configuration

```yaml
# ~/.ouroboros/config.yaml
orchestrator:
  runtime_backend: grok
  grok_cli_path: ~/.local/bin/grok    # optional; auto-detected
```

Grok is **runtime-only** — it drives the agentic orchestrator but is not
registered as an LLM-completion backend, so it is not a valid `llm.backend`
value. For completion-level diversity (consensus / role models), route xAI Grok
through `litellm`/OpenRouter.

### Per-stage routing

Grok is a strong cross-vendor stage in a multi-LLM pipeline — for example, as a
divergent reflect/unstuck stage distinct from the execute vendor:

```yaml
orchestrator:
  runtime_backend: claude
  runtime_profile:
    stages:
      execute: claude      # generate
      evaluate: gemini     # cross-vendor verify
      reflect: grok        # divergent lateral thinking
```

## Headless contract

Each task spawns:

```text
grok -p <PROMPT> \
     --output-format streaming-json \
     --permission-mode acceptEdits \
     [-m grok-build]
```

| Flag                  | Why                                                            |
|-----------------------|----------------------------------------------------------------|
| `-p`                  | Single-turn prompt; prints the response to stdout and exits    |
| `--output-format`     | `streaming-json` NDJSON events parsed by the normalizer         |
| `--permission-mode`   | Native non-blocking mode (`acceptEdits` / `bypassPermissions`)  |
| `-m`                  | Optional model override (`grok-build`, `grok-composer-2.5-fast`)|

Ouroboros maps its permission modes onto Grok's native `--permission-mode`:

| Ouroboros permission mode | `grok` value        | When used                                |
|---------------------------|---------------------|------------------------------------------|
| `acceptEdits` (default)   | `acceptEdits`       | Applies edits without TTY prompts        |
| `bypassPermissions`       | `bypassPermissions` | Explicit full bypass                     |

The interactive Ouroboros `default` mode is normalized to `acceptEdits` (with an
audit log) because a headless `grok -p` run cannot service a TTY approval
prompt.

Grok owns its own model catalog, so it is a **sentinel-model backend**:
Ouroboros lets Grok pick its configured model and only forwards `-m` when you
set an explicit id. The settings GUI **dynamically lists** the callable models
by running `grok models` (parsing its bulleted output), so the picker reflects
exactly what your Grok CLI / subscription can call; the static fallback catalog
is `grok-build` and `grok-composer-2.5-fast`.

## Event mapping

The runtime parses Grok's `streaming-json` events through the shared NDJSON
normalizer and maps them onto Ouroboros' `AgentMessage`:

| Grok event                       | Ouroboros message                       |
|----------------------------------|-----------------------------------------|
| `{"type":"thought","data":...}`  | `assistant` with `data.thinking`        |
| `{"type":"text","data":...}`     | `assistant` message                     |
| `{"type":"end","stopReason":...}`| **terminal** `assistant` marker (`data.terminal=True`) |
| `error`                          | `system` message with `data.is_error=True` |

## Capabilities

| Capability               | Status                                                |
|--------------------------|-------------------------------------------------------|
| Headless execution       | ✅ `grok -p`                                          |
| Structured event stream  | ✅ `--output-format streaming-json`                   |
| Tool calls               | ✅ (Grok-managed via `--permission-mode`)             |
| Reasoning effort         | `grok --reasoning-effort` exists; native effort routing is a planned follow-up |
| Session resumption       | ❌ not wired in v1 (`grok -r` exists; recovery at the Ouroboros lineage layer) |
| LLM-completion backend   | ❌ runtime-only                                       |

## Troubleshooting

**`grok` not found.** Install the Grok Build CLI, then set
`OUROBOROS_GROK_CLI_PATH=/abs/path/to/grok` or `orchestrator.grok_cli_path`.

**Authentication errors.** Run `grok login` once (SuperGrok / X Premium+), or
export `XAI_API_KEY` for headless/API-key use.

**The CLI hangs waiting for input.** The runtime always passes `-p` and a
non-blocking `--permission-mode`. If you see a hang, confirm you are invoking
the runtime through `ouroboros run` (or the MCP server) rather than driving
`grok` directly.
