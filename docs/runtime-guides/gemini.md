<!--
doc_metadata:
  runtime_scope: [gemini]
-->

# Running Ouroboros with Gemini CLI

> For installation and first-run onboarding, see [Getting Started](../getting-started.md).

Ouroboros can use **Google Gemini CLI** as a runtime backend. [Gemini CLI](https://github.com/google-gemini/gemini-cli) is the local Gemini execution surface that the adapter talks to. In Ouroboros, that backend is presented as a **session-oriented runtime** with the same specification-first workflow harness (acceptance criteria, evaluation principles, deterministic exit conditions), even though the adapter itself communicates with the local `gemini` executable.

No additional Python SDK is required beyond the base `ouroboros-ai` package.

> **Model recommendation:** Use **Gemini 2.5 Pro** (the Gemini CLI default) for the documented setup. Gemini 2.5 Pro provides strong coding, multi-step reasoning, and agentic task execution that pairs well with the Ouroboros specification-first workflow harness.

## Prerequisites

- **Gemini CLI** installed and on your `PATH` (see [install steps](#installing-gemini-cli) below)
- Authenticated with Gemini CLI: a **Gemini API key** (`GEMINI_API_KEY`) or a Google account (OAuth via `gemini` browser flow)
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

For alternative install methods and shell completions, see the [Gemini CLI README](https://github.com/google-gemini/gemini-cli#readme).

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

### Where Gemini users configure what

Use `~/.ouroboros/config.yaml` for Ouroboros runtime settings and per-role model overrides.

```yaml
# ~/.ouroboros/config.yaml
orchestrator:
  runtime_backend: gemini
  gemini_cli_path: /usr/local/bin/gemini   # omit if gemini is already on PATH

llm:
  backend: gemini

clarification:
  default_model: gemini-2.5-pro

evaluation:
  semantic_model: gemini-2.5-pro

consensus:
  advocate_model: gemini-2.5-pro
  devil_model: gemini-2.5-pro
  judge_model: gemini-2.5-pro
```

When these keys are left at their shipped defaults, the Gemini-aware loader resolves them to Gemini CLI's active default model. Explicit `config.yaml` values always win.

### Authentication

Gemini CLI supports two authentication methods:

**Option A — OAuth / Google account (recommended, free tier):**

Run `gemini` once and follow the browser prompt to sign in with your Google account. No environment variable needed. Free tier: 1,000 requests/day.

**Option B — Gemini API Key (automated workflows):**

```bash
export GEMINI_API_KEY="your-api-key"   # from aistudio.google.com/apikey
```

> See the [Gemini CLI authentication guide](https://github.com/google-gemini/gemini-cli?tab=readme-ov-file#-authentication-options) for the full list of options including Vertex AI enterprise setup.

## Command Surface

From the user's perspective, the Gemini integration behaves like a **session-oriented Ouroboros runtime** — the same specification-first workflow harness that drives the Claude and Codex runtimes.

Under the hood, `GeminiCLIAdapter` talks to the local `gemini` executable by writing the prompt to stdin and reading the response from stdout, making each completion a single subprocess invocation.

`ouroboros setup --runtime gemini` currently:

- Detects the `gemini` binary on your `PATH`
- Writes `orchestrator.runtime_backend: gemini` and `llm.backend: gemini` to `~/.ouroboros/config.yaml`
- Records `orchestrator.gemini_cli_path` when available

### `ooo` Skill Availability on Gemini CLI

After running `ouroboros setup --runtime gemini`, all core `ooo` skills are available via Ouroboros and their CLI equivalents listed below.

| `ooo` Skill | CLI equivalent (Terminal) |
|-------------|--------------------------|
| `ooo interview` | `ouroboros init start --llm-backend gemini "your idea"` |
| `ooo seed` | *(bundled in `ouroboros init start`)* |
| `ooo run` | `ouroboros run workflow --runtime gemini seed.yaml` |
| `ooo status` | `ouroboros status execution <session_id>` |
| `ooo evaluate` | *(MCP only)* |
| `ooo evolve` | *(MCP only)* |
| `ooo ralph` | *(MCP only)* |
| `ooo cancel` | `ouroboros cancel execution <session_id>` |
| `ooo unstuck` | *(MCP only)* |
| `ooo update` | `pip install --upgrade ouroboros-ai` |
| `ooo help` | `ouroboros --help` |
| `ooo qa` | *(MCP only)* |
| `ooo setup` | `ouroboros setup --runtime gemini` |

> **Note on `ooo seed` vs `ooo interview`:** These are two distinct skills. `ooo interview` runs a Socratic Q&A session and returns a `session_id`. `ooo seed` accepts that `session_id` and generates a structured Seed YAML (with ambiguity scoring). From the terminal, both steps are performed in a single `ouroboros init start` invocation.

## Quick Start

> For the full first-run onboarding flow (interview → seed → execute), see **[Getting Started](../getting-started.md)**.

### Verify Installation

```bash
gemini --version
ouroboros --help
```

### Run Your First Workflow

```bash
# 1. Interview to generate a seed spec
ouroboros init start --llm-backend gemini "Build a task management CLI"

# 2. Execute the generated seed (replace <seed_id> with your actual seed ID)
ouroboros run workflow --runtime gemini ~/.ouroboros/seeds/seed_<seed_id>.yaml
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
                        |  model context   |
                        +------------------+
```

The `GeminiCLIAdapter` invokes `gemini` as a subprocess, writing the prompt via stdin and reading the response from stdout. The `GeminiEventNormalizer` converts raw output (plain text or NDJSON) into the internal Ouroboros event schema, so the same workflow engine drives execution regardless of the output format the CLI emits.

> For a side-by-side comparison of all runtime backends, see the [runtime capability matrix](../runtime-capability-matrix.md).

## Gemini CLI Strengths

- **Flexible authentication** -- supports both a Google API key and OAuth (gcloud), making it easy to use in both personal and CI environments
- **Generous free tier** -- Gemini API offers a free tier suitable for personal projects and experimentation
- **Strong multimodal reasoning** -- Gemini 2.5 Pro provides excellent code generation and multi-step reasoning
- **Ouroboros harness** -- the specification-first workflow engine adds structured acceptance criteria, evaluation principles, and deterministic exit conditions on top of Gemini CLI's capabilities

## Runtime Differences

Gemini CLI, Claude Code, and Codex CLI are independent runtime backends with different tool sets, permission models, and authentication flows. The same Seed file works with all of them, but execution paths may differ.

| Aspect | Gemini CLI | Codex CLI | Claude Code |
|--------|------------|-----------|-------------|
| What it is | Ouroboros session runtime backed by Gemini CLI | Ouroboros session runtime backed by Codex CLI | Anthropic's agentic coding tool |
| Authentication | `GEMINI_API_KEY` or Google OAuth (browser) | OpenAI API key | Max Plan subscription |
| Model | Gemini 2.5 Pro (default) | GPT-5.4 with medium reasoning effort (recommended) | Claude (via claude-agent-sdk) |
| Sandbox | Gemini CLI's internal execution model | Codex CLI's own sandbox model | Claude Code's permission system |
| Tool surface | Gemini-native tools | Codex-native tools (file I/O, shell) | Read, Write, Edit, Bash, Glob, Grep |
| Cost model | Google API usage charges (free tier available) | OpenAI API usage charges | Included in Max Plan subscription |
| Windows (native) | Not supported | Not supported | Experimental |

> **Note:** The Ouroboros workflow model (Seed files, acceptance criteria, evaluation principles) is identical across runtimes. However, because Gemini CLI, Codex CLI, and Claude Code have different underlying agent capabilities, tool access, and sandboxing, they may produce different execution paths and results for the same Seed file.

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

### Environment Variables

```bash
# Override the LLM backend (highest priority)
export OUROBOROS_LLM_BACKEND=gemini

# Specify an explicit path to the gemini binary
export OUROBOROS_GEMINI_CLI_PATH=/usr/local/bin/gemini

# Provide a Gemini API key (from aistudio.google.com/apikey)
export GEMINI_API_KEY=your-api-key
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

### Example Seed File

```yaml
goal: "Build a command-line tool that summarises Git commit history"

constraints:
  - "Python >= 3.12"
  - "Use subprocess to invoke git"
  - "Output plain text — no external formatting libraries"

acceptance_criteria:
  - "Accepts a --since flag for date range filtering"
  - "Groups commits by author"
  - "Prints a configurable maximum number of commits per author"
  - "All tests pass with pytest"

ontology_schema:
  name: "GitSummary"
  description: "Git commit history summariser"
  fields:
    - name: "summary"
      field_type: "string"
      description: "The formatted commit summary"

evaluation_principles:
  - "Output is human-readable and concise"
  - "Edge cases (empty repo, no commits in range) are handled gracefully"

metadata:
  ambiguity_score: 0.12
```

## Programmatic Usage

You can also use `GeminiCLIAdapter` directly in Python code:

```python
import asyncio
from ouroboros.providers.factory import create_llm_adapter
from ouroboros.providers.base import CompletionConfig, Message, MessageRole


async def main() -> None:
    # Create a Gemini CLI adapter via the factory
    adapter = create_llm_adapter(
        backend="gemini",
        timeout=60.0,
        max_retries=2,
    )

    messages = [
        Message(role=MessageRole.USER, content="Explain async/await in Python in two sentences."),
    ]
    config = CompletionConfig(model="gemini-2.5-pro")

    result = await adapter.complete(messages, config)

    if result.is_ok:
        print(result.value.content)
    else:
        print(f"Error: {result.error.message}")


asyncio.run(main())
```

To instantiate the adapter directly (bypassing the factory):

```python
from ouroboros.providers.gemini_cli_adapter import GeminiCLIAdapter
from ouroboros.providers.base import CompletionConfig, Message, MessageRole

adapter = GeminiCLIAdapter(
    cli_path="/usr/local/bin/gemini",  # optional; falls back to PATH
    timeout=30.0,
    max_retries=3,
    on_message=lambda msg_type, content: print(f"[{msg_type}] {content}"),
)
```

### Using the Event Normalizer

If you are processing raw Gemini CLI output outside of the adapter, use `GeminiEventNormalizer` directly:

```python
from ouroboros.providers.gemini_event_normalizer import GeminiEventNormalizer

normalizer = GeminiEventNormalizer()

# Process plain-text output
for line in gemini_output.splitlines():
    event = normalizer.normalize_line(line)
    if not event["is_error"]:
        print(event["content"])

# Or process an entire output block at once
events = normalizer.normalize_lines(gemini_output)
text_events = [e for e in events if e["type"] == "text"]
```

## Troubleshooting

### Gemini CLI not found

Ensure `gemini` is installed and available on your `PATH`:

```bash
which gemini
gemini --version
```

If not installed, install via npm:

```bash
npm install -g @google/gemini-cli
```

See the [Gemini CLI README](https://github.com/google-gemini/gemini-cli#readme) for alternative installation methods.

### Authentication errors

If using an API key, verify it is set:

```bash
echo $GEMINI_API_KEY   # should be non-empty
```

If using OAuth (Google account), re-run the browser sign-in flow:

```bash
gemini   # follow the "Sign in with Google" prompt
```

### Rate limit / quota errors

The free tier of the Gemini API has rate limits. If you see `"rate limit"` or `"resource exhausted"` in the error output, the adapter will automatically retry with exponential backoff (up to `max_retries` attempts). To avoid hitting limits on long workflows, reduce parallelism or use an API key with a paid quota tier.

### "Providers: warning" in health check

This is normal when using the orchestrator runtime backends. The warning refers to LiteLLM providers, which are not used in orchestrator mode.

### "EventStore not initialized"

The database will be created automatically at `~/.ouroboros/ouroboros.db`.

### Explicit CLI path

If `gemini` is not on your `PATH`, set the path explicitly:

```bash
export OUROBOROS_GEMINI_CLI_PATH=/opt/homebrew/bin/gemini
```

Or set it in your config:

```yaml
# ~/.ouroboros/config.yaml
orchestrator:
  gemini_cli_path: /opt/homebrew/bin/gemini
```

## Cost

Using Gemini CLI as the runtime backend may incur Google API usage charges depending on your authentication method and quota tier:

- **OAuth / Google account** — free tier: 1,000 requests/day; suitable for experimentation and personal projects
- **Gemini API key (`GEMINI_API_KEY`)** — standard per-token charges above the free quota; see [Google AI pricing](https://ai.google.dev/pricing) for current rates

Refer to Google's documentation for quota limits and pricing details.
