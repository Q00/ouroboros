<!--
doc_metadata:
  runtime_scope: [codex]
-->

# Running Ouroboros with Codex CLI

> For installation and first-run onboarding, see [Getting Started](../getting-started.md).

Ouroboros can use **OpenAI Codex CLI** as a runtime backend. [Codex CLI](https://github.com/openai/codex) is the local Codex execution surface that the adapter talks to. In Ouroboros, that backend is presented as a **session-oriented runtime** with the same specification-first workflow harness (acceptance criteria, evaluation principles, deterministic exit conditions), even though the adapter itself communicates with the local `codex` executable.

No additional Python SDK is required beyond the base `ouroboros-ai` package.

> **Model recommendation:** Use **GPT-5.4** (or later) for best results with Codex CLI. GPT-5.4 provides strong coding, multi-step reasoning, and agentic task execution that pairs well with the Ouroboros specification-first workflow harness.

## Prerequisites

- **Codex CLI** installed and on your `PATH` (see [install steps](#installing-codex-cli) below)
- An **OpenAI API key** with access to GPT-5.4 (set `OPENAI_API_KEY`)
- **Python >= 3.12**

## Installing Codex CLI

Codex CLI is distributed as an npm package. Install it globally:

```bash
npm install -g @openai/codex
```

Verify the installation:

```bash
codex --version
```

For alternative install methods and shell completions, see the [Codex CLI README](https://github.com/openai/codex#readme).

## Installing Ouroboros

> For all installation options (pip, one-liner, from source) and first-run onboarding, see **[Getting Started](../getting-started.md)**.
> The base `ouroboros-ai` package includes the Codex CLI runtime adapter — no extras are required.

## Platform Notes

| Platform | Status | Notes |
|----------|--------|-------|
| macOS (ARM/Intel) | Supported | Primary development platform |
| Linux (x86_64/ARM64) | Supported | Tested on Ubuntu 22.04+, Debian 12+, Fedora 38+ |
| Windows (WSL 2) | Supported | Recommended path for Windows users |
| Windows (native) | Experimental | WSL 2 strongly recommended; native Windows may have path-handling and process-management issues. Codex CLI itself does not support native Windows. |

> **Windows users:** Install and run both Codex CLI and Ouroboros inside a WSL 2 environment for full compatibility. See [Platform Support](../platform-support.md) for details.

## Configuration

To select Codex CLI as the runtime backend, set the following in your Ouroboros configuration:

```yaml
orchestrator:
  runtime_backend: codex
```

Or pass the backend on the command line:

```bash
uv run ouroboros run workflow --runtime codex ~/.ouroboros/seeds/seed_abcd1234ef56.yaml
```

## Command Surface

From the user's perspective, the Codex integration behaves like a **session-oriented Ouroboros runtime** — the same specification-first workflow harness that drives the Claude runtime.

Under the hood, `CodexCliRuntime` still talks to the local `codex` executable, but it preserves native session IDs and resume handles, and the Codex command dispatcher can route `ooo`-style skill commands through the in-process Ouroboros MCP server.

Today, the most reliable documented entrypoint is still the `ouroboros` CLI while Codex artifact installation is being finalized.

`ouroboros setup --runtime codex` currently:

- Detects the `codex` binary on your `PATH`
- Writes `orchestrator.runtime_backend: codex` to `~/.ouroboros/config.yaml`
- Records `orchestrator.codex_cli_path` when available

Packaged Codex rule and skill assets exist in the repository, but automatic installation into `~/.codex/` is not currently part of `ouroboros setup`. Once those artifacts are installed, Codex can present an `ooo`-driven session surface similar to Claude Code. Until that setup path is fully wired, prefer the documented `ouroboros` CLI flow.

### `ooo` Skill Availability on Codex

> **Current status:** `ooo` skill shortcuts (`ooo interview`, `ooo run`, etc.) are **Claude Code-specific** — they rely on Claude Code's skill/plugin system. Automatic installation of Codex rule and skill artifacts into `~/.codex/` is **not currently part of `ouroboros setup`**. Codex users should use the equivalent `ouroboros` CLI commands from the terminal instead.

The table below maps all 14 `ooo` skills from the registry to their CLI equivalents for Codex users.

| `ooo` Skill | Available in Codex session | CLI equivalent (Terminal) |
|-------------|---------------------------|--------------------------|
| `ooo interview` | **Not yet** — Codex skill artifacts not installed | `uv run ouroboros init start --llm-backend codex "your idea"` |
| `ooo seed` | **Not yet** | *(no standalone CLI equivalent — `ooo seed` takes a `session_id` from a prior `ooo interview` run; from the terminal, both steps are bundled: `ouroboros init start` automatically offers seed generation at the end of the interview)* |
| `ooo run` | **Not yet** | `uv run ouroboros run workflow --runtime codex ~/.ouroboros/seeds/seed_{id}.yaml` |
| `ooo status` | **Not yet** | `uv run ouroboros status execution <session_id>` — or `uv run ouroboros status executions` to list all sessions *(note: neither CLI subcommand currently implements the drift-measurement that `ooo status` provides via MCP)* |
| `ooo evaluate` | **Not yet** | *(not exposed as an `ouroboros` CLI command)* |
| `ooo evolve` | **Not yet** | *(not exposed as an `ouroboros` CLI command)* |
| `ooo ralph` | **Not yet** | *(not exposed as an `ouroboros` CLI command — drives a persistent execute-verify loop via background MCP job tools: `ouroboros_start_evolve_step`, `ouroboros_job_wait`, `ouroboros_job_result`)* |
| `ooo cancel` | **Not yet** | `uv run ouroboros cancel execution <session_id>` |
| `ooo unstuck` | **Not yet** | *(not exposed as an `ouroboros` CLI command)* |
| `ooo tutorial` | **Not yet** | *(not exposed as an `ouroboros` CLI command)* |
| `ooo welcome` | **Not yet** | *(not exposed as an `ouroboros` CLI command)* |
| `ooo update` | **Not yet** | `pip install --upgrade ouroboros-ai` *(upgrades directly; the skill also checks current vs. latest version before upgrading — the CLI skips that check)* |
| `ooo help` | **Not yet** | `uv run ouroboros --help` |
| `ooo setup` | **No** — Claude Code only | `uv run ouroboros setup --runtime codex` |

> **Why are `ooo` skills not available in Codex sessions?** The `ooo` skill commands use Claude Code's skill/plugin dispatch mechanism and require skill files installed in the Claude Code environment. The equivalent Codex skill artifacts (Codex rules/commands) are present in the repository but automatic installation into `~/.codex/` is not currently wired into `ouroboros setup`. Until that path is completed, use the `ouroboros` CLI commands listed above.
>
> **Note on `ooo seed` vs `ooo interview`:** These are two distinct skills with separate roles. `ooo interview` runs a Socratic Q&A session and returns a `session_id`. `ooo seed` accepts that `session_id` and generates a structured Seed YAML (with ambiguity scoring). From the terminal, both steps are performed in a single `ouroboros init start` invocation — there is no separate seed-generation subcommand.

## Quick Start

> For the full first-run onboarding flow (interview → seed → execute), see **[Getting Started](../getting-started.md)**.

### Verify Installation

```bash
codex --version
ouroboros --help
```

## How It Works

```
+-----------------+     +------------------+     +-----------------+
|   Seed YAML     | --> |   Orchestrator   | --> |   Codex CLI     |
|  (your task)    |     | (runtime_factory)|     |   (runtime)     |
+-----------------+     +------------------+     +-----------------+
                                |
                                v
                        +------------------+
                        |  Codex executes  |
                        |  with its own    |
                        |  tool set and    |
                        |  sandbox model   |
                        +------------------+
```

The `CodexCliRuntime` adapter launches `codex` (or `codex-cli`) as its transport layer, but wraps it with session handles, resume support, and deterministic skill/MCP dispatch so the runtime behaves like a persistent Ouroboros session.

> For a side-by-side comparison of all runtime backends, see the [runtime capability matrix](../runtime-capability-matrix.md).

## Codex CLI Strengths

- **Session-aware Codex runtime** -- Ouroboros preserves Codex session handles and resume state across workflow steps
- **Strong coding and reasoning** -- GPT-5.4 provides robust code generation and multi-file editing across languages
- **Agentic task execution** -- effective at decomposing complex tasks into sequential steps and iterating autonomously
- **Open-source** -- Codex CLI is open-source (Apache 2.0), allowing inspection and contribution
- **Ouroboros harness** -- the specification-first workflow engine adds structured acceptance criteria, evaluation principles, and deterministic exit conditions on top of Codex CLI's capabilities

## Runtime Differences

Codex CLI and Claude Code are independent runtime backends with different tool sets, permission models, and sandboxing behavior. The same Seed file works with both, but execution paths may differ.

| Aspect | Codex CLI | Claude Code |
|--------|-----------|-------------|
| What it is | Ouroboros session runtime backed by Codex CLI transport | Anthropic's agentic coding tool |
| Authentication | OpenAI API key | Max Plan subscription |
| Model | GPT-5.4 (recommended) | Claude (via claude-agent-sdk) |
| Sandbox | Codex CLI's own sandbox model | Claude Code's permission system |
| Tool surface | Codex-native tools (file I/O, shell) | Read, Write, Edit, Bash, Glob, Grep |
| Session model | Session-aware via runtime handles, resume IDs, and skill dispatch | Native Claude session context |
| Cost model | OpenAI API usage charges | Included in Max Plan subscription |
| Windows (native) | Not supported | Experimental |

> **Note:** The Ouroboros workflow model (Seed files, acceptance criteria, evaluation principles) is identical across runtimes. However, because Codex CLI and Claude Code have different underlying agent capabilities, tool access, and sandboxing, they may produce different execution paths and results for the same Seed file.

## CLI Options

### Workflow Commands

```bash
# Execute workflow (Codex runtime)
# Seeds generated by ouroboros init are saved to ~/.ouroboros/seeds/seed_{id}.yaml
uv run ouroboros run workflow --runtime codex ~/.ouroboros/seeds/seed_abcd1234ef56.yaml

# Dry run (validate seed without executing)
uv run ouroboros run workflow --dry-run ~/.ouroboros/seeds/seed_abcd1234ef56.yaml

# Debug output (show logs and agent output)
uv run ouroboros run workflow --runtime codex --debug ~/.ouroboros/seeds/seed_abcd1234ef56.yaml

# Resume a previous session
uv run ouroboros run workflow --runtime codex --resume <session_id> ~/.ouroboros/seeds/seed_abcd1234ef56.yaml
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

### Codex CLI not found

Ensure `codex` or `codex-cli` is installed and available on your `PATH`:

```bash
which codex || which codex-cli
```

If not installed, install via npm:

```bash
npm install -g @openai/codex
```

See the [Codex CLI README](https://github.com/openai/codex#readme) for alternative installation methods.

### API key errors

Verify your OpenAI API key is set and has access to GPT-5.4:

```bash
echo $OPENAI_API_KEY  # should be set
```

### "Providers: warning" in health check

This is normal when using the orchestrator runtime backends. The warning refers to LiteLLM providers, which are not used in orchestrator mode.

### "EventStore not initialized"

The database will be created automatically at `~/.ouroboros/ouroboros.db`.

## Cost

Using Codex CLI as the runtime backend requires an OpenAI API key and incurs standard OpenAI API usage charges. Costs depend on:

- Model used (GPT-5.4 recommended)
- Task complexity and token usage
- Number of tool calls and iterations

Refer to [OpenAI's pricing page](https://openai.com/pricing) for current rates.
