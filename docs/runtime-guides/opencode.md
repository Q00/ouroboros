<!--
doc_metadata:
  runtime_scope: [opencode]
-->

# Running Ouroboros with OpenCode

> For installation and first-run onboarding, see [Getting Started](../getting-started.md).

Ouroboros can use **OpenCode** as a runtime backend. [OpenCode](https://opencode.ai) is an open-source AI coding agent that supports multiple model providers (Anthropic, OpenAI, Google, and others). In Ouroboros, the OpenCode backend is presented as a **session-oriented runtime** with the same specification-first workflow harness (acceptance criteria, evaluation principles, deterministic exit conditions).

No additional Python SDK is required beyond the base `ouroboros-ai` package.

## Prerequisites

- **OpenCode** installed and on your `PATH` (see [install steps](#installing-opencode) below)
- An **API key** for your configured provider (e.g., `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`)
- **Python >= 3.12**

## Installing OpenCode

OpenCode is distributed as a standalone binary. Install via the official script:

```bash
curl -fsSL https://opencode.ai/install | bash
```

Verify the installation:

```bash
opencode --version
```

For alternative install methods, see the [OpenCode documentation](https://opencode.ai/docs).

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

### Setup

Run the setup command to auto-configure:

```bash
ouroboros setup --runtime opencode
```

This:

- Detects the `opencode` binary on your `PATH`
- Writes `orchestrator.runtime_backend: opencode` to `~/.ouroboros/config.yaml`
- Registers the Ouroboros MCP server in OpenCode's configuration

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
- **Rich tool access** -- full suite of file, shell, and search tools (same surface as Claude Code)
- **Native MCP integration** -- OpenCode has built-in MCP server support
- **Open-source** -- fully open-source, allowing inspection and contribution

## CLI Options

### Workflow Commands

```bash
# Execute workflow (OpenCode runtime)
uv run ouroboros run workflow --runtime opencode ~/.ouroboros/seeds/seed_abcd1234ef56.yaml

# Debug output (show logs and agent output)
uv run ouroboros run workflow --runtime opencode --debug ~/.ouroboros/seeds/seed_abcd1234ef56.yaml

# Resume a previous session
uv run ouroboros run workflow --runtime opencode --resume <session_id> ~/.ouroboros/seeds/seed_abcd1234ef56.yaml
```

## Known Limitations

### Session pollution

Each task execution via `opencode run` creates a visible session in OpenCode's session history. Long-running workflows with many orchestrator steps will accumulate sessions. A future phase will reparent these sessions under the caller to prevent polluting the session picker (see GitHub #164 Phase 2).

### No interactive mode

The adapter uses `opencode run --format json` (non-interactive). Features that require interactive OpenCode sessions (e.g., manual approval prompts) are not available during Ouroboros execution.

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

### API key errors

Verify your provider API key is set:

```bash
# For Anthropic
echo $ANTHROPIC_API_KEY

# For OpenAI
echo $OPENAI_API_KEY
```

### "Providers: warning" in health check

This is normal when using the orchestrator runtime backends. The warning refers to LiteLLM providers, which are not used in orchestrator mode.

### "EventStore not initialized"

The database will be created automatically at `~/.ouroboros/ouroboros.db`.

## Cost

Using OpenCode as the runtime backend incurs API charges from your configured provider. Costs depend on:

- Provider and model selected in OpenCode configuration
- Task complexity and token usage
- Number of tool calls and iterations

Refer to your provider's pricing page for current rates.
