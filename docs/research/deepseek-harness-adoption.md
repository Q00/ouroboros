# DeepSeek Harness (dsh) — Adoption, Collaboration, and Improvement Map

> Analysis date: 2026-08-15, against `deepseek-ai/deepseek-harness` @ `47f9438`
> (v0.1.0-rc.5, MIT, developer preview with explicit breaking-change warning).
> Method: three parallel deep-reads (architecture docs, package mechanisms,
> integration surface) over a full clone.

DeepSeek Harness (`dsh`) is DeepSeek AI's open-source agent harness: a
TypeScript pnpm monorepo built on Cordis where **everything — the model
adapter, tool registry, session log, and the agent loop itself — is a plugin**.
It ships a Web UI, a headless CLI, a Python SDK (subprocess + JSON-RPC), an
MCP *client*, and an automation-only ACP *server*.

This document answers three questions from Ouroboros's perspective:
what to borrow, how to collaborate, and what to improve here.

---

## 1. What Ouroboros can borrow

### 1.1 Already covered — do not duplicate

| dsh mechanism | Ouroboros equivalent |
|---|---|
| `tool-ralph` round cap + fixed fresh-agent loop | `RalphLoopRunner` (`ralph_loop.py`): max_generations, `max_total_seconds`, per-iteration timeout |
| `guard/repeat-tool-reminder` (identical-call detection) | Oscillation detection via `findings_hash` window + grade-regression window |
| Monotonic `ToolGuard` (deny cannot be re-allowed) | Spec-verifier authority revocation (#2065): producer+adapter double enforcement |
| Session events as SSOT, derived views | EventStore + projections (`query_projection`) |
| Fail-closed QA authority (`unavailable` = denial) | `_qa_authoritative_failure` fail-closed posture in Ralph loop |

### 1.2 Worth borrowing — ranked

1. **Structured Ralph handoff contract** (`workflow/tool-ralph`): each round
   hands the next fresh agent `status: continue|complete|blocked` + summary /
   evidence / next-steps, hard-capped at `maxHandoffChars`. Ouroboros
   generations reconstruct from the EventStore; an explicit, size-bounded,
   typed handoff would make cross-generation drift auditable and cheap.
2. **Spill pattern** (`spill/spill-policy`): oversized payloads leave the hot
   path for files, replaced by head/tail preview + opaque locator, with the
   notice's own bytes budgeted out of the cap. Direct fit for the known
   EventStore/WAL bloat problem (13 GB DB incident): cap oversized event
   payloads at append time instead of pruning after the fact.
3. **`session/end-seed` boundary marker**: a log-only marker written as the
   first live event of any resumed/forked session, so bracket-owning consumers
   can tell "crashed mid-operation" from "operating right now". Ouroboros
   zombie-job recovery (terminal-event INSERT recipes) would become mechanical
   with this marker in the events table.
4. **Defensive pattern #1 — report orthogonal outcomes independently**
   (timeout AND signal AND exit code as separate fact fields, never nested).
   Worth an audit pass over the 7 CLI adapters' subprocess result surfaces;
   dsh's postmortem 0004 shows exactly how conflating them corrupts triage.
5. **Package-owned runtime invariants with mechanical exhaustiveness**
   (`runtime-diagnostics/invariants`): every package must ship an invariant
   companion or an explained empty one, CI-verified. A lighter Python version
   (per-module invariant registry + a "no invariant because…" convention)
   would harden the events/jobs seams.
6. **KV-cache-preserving compaction** (`compaction-basic`): summarization is a
   verbatim replay of the conversation's own prefix with the instruction
   appended last, so the provider prefix cache stays warm; a model-free pruner
   runs first and skips the LLM call entirely when pruning suffices. Relevant
   to any future Ouroboros-owned conversation surface (interview refiner).
7. **"Model Experience / token effect / KV cache effect" README sections**:
   every dsh package documents its token and prefix-cache impact as a
   first-class property. Cheap documentation discipline worth adopting for
   prompt-injecting modules (context packs, checklists, advisories).

## 2. How to collaborate

**Hard constraint:** `CONTRIBUTING.md` states external PRs are *not accepted*.
Sanctioned channels: GitHub Discussions (bugs/ideas, upvote-prioritized),
community plugins tagged `dsh-plugin` on GitHub, blog posts, Discord.

Ranked, all compatible with each other:

1. **dsh as an Ouroboros LLM backend — shipped in this branch.** dsh's
   automation ACP server (`@deepseek-ai/dsh-acp-demo`, published bin
   `dsh-acp-demo`) speaks the same ACP handshake Ouroboros already implements
   for ourocode, so `DshAcpClient`/`DshLLMAdapter` reuse that machinery. This
   gives Ouroboros a DeepSeek-native completion path (interview/seed/qa/
   evaluate) — a real story for the Chinese OSS ecosystem: *Ouroboros runs on
   DeepSeek Harness*.
2. **Mount Ouroboros's MCP server into dsh.** dsh's `mcp-client` is
   first-class (stdio / streamable-http, Claude-Code-style `mcp__…` naming).
   One overlay YAML row mounts `ouroboros serve`; `examples/mcp-memory/` is
   the exact template. Publishable from our side as a `dsh-plugin`-tagged
   overlay repo — no upstream PR needed. Caveats: dsh bridges tools only (no
   MCP prompts/resources, no deferred discovery), and the
   `host_action=spawn_subagents` fan-out meta-protocol has no dsh consumer.
3. **File the packaging bug via Discussions.** Found during this work:
   `npm install @deepseek-ai/dsh-acp-demo` is broken — the published
   `dsh-tool-bash@0.0.1-rc.1` peer-depends on `@deepseek-ai/dsh-bash-env`,
   which is 404 on npm (renamed to `shell-env` in the repo). A concrete,
   reproducible report is the highest-value first contact.
4. **Positioning note:** dsh's `tool-ralph` + `goal` overlap Ouroboros's Ralph
   but explicitly have *no independent evaluator* ("the model self-certifies
   completion"). Ouroboros's hidden-checklist AC judging is exactly the layer
   dsh lacks — the collaboration story is "spec/acceptance authority on top of
   dsh's loop", not competition.
5. **Not viable now:** upstream code PRs (refused), dsh as MCP *server*
   (doesn't exist), ACP-based full runtime backing (dsh's ACP is deliberately
   text-only/fresh-session — fine for completions, wrong for the tool-using
   orchestrator runtime).

## 3. What this branch changes

- `providers/dsh_acp_client.py` — `DshAcpClient(OurocodeAcpClient)`: launches
  `dsh-acp-demo [--config …]`, sends the mandatory `mcpServers: []` on
  `session/new`, keeps RPC errors generic (no ourocode sign-in marker), and
  never exports `OUROCODE_MODEL`.
- `providers/ourocode_acp_client.py` — three subclass seams (`_spawn_argv`,
  `_spawn_failure_hint`, `_session_new_params`) plus a `_TOOL_LABEL` used in
  error strings. ourocode behavior is byte-identical.
- `providers/dsh_llm_adapter.py` — completion adapter mirroring the ourocode
  one. Reports the honest `dsh-composition` model sentinel (the ACP wire has
  no model parameter; the Cordis composition owns provider/model), zero token
  usage, cooperative `response_format` enforcement.
- Backend registration: `dsh` (alias `deepseek_harness`) as an LLM-only
  backend — factory spec, capabilities (`supports_runtime=False`, and
  deliberately **not** in any subagent-orchestration name map), model catalog
  (custom-entry-only), profiles, CLI config env map, reviewer-independence
  vendor left unknown because the Cordis composition owns the effective
  provider, providers lazy export, and direct interview/Seed CLI selection.
- Config: `orchestrator.dsh_cli_path` / `orchestrator.dsh_config_path` +
  `get_dsh_cli_path()` / `get_dsh_config_path()`
  (`OUROBOROS_DSH_CLI_PATH` / `OUROBOROS_DSH_CONFIG_PATH`), both added to the
  untrusted-env denylist — the config path selects the Cordis composition the
  Node process loads, which is code execution, so it gets CLI-path treatment.
- Tests: protocol-fake-backed client tests + adapter tests
  (`tests/unit/providers/test_dsh_*.py`).

### Improvement backlog surfaced by this analysis (not in this branch)

- Structured inter-generation Ralph handoff (§1.2-1).
- Event-payload spill policy for the EventStore (§1.2-2).
- `end-seed`-style resume boundary marker in the events table (§1.2-3).
- Orthogonal-outcome audit across CLI adapters (§1.2-4).
- Optional: `dsh` composition template shipped as a documented example once
  dsh's npm packaging stabilizes (currently pre-1.0 with breaking changes).

## 4. Verification status

- Full unit suite (excl. `tests/unit/mcp`): 18160 passed / 87 skipped.
- `dsh` client/adapter tests: 22 passed (protocol exercised against the shared
  CI-safe ACP fake).
- Real-binary smoke: npm install path is blocked by the upstream packaging bug
  (§2-3), so the smoke ran against a from-source build (`pnpm install && pnpm
  run build` @ `47f9438`). `DshAcpClient` completed the full wire against the
  built `dsh-acp-demo` bin — spawn → `initialize` → `session/new` (the
  mandatory `mcpServers: []` was accepted) → `session/prompt` dispatched into
  dsh's LLM layer — and failed only at the expected terminal point, the
  missing `DEEPSEEK_API_KEY`, surfaced through the adapter's generic
  `rpc_error` classification.
- **Keyed full-turn run (2026-08-15)**: with a short-lived OpenRouter key, the
  complete production path ran end to end —
  `create_llm_adapter(backend="dsh")` (env-configured via
  `OUROBOROS_DSH_CLI_PATH` / `OUROBOROS_DSH_CONFIG_PATH`) → `DshAcpClient` →
  `dsh-acp-demo` → dsh's `llm-deepseek` adapter pointed at OpenRouter
  (`baseURL` + `apiKeyEnv` overrides) → `deepseek/deepseek-v4-flash`. A plain
  turn returned exactly the requested text (`finish_reason: stop`), and a
  `response_format: json_schema` request came back as valid conforming JSON
  through the adapter's cooperative extract-and-validate path. One dsh loader
  fact learned on the way: plugin package names in a composition file resolve
  relative to the **config file's directory**, so a composition must live
  where dsh's `node_modules` (or workspace) is reachable.
