# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **run/convergence**: Make `ooo run` a hidden-checklist convergence pipeline. Worker prompts no longer expose harness `verify_command` or `output_assertion` values, retries use assertion-safe harness/trace observations, completed failed runs still receive formal evaluation, and explicit evaluation rejection automatically seeds generation 1 and continues through a bounded Ralph lineage (default 3 generations, opt out with `execution.auto_evolve: false`).
- **orchestrator/worktrees**: Change the default managed task worktree cleanup policy from `keep` to `prune-merged` (#1802 owner decision). `keep` guaranteed unbounded growth of `~/.ouroboros/worktrees` (observed: 789 leftover worktrees, 26 GB on one machine). The policy still applies only through the guarded `cleanup_task_workspace` path — dirty, locked, and unmerged worktrees are never removed — and the internal fallback for a config object without the field stays `keep`, so an uncertain configuration cannot delete anything.
- **orchestrator/decomposition**: Preserve the historical non-negative decomposition-depth contract while defining `0..4` as the explicit Routing D durable-replay subset. The maximum five-way durable tree (780 child nodes) round-trips through node-local conflict projections; larger configured depths continue on the legacy non-resumable parallel path instead of being rejected.
- **orchestrator/resume**: Version the durable execution contract to v9 and fingerprint every resolved runner/executor setting that affects provider prompts, effects, acceptance, or recovery timing. The exact context-pack fragment, complete execution profile, inherited parent runtime handle, prompt strategy, tool catalog, bounded usage-limit pause window, and complete runtime capability/vocabulary declaration are sealed before provider entry. New, resumed, successor, direct, and parallel paths consume that immutable snapshot and recheck it at the provider choke point; resume rejects incomplete v2-v8 contracts, any missing or unknown v9 top-level member, or any verification, retry, decomposition, worker, backend limit, harness, replay, checkpoint, signal, prompt/tool, lineage, pause-policy, or runtime-capability drift before analyzer/executor/provider entry. Current v9 guidance and execution preferences are never synthesized from defaults.

### Fixed
- **skills/claude**: Stop shadowing Claude Code's reserved `/run`, `/status`,
  `/help`, and `/config` built-ins. The Claude-facing skill names are now the
  explicit `/ouroboros:ouroboros-run`, `/ouroboros:ouroboros-status`,
  `/ouroboros:ouroboros-help`, and `/ouroboros:ouroboros-config`; `ooo run`,
  `ooo status`, `ooo help`, `ooo config`, and the non-reserved aliases remain
  available. Runtime capability discovery continues to use stable skill
  directory identities rather than these host-facing registration names.
- **orchestrator/success contracts**: Preserve legacy prose-AC replay while failing closed when a persisted structured Seed declares an artifact path beyond the portable 255-byte limit. A schema-valid contract that cannot fit beneath the active POSIX or Windows workspace is now recorded as a typed `unmaterializable_success_contract` pre-dispatch `INVALID` result; no provider is called, sibling ACs continue, and resume/evolution reconstruction reports an explicit validation error instead of leaking a generic exception. Generated verify-command extraction now follows POSIX single-quote semantics as well: backslashes remain literal inside single quotes, adjacent quoted token segments such as `foo're'` survive extraction, and apostrophes/backslashes inside token-boundary `#` comments remain inert until the canonical outer contract delimiter. Comment admission tracks the actual unquoted word state rather than the previous raw character, so escaped whitespace/control operators cannot forge a comment or promote an escaped pipe into output-assertion syntax. Balanced `$()`/`${...}` lexical frames preserve nested quotes, escapes, delimiters, and reserved-looking pipes before the real outer DSL delimiter; prose-marker defenses and token-internal `#` behavior remain intact.
- **orchestrator/execution**: Reconcile every precreated execution receipt against event-sourced session status before claiming process-local authority, then recheck after the claim to close RUNNING-to-PAUSED races. Reusing the original RUNNING tracker after durable `PAUSED` now stops before tool or provider entry and directs callers to `resume_session`; unreadable durable state remains retryable without leaking a claim or revoking the live owner. Retained pause or terminal persistence intent now takes precedence over durable `RUNNING` at both recovery ingresses, preventing the same prepared tracker from repeating a provider effect while its prior lifecycle publication is pending.
- **orchestrator/replay**: Replace the unmatched 64-event direct/parallel and 4,096-event composite pause replay ceilings with one shared population-total high-water/keyset fold. Pause producers may persist every recoverable provider window; replay validates the complete chronological history in fixed-size memory pages, deterministically tie-breaks equal timestamps by event ID, and restores the latest unconsumed provider boundary. Real SQLite regressions cover 65 direct pauses, 65 parallel pauses, and 4,097 composite pauses.
- **orchestrator/execution**: Seal the exact initial contract in process-local authority only after durable publication, then authenticate that seal and the complete v9 contract before prompt or provider entry. Caller tracker mutations, changed Seed semantics, and nested strategy/context changes with stale fingerprints now fail with zero provider calls; unbound MCP tool authority remains valid only until the existing pre-effect binding checkpoint.
- **orchestrator/replay**: Derive terminal composite-completion query sentinels from the admitted Seed root population instead of a fixed 4,096-event ceiling. Coordinator started/completed schemas now serialize every conflict produced by an admitted stage and validate durable rows against that exact stage-derived population, so valid 4,097+ AC completions or post-effect file conflicts cannot strand replay after provider work.
- **orchestrator/routing**: Canonicalize typed hard-precondition metadata across prose and machine identifier styles, recognize numeric HTTP 401/403 authorization statuses, and share the classifier across direct and parallel routing so access/tool/config blockers cannot spend a successor route.
- **orchestrator/routing**: Propagate recoverable quota pauses across an active parallel batch immediately. A shared signal closes semaphore-waiting provider entrances and defers decomposed legacy recovery. Only siblings stopped before execution-authority entry remain pending; an entered sibling is sealed and receives a durable uncertain-effect `BLOCKED` handoff instead of being advertised as replayable. Parallel route pauses now seal and chronologically restore the complete capsule-bearing state plus the latest exact nonterminal provider boundary. Direct and resumed routes publish `PAUSED` only after an exact resumable handle is durably stored, then transfer cleanup ownership so the returning coroutine cannot terminate that live provider session; pause-persistence-pending retains the same ownership. Handle-less, terminal, or unpersisted pauses finalize as `BLOCKED` without a fresh provider retry. Direct resumed successors also build prompts from the persisted strategy rather than the mutable task-type registry.
- **run/mcp**: Make fat-harness acceptance opt-in via `seed.orchestrator.execution_mode: fat_harness` for fresh CLI/MCP seed execution. Missing/blank execution mode now uses the default runner again until seed authoring and QA guidance consistently emit profile-compatible typed evidence for every AC. This mitigates layered scaffold AC failures reported in #1202.

### Added
- **project map**: Rebuild frozen `ProjectRecord` and `ProjectRunSummary` values from the complete existing EventStore session history without a new table, index, event, reducer, or write path. The projection delegates lifecycle status to the strict related-event mode of `SessionRepository`, accepts complete public top-level anchors and labels compatible nested-only legacy anchors, validates project conflicts before workspace filtering, and raises typed reconstruction, conflict, or population-limit errors instead of returning an unmarked partial map. Project Map remains attribution-only and cannot dispatch execution or declare Final Gate acceptance.
- **project map**: Add the Project Map V1 identity anchor for runner-owned sessions. One Git-backed resolver joins nested checkouts and positively owned linked/managed worktrees to a stable identity-root UUIDv5 while retaining a relative workspace scope. Git 2.36.0 or newer owns config, gitfile, common-directory, worktree, and `HEAD` semantics; the resolver rejects an older or unrepresentable version as a non-retryable configuration error before topology queries. Bounded non-interactive queries bind top-level discovery to the validated common directory, preserve legal newline-bearing POSIX paths, detect nested markerless bare repositories before adopting enclosing markers, and require the active checkout to appear in Git's worktree population or match Git's explicit `core.worktree` top level, so redirected unowned markers and unrelated ancestor checkouts fail closed. Fresh inputs and Git-reported owners are revalidated as one complete live-directory population after topology resolution and immediately before return. Managed source and execution roots must equal Git-proven checkout roots, so caller-selected nested roots cannot collapse a real workspace scope to `.`. Git spawn, timeout, output-bound, non-representable output, and repository-local nonzero failures are retryable unavailability rather than fallback identities. The untrusted `.env` boundary denies home redirection and dynamic-loader injection, while Git receives a fixed neutral home so identity cannot drift with caller `HOME`. One resolved identity is atomically shared by the immutable session-start event and existing execution contract; the contract is detached before asynchronous publication validation, the actual provider workspace is matched to the frozen managed workspace by live directory identity and re-resolved off the event loop at the persistence choke point, and managed source/execution identities must match on both fresh and resumed paths, so canonical-equivalent symlink paths remain valid while caller mutation cannot split anchors, Git timeouts cannot stall concurrent orchestration, and deleted workspaces or stale ordinary directories cannot acquire immutable attribution. Resume terminalizes invalid current workspaces but preserves a paused live owner on transient Git unavailability. The shared provider-neutral cwd boundary resolves direct and leader-driven runtimes plus task overrides once, preserves resolved absence through one shared wrapper, propagates explicit path-resolution failures, and rejects preparation unless task, runtime-handle, publication, and provider execution owners agree; runner process cwd is never substituted for an explicit or unset provider owner. Identity remains attribution-only and grants no execution or Final Gate authority.
- **providers**: GitHub Copilot CLI adapter (`CopilotCliLLMAdapter`) — first-class peer of Codex/Gemini/OpenCode adapters. Switch with `OUROBOROS_LLM_BACKEND=copilot`. Uses local `copilot -p` non-interactive mode with `GH_TOKEN`/`GITHUB_TOKEN` auth, hard tool envelope via `--available-tools`+`--allow-tool`+`--add-dir`, sandbox-class permission mapping, JSONL stream parsing, recursion guard via shared `_OUROBOROS_DEPTH` counter (max depth 5), and auth-error short-circuit on `401`/missing-token detections. Optional install: `pip install ouroboros-ai[copilot]` (the Copilot CLI itself is installed externally).
- **opencode**: Subagent bridge plugin (`src/ouroboros/opencode/plugin/ouroboros-bridge.ts`) — routes MCP `ouroboros_*` tool calls with a `_subagent` parameter into OpenCode's native Task subagent panes via `session.promptAsync`. Fire-and-forget dispatch returns from the hook in ~10ms, eliminating the blocking 200s+ latency of the previous `session.prompt` approach. Installed automatically by `ouroboros setup`. See [OpenCode Subagent Bridge](docs/guides/opencode-subagent-bridge.md).
- **lateral_think**: Parallel multi-persona dispatch — `ouroboros_lateral_think` now accepts `persona="all"` or `personas=["hacker","architect",...]` to fan out to multiple lateral-thinking personas in a single call. Each persona runs in its own Task pane with an independent LLM context, eliminating anchoring bias across alternatives. Uses new `_subagents` (plural) JSON contract, implemented server-side via `build_lateral_multi_subagent()` and plugin-side via MAX_FANOUT=10 parallel `promptAsync` with per-payload dedupe and error isolation.
- **opencode/bridge**: Plugin v23 recognizes `_subagents` array for parallel fan-out. Per-payload validation, truncation, and dedupe. One failed dispatch does not abort the rest. New `ouroboros_subagents` and `ouroboros_dispatch_errors` metadata fields. Backwards compatible with v22 single-payload `_subagent` contract.
- **providers**: OMP (Oh My Pi) LLM adapter (`OmpLLMAdapter`) for `--llm-backend omp` —
  `omp` / `omp_cli` registered as LLM- and interview-driver-capable in the backend
  registry and provider factory; generic default models normalize to the backend-safe
  `default` sentinel so OMP picks its own configured model
- **runtime**: OMP CLI runtime (`OmpRuntime`, `orchestrator.runtime_backend: omp`) —
  Pi-family JSON-mode subprocess adapter (`omp --mode json <prompt>`, same JSONL event
  protocol as Pi) with native `--resume` targeted resume and native
  `--append-system-prompt` / `--tools` / `--no-tools` delivery; Claude-style tool names
  map to OMP's vocabulary (`Glob`/`LS` → `glob`; OMP has no `ls` built-in)
- **setup/installer**: `ouroboros setup --runtime omp` wires the managed OMP bridge
  (`~/.omp/agent/extensions/ouroboros-ooo-bridge.ts`, timeout env
  `OUROBOROS_OMP_BRIDGE_TIMEOUT_MS`), `ouroboros config backend omp` switches to it, and
  `scripts/install.sh` adds OMP to its runtime menu; spawned-CLI discovery env
  `PI_CODING_AGENT_DIR` is denied from untrusted repo `.env` files
- **docs**: OMP CLI runtime guide plus omp entries across the config reference, runtime
  capability matrix, architecture, getting-started, CLI reference, skill capability
  guides, and READMEs

### Fixed
- **skills**: Renamed the packaged `resume` skill to `resume-session` so Claude Code's built-in `/resume` session picker is no longer shadowed. Use `ooo resume-session` or `/ouroboros:resume-session` for the Ouroboros in-flight session listing.
- **mcp/security**: `FREETEXT_FIELDS` allowlist for user-input fields (goals, prompts, descriptions) — shell metacharacters (`;`, `|`, `&`, backticks, `$()`) are no longer rejected in fields where they are legitimate prose. Structural fields remain strictly validated.
- **opencode/bridge**: Robustness hardening (v22) — no uncaught errors under any input. Adds reject-path logging, frozen-content guards, empty-sessionID guard, client init-order guard, 5-second FNV-1a prompt dedupe, 100 KB prompt byte cap with truncation marker, user-visible `surfaceErr()` for dispatch failures (no more silent "dispatched but never ran"), and an absolute outer try/catch so the plugin cannot throw into the opencode runLoop.

## [0.41.0] - 2026-06-07

> Run it anywhere, and trust what it ships. Pi joins as a first-class runtime,
> the Socratic interview convenes a multi-persona panel at every ambiguity
> milestone, and the verifier's "done" verdict becomes typed, audited, and
> impossible to game.

### Added
- **providers**: Pi LLM adapter (`PiLLMAdapter`) for `--llm-backend pi` — `pi` /
  `pi_cli` registered as LLM- and interview-driver-capable in the backend
  registry and provider factory; Pi-aware default-model normalization so the
  default uses Pi's own backend default instead of forwarding an Anthropic model
  name (#1326)
- **runtime**: `ouroboros setup --runtime pi` wires the managed Pi bridge setup
  surface (5c674c11)
- **interview**: milestone lateral-review **dispatch** (promoted from advisory) —
  at `initial→progress`, `progress→refined`, `refined→ready` transitions the main
  session runs `ouroboros_lateral_think` with researcher/contrarian/simplifier
  (+architect when system shape changes) before answering or asking the returned
  question, folding findings into 2–3 concrete options or a recommended draft.
  Adds the `run_lateral_review` interview capability and per-runtime
  capability/instruction artifacts (9d229c4c)
- **harness**: promote TraceGuard verdict admission into `VerifierVerdict` —
  typed status, evidence refs, and `retry_admission`; ACCEPT / RETRY / REDISPATCH /
  ESCALATE_MODEL / ESCALATE_HUMAN / BLOCK persisted on atomic typed-evidence
  events (RFC #814) (#1330)

### Changed
- **config**: centralize every default Claude model pin into one source of truth
  (`_model_defaults.py`) and pin exact snapshots rather than the `"default"`
  sentinel for reproducible grading — Opus reasoning tier → 4.8, Sonnet judgment
  tier (`qa_model`) pinned at 4.6, retiring the dated `claude-sonnet-4-20250514`
  (#1324, #1323)
- **harness/h7**: prefer `VerifierVerdict.retry_admission` over the
  failure-class-derived policy when an explicit admission is present; re-run the
  same leaf only on `RETRY` (#1331)
- **auto**: gate runs on backend-confirmed low ambiguity (≤ 0.20) plus a pre-run
  Seed QA pass, feeding QA findings into bounded Seed-repair attempts before
  blocking (#1302)
- **auto**: normalize worktree-policy aliases (e.g. `create_isolated_worktree →
  always`) and fail fast when `complete_product=true` is paired with a too-short
  timeout (#1305)
- **deps**: prune unused optional packages (#1301)

### Fixed
- **pi**: align the runtime with documented JSON mode (#1321)
- **pi**: report malformed runtime events as a typed `ProviderError` (#1325)
- **orchestrator**: classify masked test evidence (`… | tail`) as
  `EVIDENCE_FORM_MISMATCH` — retryable with actionable feedback (e.g.
  `set -o pipefail`) — instead of `FABRICATION_SUSPECTED`; the #1208 guard holds
  (#1292)
- **installer**: run `setup` with the freshly installed `ouroboros` binary, not a
  stale one on `PATH`; preserve existing `PATH` precedence on pipx/pip paths
  (#1345, #1343)
- **opencode**: cover Windows cleanup review blockers (#1320)
- **goose**: keep LLM completion calls profile-free (#1303)
- **run**: guard the home directory in `_detect_project_root_from_seed_path`
  (#1313)
- **deps**: pin `typer` before the vendored `click` to stabilize resolution
  (#1300)

### Testing
- **orchestrator**: opt-in native Pi CLI smoke test (#1329)

### Docs
- Pi provider surfaces (#1327) and Pi runtime guide; fix shipped-backend wording
  (#1332); AgentOS issue-sequencing graph snapshot (#1293); Verdict Envelope v1
  RFC and verifier-evidence-policy; runtime-capability-matrix and
  contributing/key-patterns updates

## [0.14.1] - 2025-02-27

### Fixed
- **interview**: Fix empty response bypass in ClaudeCodeAdapter — empty content now always triggers error regardless of session_id
- **interview**: Fix sub-agent turn exhaustion — increase max_turns from 1 to 3 so the agent can use tools and still generate the question

### Maintenance
- **style**: Apply ruff format to 4 files
- **ci**: Resolve ruff and mypy CI failures

## [0.13.4] - 2025-02-24

### Fixed
- **mcp**: Initialize EventStore in ExecuteSeedHandler before passing to OrchestratorRunner

## [0.13.3] - 2025-02-24

### Fixed
- **mcp**: Remove double-registration in CLI that overwrote dependency-injected handlers with empty ones
- **mcp**: Return proper MCP error responses (isError:true) instead of error text in success
- **mcp**: Catch `pydantic.ValidationError` in ExecuteSeed, MeasureDrift, Evaluate handlers
- **mcp**: Initialize EventStore before EvolutionaryLoop.evolve_step accesses it
- **mcp**: Forward host/port CLI args to server for SSE transport
- **mcp**: Remove dead code (discarded EvaluationPipeline/LateralThinker instances)
- **mcp**: Remove invalid `llm_adapter` kwarg from ClaudeAgentAdapter init
- **orchestrator**: Handle DependencyAnalyzer error with all-parallel fallback instead of crash
- **seed**: Add Pydantic aliases (`type` for field_type, `criteria` for evaluation_criteria)
- **eval**: Change EvaluationPipeline/SeedGenerator type annotations from LiteLLMAdapter to LLMAdapter Protocol
- **security**: Validate nested string values in InputValidator, not just top-level
- **security**: Use MappingProxyType for frozen dataclass AuthContext.metadata
- **protocol**: Add credentials param to MCPServer protocol to match implementation

### Changed
- **build**: Use dynamic version from `__init__.py` via hatchling (single source of truth)

## [0.13.2] - 2025-02-24

### Fixed
- **adapter**: Handle unknown message types (`rate_limit_event`) from Claude Agent SDK with retry logic
- **interview**: Ensure first response is a direct question, not introduction
- **mcp**: Correct uvx command syntax to use `--python 3.14 --from ouroboros-ai` for proper version resolution

## [0.10.0] - 2026-02-14

### Added

#### Plugin System - Agent Orchestration Framework (Phase 1)

**Agent System (`ouroboros.plugin.agents`)**
- `AgentRegistry` - Dynamic agent discovery with custom `.md` file support from `.claude-plugin/agents/`
- `AgentPool` - Reusable agent pool with load balancing, auto-scaling, and health monitoring
- `AgentRole` enum - Type-safe role categorization (ANALYSIS, PLANNING, EXECUTION, REVIEW, DOMAIN, PRODUCT, COORDINATION)
- `AgentSpec` - Frozen dataclass for agent specifications with tools, capabilities, and model preferences
- 4 builtin agents: `executor`, `planner`, `verifier`, `analyst`

**Skill System (`ouroboros.plugin.skills`)**
- `SkillRegistry` - Hot-reloadable skill discovery from `.claude-plugin/skills/`
- `MagicKeywordDetector` - "ooo:" prefix and trigger keyword routing
- `SkillExecutor` - Context-aware skill execution with history tracking
- `SkillDocumentation` - Auto-generated documentation from SKILL.md files
- 9 new execution mode skills:
  - `autopilot` - Autonomous execution from idea to working code
  - `ultrawork` - Maximum parallelism with parallel agent orchestration
  - `ralph` - Self-referential loop with verifier verification (includes ultrawork)
  - `ultrapilot` - Parallel autopilot with file ownership partitioning
  - `ecomode` - Token-efficient execution using haiku and sonnet
  - `swarm` - N coordinated agents using native runtime teams
  - `pipeline` - Sequential agent chaining with data passing
  - `tutorial` - Interactive guided tour for new users
  - `swarm` - Team coordination mode

**Orchestration (`ouroboros.plugin.orchestration`)**
- `ModelRouter` - PAL (Progressive Auto-escalation) routing with tier selection
- `Scheduler` - Parallel task execution with dependency resolution via `TaskGraph`
- `RoutingContext` - Complexity-aware routing with learning from history
- `ScheduledTask` - Task wrapper with priority, dependencies, and timeout support

**State Management**
- Removed: `StateStore`, `StateManager`, `RecoveryManager`, `StateCompression` (dead code — all runtime state managed by EventStore/SQLite)

**TUI HUD Components (`ouroboros.tui.components`)**
- `AgentsPanel` - Real-time agent pool status visualization
- `TokenTracker` - Per-agent token usage with cost estimation
- `ProgressBar` - Multi-phase progress with animated spinners
- `EventLog` - Scrolling event history with color-coded severity
- `HUDDashboard` - Unified HUD screen integrating all components

**Documentation**
- `docs/compare-alternatives.md` - Comparison with other AI agents and frameworks
- `docs/onboarding-metrics.md` - User onboarding metrics and optimization strategies
- `docs/marketing/` - Marketing assets (social media templates, star campaign, why ouroboros)
- `docs/screenshots/` - Screenshot capture guides and production scripts
- `docs/videos/` - Video production guides and demo scripts
- Updated `CONTRIBUTING.md` - Full development setup and contribution guide
- Updated `docs/architecture.md` - Plugin system architecture documentation
- Updated `docs/getting-started.md` - Enhanced onboarding experience

**Developer Experience**
- GitHub workflows: `.github/workflows/lint.yml`, `test.yml`, `release.yml`
- `playground/` directory with example models and configurations
- 161 new passing tests (149 unit + 12 integration)

**Skill Files Updated**
- Updated `help`, `setup`, `welcome` skills with progressive disclosure
- Added 8 new skill SKILL.md files (autopilot, ultrawork, ralph, ultrapilot, ecomode, swarm, pipeline, tutorial)

### Changed
- Updated CLI onboarding flow to reference new plugin system
- Enhanced skill discovery with automatic trigger keyword indexing
- Improved state persistence across /clear and session restarts

### Tests
- 161 new tests for plugin system (149 unit + 12 integration)
- All existing TUI and tree tests continue to pass (190 tests)
- Total test count: 1731 passing tests

## [0.3.0] - 2026-01-28

### Added

#### Documentation
- **CLI Reference** (`docs/cli-reference.md`) - Complete command reference with examples
- **Prerequisites section** in README with Python 3.14+ requirement
- **Contributing section** with links to Issues and Discussions
- **OSS badges** - PyPI version, Python version, License

#### Interview System
- **Tiered confirmation system** for interview rounds:
  - Rounds 1-3: Auto-continue (minimum context gathering)
  - Rounds 4-15: Ask "Continue?" after each round
  - Rounds 16+: Ask "Continue?" with diminishing returns warning
- **No hard round limit** - User controls when to stop
- New constants: `MIN_ROUNDS_BEFORE_EARLY_EXIT`, `SOFT_LIMIT_WARNING_THRESHOLD`

### Changed

#### Interview Engine
- Removed `MAX_INTERVIEW_ROUNDS` hard limit (was 10)
- `is_complete` now only checks status (user-controlled completion)
- `record_response()` no longer auto-completes at max rounds
- System prompt simplified to show "Round N" instead of "Round N of 10"

#### CLI Init Command
- Extracted `_run_interview_loop()` helper to eliminate code duplication (~60 lines)
- State saved immediately after status mutation for consistency
- Updated welcome message to reflect no round limit

### Removed
- Korean-language requirement documents (`requirement/` folder)
- Hard round limit enforcement in interview engine

### Fixed
- Code duplication in init.py interview continuation flow

## [0.2.0] - 2026-01-27

### Added

#### Security Module (`ouroboros.core.security`)
- New security utilities module with comprehensive protection features
- **API Key Management**
  - `mask_api_key()` - Safely mask API keys for logging (shows only last 4 chars)
  - `validate_api_key_format()` - Basic format validation for API keys
- **Sensitive Data Detection**
  - `is_sensitive_field()` - Detect sensitive field names (api_key, password, token, etc.)
  - `is_sensitive_value()` - Detect values that look like secrets
  - `mask_sensitive_value()` - Mask potentially sensitive values
  - `sanitize_for_logging()` - Create sanitized copies of dicts for safe logging
- **Input Validation**
  - `InputValidator` class with size limits for DoS prevention:
    - `MAX_INITIAL_CONTEXT_LENGTH` = 50KB
    - `MAX_USER_RESPONSE_LENGTH` = 10KB
    - `MAX_SEED_FILE_SIZE` = 1MB
    - `MAX_LLM_RESPONSE_LENGTH` = 100KB

#### Logging Security
- Automatic sensitive data masking in structlog processor chain
- API keys, passwords, tokens are now automatically redacted in all log outputs
- Nested dictionaries are recursively sanitized
- Pattern-based detection for values starting with `sk-`, `pk-`, `Bearer`, etc.

### Changed

#### Interview Engine
- Input validation now uses `InputValidator` for consistent size limits
- `start_interview()` validates initial context length
- `record_response()` validates user response length

#### LiteLLM Adapter
- LLM responses are now validated and truncated if exceeding size limits
- Warning logged when response truncation occurs

#### CLI Run Command
- Seed file size is now validated before loading
- Protection against oversized seed files

### Security

- **API Key Management**: Keys are masked in logs, showing only provider prefix and last 4 characters
- **Input Validation**: All external inputs have size limits to prevent DoS attacks
- **Log Sanitization**: Sensitive data is automatically masked in all log outputs
- **Credentials Protection**: `credentials.yaml` continues to use chmod 600 permissions

### Tests

- Added comprehensive test suite for security module (39 tests)
- Added sensitive data masking tests for logging module (5 tests)
- All 1341 tests passing

## [0.1.1] - 2026-01-15

### Added
- Initial release with core Ouroboros workflow system
- Big Bang (Phase 0) - Interview and Seed generation
- PAL Router (Phase 1) - Progressive Adaptive LLM selection
- Double Diamond (Phase 2) - Execution engine
- Resilience (Phase 3) - Stagnation detection and lateral thinking
- Evaluation (Phase 4) - Mechanical, semantic, and consensus evaluation
- Secondary Loop (Phase 5) - TODO registry and batch scheduler
- Orchestrator (Epic 8) - Runtime abstraction and orchestration
- CLI interface with Typer
- Event sourcing with SQLite persistence
- Structured logging with structlog

### Fixed
- Various bug fixes and stability improvements

## [0.1.0] - 2026-01-01

### Added
- Initial project structure
- Core types and error hierarchy
- Basic configuration system
