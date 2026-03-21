<!--
doc_metadata:
  runtime_scope: [gemini]
-->

# Running Ouroboros with Gemini CLI

> For installation and first-run onboarding, see [Getting Started](../getting-started.md).

Ouroboros can use **Google Gemini CLI** as a runtime backend. [Gemini CLI](https://ai.google.dev/cli) is the local Gemini execution surface that the adapter talks to. In Ouroboros, that backend is presented as a **session-oriented runtime** with the same specification-first workflow harness (acceptance criteria, evaluation principles, deterministic exit conditions), even though the adapter itself communicates with the local `gemini` executable.

No additional Python SDK is required beyond the base `ouroboros-ai` package.

> **Model recommendation:** Use **Gemini 2.5 Pro** (or later) for best results with Gemini CLI. Gemini 2.5 Pro provides strong coding, multi-step reasoning, and agentic task execution that pairs well with the Ouroboros specification-first workflow harness.

## Prerequisites

- **Gemini CLI** installed and on your `PATH` (see [install steps](#installing-gemini-cli) below)
- A **Google API key** with access to Gemini (set `GOOGLE_API_KEY`). See [`credentials.yaml`](../config-reference.md#credentialsyaml) for file-based key management
- **Python >= 3.12**

## Installing Gemini CLI

Gemini CLI is distributed as an npm package. Install it globally:

```bash
npm install -g @google/gemini-cli
```

Verify the installation:

```bash
gemini --version
```

For alternative install methods and shell completions, see the [Gemini CLI documentation](https://ai.google.dev/cli).

## Installing Ouroboros

> For all installation options (pip, one-liner, from source) and first-run onboarding, see **[Getting Started](../getting-started.md)**.
> The base `ouroboros-ai` package includes the Gemini CLI runtime adapter — no extras are required.

## Platform Notes

| Platform | Status | Notes |
|----------|--------|-------|
| macOS (ARM/Intel) | Supported | Primary development platform |
| Linux (x86_64/ARM64) | Supported | Tested on Ubuntu 22.04+, Debian 12+, Fedora 38+ |
| Windows (WSL 2) | Supported | Recommended path for Windows users |
| Windows (native) | Experimental | WSL 2 strongly recommended; native Windows may have path-handling and process-management issues. Gemini CLI itself does not support native Windows. |

> **Windows users:** Install and run both Gemini CLI and Ouroboros inside a WSL 2 environment for full compatibility. See [Platform Support](../platform-support.md) for details.

## Configuration

To select Gemini CLI as the runtime backend, set the following in your Ouroboros configuration:

```yaml
orchestrator:
  runtime_backend: gemini
```

Or pass the backend on the command line:

```bash
uv run ouroboros run workflow --runtime gemini ~/.ouroboros/seeds/seed_abcd1234ef56.yaml
```

### Additional Configuration Options

For fine-grained control over Gemini CLI behavior, you can configure the following in your Ouroboros configuration:

```yaml
orchestrator:
  gemini_cli_path: /usr/local/bin/gemini  # Path to gemini executable
  gemini_permission_mode: sandbox           # Permission mode: sandbox (default), auto_edit, bypassPermissions
```

Or set via environment variables:

```bash
export OUROBOROS_GEMINI_CLI_PATH=/usr/local/bin/gemini
export OUROBOROS_GEMINI_PERMISSION_MODE=sandbox
export OUROBOROS_AGENT_RUNTIME=gemini
```

#### Permission Modes

Gemini CLI supports three permission modes that control how the agent interacts with your file system and resources:

| Mode | CLI Flag | Config Value | Description |
|------|----------|--------------|-------------|
| Sandbox (default) | `--sandbox` | `sandbox` | Safe mode with limited file system access |
| Auto-Edit | `--approval-mode auto_edit` | `auto_edit` | Automatically accepts file edits without confirmation |
| Bypass Permissions | `--yolo` | `bypassPermissions` | Full access without restrictions (use with caution) |

## Command Surface

From the user's perspective, the Gemini integration behaves like a **session-oriented Ouroboros runtime** — the same specification-first workflow harness that drives the Claude runtime.

Under the hood, `GeminiCliRuntime` still talks to the local `gemini` executable, but it preserves native session IDs and resume handles, and the Gemini command dispatcher can route `ooo`-style skill commands through the in-process Ouroboros MCP server.

Today, the most reliable documented entrypoint is still the `ouroboros` CLI while Gemini artifact installation is being finalized.

`ouroboros setup --runtime gemini` currently:

- Detects the `gemini` binary on your `PATH`
- Writes `orchestrator.runtime_backend: gemini` to `~/.ouroboros/config.yaml`
- Records `orchestrator.gemini_cli_path` when available

Packaged Gemini rule and skill assets exist in the repository, but automatic installation into `~/.gemini/` is not currently part of `ouroboros setup`. Once those artifacts are installed, Gemini can present an `ooo`-driven session surface similar to Claude Code. Until that setup path is fully wired, prefer the documented `ouroboros` CLI flow.

### `ooo` Skill Availability on Gemini

> **Current status:** `ooo` skill shortcuts (`ooo interview`, `ooo run`, etc.) are **Claude Code-specific** — they rely on Claude Code's skill/plugin system. Automatic installation of Gemini rule and skill artifacts into `~/.gemini/` is **not currently part of `ouroboros setup`**. Gemini users should use the equivalent `ouroboros` CLI commands from the terminal instead.

The table below maps all 14 `ooo` skills from the registry to their CLI equivalents for Gemini users.

| `ooo` Skill | Available in Gemini session | CLI equivalent (Terminal) |
|-------------|---------------------------|--------------------------|
| `ooo interview` | **Not yet** — Gemini skill artifacts not installed | `uv run ouroboros init start --llm-backend gemini "your idea"` |
| `ooo seed` | **Not yet** | *(no standalone CLI equivalent — `ooo seed` takes a `session_id` from a prior `ooo interview` run; from the terminal, both steps are bundled: `ouroboros init start` automatically offers seed generation at the end of the interview)* |
| `ooo run` | **Not yet** | `uv run ouroboros run workflow --runtime gemini ~/.ouroboros/seeds/seed_{id}.yaml` |
| `ooo status` | **Not yet** | `ouroboros status executions` (list all) or `ouroboros status execution <id>` (show details) — neither implements drift-measurement via MCP |
| `ooo evaluate` | **Not yet** | *(not exposed as an `ouroboros` CLI command)* |
| `ooo evolve` | **Not yet** | *(not exposed as an `ouroboros` CLI command)* |
| `ooo ralph` | **Not yet** | *(not exposed as an `ouroboros` CLI command — drives a persistent execute-verify loop via background MCP job tools: `ouroboros_start_evolve_step`, `ouroboros_job_wait`, `ouroboros_job_result`)* |
| `ooo cancel` | **Not yet** | `uv run ouroboros cancel execution <session_id>` |
| `ooo unstuck` | **Not yet** | *(not exposed as an `ouroboros` CLI command)* |
| `ooo tutorial` | **Not yet** | *(not exposed as an `ouroboros` CLI command)* |
| `ooo welcome` | **Not yet** | *(not exposed as an `ouroboros` CLI command)* |
| `ooo update` | **Not yet** | `pip install --upgrade ouroboros-ai` *(upgrades directly; the skill also checks current vs. latest version before upgrading — the CLI skips that check)* |
| `ooo help` | **Not yet** | `uv run ouroboros --help` |
| `ooo setup` | **No** — Claude Code only | `uv run ouroboros setup --runtime gemini` |

> **Why are `ooo` skills not available in Gemini sessions?** The `ooo` skill commands use Claude Code's skill/plugin dispatch mechanism and require skill files installed in the Claude Code environment. The equivalent Gemini skill artifacts (Gemini rules/commands) are present in the repository but automatic installation into `~/.gemini/` is not currently wired into `ouroboros setup`. Until that path is completed, use the `ouroboros` CLI commands listed above.
>
> **Note on `ooo seed` vs `ooo interview`:** These are two distinct skills with separate roles. `ooo interview` runs a Socratic Q&A session and returns a `session_id`. `ooo seed` accepts that `session_id` and generates a structured Seed YAML (with ambiguity scoring). From the terminal, both steps are performed in a single `ouroboros init start` invocation — there is no separate seed-generation subcommand.

## Quick Start

> For the full first-run onboarding flow (interview → seed → execute), see **[Getting Started](../getting-started.md)**.

### Verify Installation

```bash
gemini --version
ouroboros --help
```

## How It Works

```
+-----------------+     +------------------+     +-----------------+
|   Seed YAML     | --> |   Orchestrator   | --> |   Gemini CLI    |
|  (your task)    |     | (runtime_factory)|     |   (runtime)     |
+-----------------+     +------------------+     +-----------------+
                                |
                                v
                        +------------------+
                        |  Gemini executes |
                        |  with its own    |
                        |  tool set and    |
                        |  sandbox model   |
                        +------------------+
```

The `GeminiCliRuntime` adapter launches `gemini` as its transport layer, but wraps it with session handles, resume support, and deterministic skill/MCP dispatch so the runtime behaves like a persistent Ouroboros session.

> For a side-by-side comparison of all runtime backends, see the [runtime capability matrix](../runtime-capability-matrix.md).

## Gemini CLI Strengths

- **Session-aware Gemini runtime** -- Ouroboros preserves Gemini session handles and resume state across workflow steps
- **Strong coding and reasoning** -- Gemini 2.5 Pro provides robust code generation and multi-file editing across languages
- **Agentic task execution** -- effective at decomposing complex tasks into sequential steps and iterating autonomously
- **Open-source model weights** -- Gemini models are available with open-source weights, allowing inspection and self-hosting
- **Ouroboros harness** -- the specification-first workflow engine adds structured acceptance criteria, evaluation principles, and deterministic exit conditions on top of Gemini CLI's capabilities

## Runtime Differences

Gemini CLI and Claude Code are independent runtime backends with different tool sets, permission models, and sandboxing behavior. The same Seed file works with both, but execution paths may differ.

| Aspect | Gemini CLI | Claude Code |
|--------|-----------|-------------|
| What it is | Ouroboros session runtime backed by Gemini CLI transport | Anthropic's agentic coding tool |
| Authentication | Google API key | Max Plan subscription |
| Model | Gemini 2.5 Pro (recommended) | Claude (via claude-agent-sdk) |
| Sandbox | Gemini CLI's own sandbox model | Claude Code's permission system |
| Tool surface | Gemini-native tools (file I/O, shell) | Read, Write, Edit, Bash, Glob, Grep |
| Session model | Session-aware via runtime handles, resume IDs, and skill dispatch | Native Claude session context |
| Cost model | Google API usage charges | Included in Max Plan subscription |
| Windows (native) | Not supported | Experimental |

> **Note:** The Ouroboros workflow model (Seed files, acceptance criteria, evaluation principles) is identical across runtimes. However, because Gemini CLI and Claude Code have different underlying agent capabilities, tool access, and sandboxing, they may produce different execution paths and results for the same Seed file.

## CLI Options

### Workflow Commands

```bash
# Execute workflow (Gemini runtime)
# Seeds generated by ouroboros init are saved to ~/.ouroboros/seeds/seed_{id}.yaml
uv run ouroboros run workflow --runtime gemini ~/.ouroboros/seeds/seed_abcd1234ef56.yaml

# Dry run (validate seed without executing)
uv run ouroboros run workflow --dry-run ~/.ouroboros/seeds/seed_abcd1234ef56.yaml

# Debug output (show logs and agent output)
uv run ouroboros run workflow --runtime gemini --debug ~/.ouroboros/seeds/seed_abcd1234ef56.yaml

# Resume a previous session
uv run ouroboros run workflow --runtime gemini --resume <session_id> ~/.ouroboros/seeds/seed_abcd1234ef56.yaml
```

### Permission Mode Options

```bash
# Sandbox mode (default, most restrictive)
uv run ouroboros run workflow --runtime gemini --permission-mode sandbox ~/.ouroboros/seeds/seed_abcd1234ef56.yaml

# Auto-edit mode (automatically accepts file edits)
uv run ouroboros run workflow --runtime gemini --permission-mode auto_edit ~/.ouroboros/seeds/seed_abcd1234ef56.yaml

# Bypass permissions (full access without restrictions)
uv run ouroboros run workflow --runtime gemini --permission-mode bypassPermissions ~/.ouroboros/seeds/seed_abcd1234ef56.yaml
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

## Troubleshooting

### Gemini CLI not found

Ensure `gemini` is installed and available on your `PATH`:

```bash
which gemini
```

If not installed, install via npm:

```bash
npm install -g @google/gemini-cli
```

See the [Gemini CLI documentation](https://ai.google.dev/cli) for alternative installation methods.

### API key errors

Verify your Google API key is set and has access to Gemini models:

```bash
echo $GOOGLE_API_KEY  # should be set
```

You can generate a free API key at [Google AI Studio](https://aistudio.google.com/app/apikey).

### "Providers: warning" in health check

This is normal when using the orchestrator runtime backends. The warning refers to LiteLLM providers, which are not used in orchestrator mode.

### "EventStore not initialized"

The database will be created automatically at `~/.ouroboros/ouroboros.db`.

## Cost

Using Gemini CLI as the runtime backend requires a Google API key and incurs Google API usage charges. Costs depend on:

- Model used (Gemini 2.5 Pro recommended)
- Task complexity and token usage
- Number of tool calls and iterations

Refer to [Google's pricing page](https://ai.google.dev/pricing) for current rates. Note that free tier access is available with rate limits.
