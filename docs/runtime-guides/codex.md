# Running Ouroboros with Codex CLI

Ouroboros can use **OpenAI Codex CLI** as a runtime backend. [Codex CLI](https://github.com/openai/codex) is OpenAI's open-source terminal-based coding agent -- it reads your codebase, proposes changes, and executes commands directly in your terminal. Ouroboros drives Codex CLI as a subprocess, wrapping it with the specification-first workflow harness (acceptance criteria, evaluation principles, deterministic exit conditions).

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

```bash
pip install ouroboros-ai
# or
uv pip install ouroboros-ai
```

The base package includes the Codex CLI runtime adapter. No extras are required.

### From Source (Development)

```bash
git clone https://github.com/Q00/ouroboros
cd ouroboros
uv sync
```

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
uv run ouroboros run workflow --runtime codex seed.yaml
```

## Skill Shortcuts (`ooo` commands)

Codex CLI supports `ooo` skill commands just like Claude Code. When you run `ouroboros setup` with the Codex runtime, Ouroboros installs rules and skill files into `~/.codex/`:

- **Rules** (`~/.codex/rules/ouroboros.md`) -- teaches Codex to route `ooo` commands to the corresponding MCP tools
- **Skills** (`~/.codex/skills/ouroboros-*`) -- provides each skill's instructions (interview, seed, run, evaluate, etc.)

After setup, you can use `ooo` commands inside a Codex session:

```
ooo interview "Build a REST API for task management"
ooo seed
ooo run seed.yaml
ooo evaluate
```

These map to the same MCP tools as the Claude Code `ooo` commands. Codex reads the installed rules and routes each command to the appropriate Ouroboros MCP tool automatically.

## Quick Start

### Check System Health

```bash
uv run ouroboros status health
```

Expected output:

```
+---------------+---------+
| Database      |   ok    |
| Configuration |   ok    |
| Providers     | warning |  # OK when using Codex as the runtime backend
+---------------+---------+
```

### Option A: Create Seed via Interview (Recommended)

Don't know how to write a Seed file? Use the interactive interview:

```bash
uv run ouroboros init start --orchestrator "Build a REST API for task management"
```

This will:

1. Ask clarifying questions (Socratic method)
2. Reduce ambiguity through dialogue
3. Generate a Seed file automatically

### Option B: Write Seed Manually

Create a YAML file describing your task. Example `my-task.yaml`:

```yaml
goal: "Implement a user authentication module"
constraints:
  - "Python >= 3.12"
  - "Use bcrypt for password hashing"
  - "Follow existing project patterns"
acceptance_criteria:
  - "Create auth/models.py with User model"
  - "Create auth/service.py with login/register functions"
  - "Add unit tests with pytest"
ontology_schema:
  name: "AuthModule"
  description: "User authentication system"
  fields:
    - name: "users"
      field_type: "object"
      description: "User data structure"
      required: true
evaluation_principles:
  - name: "security"
    description: "Code follows security best practices"
    weight: 1.0
  - name: "testability"
    description: "Code is well-tested"
    weight: 0.8
exit_conditions:
  - name: "all_tests_pass"
    description: "All acceptance criteria met and tests pass"
    evaluation_criteria: "pytest returns 0"
metadata:
  ambiguity_score: 0.15
```

### Run with Orchestrator Mode

```bash
uv run ouroboros run workflow --runtime codex my-task.yaml
```

This will:

1. Parse your seed file
2. Launch Codex CLI as a subprocess
3. Execute the task autonomously using GPT-5.4
4. Report progress and results

## How It Works

```
+-----------------+     +------------------+     +-----------------+
|   Seed YAML     | --> |   Orchestrator   | --> |   Codex CLI     |
|  (your task)    |     | (runtime_factory)|     |   (subprocess)  |
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

The `CodexCliRuntime` adapter launches `codex` (or `codex-cli`) as a subprocess, streams output, and maps results back into the Ouroboros event model.

> For a side-by-side comparison of all runtime backends, see the [runtime capability matrix](../runtime-capability-matrix.md).

## Codex CLI Strengths

- **Terminal-native agent** -- Codex CLI runs directly in your terminal, reading and editing files, executing shell commands, and iterating on code autonomously
- **Strong coding and reasoning** -- GPT-5.4 provides robust code generation and multi-file editing across languages
- **Agentic task execution** -- effective at decomposing complex tasks into sequential steps and iterating autonomously
- **Open-source** -- Codex CLI is open-source (Apache 2.0), allowing inspection and contribution
- **Ouroboros harness** -- the specification-first workflow engine adds structured acceptance criteria, evaluation principles, and deterministic exit conditions on top of Codex CLI's capabilities

## Runtime Differences

Codex CLI and Claude Code are independent runtime backends with different tool sets, permission models, and sandboxing behavior. The same Seed file works with both, but execution paths may differ.

| Aspect | Codex CLI | Claude Code |
|--------|-----------|-------------|
| What it is | Open-source terminal coding agent | Anthropic's agentic coding tool |
| Authentication | OpenAI API key | Max Plan subscription |
| Model | GPT-5.4 (recommended) | Claude (via claude-agent-sdk) |
| Sandbox | Codex CLI's own sandbox model | Claude Code's permission system |
| Tool surface | Codex-native tools (file I/O, shell) | Read, Write, Edit, Bash, Glob, Grep |
| Cost model | OpenAI API usage charges | Included in Max Plan subscription |
| Windows (native) | Not supported | Experimental |

> **Note:** The Ouroboros workflow model (Seed files, acceptance criteria, evaluation principles) is identical across runtimes. However, because Codex CLI and Claude Code have different underlying agent capabilities, tool access, and sandboxing, they may produce different execution paths and results for the same Seed file.

## CLI Options

### Workflow Commands

```bash
# Execute workflow (Codex runtime)
uv run ouroboros run workflow --runtime codex seed.yaml

# Dry run (validate seed without executing)
uv run ouroboros run workflow --dry-run seed.yaml

# Debug output (show logs and agent output)
uv run ouroboros run workflow --runtime codex --debug seed.yaml

# Resume a previous session
uv run ouroboros run workflow --runtime codex --resume <session_id> seed.yaml
```

## Seed File Reference

| Field | Required | Description |
|-------|----------|-------------|
| `goal` | Yes | Primary objective |
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
