# Contributing to Ouroboros

Thank you for your interest in contributing to Ouroboros! This guide covers everything you need to get started.

## Table of Contents

- [Quick Setup](#quick-setup)
- [Development Workflow](#development-workflow)
- [Ways to Contribute](#ways-to-contribute)
- [Development Environment](#development-environment)
- [Code Style Guide](#code-style-guide)
- [Commit Message Convention](#commit-message-convention)
- [Project Structure](#project-structure)
- [Key Patterns](#key-patterns)
- [Documentation Coverage](#documentation-coverage)
  - [CLI Commands → Doc Mapping](#cli-commands--doc-mapping)
  - [Orchestrator → Doc Mapping](#orchestrator--doc-mapping)
  - [Configuration → Doc Mapping](#configuration--doc-mapping)
  - [Evaluation Pipeline → Doc Mapping](#evaluation-pipeline--doc-mapping)
  - [TUI Source → Doc Mapping](#tui-source--doc-mapping)
  - [Skills / Plugin → Doc Mapping](#skills--plugin--doc-mapping)
  - [New Command or Flag Checklist](#new-command-or-flag-checklist)
  - [New Runtime Backend Checklist](#new-runtime-backend-checklist)
  - [Documentation Issue Severity Rubric](#documentation-issue-severity-rubric)
  - [Documentation Decay Detection](#documentation-decay-detection)
- [Contributor Docs](#contributor-docs)
- [Code of Conduct](#code-of-conduct)

---

## Quick Setup

> **First time?** See [Getting Started](./docs/getting-started.md) for full install options (Claude Code plugin, pip, or from source).

**Dev setup (from source):**

```bash
git clone https://github.com/Q00/ouroboros && cd ouroboros
uv sync
uv run ouroboros --version   # verify
uv run pytest tests/unit/ -q # run tests
```

**Requirements**: Python >= 3.12, [uv](https://github.com/astral-sh/uv)

---

## Development Workflow

### 1. Find or Create an Issue

- Check [GitHub Issues](https://github.com/Q00/ouroboros/issues) for open tasks
- For new features, open an issue first to discuss the approach
- Label your issue with appropriate tags: `bug`, `enhancement`, `documentation`, etc.

### 2. Branch

```bash
git checkout -b feat/your-feature   # for new features
git checkout -b fix/your-bugfix     # for bug fixes
git checkout -b docs/your-changes   # for documentation
```

### 3. Code

- Follow the project structure (see [Architecture for Contributors](./docs/contributing/architecture-overview.md))
- Use frozen dataclasses or Pydantic models for data
- Use the `Result[T, E]` type instead of exceptions for expected failures
- Write tests alongside your code

### 4. Test

```bash
# Full unit test suite
uv run pytest tests/unit/ -v

# Specific module
uv run pytest tests/unit/evaluation/ -v

# With coverage
uv run pytest tests/unit/ --cov=src/ouroboros --cov-report=term-missing
```

See [Testing Guide](./docs/contributing/testing-guide.md) for more details.

### 5. Lint and Format

```bash
# Check
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/

# Auto-fix
uv run ruff check --fix src/ tests/
uv run ruff format src/ tests/

# Type check
uv run mypy src/ouroboros
```

### 6. Submit PR

- Write a clear PR description explaining **what** and **why**
- Reference the related issue (e.g., `Closes #123`)
- Ensure all tests pass and linting is clean
- Wait for code review and address feedback

---

## Ways to Contribute

### Bug Reports

Found a bug? Please open an issue with:

1. **Clear title**: Summarize the bug
2. **Description**: Steps to reproduce, expected vs actual behavior
3. **Environment**: Python version, OS, `uv run ouroboros --version`
4. **Logs**: Relevant error messages or stack traces

```markdown
## Bug Description
[What went wrong]

## Steps to Reproduce
1. Run `ooo interview "test"`
2. Enter X when prompted
3. Observe error

## Expected Behavior
[What should happen]

## Environment
- Python: 3.12+
- Ouroboros: v0.9.0
- OS: macOS 15.2

## Logs
```
[paste error output]
```
```

### Feature Proposals

Have an idea? Open an issue with:

1. **Problem statement**: What problem does this solve?
2. **Proposed solution**: How should it work?
3. **Alternatives considered**: What other approaches did you think about?
4. **Scope**: Is this a breaking change? Can it be incremental?

### Pull Requests

When submitting a PR:

1. **Small, focused changes**: One logical change per PR
2. **Tests included**: New features need tests
3. **Docs updated**: Update relevant documentation
4. **Clean history**: Squash commits before submitting if needed

### Documentation

Help improve docs by:

- Fixing typos and unclear explanations
- Adding examples to existing features
- Translating documentation (if you speak multiple languages)
- Creating tutorials or guides

When reporting or fixing a documentation problem, apply the [Documentation Issue Severity Rubric](#documentation-issue-severity-rubric) to label the issue (`docs:critical`, `docs:high`, `docs:medium`, or `docs:low`) so maintainers can triage and prioritise correctly.

### Code Review

Review open PRs to:

- Catch bugs before merge
- Suggest improvements
- Learn the codebase

---

## Development Environment

### Environment Setup

```bash
# Copy environment template
cp .env.example .env

# Edit .env with your API keys
# Required: ANTHROPIC_API_KEY or OPENAI_API_KEY
```

### Running Tests

```bash
# Unit tests (fast, no network)
uv run pytest tests/unit/ -v

# Integration tests (requires MCP server)
uv run pytest tests/integration/ -v

# E2E tests (full system)
uv run pytest tests/e2e/ -v

# Skip slow tests for fast iteration
uv run pytest tests/ --ignore=tests/unit/mcp --ignore=tests/integration/mcp --ignore=tests/e2e
```

### Testing Specific Features

```bash
# TUI tests
uv run pytest tests/ --ignore=tests/unit/mcp --ignore=tests/integration/mcp --ignore=tests/e2e -k "tui or tree"

# Evaluation pipeline
uv run pytest tests/unit/evaluation/ -v

# Orchestrator
uv run pytest tests/unit/orchestrator/ -v
```

### Pre-commit Hooks (Optional)

```bash
# Install pre-commit hooks
uv run pre-commit install

# Hooks run automatically on git commit
# Manual run:
uv run pre-commit run --all-files
```

---

## Code Style Guide

### Formatting

- **Line length**: 100 characters
- **Quotes**: Double quotes for strings
- **Indentation**: 4 spaces (no tabs)
- **Tool**: Ruff (auto-formats on save)

```bash
# Format code
uv run ruff format src/ tests/
```

### Type Checking

- **Tool**: mypy (Python 3.12 target)
- **Missing imports**: Ignored (`ignore_missing_imports = true`)
- See `pyproject.toml [tool.mypy]` for the full configuration

```bash
# Type check
uv run mypy src/ouroboros
```

### Linting

Ruff enforces:
- Pycodestyle (E, W)
- Pyflakes (F)
- isort (I)
- flake8-bugbear (B)
- flake8-comprehensions (C4)
- pyupgrade (UP)
- flake8-unused-arguments (ARG)
- flake8-simplify (SIM)

```bash
# Lint
uv run ruff check src/ tests/
```

### Python Version

- **Minimum**: Python 3.12
- **Target**: Python >= 3.12
- Use modern Python features (type unions `|`, match statements, etc.)

---

## Commit Message Convention

We follow a simplified semantic commit format:

```
<type>(<scope>): <subject>

[optional body]
```

### Types

| Type | When to Use |
|------|-------------|
| `feat` | New feature |
| `fix` | Bug fix |
| `docs` | Documentation changes |
| `chore` | Build, tooling, dependency updates |
| `refactor` | Code refactoring (no behavior change) |
| `test` | Test changes |
| `perf` | Performance improvements |

### Scopes

Common scopes: `cli`, `tui`, `evaluation`, `orchestrator`, `mcp`, `plugin`, `core`

### Examples

```bash
# Feature
git commit -m "feat(evaluation): add consensus trigger for seed drift > 0.3"

# Bug fix
git commit -m "fix(tui): resolve crash when AC tree is empty"

# Docs
git commit -m "docs: update CLI reference with new flags"

# Refactor
git commit -m "refactor(orchestrator): extract parallel execution to separate module"
```

### Body (Optional)

For complex changes, add a body explaining the **why**:

```bash
git commit -m "feat(evaluation): add stage 3 consensus trigger

This enables multi-model voting when:
- Seed is modified during execution
- Ontology evolves significantly
- Drift score exceeds 0.3

Closes #42"
```

---

## Project Structure

```
src/ouroboros/
  core/          # Foundation: Result type, Seed, errors, context
  bigbang/       # Phase 0: Interview and seed generation
  routing/       # Phase 1: PAL Router (model tier selection)
  execution/     # Phase 2: Double Diamond execution
  resilience/    # Phase 3: Stagnation detection, lateral thinking
  evaluation/    # Phase 4: Three-stage evaluation pipeline
  secondary/     # Phase 5: TODO registry
  orchestrator/  # Runtime abstraction and orchestration
  providers/     # LLM provider adapters (LiteLLM)
  persistence/   # Event sourcing, checkpoints
  tui/           # Terminal UI (Textual)
  cli/           # CLI commands (Typer)
  mcp/           # Model Context Protocol server/client
  config/        # Configuration management

tests/
  unit/          # Fast, isolated tests (no network, no DB)
  integration/   # Tests with real dependencies
  e2e/           # End-to-end CLI tests
  fixtures/      # Shared test data

.claude-plugin/  # Plugin definitions (skills, agents, hooks)
  agents/        # Custom agent prompts
  skills/        # Plugin skill definitions
  hooks/         # Plugin hooks
```

---

## Key Patterns

Detailed explanations: [Key Patterns](./docs/contributing/key-patterns.md)

### Result Type for Error Handling

```python
from ouroboros.core.types import Result

def validate_score(score: float) -> Result[float, ValidationError]:
    if 0.0 <= score <= 1.0:
        return Result.ok(score)
    return Result.err(ValidationError(f"Score {score} out of range"))

# Consume
result = validate_score(0.85)
if result.is_ok:
    process(result.value)
else:
    log_error(result.error.message)
```

### Frozen Dataclasses

```python
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class CheckResult:
    check_type: CheckType
    passed: bool
    message: str
```

### Event Sourcing

```python
# Events are immutable and append-only
event = create_stage1_completed_event(execution_id="exec_123", ...)
await event_store.append(event)
```

### Protocol Classes

```python
from typing import Protocol

@runtime_checkable
class ExecutionStrategy(Protocol):
    def get_tools(self) -> list[str]: ...
```

---

## Documentation Coverage

This section defines **which documentation files must be updated when a specific source file or code path changes**. Reviewers should verify that all relevant doc files are updated before merging any PR that touches the listed source paths.

### Source of Truth

The authoritative implementation directories are:

| Directory | What it controls |
|-----------|-----------------|
| `src/ouroboros/cli/commands/` | All user-facing CLI commands and flags |
| `src/ouroboros/orchestrator/` | Orchestrator runtime, session management, parallel execution |
| `src/ouroboros/config/` | Configuration schema and defaults |

---

### CLI Commands → Doc Mapping

Any change to a file under `src/ouroboros/cli/commands/` requires reviewing and updating the corresponding documentation:

#### `init.py` — `ouroboros init` / `ouroboros init start`

Flags covered: `--resume`, `--state-dir`, `--orchestrator`, `--runtime`, `--llm-backend`, `--debug`

**Must update:**
- `docs/cli-reference.md` — `init` command section (flags, examples)
- `docs/guides/cli-usage.md` — interview workflow description
- `docs/getting-started.md` — introductory `ooo init` / `ouroboros init` examples
- `docs/getting-started.md` — onboarding flow

**Also check:**
- `docs/runtime-guides/claude-code.md` and `docs/runtime-guides/codex.md` — if `--orchestrator` or `--runtime` behavior changes

#### `run.py` — `ouroboros run workflow`

Flags covered: `--orchestrator/--no-orchestrator`, `--resume`, `--mcp-config`, `--mcp-tool-prefix`, `--dry-run`, `--debug`, `--sequential`, `--runtime`, `--no-qa`

**Must update:**
- `docs/cli-reference.md` — `run` command section (flags, examples, defaults)
- `docs/guides/cli-usage.md` — execution workflow description
- `docs/getting-started.md` — `ooo run` / `ouroboros run` examples

**Also check:**
- `docs/runtime-guides/claude-code.md` and `docs/runtime-guides/codex.md` — if `--runtime` semantics change
- `docs/runtime-capability-matrix.md` — if a runtime backend is added or removed

#### `config.py` — `ouroboros config`

Subcommands: `show`, `init`, `set`, `validate`

> **Note**: All four subcommands are currently placeholder stubs. Mark as `[Placeholder — not yet implemented]` in docs until fully implemented.

**Must update:**
- `docs/cli-reference.md` — `config` command section
- `docs/guides/cli-usage.md` — configuration management section

#### `status.py` — `ouroboros status`

Subcommands: `executions`, `execution`, `health`

> **Note**: All subcommands return placeholder data. Mark as `[Placeholder — not yet implemented]` in docs until real persistence reads are wired in.

**Must update:**
- `docs/cli-reference.md` — `status` command section

#### `mcp.py` — `ouroboros mcp`

**Must update:**
- `docs/cli-reference.md` — `mcp` command section
- `docs/api/mcp.md` — MCP server/client configuration

#### `setup.py` — `ouroboros setup`

**Must update:**
- `docs/cli-reference.md` — `setup` command section
- `docs/getting-started.md` — setup step in onboarding

#### `tui.py` — `ouroboros tui`

**Must update:**
- `docs/cli-reference.md` — `tui` command section
- `docs/guides/tui-usage.md` — TUI usage guide

#### `cancel.py` — `ouroboros cancel`

**Must update:**
- `docs/cli-reference.md` — `cancel` command section

---

### Orchestrator → Doc Mapping

Changes under `src/ouroboros/orchestrator/` affect runtime behavior documentation:

| Source file | Must update |
|-------------|-------------|
| `runtime_factory.py` | `docs/runtime-capability-matrix.md`, `docs/runtime-guides/claude-code.md`, `docs/runtime-guides/codex.md` — if a backend is added, removed, or changes its `NotImplementedError` status |
| `adapter.py` (`ClaudeAgentAdapter`) | `docs/runtime-guides/claude-code.md` — permission modes, session flow |
| `codex_cli_runtime.py` (`CodexCliRuntime`) | `docs/runtime-guides/codex.md` — permission modes, `--runtime codex` behavior |
| `opencode_runtime.py` (`OpenCodeRuntime`) | `docs/runtime-capability-matrix.md` — mark `[Not yet available]` until `NotImplementedError` is removed; `docs/runtime-guides/` — create guide only when fully shipped |
| `runner.py` (`OrchestratorRunner`) | `docs/architecture.md` — orchestration lifecycle; `docs/guides/cli-usage.md` — session ID output, resume flow |
| `parallel_executor.py` | `docs/cli-reference.md` — `--sequential` flag behavior; `docs/api/parallel-execution.md` |
| `coordinator.py` (`LevelCoordinator`) | `docs/architecture.md` — inter-level conflict resolution; `docs/api/parallel-execution.md` — coordinator review gate |
| `session.py` | `docs/cli-reference.md` — session ID format, resume semantics |
| `workflow_state.py` | `docs/architecture.md` — AC state machine, `ActivityType` values; `docs/guides/tui-usage.md` — if activity display changes |
| `dependency_analyzer.py` | `docs/architecture.md` — dependency level computation description |
| `execution_strategy.py` | `docs/architecture.md` — execution strategy types (`code`, `research`, `analysis`); `docs/guides/seed-authoring.md` if strategy selection is user-facing |
| `mcp_config.py` / `mcp_tools.py` | `docs/api/mcp.md` — MCP config YAML schema |
| `command_dispatcher.py` | `docs/architecture.md` — command dispatch model |
| `level_context.py` | `docs/architecture.md` — level context description |

**Runtime availability rule**: If `create_agent_runtime()` raises `NotImplementedError` for a backend, that backend **must not** appear in docs as a working option. Currently `opencode` is unimplemented — it must be marked `[Not yet available]` wherever documented.

---

### Configuration → Doc Mapping

Changes under `src/ouroboros/config/` affect configuration reference documentation:

| Source class | Config key path | Must update |
|---|---|---|
| `OrchestratorConfig` | `orchestrator.*` | `docs/cli-reference.md` — `--runtime` flag; `README.md` config snippet |
| `LLMConfig` | `llm.*` | `docs/architecture.md`, `docs/api/core.md` — model defaults |
| `EconomicsConfig` / `TierConfig` | `economics.*` | `docs/architecture.md` — tier descriptions |
| `ClarificationConfig` | `clarification.*` | `docs/guides/seed-authoring.md` — ambiguity threshold |
| `ExecutionConfig` | `execution.*` | `docs/architecture.md` — iteration limits |
| `ResilienceConfig` | `resilience.*` | `docs/architecture.md` — stagnation/lateral thinking |
| `EvaluationConfig` | `evaluation.*` | `docs/architecture.md` — three-stage evaluation |
| `ConsensusConfig` | `consensus.*` | `docs/architecture.md` — Stage 3 consensus |
| `DriftConfig` | `drift.*` | `docs/architecture.md` — drift monitoring thresholds |
| `PersistenceConfig` | `persistence.*` | `docs/getting-started.md` — database path |

When a **new config key** is added to any model class, check `README.md` and `docs/getting-started.md` for any sample `config.yaml` snippets that may need updating.

**`config/loader.py`**: If the config file search path, environment variable names (e.g., `OUROBOROS_CONFIG`), or YAML loading logic change, update:
- `docs/getting-started.md` — config file location instructions
- `docs/config-reference.md` — environment variable overrides section
- `README.md` — any config bootstrap snippet

---

### Evaluation Pipeline → Doc Mapping

Changes under `src/ouroboros/evaluation/` affect:

| Source file | Must update |
|-------------|-------------|
| `pipeline.py` | `docs/architecture.md` — Stage descriptions (Stage 1 Mechanical, Stage 2 Semantic, Stage 3 Consensus); `docs/guides/evaluation-pipeline.md` |
| `trigger.py` | `docs/architecture.md` — consensus trigger thresholds; `docs/guides/evaluation-pipeline.md` — when Stage 3 is invoked |
| `mechanical.py` | `docs/guides/evaluation-pipeline.md` — Stage 1 check list |
| `models.py` | `docs/api/core.md` — evaluation result types |
| `artifact_collector.py` | `docs/architecture.md` — artifact collection description |

---

### TUI Source → Doc Mapping

Changes under `src/ouroboros/tui/` that alter the visible interface or user interactions affect:

| Source path | Must update |
|-------------|-------------|
| `screens/dashboard_v3.py` | `docs/guides/tui-usage.md` — dashboard layout, key bindings |
| `widgets/ac_tree.py` | `docs/guides/tui-usage.md` — AC tree display; `docs/architecture.md` if AC state rendering changes |
| `widgets/drift_meter.py` | `docs/guides/tui-usage.md` — drift meter description |
| `widgets/phase_progress.py` | `docs/guides/tui-usage.md` — phase progress bar description |
| `screens/lineage_selector.py` / `lineage_detail.py` | `docs/guides/tui-usage.md` — lineage navigation section |
| Any new screen added to `screens/` | `docs/guides/tui-usage.md` — add a new section; `docs/cli-reference.md` if a new key binding or `tui` sub-command is introduced |

> **Note**: TUI key bindings visible in `screens/*.py` (`BINDINGS = [...]`) are user-facing and must be listed in `docs/guides/tui-usage.md`.

---

### Skills / Plugin → Doc Mapping

Changes under `skills/` (YAML skill definitions used by Claude and Codex) or `src/ouroboros/plugin/` affect:

| Source path | Must update |
|-------------|-------------|
| `skills/codex.md` | `docs/runtime-guides/codex.md` — if skill instructions change |
| `skills/*.yaml` or `src/ouroboros/agents/*.md` | `docs/` guide that describes the affected skill/agent behaviour |
| `src/ouroboros/plugin/skills/executor.py` | `docs/architecture.md` — skill execution model |
| `src/ouroboros/plugin/agents/registry.py` | `docs/architecture.md` — agent registry; `docs/runtime-capability-matrix.md` if supported agents change per runtime |

> **Note**: `skills/` YAML files are a user-visible configuration surface. Any new skill must be listed in the relevant runtime guide before the PR is merged.

---

### New Command or Flag Checklist

When adding a **new CLI command or flag**, use this checklist before submitting a PR:

- [ ] `docs/cli-reference.md` updated with the new command/flag, its type, default, and at least one example
- [ ] `docs/guides/cli-usage.md` updated if the flag changes workflow behavior
- [ ] `docs/getting-started.md` reviewed — update if a common flow is affected
- [ ] `README.md` reviewed — update the quick-start snippet if the new command changes day-1 usage
- [ ] If the feature is a placeholder/stub: docs must include `> **Note**: This feature is not yet implemented.`

### New Runtime Backend Checklist

When adding support for a **new runtime backend** (e.g., new entry in `AgentRuntimeBackend` enum):

- [ ] `docs/runtime-capability-matrix.md` — add a new row
- [ ] `docs/runtime-guides/` — create a new guide file `<runtime>.md`
- [ ] `docs/cli-reference.md` — add the backend name to `--runtime` option description
- [ ] `docs/getting-started.md` — update prerequisites section
- [ ] Remove any `[Not yet available]` or `NotImplementedError` markers once fully shipped

### Documentation Issue Severity Rubric

When a reviewer or contributor identifies a documentation problem, classify it by severity before filing an issue or leaving a PR comment. This classification determines urgency and whether a PR can be merged with the issue open.

| Severity | Label | Definition | User Impact | Merge Policy |
|----------|-------|------------|-------------|--------------|
| **Critical** | `docs:critical` | The documented information is **factually wrong**: a command, flag, path, or option described in the docs does not exist or behaves differently than described. | User follows the docs and **fails** — the command errors, the path is missing, the flag is rejected. | **Block merge.** The PR must not ship until fixed. |
| **High** | `docs:high` | The documentation is **misleading**: information is technically present but framed in a way that causes confusion, omits a required step, or implies a capability that is unimplemented. This includes wrong environment variable names that silently have no effect. | User follows the docs and **proceeds incorrectly** — they finish the step but reach a wrong state or have false expectations. | **Block merge** unless the issue is filed and linked. Fix within the same sprint. |
| **Medium** | `docs:medium` | The documentation has **inconsistent style or terminology**: the same concept is named differently across files, formatting does not follow the project's conventions, or phrasing is ambiguous but not incorrect. Also applies to missing-content findings where the gap is for an edge case or optional feature and users can succeed with defaults or alternative docs. | User is mildly confused by inconsistency but can still succeed. | **Non-blocking.** Can merge; fix before the next release. |
| **Low** | `docs:low` | The documentation has a **minor cosmetic gap**: an alternative invocation form is undocumented, a behavior note is absent but has no user-visible impact, or an edge case is missing from one file but covered elsewhere. No confusion or incorrect behavior results. | User experiences minor friction at most; no incorrect outcome. | **Non-blocking.** Address opportunistically. |

#### Severity Examples

| Example | Severity | Why |
|---------|----------|-----|
| `docs/cli-reference.md` lists `--foo` flag that does not exist in the source | Critical | User runs the command and gets "no such option" |
| `docs/getting-started.md` omits `uv sync` before `uv run ouroboros` | Critical | User's first command fails with ModuleNotFoundError |
| `opencode` listed as a working `--runtime` value without `[Not yet available]` | High | User configures `--runtime opencode` and gets a confusing `NotImplementedError` |
| `OUROBOROS_AGENT_RUNTIME` written as `OUROBOROS_RUNTIME_BACKEND` in one file | High | User sets the wrong env var and the setting silently has no effect |
| Docs recommend `export OUROBOROS_MAX_PARALLEL=2` but the variable does not exist | High | User sets the variable; parallelism is not actually limited (false expectation) |
| A major config section (`economics:`, `evaluation:`) entirely absent from docs | High | User who needs non-default configuration for that section has no documentation to follow; they omit a required step |
| `claude-code` vs `claude_code` used interchangeably across different docs files | Medium | Minor confusion; both forms resolve correctly in the CLI |
| Section headings use Title Case in some files and Sentence case in others | Medium | Style inconsistency; no functional impact |
| A minor config section (`drift:` thresholds) absent from docs; defaults are safe | Medium | User can operate with defaults; gap only matters for advanced tuning |
| An alternative invocation (`ouroboros tui` bare vs `ouroboros tui monitor`) absent | Low | User can use the documented form; no incorrect outcome |

#### How to Apply the Rubric in PRs

1. **When reviewing a docs-affecting PR**, scan each changed file against the [Documentation Decay Detection](#documentation-decay-detection) checks below and classify any finding using the table above.
2. **When filing a GitHub issue** for a documentation problem, add the appropriate `docs:critical`, `docs:high`, `docs:medium`, or `docs:low` label.
3. **When writing a PR description** that fixes a documentation problem, state the severity in the PR summary (e.g., _"Fixes docs:critical — `--resume` flag was listed with wrong default"_).
4. **Critical and High issues found during review must be resolved or have a linked follow-up issue before the PR is approved.**
5. **Record new findings** in [`docs/doc-issues-register.md`](./docs/doc-issues-register.md) using the filing template at the bottom of that file. When a fix is merged, move the entry to "Resolved Issues" and add the resolution date.

> **Current open issues** are tracked in [`docs/doc-issues-register.md`](./docs/doc-issues-register.md).

---

### Documentation Decay Detection

To catch doc drift during development, reviewers should check:

1. **Flag parity**: Run `ouroboros <cmd> --help` and compare every flag to `docs/cli-reference.md`. Any mismatch is a documentation bug.
2. **Placeholder honesty**: If a command's implementation body is `# Placeholder implementation`, the corresponding doc entry must say `[Placeholder — not yet implemented]`.
3. **Runtime parity**: `claude` and `codex` are the only fully-implemented backends. Any doc that lists `opencode` without a `[Not yet available]` marker is incorrect.
4. **Config key drift**: After any change to `src/ouroboros/config/models.py`, grep for the changed key name across `docs/` to find stale references.
5. **TUI key bindings**: If `screens/*.py` `BINDINGS` arrays change, verify `docs/guides/tui-usage.md` reflects the new keys.
6. **Skills registry drift**: If a new `skills/*.yaml` file is added, check that `docs/runtime-guides/codex.md` or the relevant guide mentions it.
7. **Orchestrator new file**: If a new `.py` file is added to `src/ouroboros/orchestrator/`, add it to the Orchestrator → Doc Mapping table above before the PR is merged.

```bash
# Quick doc-drift scan: compare CLI help output with cli-reference.md
uv run ouroboros init --help
uv run ouroboros run workflow --help
uv run ouroboros config --help
uv run ouroboros status --help

# Find stale config key references
grep -r "opencode_permission_mode\|runtime_backend\|codex_cli_path" docs/

# Find any 'opencode' reference in docs that lacks the [Not yet available] marker
grep -rn "opencode" docs/ | grep -v "Not yet available" | grep -v "semantic-link-rot" | grep -v "cli-audit"

# Check TUI key bindings are documented
grep -rn "BINDINGS" src/ouroboros/tui/screens/ | grep -v "__pycache__"

# List skill YAML files to cross-check against runtime guides
ls skills/*.yaml 2>/dev/null || echo "No skill YAML files found"
```

---

## Contributor Docs

- [Architecture Overview](./docs/contributing/architecture-overview.md) - How the system fits together
- [Testing Guide](./docs/contributing/testing-guide.md) - How to write and run tests
- [Key Patterns](./docs/contributing/key-patterns.md) - Core patterns with code examples

---

## Getting Help

- **GitHub Issues**: [Report bugs or request features](https://github.com/Q00/ouroboros/issues)
- **GitHub Discussions**: [Ask questions or share ideas](https://github.com/Q00/ouroboros/discussions)

---

## Code of Conduct

### Our Pledge

We pledge to make participation in our community a harassment-free experience for everyone, regardless of age, body size, disability, ethnicity, gender identity and expression, level of experience, nationality, personal appearance, race, religion, or sexual identity and orientation.

### Our Standards

**Positive behavior includes**:
- Being respectful and inclusive
- Gracefully accepting constructive criticism
- Focusing on what is best for the community
- Showing empathy towards other community members

**Unacceptable behavior includes**:
- Harassment, trolling, or derogatory comments
- Personal or political attacks
- Public or private harassment
- Publishing private information without permission
- Any other conduct which could reasonably be considered inappropriate

### Enforcement

Project maintainers may remove, edit, or reject comments, commits, code, wiki edits, issues, and other contributions that are not aligned with this Code of Conduct.

**Contact**: For any questions or concerns, please open a GitHub issue with the `conduct` label.

---

## License

By contributing to Ouroboros, you agree that your contributions will be licensed under the [MIT License](LICENSE).
