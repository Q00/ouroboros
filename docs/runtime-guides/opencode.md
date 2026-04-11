<!--
doc_metadata:
  runtime_scope: [opencode]
-->

# Running Ouroboros with OpenCode

> For installation and first-run onboarding, see [Getting Started](../getting-started.md).

Ouroboros can use **OpenCode** as a runtime backend. [OpenCode](https://opencode.ai) is an open-source AI coding agent that supports multiple model providers (Anthropic, OpenAI, Google, and others) through its own provider management. In Ouroboros, the OpenCode backend is presented as a **session-oriented runtime** with the same specification-first workflow harness (acceptance criteria, evaluation principles, deterministic exit conditions).

No additional Python SDK is required beyond the base `ouroboros-ai` package.

> **Model recommendation:** OpenCode supports any model available through your configured provider. For best results with Ouroboros workflows, use a frontier-class model (Claude Opus, GPT-5.4, or equivalent) that handles multi-step agentic coding tasks well.

## Prerequisites

- **OpenCode** installed, configured, and on your `PATH` (see [install steps](#installing-opencode) below)
- A **provider configured in OpenCode** (run `opencode` and complete the first-run setup, or use `opencode providers auth <provider>`)
- **Python >= 3.12**

> **Note:** OpenCode manages its own provider authentication. You do not need to set `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` environment variables for Ouroboros — OpenCode handles provider credentials internally via its own configuration at `~/.config/opencode/opencode.jsonc` (or `opencode.json`).

## Installing OpenCode

OpenCode is distributed as a standalone binary. Install via the official installer script or npm:

```bash
# Recommended: official installer
curl -fsSL https://opencode.ai/install | bash

# Alternative: npm
npm i -g opencode-ai@latest
```

Verify the installation:

```bash
opencode --version
```

After install, run `opencode` once to complete first-run provider setup (select a provider and authenticate).

For alternative install methods, see the [OpenCode documentation](https://opencode.ai/docs).

## Installing Ouroboros

> For all installation options (pip, one-liner, from source) and first-run onboarding, see **[Getting Started](../getting-started.md)**.
> The base `ouroboros-ai` package includes the OpenCode runtime adapter — no extras are required.

## Platform Notes

The OpenCode runtime adapter has been developed and tested on Linux. Other platforms may work but have not been verified with the Ouroboros integration.

| Platform | Status | Notes |
|----------|--------|-------|
| Linux (x86_64/ARM64) | Tested | Primary development and testing platform |
| macOS (ARM/Intel) | Untested | Expected to work — OpenCode supports macOS natively |
| Windows (WSL 2) | Untested | Expected to work via WSL 2 — not verified with Ouroboros |
| Windows (native) | Untested | Not recommended — subprocess and path handling may have issues |

> If you test on macOS or Windows and encounter issues, please report them.

## Configuration

To select OpenCode as the runtime backend, set the following in your Ouroboros configuration:

```yaml
orchestrator:
  runtime_backend: opencode
```

Or pass the backend on the command line:

```bash
uv run ouroboros run workflow --runtime opencode ~/.ouroboros/seeds/seed_abcd1234ef56.yaml
```

### Where OpenCode users configure what

Use `~/.ouroboros/config.yaml` for Ouroboros runtime settings (backend selection, permission mode, CLI path).

Use `~/.config/opencode/opencode.jsonc` or `opencode.json` (OpenCode's own config) for provider/model selection, MCP servers, and tool permissions. `ouroboros setup --runtime opencode` writes the Ouroboros MCP server entry into this file automatically.

```yaml
# ~/.ouroboros/config.yaml
orchestrator:
  runtime_backend: opencode
  opencode_cli_path: /usr/local/bin/opencode   # omit if opencode is already on PATH

llm:
  backend: opencode
```

Model selection for OpenCode-backed workflows is configured in OpenCode itself, not in `config.yaml`. The `orchestrator.opencode_permission_mode` defaults to `bypassPermissions` since OpenCode runs non-interactively via `opencode run --format json`.

### Setup

Run the setup command to auto-configure:

```bash
ouroboros setup --runtime opencode
```

This:

- Detects the `opencode` binary on your `PATH` and records it as `orchestrator.opencode_cli_path`
- Writes `orchestrator.runtime_backend: opencode` and `llm.backend: opencode` to `~/.ouroboros/config.yaml`
- Registers the Ouroboros MCP server in OpenCode's configuration file (`~/.config/opencode/opencode.jsonc` or `opencode.json`). If an existing `.jsonc` config is found, setup updates it in place instead of creating a separate `.json` file. Note: The setup process rewrites the file as plain JSON and removes comments (the file supports JSONC format initially, but setup normalizes to JSON for compatibility).

### `ooo` Skill Availability on OpenCode

After running `ouroboros setup --runtime opencode`, the Ouroboros MCP server is registered in OpenCode's config. The `ooo` skills are available via MCP tool dispatch within OpenCode sessions.

| `ooo` Skill | OpenCode session | CLI equivalent (Terminal) |
|-------------|------------------|--------------------------|
| `ooo interview` | Yes | `ouroboros init start --llm-backend opencode "your idea"` |
| `ooo seed` | Yes | *(bundled in `ouroboros init start`)* |
| `ooo run` | Yes | `ouroboros run workflow --runtime opencode seed.yaml` |
| `ooo status` | Yes | `ouroboros status execution <execution_id>` |
| `ooo evaluate` | Yes | *(MCP only)* |
| `ooo evolve` | Yes | *(MCP only)* |
| `ooo ralph` | Yes | *(MCP only)* |
| `ooo cancel` | Yes | `ouroboros cancel execution <execution_id>` |
| `ooo unstuck` | Yes | *(MCP only)* |
| `ooo tutorial` | Yes | *(MCP only)* |
| `ooo welcome` | Yes | *(MCP only)* |
| `ooo update` | Yes | `pip install --upgrade ouroboros-ai` |
| `ooo help` | Yes | `ouroboros --help` |
| `ooo qa` | Yes | *(MCP only)* |
| `ooo setup` | Yes | `ouroboros setup --runtime opencode` |
| `ooo publish` | Yes | *(no direct `ouroboros publish` subcommand; skill/runtime flow uses `gh` CLI)* |

> **Note on `ooo seed` vs `ooo interview`:** These are two distinct skills with separate roles. `ooo interview` runs a Socratic Q&A session and returns a `session_id`. `ooo seed` accepts that `session_id` and generates a structured Seed YAML (with ambiguity scoring). From the terminal, both steps are performed in a single `ouroboros init start` invocation.

## Quick Start

> For the full first-run onboarding flow (interview -> seed -> execute), see **[Getting Started](../getting-started.md)**.

### Verify Installation

```bash
opencode --version
ouroboros --help
```

## How It Works

```
+-----------------+     +------------------+     +-----------------+
|   Seed YAML     | --> |   Orchestrator   | --> |    OpenCode     |
|  (your task)    |     | (runtime_factory)|     |   (runtime)     |
+-----------------+     +------------------+     +-----------------+
                                |
                                v
                        +------------------+
                        |  Tools Available |
                        |  - Read          |
                        |  - Write         |
                        |  - Edit          |
                        |  - Bash          |
                        |  - Glob          |
                        |  - Grep          |
                        +------------------+
```

The `OpenCodeRuntime` adapter launches `opencode run --format json` as a subprocess for each task execution. The orchestrator pipes the prompt via stdin and parses the structured JSON event stream from stdout.

> For a side-by-side comparison of all runtime backends, see the [runtime capability matrix](../runtime-capability-matrix.md).

## OpenCode-Specific Strengths

- **Multi-provider support** -- use Anthropic, OpenAI, Google, or other providers through a single runtime
- **Built-in provider management** -- OpenCode handles its own authentication and provider configuration, no env var setup required
- **Rich tool access** -- full suite of file, shell, and search tools (same surface as Claude Code)
- **Native MCP integration** -- OpenCode has built-in MCP server support
- **Open-source** -- fully open-source, allowing inspection and contribution
- **Session-aware runtime** -- Ouroboros preserves OpenCode session handles and resume state across workflow steps via `--session` flag

## Runtime Differences

OpenCode, Claude Code, and Codex CLI are independent runtime backends with different tool sets, permission models, and provider ecosystems. The same Seed file works with all three, but execution paths may differ.

| Aspect | OpenCode | Claude Code | Codex CLI |
|--------|----------|-------------|-----------|
| What it is | Ouroboros session runtime backed by OpenCode subprocess | Anthropic's agentic coding tool | Ouroboros session runtime backed by Codex CLI transport |
| Authentication | Managed by OpenCode (`opencode providers auth`) | Max Plan subscription | OpenAI API key |
| Model | Any model supported by configured provider | Claude (via claude-agent-sdk) | GPT-5.4 with medium reasoning effort (recommended) |
| Tool surface | Read, Write, Edit, Bash, Glob, Grep | Read, Write, Edit, Bash, Glob, Grep | Codex-native tools (file I/O, shell) |
| Session model | Session-aware via `--session` flag and runtime handles | Native Claude session context | Session-aware via runtime handles, resume IDs, and skill dispatch |
| Transport | Subprocess (`opencode run --format json`), prompt via stdin | Claude Agent SDK (direct API) | Subprocess (`codex` executable) |
| Cost model | Provider API usage charges | Included in Max Plan subscription | OpenAI API usage charges |
| Tested platforms | Linux | Linux, macOS | Linux, macOS |

> **Note:** The Ouroboros workflow model (Seed files, acceptance criteria, evaluation principles) is identical across runtimes. However, because OpenCode, Claude Code, and Codex CLI have different underlying agent capabilities, tool access, and provider ecosystems, they may produce different execution paths and results for the same Seed file.

## CLI Options

### Workflow Commands

```bash
# Execute workflow (OpenCode runtime)
# Seeds generated by ouroboros init are saved to ~/.ouroboros/seeds/seed_{id}.yaml
uv run ouroboros run workflow --runtime opencode ~/.ouroboros/seeds/seed_abcd1234ef56.yaml

# Debug output (show logs and agent output)
uv run ouroboros run workflow --runtime opencode --debug ~/.ouroboros/seeds/seed_abcd1234ef56.yaml

# Resume a previous session
uv run ouroboros run workflow --runtime opencode --resume <session_id> ~/.ouroboros/seeds/seed_abcd1234ef56.yaml
```

## Seed File Reference

| Field | Required | Description |
|-------|----------|-------------|
| `goal` | Yes | Primary objective |
| `task_type` | No | Execution strategy: `code` (default), `research`, or `analysis` |
| `constraints` | No | Hard constraints to satisfy |
| `acceptance_criteria` | No | Specific success criteria |
| `ontology_schema` | Yes | Output structure definition |
| `evaluation_principles` | No | Principles for evaluation |
| `exit_conditions` | No | Termination conditions |
| `metadata.ambiguity_score` | Yes | Must be <= 0.2 |

## Known Limitations

### Session pollution

Each task execution via `opencode run` creates a visible session in OpenCode's session history. Long-running workflows with many orchestrator steps will accumulate sessions. A future phase will reparent these sessions under the caller to prevent polluting the session picker (see [#331](https://github.com/Q00/ouroboros/issues/331)).

### No interactive mode

The adapter uses `opencode run --format json` (non-interactive). Features that require interactive OpenCode sessions (e.g., manual approval prompts) are not available during Ouroboros execution.

### Permission mode not wired to CLI

OpenCode's `opencode run` command does not expose a `--permission-mode` flag. The `opencode_permission_mode` config value is stored for forward compatibility but is not currently passed to the subprocess. OpenCode runs non-interactively by default, so there is no approval dialogue to bypass.

## Troubleshooting

### OpenCode not found

Ensure `opencode` is installed and available on your `PATH`:

```bash
which opencode
```

If not installed:

```bash
curl -fsSL https://opencode.ai/install | bash
```

### Provider not configured

If OpenCode reports a provider error, ensure you have completed first-run setup:

```bash
opencode                        # interactive first-run setup
# or
opencode providers auth anthropic   # configure a specific provider
```

OpenCode manages its own provider credentials — you do not need to set `ANTHROPIC_API_KEY` or similar environment variables for the Ouroboros integration.

### "Providers: warning" in health check

This is normal when using the orchestrator runtime backends. The warning refers to LiteLLM providers, which are not used in orchestrator mode.

### "EventStore not initialized"

The database will be created automatically at `~/.ouroboros/ouroboros.db`.

## Cost

Using OpenCode as the runtime backend incurs API charges from your configured provider. Costs depend on:

- Provider and model selected in OpenCode's configuration
- Task complexity and token usage
- Number of tool calls and iterations

Refer to your provider's pricing page for current rates.
