# Runtime Capability Matrix

Ouroboros is a **specification-first workflow engine**. The core workflow model -- Seed files, acceptance criteria, evaluation principles, and exit conditions -- is identical regardless of which runtime backend executes it. The runtime backend determines *how* and *where* agent work happens, not *what* gets specified.

> **Key insight:** Same core workflow, different UX surfaces.

## Configuration

The runtime backend is selected via the `orchestrator.runtime_backend` config key:

```yaml
orchestrator:
  runtime_backend: claude   # or: codex
```

Or on the command line with `--runtime`:

```bash
ouroboros run workflow --runtime codex seed.yaml
```

## Capability Matrix

### Workflow Layer (identical across runtimes)

These capabilities are part of the Ouroboros core engine and work the same way regardless of runtime backend.

| Capability | Claude Code | Codex CLI | Notes |
|------------|:-----------:|:---------:|-------|
| Seed file parsing | Yes | Yes | Same YAML schema, same validation |
| Acceptance criteria tree | Yes | Yes | Structured AC decomposition |
| Evaluation principles | Yes | Yes | Weighted scoring against principles |
| Exit conditions | Yes | Yes | Deterministic termination logic |
| Event sourcing (SQLite) | Yes | Yes | Full event log, replay support |
| Checkpoint / resume | Yes | Yes | `--resume <session_id>` |
| TUI dashboard | Yes | Yes | Textual-based progress view |
| Interview (Socratic seed creation) | Yes | Yes | `ouroboros init start --orchestrator` |
| Dry-run validation | Yes | Yes | `--dry-run` validates without executing |

### Runtime Layer (differs by backend)

These capabilities depend on the runtime backend's native features and execution model.

| Capability | Claude Code | Codex CLI | Notes |
|------------|:-----------:|:---------:|-------|
| **Execution model** | In-process SDK | Subprocess | Claude Code uses `claude-agent-sdk`; Codex runs as a child process |
| **Authentication** | Max Plan subscription | OpenAI API key | No API key needed for Claude Code |
| **Underlying model** | Claude (Anthropic) | GPT-5.4+ (OpenAI) | Model choice follows the runtime |
| **Tool surface** | Read, Write, Edit, Bash, Glob, Grep | Codex-native tool set | Different tool implementations; same task outcomes |
| **Sandbox / permissions** | Claude Code permission system | Codex sandbox model | Each runtime manages its own safety boundaries |
| **Cost model** | Included in Max Plan | Per-token API charges | See [OpenAI pricing](https://openai.com/pricing) for Codex costs |

### Integration Surface (UX differences)

| Aspect | Claude Code | Codex CLI |
|--------|-------------|-----------|
| **Primary UX** | In-session skills and MCP server | Terminal-native CLI with `ooo` skill support |
| **Skill shortcuts (`ooo`)** | Yes -- skills loaded into Claude Code session | Yes -- rules and skills installed to `~/.codex/` |
| **MCP integration** | Native MCP server support | MCP tools routed via Codex rules |
| **Session context** | Shares Claude Code session context | Isolated subprocess per invocation |
| **Install extras** | `ouroboros-ai[claude]` | `ouroboros-ai` (base package) + `codex` on PATH |

## What Stays the Same

Regardless of runtime backend, every Ouroboros workflow:

1. **Starts from the same Seed file** -- YAML specification with goal, constraints, acceptance criteria, ontology, and evaluation principles.
2. **Follows the same orchestration pipeline** -- the 6-phase pipeline (parse, plan, execute, evaluate, iterate, report) is runtime-agnostic.
3. **Produces the same event stream** -- all events are stored in the shared SQLite event store with identical schemas.
4. **Evaluates against the same criteria** -- acceptance criteria and evaluation principles are applied uniformly.
5. **Reports through the same interfaces** -- CLI output, TUI dashboard, and event logs work identically.

## What Differs

The runtime backend affects:

- **Agent capabilities**: Each runtime has its own model, tool set, and reasoning characteristics. The same Seed file may produce different execution paths.
- **Performance profile**: Token costs, latency, and throughput vary by provider and model.
- **Permission model**: Sandbox behavior and file-system access rules are runtime-specific.
- **Error surfaces**: Error messages and failure modes reflect the underlying runtime.

> **No implied parity:** Claude Code and Codex CLI are independent products with different strengths. Ouroboros provides a unified workflow harness, but does not guarantee identical behavior or output quality across runtimes.

## Choosing a Runtime

| If you... | Consider |
|-----------|----------|
| Have a Claude Code Max Plan and want zero API key setup | Claude Code (`runtime_backend: claude`) |
| Prefer terminal-native workflows without an IDE session | Codex CLI (`runtime_backend: codex`) |
| Want to use Anthropic's Claude models | Claude Code |
| Want to use OpenAI's GPT models | Codex CLI |
| Need MCP server integration | Claude Code |
| Want minimal Python dependencies | Codex CLI (base package only) |

## Further Reading

- [Claude Code runtime guide](runtime-guides/claude-code.md)
- [Codex CLI runtime guide](runtime-guides/codex.md)
- [Platform support matrix](platform-support.md) (OS and Python version compatibility)
- [Architecture overview](architecture.md)
