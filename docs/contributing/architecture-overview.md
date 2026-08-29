# Architecture Overview for Contributors

This page is the code-ownership map for the current repository. It answers two
questions: where a behavior is owned, and which boundary must stay authoritative
when that behavior changes.

This page does not redefine product behavior. Use the code-backed CLI, config,
runtime and evaluation references plus the owning issue or RFC for the boundary
you are changing. Historical roadmaps under `docs/history/` are evidence, not
current work orders.

## End-to-End Flow

```text
User request or existing Seed
  -> CLI / MCP / packaged skill / runtime bridge
  -> optional interview and Seed authoring
  -> an entry point resolves run inputs and constructs OrchestratorRunner
  -> direct or parallel acceptance-criterion execution through an AgentRuntime
  -> orchestrator verification plus EventStore acceptance fencing
  -> durable events, checkpoints, and artifact records
  -> recovery reads durable state; projections render status and dashboards
```

The execution path is not a numbered package pipeline. Model and harness
selection, decomposition, retries, verification, and recovery are policies
inside the live orchestrator. The retired model-routing package is not the
current `src/ouroboros/router/` command dispatcher, and the compatibility-only
`src/ouroboros/execution/` package does not own execution.

## Authority Boundaries

| Boundary | Primary owner and fence | Important consumers |
|---|---|---|
| User intent and acceptance contract | `src/ouroboros/core/seed.py`; authoring paths may propose candidates but only the resulting Seed carries the run contract | `auto/`, `bigbang/`, `pm/`, `orchestrator/` |
| Run construction and lifecycle | `src/ouroboros/orchestrator/runner.py`; durable lifecycle and terminal writes are fenced by `src/ouroboros/persistence/event_store.py` | CLI run, MCP execution, evolution |
| Runtime capability and construction | `AgentRuntime.capabilities` in `src/ouroboros/orchestrator/adapter.py`, `src/ouroboros/backends/`, and `runtime_factory.py` | setup, config, execution handlers |
| LLM-only completion | `src/ouroboros/providers/` | interview, semantic evaluation, advisory lanes |
| Attempt execution and effects | direct/resume paths in `runner.py`; parallel paths in `parallel_executor.py` and `leaf_dispatcher.py` | dashboards, verification, recovery |
| Final AC acceptance | orchestrator verifier outcome plus the EventStore's atomic acceptance fence | projections, evaluate/evolve successors |
| Durable facts | event factories in `src/ouroboros/events/` and orchestrator code; storage in `src/ouroboros/persistence/` | status, replay, project map, dashboards |
| Evidence and deliver analysis | `src/ouroboros/harness/`; the current TraceGuard live path is observe-only unless an owning contract explicitly wires it | evaluation, benchmarks and projections |
| Read models | `src/ouroboros/project_map.py`, harness projection builders, dashboard reducers and TUI | humans and automation; never execution authority |
| Plugin capability and lifecycle | `src/ouroboros/plugin/`; top-level command fallback in `src/ouroboros/cli/main.py` | CLI plugin dispatch and UserLevel workflows |

Two rules follow from this table:

1. A projection may summarize an authoritative event, but it may not invent a
   new execution or acceptance decision.
2. Runtime and LLM backends are separate selectors. A runtime executes agent
   work; an LLM adapter produces completions for authoring or evaluation.

## Package Ownership Map

### Front Doors and Host Integration

| Package | Owns |
|---|---|
| `src/ouroboros/cli/` | Typer commands, terminal UX, setup and maintenance commands |
| `src/ouroboros/mcp/` | MCP SDK mapping, tools, jobs, resources, server lifecycle |
| `src/ouroboros/router/` | Shared `ooo` command parsing and skill dispatch; not model routing |
| root `skills/` | Authored workflow skill bundles that are packaged into the wheel |
| `src/ouroboros/skills/` | Packaged-skill discovery and artifact helpers |
| `src/ouroboros/agents/` | Bundled agent prompt assets and loading |
| `src/ouroboros/codex/`, `src/ouroboros/hermes/`, `src/ouroboros/opencode/`, `src/ouroboros/kiro/`, `src/ouroboros/copilot/`, `src/ouroboros/gjc_bridge/` | Host-specific setup artifacts and bridges |

### Authoring and Product Programs

