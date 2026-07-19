<!--
doc_metadata:
  runtime_scope: [codex]
-->

# Running Ouroboros with Codex CLI

> For installation and first-run onboarding, see [Getting Started](../getting-started.md).

Ouroboros can use **OpenAI Codex CLI** as a runtime backend. [Codex CLI](https://github.com/openai/codex) is the local Codex execution surface that the adapter talks to. In Ouroboros, that backend is presented as a **session-oriented runtime** with the same specification-first workflow harness (acceptance criteria, evaluation principles, deterministic exit conditions), even though the adapter itself communicates with the local `codex` executable.

No additional Python SDK is required beyond the base `ouroboros-ai` package.

> **Model recommendation:** Use **GPT-5.4** with **medium** reasoning effort for the documented Codex setup. GPT-5.4 provides strong coding, multi-step reasoning, and agentic task execution that pairs well with the Ouroboros specification-first workflow harness.

## Prerequisites

- **Codex CLI** installed and on your `PATH` (see [install steps](#installing-codex-cli) below)
- An **OpenAI API key** with access to GPT-5.4 (set `OPENAI_API_KEY`). See [`credentials.yaml`](../config-reference.md#credentialsyaml) for file-based key management
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

### Where Codex users configure what

Use `~/.ouroboros/config.yaml` for Ouroboros runtime settings. For everyday model selection, open `ouroboros config` or `ouroboros config --web`; both open the same settings UI.

Choose **Use Codex default model** to keep Codex's current default model. This is the recommended setting: Ouroboros passes only the role's reasoning effort to each Codex invocation, so a newer model selected in Codex App or CLI is used automatically. Choose a listed model or **Enter another model ID…** only when you deliberately want to pin a model for a stage, including Execute.

Use `~/.codex/config.toml` for the Codex MCP/env hookup and any user-managed native Codex profiles.

If you want Codex-backed Ouroboros roles to use explicit models instead of inheriting Codex CLI's active default/profile, set the existing `config.yaml` keys directly:

```yaml
# ~/.ouroboros/config.yaml
orchestrator:
  runtime_backend: codex
  codex_cli_path: /usr/local/bin/codex   # omit if codex is already on PATH

llm:
  backend: codex
  qa_model: gpt-5.4

clarification:
  default_model: gpt-5.4

evaluation:
  semantic_model: gpt-5.4

consensus:
  advocate_model: gpt-5.4
  devil_model: gpt-5.4
  judge_model: gpt-5.4
  # Optional: the simple-voting roster also lives here as `consensus.models`
```

When these keys are left at their shipped defaults, Codex setup adds provider-neutral `llm_profiles` plus `llm_role_profiles` mappings. Their Codex mappings set the per-invocation reasoning effort (fast: low, standard: medium, deep: high, frontier: xhigh) without selecting a Codex model or generated Codex profile. Explicit `config.yaml` model values still win.

## Command Surface

From the user's perspective, the Codex integration behaves like a **session-oriented Ouroboros runtime** — the same specification-first workflow harness that drives the Claude runtime.

Under the hood, `CodexCliRuntime` still talks to the local `codex` executable, but it preserves native session IDs and resume handles, and the Codex command dispatcher can route `ooo`-style skill commands through the in-process Ouroboros MCP server.

`ouroboros setup --runtime codex` currently:

- Detects the `codex` binary on your `PATH`
- Writes `orchestrator.runtime_backend: codex` and `llm.backend: codex` to `~/.ouroboros/config.yaml`
- Adds missing provider-neutral `llm_profiles` and `llm_role_profiles` defaults for Codex LLM calls and agent-runtime sessions, with per-invocation reasoning effort and no model pin
- Records `orchestrator.codex_cli_path` when available
- Installs managed Ouroboros rules into `~/.codex/rules/`
- Installs managed Ouroboros skills into `~/.codex/skills/`
- Registers the Ouroboros MCP/env hookup in `~/.codex/config.toml` when absent, refreshes setup-managed stdio blocks, and preserves user-managed URL/custom entries by default
- Retires only untouched legacy generated `ouroboros-*.config.toml` task-profile anchors; user-created Codex profiles are preserved
- Registers a managed `ouroboros-worker.config.toml` file so Agent OS worker subprocesses can opt out of interactive Codex defaults without losing the MCP/env hookup

`~/.codex/config.toml` is not where Ouroboros stage model pins belong. Use the settings UI or the equivalent `~/.ouroboros/config.yaml` values; keep user-managed native Codex profiles when you need an explicit `--profile`. If you manage a long-running URL-based Ouroboros MCP server, keep that URL entry in `~/.codex/config.toml`; `ouroboros setup --runtime codex` preserves it by default. Use `--mcp-mode stdio` only when you intentionally want setup to replace the entry with the managed command-spawned server.

### Worker subprocess isolation (Agent OS `runtime_profile`)

Interactive `codex` sessions and Ouroboros-managed worker subprocesses sometimes want different defaults — for example a different model, sandbox, or notify hook. Set the orchestrator-level runtime profile to `worker` to opt every Ouroboros-spawned `codex exec` invocation into the managed `~/.codex/ouroboros-worker.config.toml` profile:

```yaml
# ~/.ouroboros/config.yaml
orchestrator:
  runtime_backend: codex
  runtime_profile:
    backend_profile: worker   # optional; default unset preserves today's behavior
```

Or via the environment for one-off runs:

```bash
OUROBOROS_RUNTIME_PROFILE=worker ouroboros run workflow --runtime codex seed.yaml
```

Customize the worker overrides directly in `~/.codex/ouroboros-worker.config.toml`:

```toml
model = "o3-mini"
notify = []
sandbox = "workspace-write"
```

When `runtime_profile` is unset (the default), Ouroboros emits `codex exec` exactly as before — no profile flag, full user-config inheritance. This is the Codex-side mapping of the cross-runtime Agent OS profile contract; OpenCode, Hermes, Claude Code, and LiteLLM mappings can add their own backend-local mappings separately.

### `ooo` Skill Availability on Codex

After running `ouroboros setup --runtime codex`, the bundled `ooo` skills are installed into `~/.codex/skills/ouroboros-*` and the routing rules into `~/.codex/rules/`. To refresh only those artifacts after upgrading Ouroboros, run `ouroboros codex refresh`; it does not modify `~/.codex/config.toml` or `~/.ouroboros/config.yaml`. The table below shows each skill and its CLI equivalent for terminal-only workflows.

| `ooo` Skill | Codex session | CLI equivalent (Terminal) |
|-------------|---------------|--------------------------|
| `ooo interview` | Yes | `ouroboros init start --llm-backend codex "your idea"` |
| `ooo seed` | Yes | *(bundled in `ouroboros init start`)* |
| `ooo run` | Yes | `ouroboros run workflow --runtime codex seed.yaml` |
| `ooo status` | Yes | `ouroboros status execution <execution_id>` |
| `ooo evaluate` | Yes | *(MCP only)* |
| `ooo evolve` | Yes | *(MCP only)* |
| `ooo ralph` | Yes | MCP-owned `ouroboros_ralph` background job, monitored with job tools |
| `ooo cancel` | Yes | `ouroboros cancel execution <execution_id>` |
| `ooo unstuck` | Yes | *(MCP only)* |
| `ooo tutorial` | Yes | *(MCP only)* |
| `ooo welcome` | Yes | *(MCP only)* |
| `ooo update` | Yes | `pip install --upgrade ouroboros-ai` |
| `ooo help` | Yes | `ouroboros --help` |
| `ooo qa` | Yes | *(MCP only)* |
| `ooo setup` | Yes | `ouroboros setup --runtime codex` |
| `ooo publish` | Yes | *(no direct `ouroboros publish` subcommand; skill/runtime flow uses `gh` CLI)* |

> **Ralph note (#528):** `ooo ralph` now starts one MCP-owned `ouroboros_ralph` background job and monitors it with the standard job tools. The skill no longer reimplements the multi-generation loop with client-side `evolve_step` polling. To stop a running Ralph job, use the MCP job cancellation tool `ouroboros_cancel_job(job_id)`; `ouroboros cancel execution <execution_id>` is only for execution sessions and does not cancel Ralph job IDs.

> **Note on `ooo seed` vs `ooo interview`:** These are two distinct skills with separate roles. `ooo interview` runs a Socratic Q&A session and returns a `session_id`. `ooo seed` accepts that `session_id` and generates a structured Seed YAML (with ambiguity scoring). From the terminal, both steps are performed in a single `ouroboros init start` invocation.

> **Note on `ooo publish`:** In Codex sessions, `ooo publish` is provided as a skill/runtime surface after setup installs the managed rules and skills. It currently relies on the external `gh` CLI plus GitHub authentication, rather than a dedicated `ouroboros publish` shell subcommand.

Codex uses the shared stateless `ouroboros.router` resolver for exact `ooo`
and `/ouroboros:` skill dispatch. Adding or changing a command only requires
updating the relevant `SKILL.md` frontmatter; the runtime keeps logging,
message assembly, and MCP invocation local. See
[Shared `ooo` Skill Dispatch Router](../guides/ooo-skill-dispatch-router.md).

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
- **Strong coding and reasoning** -- uses the model currently selected by Codex, while Ouroboros applies the appropriate task reasoning effort
- **Agentic task execution** -- effective at decomposing complex tasks into sequential steps and iterating autonomously
- **Open-source** -- Codex CLI is open-source (Apache 2.0), allowing inspection and contribution
- **Ouroboros harness** -- the specification-first workflow engine adds structured acceptance criteria, evaluation principles, and deterministic exit conditions on top of Codex CLI's capabilities

## Runtime Differences

Codex CLI and Claude Code are independent runtime backends with different tool sets, permission models, and sandboxing behavior. The same Seed file works with both, but execution paths may differ.

| Aspect | Codex CLI | Claude Code |
|--------|-----------|-------------|
| What it is | Ouroboros session runtime backed by Codex CLI transport | Anthropic's agentic coding tool |
| Authentication | OpenAI API key | Max Plan subscription |
| Model | Codex's current default model (recommended) | Claude (via claude-agent-sdk) |
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

- Model selected by Codex (**Use Codex default model** is recommended)
- Task complexity and token usage
- Number of tool calls and iterations

Refer to [OpenAI's pricing page](https://openai.com/pricing) for current rates.

## Active Conductor and Synapse

Codex CLI is a proven Synapse `inform` and `after_turn` backend: Ouroboros
resumes the same persisted Codex thread after the current turn, and only reports
`applied` after the resumed provider turn emits an acknowledgement. It does not
advertise live checkpoint `redirect` or hard `replace`.

This becomes publicly callable only in the complete MCP host layer, which
registers the discovery/delivery tools and shares one Synapse hub with run and
Auto execution. Contract-only or runtime-only stack layers provide test and
manual-smoke coverage but do not by themselves expose the public control path.

During `ooo run`/`ooo auto`, the main host keeps one exclusive read-only observer
and reports runtime/model routing, efficiency/frugality policy, the bounded
Discover summary, total dependency/parallel levels, first scheduled ACs, route or
harness changes, attention, and terminal assurance. The user can keep talking in
the main session; it semantically selects the affected AC and never asks for
internal IDs. English is the canonical guidance language, while the host phrases
these facts naturally in the user's current conversation language.
