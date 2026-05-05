# Runtime Capability Matrix

> **New here?** Start with the [Getting Started guide](getting-started.md) for install and onboarding.
> This page is a **reference table** for comparing runtime backends.

Ouroboros is a **specification-first workflow engine**. The core workflow model -- Seed files, acceptance criteria, evaluation principles, and exit conditions -- is identical regardless of which runtime backend executes it. The runtime backend determines *how* and *where* agent work happens, not *what* gets specified.

> **Key insight:** Same core workflow, different UX surfaces.

## Configuration

The runtime backend is selected via the `orchestrator.runtime_backend` config key:

```yaml
orchestrator:
  runtime_backend: claude   # Supported values: claude | codex | opencode | hermes | kiro | copilot
                            # The runtime abstraction layer also accepts custom
                            # adapters registered in runtime_factory.py
```

Or on the command line with `--runtime`:

```bash
ouroboros run workflow --runtime codex seed.yaml
```

You can also override the configured backend with the `OUROBOROS_AGENT_RUNTIME` environment variable.

> **Extensibility:** Ouroboros uses a pluggable `AgentRuntime` protocol. Claude Code, Codex CLI, OpenCode, Hermes, Kiro CLI, and GitHub Copilot CLI are the natively shipped backends; additional runtimes can be registered by implementing the protocol and extending `runtime_factory.py`. See [Architecture — How to add a new runtime adapter](architecture.md#how-to-add-a-new-runtime-adapter).

## Capability Matrix

### Workflow Layer (identical across runtimes)

These capabilities are part of the Ouroboros core engine and work the same way regardless of runtime backend.

| Capability                         | Claude Code | Codex CLI | OpenCode | Hermes | Kiro CLI | Copilot CLI | Notes                                                       |
| ---------------------------------- | :---------: | :-------: | :------: | :----: | :------: | :---------: | ----------------------------------------------------------- |
| Seed file parsing                  |     Yes     |    Yes    |   Yes    |  Yes   |   Yes    |     Yes     | Same YAML schema, same validation                           |
| Acceptance criteria tree           |     Yes     |    Yes    |   Yes    |  Yes   |   Yes    |     Yes     | Structured AC decomposition                                 |
| Evaluation principles              |     Yes     |    Yes    |   Yes    |  Yes   |   Yes    |     Yes     | Weighted scoring against principles                         |
| Exit conditions                    |     Yes     |    Yes    |   Yes    |  Yes   |   Yes    |     Yes     | Deterministic termination logic                             |
| Event sourcing (SQLite)            |     Yes     |    Yes    |   Yes    |  Yes   |   Yes    |     Yes     | Full event log, replay support                              |
| Checkpoint / resume                |     Yes     |    Yes    |   Yes    |  Yes   | Partial  |     No      | `--resume <session_id>` everywhere; Kiro forwards to `--resume-id` **when the caller supplies the id** — headless does not surface session ids, so automatic checkpoint/resume is future work; Copilot CLI does not expose a resume API |
| TUI dashboard                      |     Yes     |    Yes    |   Yes    |  Yes   |   Yes    |     Yes     | Textual-based progress view                                 |
| Interview (Socratic seed creation) |     Yes     |    Yes    |   Yes    |  Yes   |   Yes    |     Yes     | `ouroboros init start ...` with the appropriate LLM backend |
| Dry-run validation                 |     Yes     |    Yes    |   Yes    |  Yes   |   Yes    |     Yes     | `--dry-run` validates without executing                     |
| Live model discovery               |     No      |    No     |    No    |   No   |    No    |     Yes     | Only Copilot queries its provider's models API at setup time and lets you pick a default from the live list |

### Runtime Layer (differs by backend)

These capabilities depend on the runtime backend's native features and execution model.

| Capability                |             Claude Code             |       Codex CLI       |                         OpenCode                          |                                   Hermes                                   |           Kiro CLI           |                                Copilot CLI                                | Notes                                                            |
| ------------------------- | :---------------------------------: | :-------------------: | :-------------------------------------------------------: | :------------------------------------------------------------------------: | :--------------------------: | :-----------------------------------------------------------------------: | ---------------------------------------------------------------- |
| **Authentication**        |        Max Plan subscription        |    OpenAI API key     |        Provider API keys (configured in OpenCode)         |        NousResearch (or compatible provider) API key or local model        |      Kiro AWS sign-in        | GitHub Copilot subscription via `gh auth login`                           | No API key needed for Claude Code, Kiro, or Copilot              |
| **Underlying model**      |         Claude (Anthropic)          |   GPT-5.4+ (OpenAI)   | Provider-dependent (OpenCode supports multiple providers) | Provider-dependent (Hermes supports multiple providers) or Any local model | Claude (via AWS) + others    | Live-discovered (Claude, GPT-5, etc.; whatever your subscription grants)  | Copilot is the only runtime with a live model picker             |
| **Tool surface**          | Read, Write, Edit, Bash, Glob, Grep | Codex-native tool set |            Read, Write, Edit, Bash, Glob, Grep            |                      Custom skills via MCP + run cmd                       |   Kiro-native tool set       | Read, Write, Edit, Bash, Glob, Grep (via `--available-tools` allowlist)   | Different tool implementations; same task outcomes               |
| **Sandbox / permissions** |    Claude Code permission system    |  Codex sandbox model  |                OpenCode permission system                 |                          Hermes permission system                          | `--trust-tools` / `--trust-all-tools` | `--add-dir <CWD>` boundary + `--allow-tool` envelope             | Each runtime manages its own safety boundaries                   |
| **Cost model**            |        Included in Max Plan         | Per-token API charges |              Depends on configured provider               |                            Depends on API/Local                            |    Included in Kiro plan     | Included in Copilot subscription                                          | See [OpenAI pricing](https://openai.com/pricing) for Codex costs |
| **Declared capabilities** | skill_dispatch, targeted_resume, structured_output | all three | all three | all three | skill_dispatch only; `targeted_resume=False` (headless does not surface session ids), `structured_output=False` (plain-text stdio) | skill_dispatch only; `targeted_resume=False` (no resume API); `structured_output=False` (no `--output-schema`, JSON via prompt directive) | See `RuntimeCapabilities` on the adapter |

### Integration Surface (UX differences)

| Aspect                      | Claude Code                                   | Codex CLI                                                                                                                                                                                                                                                    | OpenCode                                           | Hermes                                           | Kiro CLI                                                                                      | Copilot CLI                                                                                                                                                  |
| --------------------------- | --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------- | ------------------------------------------------ | --------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Primary UX**              | In-session skills and MCP server              | Session-oriented Ouroboros runtime over Codex CLI transport                                                                                                                                                                                                  | MCP server integration                             | In-session skills and MCP server                 | In-session skills and MCP server (Kiro headless mode)                                         | In-session skills and MCP server (Copilot CLI session)                                                                                                       |
| **Skill shortcuts (`ooo`)** | Yes -- skills loaded into Claude Code session | Yes -- after `ouroboros setup --runtime codex` installs managed skills into `~/.codex/skills/`, rules into `~/.codex/rules/`, and the MCP/env hookup into `~/.codex/config.toml`. Keep role-specific Ouroboros model overrides in `~/.ouroboros/config.yaml` | Yes -- after `ouroboros setup --runtime opencode`  | Yes -- after `ouroboros setup --runtime hermes`  | Yes -- after `ouroboros setup --runtime kiro` the Ouroboros MCP server is registered in `~/.kiro/settings/mcp.json` with `OUROBOROS_RUNTIME=kiro` / `OUROBOROS_LLM_BACKEND=kiro` baked in | Yes -- after `ouroboros setup --runtime copilot` the MCP server is registered in `~/.copilot/mcp-config.json` with `OUROBOROS_AGENT_RUNTIME=copilot` / `OUROBOROS_LLM_BACKEND=copilot` baked in |
| **MCP integration**         | Native MCP server support                     | Deterministic skill/MCP dispatch through the Ouroboros Codex adapter                                                                                                                                                                                         | Native MCP server support                          | Native MCP server support                        | Native MCP server support; `SkillInterceptor` routes `ooo <skill>` prefixes before spawning Kiro subprocess | Native MCP server support; restart required after first registration so Copilot binds the new child         |
| **Session context**         | Shares Claude Code session context            | Preserved via runtime handles, native session IDs, and resume support                                                                                                                                                                                        | Session IDs + resume via `--session`               | Preserved via session IDs and internal parser    | Headless runs do not surface session IDs; callers may pass an externally sourced `--resume-id`, but automatic targeted resume is not declared yet | Copilot CLI does not expose a session resume API; checkpointing happens at the Ouroboros lineage layer                                                       |
| **Install extras**          | `ouroboros-ai[claude]`                        | `ouroboros-ai` (base package) + `codex` on PATH                                                                                                                                                                                                              | `ouroboros-ai` (base package) + `opencode` on PATH | `ouroboros-ai` (base package) + `hermes` on PATH | `ouroboros-ai[claude]` + `kiro-cli` on PATH                                                   | `ouroboros-ai[mcp]` + `copilot` on PATH + `gh` on PATH (for live model discovery)                                                                            |

## What Stays the Same

Regardless of runtime backend, every Ouroboros workflow:

1. **Starts from the same Seed file** -- YAML specification with goal, constraints, acceptance criteria, ontology, and evaluation principles.
2. **Follows the same orchestration pipeline** -- the 6-phase pipeline (Big Bang → PAL Router → Double Diamond → Resilience → Evaluation → Secondary Loop) is runtime-agnostic. See [Architecture](architecture.md#the-six-phases) for the canonical phase definitions.
3. **Produces the same event stream** -- all events are stored in the shared SQLite event store with identical schemas.
4. **Evaluates against the same criteria** -- acceptance criteria and evaluation principles are applied uniformly.
5. **Reports through the same interfaces** -- CLI output, TUI dashboard, and event logs work identically.

## What Differs

The runtime backend affects:

- **Agent capabilities**: Each runtime has its own model, tool set, and reasoning characteristics. The same Seed file may produce different execution paths.
- **Performance profile**: Token costs, latency, and throughput vary by provider and model.
- **Permission model**: Sandbox behavior and file-system access rules are runtime-specific.
- **Error surfaces**: Error messages and failure modes reflect the underlying runtime.

> **No implied parity:** Each supported runtime is an independent product with its own strengths, limitations, and behavior. Ouroboros provides a unified workflow harness, but does not guarantee identical behavior or output quality across runtimes. This applies equally to any future or custom adapter implementations.

## Choosing a Runtime

The table below covers the three currently shipped backends. Because Ouroboros uses a pluggable `AgentRuntime` protocol, teams can register additional backends without modifying the core engine.

| If you...                                                              | Consider                                                                      |
| ---------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| Have a Claude Code Max Plan and want zero API key setup                | Claude Code (`runtime_backend: claude`)                                       |
| Want a Codex-backed Ouroboros session instead of a Claude Code session | Codex CLI (`runtime_backend: codex`)                                          |
| Want to use OpenCode with multiple model providers                     | OpenCode (`runtime_backend: opencode`)                                        |
| Want to use the Hermes Agent (open source local & API agent)           | Hermes (`runtime_backend: hermes`)                                            |
| Want to use Kiro CLI (AWS-provided coding agent, browser-free headless) | Kiro (`runtime_backend: kiro`)                                                |
| Have a GitHub Copilot subscription and want the live model picker       | Copilot CLI (`runtime_backend: copilot`)                                      |
| Want to use Anthropic's Claude models                                  | Claude Code or Copilot CLI (Claude models exposed via Copilot subscription)   |
| Want to use OpenAI's GPT models                                        | Codex CLI or Copilot CLI                                                      |
| Want to use multiple providers via a single runtime                    | OpenCode or Copilot CLI (Copilot multiplexes Anthropic + OpenAI under one auth) |
| Need MCP server integration                                            | Claude Code, OpenCode, Hermes, Kiro, or Copilot CLI                           |
| Want minimal Python dependencies                                       | Codex CLI, OpenCode, or Hermes                                                |
| Want to integrate a custom or third-party AI coding agent              | Implement the `AgentRuntime` protocol and register it in `runtime_factory.py` |

## Further Reading

- [Claude Code runtime guide](runtime-guides/claude-code.md)
- [Codex CLI runtime guide](runtime-guides/codex.md)
- [Hermes Agent runtime guide](runtime-guides/hermes.md)
- [OpenCode runtime guide](runtime-guides/opencode.md)
- [Kiro CLI runtime guide](runtime-guides/kiro.md)
- [GitHub Copilot CLI runtime guide](runtime-guides/copilot.md)
- [Platform support matrix](platform-support.md) (OS and Python version compatibility)
- [Architecture overview](architecture.md) — including [How to add a new runtime adapter](architecture.md#how-to-add-a-new-runtime-adapter)