| Package | Owns |
|---|---|
| `src/ouroboros/bigbang/` | Interview engine, ambiguity work, Seed generation |
| `src/ouroboros/interview_adapters/` | Confirmation-required reference candidates and glossary adapters |
| `src/ouroboros/pm/` | Stable PM import and handoff facade; detailed document generation remains in `bigbang/` |
| `src/ouroboros/auto/` | Goal-to-Seed-to-run supervision and bounded recovery |
| `src/ouroboros/evolution/` | Wonder/Reflect and convergence workflows |
| `src/ouroboros/resilience/` | Stagnation classification and lateral strategies |

### Kernel and Execution

| Package | Owns |
|---|---|
| `src/ouroboros/core/` | Seed and domain types, project identity, safety and control contracts |
| `src/ouroboros/orchestrator/` | Run assembly, runtime execution, routing policy, verification wiring, resume |
| `src/ouroboros/backends/` | Backend names, capabilities, factory metadata and model catalogs |
| `src/ouroboros/providers/` | LLM-only adapters and completion contracts |
| `src/ouroboros/runtime/` | Shared runtime lifecycle and watchdog utilities |
| `src/ouroboros/profiles/`, `src/ouroboros/strategies/` | Data-driven execution profiles and strategy helpers |

`src/ouroboros/execution/` is an empty compatibility package. Do not add new
execution logic there. New live execution behavior belongs to an existing
orchestrator collaborator or to a newly approved collaborator under the
orchestrator boundary.

### Verification, State, and Observation

| Package | Owns |
|---|---|
| `src/ouroboros/evaluation/` | Mechanical, semantic, and consensus evaluation |
| `src/ouroboros/verification/` | Typed verification extraction and models |
| `src/ouroboros/harness/` | Evidence normalization, deliver gates and rebuildable projections |
| `src/ouroboros/events/` | Durable event types and event factories |
| `src/ouroboros/persistence/` | EventStore, checkpoints, artifacts and dashboard picker indexes |
| `src/ouroboros/project_map.py` | Read-only cross-run Project Map projection |
| `src/ouroboros/observability/` | Logging, drift and retrospective reports |

### Presentation and Configuration

| Package | Owns |
|---|---|
| `src/ouroboros/tui/` | Textual monitoring UI |
| `src/ouroboros/dashboard/`, `src/ouroboros/dashboard_web/` | Event-folded boards and web presentation |
| `src/ouroboros/config_tui/` | Interactive configuration UI |
| `src/ouroboros/config/` | Config schema, loading, environment trust and precedence |
| `src/ouroboros/plugin/` | UserLevel plugin manifests, permissions, hooks and dispatch |

## Where to Start

| Change | Read first | Verify first |
|---|---|---|
| Interview or Seed semantics | `bigbang/`, `core/seed.py`, owning RFC | focused Big Bang/core tests and one authoring surface |
| Runtime backend | backend capability registry, runtime factory, sibling runtime | runtime factory, adapter contract, setup and native smoke |
| LLM backend | provider factory and sibling adapter | provider contract and response-format tests |
| AC execution or resume | runner, parallel executor, execution authority | exact changed path plus cold replay and terminal tests |
| Final acceptance | orchestrator verify-gate outcomes and the EventStore acceptance fence | negative corpus proving false evidence is rejected |
| Harness projection or deliver analysis | harness projection/deliver modules and their owning RFC | offline/observe-only evidence and projection tests |
| Persistence | event/artifact schema and EventStore | idempotency, interruption, cold replay, malformed state |
| CLI or MCP | canonical command/tool registry | real CLI/tool invocation, not only helper tests |
| Dashboard or TUI | authoritative event and shared reducer | reducer test plus rendered/manual surface check |
| Plugin behavior | manifest schema, firewall, hook owner | permission and end-to-end plugin tests |

## Change Rules

1. Read the owning issue or RFC before changing a boundary.
2. Keep public behavior in one owner; remove duplicate interpretation instead of
   synchronizing copies.
3. Do not let a runtime adapter, dashboard, or worker self-report mint final
   acceptance; preserve the EventStore acceptance fence.
4. Persist enough identity to resume or fail closed; never reconstruct authority
   from labels, paths, or prose.
5. Add a regression at the boundary that failed, then exercise the real user
   surface described in [The Development Loop](./developing.md).

The repository is large and active. File counts and package counts are omitted
deliberately because they become stale without helping a contributor choose an
owner.
